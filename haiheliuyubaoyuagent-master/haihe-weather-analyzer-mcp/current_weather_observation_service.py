"""当前气象实况的天擎双接口查询与确定性统计。"""
from __future__ import annotations

import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import time_source


BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
# 实况短 TTL 缓存：同「时次桶 + hours_back」在 TTL 内命中，跨时次必 miss。
CURRENT_WEATHER_CACHE_TTL = int(os.getenv("CURRENT_WEATHER_CACHE_TTL", "60"))
_current_weather_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_current_weather_cache_lock = threading.Lock()
REGION_ADMIN_CODES = "110000,120000,130000"
HAIHE_BASIN_CODE = "HHLY"
OBSERVATION_DATA_CODE = "SURF_CHN_MUL_MIN"
OBSERVATION_ELEMENTS = (
    "Station_Id_C,Station_Name,Cnty,City,Province,PRE_1h,PRE,Datetime"
)
CENTRAL_TIANJIN_COUNTIES = frozenset({"和平", "河东", "河西", "南开"})
JIZHOU_ALIASES = ("蓟州区", "蓟州", "蓟县")
MISSING_PRECIPITATION_MIN = 9999.0


def build_latest_utc_hour_candidates(
    now: datetime | None = None,
    hours_back: int = 6,
) -> list[datetime]:
    """按 POI 实况口径生成 UTC 整点候选时次。"""
    current = now or time_source.now(BEIJING_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING_TIMEZONE)
    current_utc = current.astimezone(timezone.utc).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    return [
        current_utc - timedelta(hours=offset)
        for offset in range(max(int(hours_back or 6), 1))
    ]


def _safe_precipitation(value: Any) -> float | None:
    if value in (None, "", "None", "-", "--"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or abs(number) >= MISSING_PRECIPITATION_MIN:
        return None
    return number


def _normalize_area_name(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or ""))
    return re.sub(r"(省|市|区|县)$", "", text)


def _record_time_value(record: dict[str, Any]) -> float:
    text = str(record.get("Datetime") or record.get("Datatime") or "").strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y%m%d%H%M%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def _station_key(record: dict[str, Any]) -> str:
    station_id = str(record.get("Station_Id_C") or "").strip()
    if station_id:
        return f"id:{station_id}"
    return "name:" + "|".join(
        str(record.get(field) or "").strip()
        for field in ("Province", "City", "Cnty", "Station_Name")
    )


def _deduplicate_station_records(records: Any) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for item in records or []:
        if not isinstance(item, dict):
            continue
        key = _station_key(item)
        if key == "name:|||":
            continue
        previous = latest.get(key)
        if previous is None or _record_time_value(item) >= _record_time_value(previous):
            latest[key] = item
    return list(latest.values())


def _matches_province(record: dict[str, Any], name: str) -> bool:
    province = _normalize_area_name(record.get("Province"))
    city = _normalize_area_name(record.get("City"))
    target = _normalize_area_name(name)
    return target in {province, city}


def _has_required_region_coverage(records: list[dict[str, Any]]) -> bool:
    return all(
        any(_matches_province(record, name) for record in records)
        for name in ("天津", "北京", "河北")
    )


def _is_central_tianjin_record(record: dict[str, Any]) -> bool:
    return _normalize_area_name(record.get("Cnty")) in CENTRAL_TIANJIN_COUNTIES


def _is_jizhou_record(record: dict[str, Any]) -> bool:
    area_text = "".join(
        str(record.get(field) or "")
        for field in ("Cnty", "City", "Station_Name")
    )
    return any(alias in area_text for alias in JIZHOU_ALIASES)


def _station_details(record: dict[str, Any] | None, include_location: bool = True) -> dict[str, str] | None:
    if not record:
        return None
    station_name = str(record.get("Station_Name") or "未知站点").strip()
    county = str(record.get("Cnty") or "").strip()
    city = str(record.get("City") or "").strip()
    province = str(record.get("Province") or "").strip()
    if include_location:
        location_parts = []
        for part in (county, station_name):
            if part and part not in "".join(location_parts):
                location_parts.append(part)
        display = "".join(location_parts) or station_name
    else:
        display = station_name
    return {
        "station_name": station_name,
        "county": county,
        "city": city,
        "province": province,
        "display": display,
    }


