"""POI 最近观测站实况工具。

业务场景：领导要求“这些点的天气，最近的观测站的值”。
实现：复用现有 POI 查询和 MUSIC 站点实况接口，不改原 POI 工具链。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from fastmcp import FastMCP

from constants import DEFAULT_BASIN_CODES
from haihe_mcp_tools import MusicClient, MusicConfig, _search_poi_core


OBS_ELEMENTS = (
    "Station_Id_C,Station_levl,Lat,Lon,Alti,City,Station_Name,Cnty,Province,Town,"
    "Datetime,UPDATE_TIME,PRE_1h,PRE_3h,PRE_6h,PRE_12h,PRE_24h,PRE,"
    "TEM,RHU,PRS,WIN_D_Avg_2mi,WIN_S_Avg_2mi,WIN_D_INST,WIN_S_INST,VIS_HOR_1MI"
)


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


def _distance_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _latest_hour_candidates(hours_back: int = 4) -> list[str]:
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    return [(now - timedelta(hours=i)).strftime("%Y%m%d%H%M%S") for i in range(max(int(hours_back), 1))]


def _pick_first_poi(keyword: str) -> dict | None:
    poi_result = _search_poi_core(keyword=keyword, size=5)
    pois = poi_result.get("pois") or []
    for poi in pois:
        lon = _safe_float(poi.get("longitude"))
        lat = _safe_float(poi.get("latitude"))
        if lon is not None and lat is not None:
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
    return None


def _station_id(record: dict) -> str:
    return str(record.get("Station_Id_C") or record.get("station_id") or "").strip()


def _station_name(record: dict) -> str:
    return str(record.get("Station_Name") or record.get("station_name") or "未知站点").strip()


def _observation_time(record: dict, fallback_time: str) -> str:
    return str(record.get("Datetime") or record.get("UPDATE_TIME") or fallback_time or "").strip()


def _clean_observation(record: dict) -> dict:
    field_map = {
        "PRE_1h": "1小时降水量(mm)",
        "PRE_3h": "3小时降水量(mm)",
        "PRE_6h": "6小时降水量(mm)",
        "PRE_12h": "12小时降水量(mm)",
        "PRE_24h": "24小时降水量(mm)",
        "PRE": "分钟/当前降水量(mm)",
        "TEM": "气温(℃)",
        "RHU": "相对湿度(%)",
        "PRS": "气压(hPa)",
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


def _query_station_records(client: MusicClient, basin_codes: str, hours_back: int) -> tuple[str, list[dict]]:
    last_error = ""
    for time_s in _latest_hour_candidates(hours_back):
        try:
            rows = client.get_surf_ele_in_basin_by_time(
                basin_codes=basin_codes,
                times=time_s,
                elements=OBS_ELEMENTS,
                data_code="SURF_CHN_MUL_HOR",
            )
        except Exception as exc:
            last_error = str(exc)[:200]
            rows = []
        valid = [r for r in rows or [] if isinstance(r, dict) and _safe_float(r.get("Lon")) is not None and _safe_float(r.get("Lat")) is not None]
        if valid:
            return time_s, valid
    raise RuntimeError(last_error or "未查询到含经纬度的站点实况数据")


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
        hours_back: int = 4,
        max_distance_km: float = 80.0,
    ) -> dict:
        """查询某个 POI 的经纬度，并返回最近观测站的实况值。"""
        keyword = str(keyword or "").strip()
        if not keyword:
            return _error_payload(keyword, "POI 名称不能为空。")

        try:
            poi = _pick_first_poi(keyword)
        except Exception as exc:
            return _error_payload(keyword, "POI 查询失败。", str(exc))
        if not poi:
            return _error_payload(keyword, f"未查询到“{keyword}”的有效经纬度。")

        try:
            client = MusicClient(MusicConfig())
            query_time, records = _query_station_records(client, basin_codes, hours_back)
            nearest = _nearest_station(poi, records)
        except Exception as exc:
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
            "message": "已查询到 POI 最近观测站实况。",
        }
