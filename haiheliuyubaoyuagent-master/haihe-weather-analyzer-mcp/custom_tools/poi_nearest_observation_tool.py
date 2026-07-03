"""POI 最近观测站实况工具。

业务场景：领导要求“这些点的天气，最近的观测站的值”。
实现：复用现有 POI 查询和 MUSIC 逐小时站点实况接口，不改原 POI 工具链。
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any

from fastmcp import FastMCP

from constants import DEFAULT_BASIN_CODES, DEFAULT_OBS_ELEMENTS
from haihe_mcp_tools import MusicClient, MusicConfig, _search_poi_core


logger = logging.getLogger(__name__)

HOURLY_DATA_CODE = "SURF_CHN_MUL_HOR"
TIANJIN_ADMIN_CODE = "120000"
FULL_OBS_ELEMENTS = (
    "Station_Id_C,Station_levl,Lat,Lon,Alti,City,Station_Name,Cnty,Province,Town,"
    "Datetime,UPDATE_TIME,PRE_1h,PRE_3h,PRE_6h,PRE_12h,PRE_24h,PRE,"
    "TEM,RHU,PRS,WIN_D_Avg_2mi,WIN_S_Avg_2mi,WIN_D_INST,WIN_S_INST,VIS_HOR_1MI"
)
OBS_ELEMENT_CANDIDATES = [FULL_OBS_ELEMENTS, DEFAULT_OBS_ELEMENTS]


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, "", "None", "-", "--"):
            return None
        number = float(value)
    except Exception:
        return None
    if not math.isfinite(number):
        return None
    if abs(number) >= 99999:
        return None
    return number


def _is_tianjin_text(item: dict) -> bool:
    text = " ".join(
        str(item.get(k) or "")
        for k in ("name", "address", "category_1", "category_2")
    )
    return "天津" in text


def _distance_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _latest_hour_candidates(hours_back: int = 6) -> list[str]:
    """生成最近逐小时实况候选时次。

    项目原降雨核心链路按“北京时间 - 8小时”查询天擎，因此这里也按 UTC 时次倒查。
    例如北京时间 14:52，优先查接口时次 06:00，其次 05:00、04:00。
    """
    now_bjt = datetime.now().replace(minute=0, second=0, microsecond=0)
    now_api = now_bjt - timedelta(hours=8)
    count = max(int(hours_back or 6), 1)
    return [(now_api - timedelta(hours=i)).strftime("%Y%m%d%H%M%S") for i in range(count)]


def _poi_to_normalized(keyword: str, poi_result: dict, poi: dict) -> dict | None:
    lon = _safe_float(poi.get("longitude"))
    lat = _safe_float(poi.get("latitude"))
    if lon is None or lat is None:
        return None
    return {
        "name": poi.get("name") or keyword,
        "address": poi.get("address"),
        "category_1": poi.get("category_1"),
        "category_2": poi.get("category_2"),
        "longitude": lon,
        "latitude": lat,
        "match_type": poi_result.get("match_type"),
        "total": poi_result.get("total"),
    }


def _search_poi_candidates(keyword: str) -> list[dict]:
    candidates: list[dict] = []
    seen: set[tuple] = set()
    search_terms = [keyword]
    if "天津" in keyword:
        compact = keyword.replace("天津市", "").replace("天津", "").strip()
        if compact and compact != keyword:
            search_terms.append(compact)
    for term in search_terms:
        poi_result = _search_poi_core(keyword=term, size=30)
        for poi in poi_result.get("pois") or []:
            item = _poi_to_normalized(keyword, poi_result, poi)
            if not item:
                continue
            key = (item.get("name"), item.get("address"), item.get("longitude"), item.get("latitude"))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(item)
    return candidates


def _pick_first_poi(keyword: str) -> dict | None:
    candidates = _search_poi_candidates(keyword)
    if not candidates:
        return None

    if "天津" in keyword:
        # 用户明确指定天津时，必须在 POI 名称/地址/类别文本中出现“天津”。
        # 不能只靠经纬度粗框，否则会把唐山、河北同名“气象局”误认为天津。
        candidates = [item for item in candidates if _is_tianjin_text(item)]
        if not candidates:
            logger.warning("[poi_nearest_observation] no textual Tianjin POI candidate for keyword=%s", keyword)
            return None

    def score(item: dict) -> tuple[int, int, int]:
        name = str(item.get("name") or "")
        address = str(item.get("address") or "")
        exact = 1 if name == keyword else 0
        contains = 1 if keyword in name or name in keyword else 0
        tj = 1 if ("天津" in address or "天津" in name) else 0
        return exact, contains, tj

    candidates.sort(key=score, reverse=True)
    return candidates[0]


def _station_id(record: dict) -> str:
    return str(record.get("Station_Id_C") or record.get("station_id") or "").strip()


def _station_name(record: dict) -> str:
    return str(record.get("Station_Name") or record.get("station_name") or "未知站点").strip()


def _observation_time(record: dict, fallback_time: str) -> str:
    return str(record.get("Datetime") or record.get("UPDATE_TIME") or fallback_time or "").strip()


def _clean_observation(record: dict) -> dict:
    field_map = {
        "TEM": "气温(℃)",
        "RHU": "相对湿度(%)",
        "PRS": "气压(hPa)",
        "PRE_1h": "1小时降水量(mm)",
        "PRE_3h": "3小时降水量(mm)",
        "PRE_6h": "6小时降水量(mm)",
        "PRE_12h": "12小时降水量(mm)",
        "PRE_24h": "24小时降水量(mm)",
        "PRE": "当前降水量(mm)",
        "WIN_D_Avg_2mi": "2分钟平均风向(°)",
        "WIN_S_Avg_2mi": "2分钟平均风速(m/s)",
        "WIN_D_INST": "瞬时风向(°)",
        "WIN_S_INST": "瞬时风速(m/s)",
        "VIS_HOR_1MI": "1分钟水平能见度(m)",
    }
    obs: dict[str, Any] = {}
    for key, label in field_map.items():
        value = record.get(key)
        number = _safe_float(value)
        if number is not None:
            obs[label] = round(number, 2)
        elif value not in (None, "", "None", "-", "--"):
            obs[label] = value
    return obs


def _valid_station_rows(rows: Any) -> list[dict]:
    return [
        r for r in (rows or [])
        if isinstance(r, dict)
        and _safe_float(r.get("Lon")) is not None
        and _safe_float(r.get("Lat")) is not None
    ]


def _query_region_rows(client: MusicClient, time_s: str, elements: str, admin_code: str) -> list[dict]:
    return client.call_api(
        "getSurfEleInRegionByTime",
        dataCode=HOURLY_DATA_CODE,
        elements=elements,
        times=time_s,
        adminCodes=admin_code,
    )


def _query_basin_rows(client: MusicClient, time_s: str, elements: str, basin_codes: str) -> list[dict]:
    return client.get_surf_ele_in_basin_by_time(
        basin_codes=basin_codes,
        times=time_s,
        elements=elements,
        data_code=HOURLY_DATA_CODE,
    )


def _query_station_records(
    client: MusicClient,
    basin_codes: str,
    hours_back: int,
    admin_code: str = TIANJIN_ADMIN_CODE,
) -> tuple[str, list[dict], str]:
    last_error = ""
    tried: list[str] = []
    query_modes = (
        ("region", lambda t, e: _query_region_rows(client, t, e, admin_code)),
        ("basin", lambda t, e: _query_basin_rows(client, t, e, basin_codes)),
    )
    for time_s in _latest_hour_candidates(hours_back):
        for mode, query_func in query_modes:
            for elements in OBS_ELEMENT_CANDIDATES:
                source = f"hourly_{mode}_{'full' if elements == FULL_OBS_ELEMENTS else 'basic'}"
                tried.append(f"{source}@{time_s}")
                try:
                    rows = query_func(time_s, elements)
                    logger.warning(
                        "[poi_nearest_observation] %s@%s returned rows=%s",
                        source,
                        time_s,
                        len(rows or []),
                    )
                except Exception as exc:
                    last_error = f"{source}@{time_s}: {str(exc)[:180]}"
                    logger.warning("[poi_nearest_observation] %s", last_error)
                    rows = []
                valid = _valid_station_rows(rows)
                if valid:
                    logger.warning(
                        "[poi_nearest_observation] hit %s@%s valid_rows=%s",
                        source,
                        time_s,
                        len(valid),
                    )
                    return time_s, valid, source
    tried_text = ";".join(tried)[:260]
    raise RuntimeError(last_error or f"逐小时站点实况无有效经纬度数据，tried={tried_text}")


def _nearest_station(poi: dict, records: list[dict]) -> dict | None:
    lon, lat = float(poi["longitude"]), float(poi["latitude"])
    nearest = None
    best = None
    for row in records:
        slon = _safe_float(row.get("Lon"))
        slat = _safe_float(row.get("Lat"))
        if slon is None or slat is None:
            continue
        d = _distance_km(lon, lat, slon, slat)
        if best is None or d < best:
            best = d
            nearest = row
    if nearest is None or best is None:
        return None
    return {"record": nearest, "distance_km": round(best, 2)}


def _error_payload(keyword: str, message: str, debug_reason: str = "") -> dict:
    return {
        "status": "no_data",
        "query_type": "poi_nearest_observation",
        "keyword": keyword,
        "message": message,
        "debug_reason": debug_reason[:300] if debug_reason else "",
    }


def register_poi_nearest_observation_tool(mcp: FastMCP) -> None:
    @mcp.tool()
    def query_poi_nearest_observation(
        keyword: str,
        basin_codes: str = DEFAULT_BASIN_CODES,
        hours_back: int = 6,
        max_distance_km: float = 80.0,
        admin_code: str = TIANJIN_ADMIN_CODE,
    ) -> dict:
        """查询某个 POI 的经纬度，并返回最近观测站逐小时实况值。"""
        keyword = str(keyword or "").strip()
        if not keyword:
            return _error_payload(keyword, "POI 名称不能为空。")

        try:
            poi = _pick_first_poi(keyword)
            logger.warning(
                "[poi_nearest_observation] poi keyword=%s name=%s address=%s lon=%s lat=%s",
                keyword,
                poi.get("name") if poi else None,
                poi.get("address") if poi else None,
                poi.get("longitude") if poi else None,
                poi.get("latitude") if poi else None,
            )
        except Exception as exc:
            return _error_payload(keyword, "POI 查询失败。", str(exc))
        if not poi:
            return _error_payload(keyword, f"未查询到“{keyword}”的天津范围内有效经纬度。")

        try:
            client = MusicClient(MusicConfig())
            query_time, records, obs_source = _query_station_records(client, basin_codes, hours_back, admin_code)
            nearest = _nearest_station(poi, records)
        except Exception as exc:
            logger.warning("[poi_nearest_observation] failed keyword=%s error=%s", keyword, exc)
            return _error_payload(keyword, "最近观测站实况查询失败。", str(exc))

        if not nearest:
            return _error_payload(keyword, "未找到可用于匹配的最近观测站。")
        if float(nearest["distance_km"]) > float(max_distance_km):
            return _error_payload(
                keyword,
                f"已定位到 POI，但 {max_distance_km:g} 公里内未找到可用观测站。",
                f"nearest_distance_km={nearest['distance_km']}",
            )

        record = nearest["record"]
        return {
            "status": "ok",
            "query_type": "poi_nearest_observation",
            "keyword": keyword,
            "poi": poi,
            "query_time": query_time,
            "observation_time": _observation_time(record, query_time),
            "observation_source": obs_source,
            "data_code": HOURLY_DATA_CODE,
            "interface_id": "getSurfEleInRegionByTime/getSurfEleInBasinByTime",
            "nearest_station": {
                "station_id": _station_id(record),
                "station_name": _station_name(record),
                "province": record.get("Province"),
                "city": record.get("City"),
                "county": record.get("Cnty"),
                "town": record.get("Town"),
                "longitude": _safe_float(record.get("Lon")),
                "latitude": _safe_float(record.get("Lat")),
                "distance_km": nearest["distance_km"],
            },
            "observation": _clean_observation(record),
            "message": "已查询到 POI 最近观测站逐小时实况。",
        }