def rainfall_level(maximum_mm: float | None) -> dict[str, Any]:
    """按24小时降水等级阈值给出代码判定结果。"""
    if maximum_mm is None:
        return {"has_data": False, "has_rain": False, "level": "暂无有效数据"}
    if maximum_mm < 0.1:
        return {"has_data": True, "has_rain": False, "level": "无降水"}
    if maximum_mm < 10:
        level = "小雨"
    elif maximum_mm < 25:
        level = "中雨"
    elif maximum_mm < 50:
        level = "大雨"
    elif maximum_mm < 100:
        level = "暴雨"
    elif maximum_mm < 250:
        level = "大暴雨"
    else:
        level = "特大暴雨"
    return {"has_data": True, "has_rain": True, "level": level}


def _calculate_area_stats(
    records: list[dict[str, Any]],
    *,
    station_with_location: bool = True,
) -> dict[str, Any]:
    pre_rows = [
        (record, value)
        for record in records
        if (value := _safe_precipitation(record.get("PRE"))) is not None
    ]
    hourly_rows = [
        (record, value)
        for record in records
        if (value := _safe_precipitation(record.get("PRE_1h"))) is not None
    ]
    max_pre_record, max_pre = max(pre_rows, key=lambda item: item[1]) if pre_rows else (None, None)
    max_hourly_record, max_hourly = (
        max(hourly_rows, key=lambda item: item[1])
        if hourly_rows
        else (None, None)
    )
    average_pre = (
        round(sum(value for _, value in pre_rows) / len(pre_rows), 1)
        if pre_rows
        else None
    )
    rounded_max_pre = round(max_pre, 1) if max_pre is not None else None
    rounded_max_hourly = round(max_hourly, 1) if max_hourly is not None else None
    rain_basis = rounded_max_pre if rounded_max_pre is not None else rounded_max_hourly
    level = rainfall_level(rain_basis)
    return {
        "record_count": len(records),
        "valid_pre_station_count": len(pre_rows),
        "valid_pre_1h_station_count": len(hourly_rows),
        "average_pre_mm": average_pre,
        "max_pre_mm": rounded_max_pre,
        "max_pre_station": _station_details(
            max_pre_record,
            include_location=station_with_location,
        ),
        "max_pre_1h_mm": rounded_max_hourly,
        "max_pre_1h_station": _station_details(
            max_hourly_record,
            include_location=station_with_location,
        ),
        "rainfall_judgement": level,
    }


def _group_tianjin_districts(
    tianjin_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按 Cnty 把天津站点分组，逐区县复用 _calculate_area_stats 统计。

    供"天津当前天气实况"列出天津各区县明细（2026-09-01 用户口径：问天津就该
    列天津各区县，而不是只给全市/中心城区/蓟州/海河流域汇总行）。零编造：展示名
    用原始 Cnty 不改写；缺 Cnty 的记录归入"未分区"，不丢数据。按雨量降序、
    完全无降水数据排最后、名称次序兜底。确定性"滚动实况"路径只读 REGION_LABELS
    固定键，本列表不影响该路径。
    """

    def _sort_key(item: dict[str, Any]) -> tuple:
        # 排序口径与 rainfall_judgement 的 rain_basis 一致：优先累计 PRE，
        # 累计缺测回退小时 PRE_1h——否则"累计缺测但小时有雨"的区县会被当无数据排最后。
        effective = item["max_pre_mm"]
        if effective is None:
            effective = item["max_pre_1h_mm"]
        return (
            effective is None,  # 完全无降水数据排最后
            -(effective if effective is not None else 0.0),  # 按雨量降序
            item["name"],  # 名称兜底，保证确定性
        )

    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in tianjin_records:
        county = str(record.get("Cnty") or "").strip() or "未分区"
        grouped.setdefault(county, []).append(record)
    districts = [
        {"name": county, **_calculate_area_stats(rows)}
        for county, rows in grouped.items()
    ]
    districts.sort(key=_sort_key)
    return districts


def _record_time_key(record: dict[str, Any]) -> str | None:
    """把记录 Datetime 归一化为 UTC YYYYMMDDHHMMSS（天擎 times 格式）。

    天擎 ByTime 接口 times 参数为 UTC 时次，记录 Datetime 也是 UTC——按字符串直解
    不做时区换算（naive .timestamp() 依赖服务器时区，会错位）。
    """
    text = str(record.get("Datetime") or record.get("Datatime") or "").strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y%m%d%H%M%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            return datetime.strptime(text, fmt).strftime("%Y%m%d%H%M%S")
        except ValueError:
            continue
    return None


def _group_records_by_time(records: Any) -> dict[str, list[dict[str, Any]]]:
    """按记录时间字段分组（天擎多时次一次返回的全部记录，按时次切回）。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in records or []:
        if not isinstance(item, dict):
            continue
        key = _record_time_key(item)
        if key:
            grouped.setdefault(key, []).append(item)
    return grouped


def _query_same_successful_time(
    client: Any,
    *,
    now: datetime | None,
    hours_back: int,
) -> tuple[datetime | None, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    attempts: list[dict[str, str]] = []
    candidates = build_latest_utc_hour_candidates(now=now, hours_back=hours_back)

    # 多时次合并：MUSIC ByTime 接口 times 支持逗号连接，一次请求返回全部时次 →
    # region/basin 各 1 次调用（12 次 → 2 次）。服务端不支持（只回单个时次 / 抛错）
    # 时回退逐时次串行，行为与原实现完全一致。
    coalesced_region: list[dict[str, Any]] = []
    coalesced_basin: list[dict[str, Any]] = []
    coalesced_ok = True
    try:
        times_joined = ",".join(c.strftime("%Y%m%d%H%M%S") for c in candidates)
        coalesced_region = client.get_surf_ele_in_region_by_time(
            admin_codes=REGION_ADMIN_CODES,
            times=times_joined,
            elements=OBSERVATION_ELEMENTS,
            data_code=OBSERVATION_DATA_CODE,
        )
        coalesced_basin = client.get_surf_ele_in_basin_by_time(
            basin_codes=HAIHE_BASIN_CODE,
            times=times_joined,
            elements=OBSERVATION_ELEMENTS,
            data_code=OBSERVATION_DATA_CODE,
        )
    except Exception:
        coalesced_ok = False

    region_by_time = _group_records_by_time(coalesced_region)
    basin_by_time = _group_records_by_time(coalesced_basin)
    if coalesced_ok and len(region_by_time) >= 2 and len(basin_by_time) >= 2:
        # 从新到旧选第一个「region 覆盖完整 + basin 非空」时次（与原循环语义等价）。
        for candidate in candidates:
            times = candidate.strftime("%Y%m%d%H%M%S")
            raw_region = region_by_time.get(times) or []
            raw_basin = basin_by_time.get(times) or []
            region_records = _deduplicate_station_records(raw_region)
            attempts.append({
                "times_utc": times,
                "region_count": str(len(raw_region)),
                "basin_count": str(len(raw_basin)),
                "region_coverage": (
                    "complete" if _has_required_region_coverage(region_records) else "incomplete"
                ),
                "region_error": "",
                "basin_error": "",
            })
            if region_records and _has_required_region_coverage(region_records) and raw_basin:
                return (
                    candidate,
                    region_records,
                    _deduplicate_station_records(raw_basin),
                    attempts,
                )
        return None, [], [], attempts

    # 回退：逐时次串行（原逻辑逐字保留）
    for candidate in candidates:
        times = candidate.strftime("%Y%m%d%H%M%S")
        region_records: list[dict[str, Any]] = []
        basin_records: list[dict[str, Any]] = []
        region_error = ""
        basin_error = ""
        try:
            region_records = client.get_surf_ele_in_region_by_time(
                admin_codes=REGION_ADMIN_CODES,
                times=times,
                elements=OBSERVATION_ELEMENTS,
                data_code=OBSERVATION_DATA_CODE,
            )
        except Exception as exc:
            region_error = str(exc)
        try:
            basin_records = client.get_surf_ele_in_basin_by_time(
                basin_codes=HAIHE_BASIN_CODE,
                times=times,
                elements=OBSERVATION_ELEMENTS,
                data_code=OBSERVATION_DATA_CODE,
            )
        except Exception as exc:
            basin_error = str(exc)
        attempts.append({
            "times_utc": times,
            "region_count": str(len(region_records or [])),
            "basin_count": str(len(basin_records or [])),
            "region_coverage": (
                "complete"
                if _has_required_region_coverage(_deduplicate_station_records(region_records))
                else "incomplete"
            ),
            "region_error": region_error[:200],
            "basin_error": basin_error[:200],
        })
        deduplicated_region_records = _deduplicate_station_records(region_records)
        if (
            deduplicated_region_records
            and _has_required_region_coverage(deduplicated_region_records)
            and basin_records
        ):
            return (
                candidate,
                deduplicated_region_records,
                _deduplicate_station_records(basin_records),
                attempts,
            )
    return None, [], [], attempts


def query_current_weather_observation_core(
    client_factory: Callable[[], Any],
    *,
    now: datetime | None = None,
    hours_back: int = 6,
) -> dict[str, Any]:
    """查询同一时次的地区与流域实况，并返回代码统计结果。"""
    current = now or time_source.now(BEIJING_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING_TIMEZONE)
    cache_key = f"{current.strftime('%Y%m%d%H')}|{hours_back}"
    with _current_weather_cache_lock:
        hit = _current_weather_cache.get(cache_key)
        if hit and (time.time() - hit[0]) < CURRENT_WEATHER_CACHE_TTL:
            return hit[1]

    client = client_factory()
    api_time, region_records, basin_records, attempts = _query_same_successful_time(
        client,
        now=now,
        hours_back=hours_back,
    )
    if api_time is None:
        return {
            "status": "no_data",
            "message": "最近候选时次未同时取得地区和海河流域实况数据。",
            "data_source": "天擎自动站",
            "attempts": attempts,
        }

    beijing_time = api_time.astimezone(BEIJING_TIMEZONE)
    tianjin_records = [
        record for record in region_records
        if _matches_province(record, "天津")
    ]
    beijing_records = [
        record for record in region_records
        if _matches_province(record, "北京")
    ]
    hebei_records = [
        record for record in region_records
        if _matches_province(record, "河北")
    ]
    central_records = [
        record for record in tianjin_records
        if _is_central_tianjin_record(record)
    ]
    jizhou_records = [
        record for record in tianjin_records
        if _is_jizhou_record(record)
    ]

    result = {
        "status": "ok",
        "data_source": "天擎自动站",
        "query_time_utc": api_time.strftime("%Y-%m-%d %H:%M:%S"),
        "observation_time_beijing": beijing_time.strftime("%Y-%m-%d %H:%M:%S"),
        "observation_time_label": (
            f"截至{beijing_time.month}月{beijing_time.day}日"
            f"{beijing_time.hour}时{beijing_time.minute:02d}分"
        ),
        "query_parameters": {
            "region_interface": "getSurfEleInRegionByTime",
            "admin_codes": REGION_ADMIN_CODES,
            "basin_interface": "getSurfEleInBasinByTime",
            "basin_codes": HAIHE_BASIN_CODE,
            "data_code": OBSERVATION_DATA_CODE,
            "elements": OBSERVATION_ELEMENTS,
        },
        "regions": {
            "tianjin": _calculate_area_stats(tianjin_records),
            "tianjin_central": _calculate_area_stats(central_records),
            "jizhou": _calculate_area_stats(jizhou_records),
            "tianjin_districts": _group_tianjin_districts(tianjin_records),
            "beijing": _calculate_area_stats(beijing_records),
            "hebei": _calculate_area_stats(hebei_records),
            "haihe_basin": _calculate_area_stats(
                basin_records,
                station_with_location=False,
            ),
        },
        "matching_rules": {
            "tianjin_central_counties": sorted(CENTRAL_TIANJIN_COUNTIES),
            "jizhou_aliases": list(JIZHOU_ALIASES),
        },
        "record_counts": {
            "region": len(region_records),
            "basin": len(basin_records),
        },
        "attempts": attempts,
    }
    with _current_weather_cache_lock:
        _current_weather_cache[cache_key] = (time.time(), result)
    return result
