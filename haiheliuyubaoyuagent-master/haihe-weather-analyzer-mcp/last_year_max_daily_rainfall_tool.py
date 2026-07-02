"""去年最大日降雨量 MCP 工具。

用于回答“去年最大日降雨量是多少”这类明确历史统计问题。
默认统计上一个自然年内海河流域站点日降雨量，并返回最大站点日雨量。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastmcp import FastMCP

from constants import DEFAULT_BASIN_CODES
from tools import _get_music_client


def _previous_calendar_year_range(now: datetime | None = None) -> tuple[datetime, datetime, str, str, str]:
    now = now or datetime.now()
    year = now.year - 1
    start = datetime(year, 1, 1, 0, 0, 0)
    end = datetime(year, 12, 31, 23, 59, 59)
    readable = f"{start:%Y-%m-%d %H:%M:%S} ~ {end:%Y-%m-%d %H:%M:%S}"
    music_range = f"[{start:%Y%m%d%H%M%S},{end:%Y%m%d%H%M%S}]"
    return start, end, readable, music_range, str(year)


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, "", "None"):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number > 9999:
        return None
    return number


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("<br>", "").replace("<br/>", "").replace("</br>", "").strip()


def _iter_daily_ranges(start: datetime, end: datetime):
    cur = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while cur <= end:
        day_start = max(cur, start)
        day_end = min(cur.replace(hour=23, minute=59, second=59), end)
        yield day_start, day_end
        cur += timedelta(days=1)


def _hour_times_for_day(day_start: datetime, day_end: datetime) -> str:
    times: list[str] = []
    cur = day_start.replace(minute=0, second=0, microsecond=0)
    while cur <= day_end:
        times.append(cur.strftime("%Y%m%d%H%M%S"))
        cur += timedelta(hours=1)
    return ",".join(times)


def _station_name(record: dict) -> str:
    return (
        _safe_text(record.get("Station_Name"))
        or _safe_text(record.get("station_name"))
        or _safe_text(record.get("站名"))
        or _safe_text(record.get("Station_Id_C"))
        or "未知站点"
    )


def _station_id(record: dict) -> str:
    return _safe_text(record.get("Station_Id_C")) or _safe_text(record.get("station_id"))


def _station_location(record: dict) -> dict:
    return {
        "province": _safe_text(record.get("Province")),
        "city": _safe_text(record.get("City")),
        "county": _safe_text(record.get("Cnty")),
        "town": _safe_text(record.get("Town")),
        "lon": _safe_float(record.get("Lon")),
        "lat": _safe_float(record.get("Lat")),
    }


def _date_from_record(record: dict, fallback: str = "") -> str:
    raw = _safe_text(record.get("Datetime") or record.get("datetime") or fallback)
    if not raw:
        return ""
    raw = raw.replace("/", "-")
    if len(raw) >= 10 and raw[4] == "-":
        return raw[:10]
    if len(raw) >= 8 and raw[:8].isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def _daily_rain_from_record(record: dict) -> float | None:
    for key in ("MAX_PRE_Time_0808", "PRE_Time_0808", "SUM_PRE_1H", "SUM_PRE_1h", "daily_rainfall_mm"):
        value = _safe_float(record.get(key))
        if value is not None:
            return value
    return None


def _candidate_from_record(record: dict, fallback_date: str = "") -> dict | None:
    rain = _daily_rain_from_record(record)
    if rain is None or rain <= 0:
        return None
    return {
        "date": _date_from_record(record, fallback=fallback_date),
        "daily_rainfall_mm": round(float(rain), 2),
        "station_id": _station_id(record),
        "station_name": _station_name(record),
        **_station_location(record),
    }


def _update_top_records(top_records: list[dict], candidate: dict, top_n: int) -> list[dict]:
    top_records.append(candidate)
    top_records.sort(key=lambda x: float(x.get("daily_rainfall_mm") or 0.0), reverse=True)
    return top_records[: max(1, int(top_n or 10))]


def _query_fast_stat(client, music_range: str, top_n: int) -> list[dict]:
    """优先使用天擎统计接口直接取全年最大日降雨量，失败时返回空列表。"""
    try:
        records = client.stat_surf_pre_in_basin_new(
            basin_codes=DEFAULT_BASIN_CODES,
            timeRange=music_range,
            elements="Station_Id_C,Station_Name,Lat,Lon,City,Cnty,Province,Town,Datetime",
            statEles="MAX_PRE_Time_0808",
            ele_value_ranges="PRE_Time_0808:(,9999)",
            order_by="MAX_PRE_Time_0808:desc",
            limit_cnt=max(1, int(top_n or 10)),
            data_code="SURF_CHN_MUL_DAY",
        )
    except Exception as exc:
        print(f"[last_year_max_daily_rainfall] 统计接口失败，回退逐日小时累加：{exc}")
        return []

    candidates: list[dict] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        candidate = _candidate_from_record(record)
        if candidate:
            candidates.append(candidate)
    candidates.sort(key=lambda x: float(x.get("daily_rainfall_mm") or 0.0), reverse=True)
    return candidates[: max(1, int(top_n or 10))]


def _query_by_daily_hourly_sum(client, start: datetime, end: datetime, top_n: int) -> tuple[list[dict], dict]:
    top_records: list[dict] = []
    processed_days = 0
    days_with_data = 0
    total_station_day_count = 0

    for day_start, day_end in _iter_daily_ranges(start, end):
        processed_days += 1
        times = _hour_times_for_day(day_start, day_end)
        if not times:
            continue
        try:
            records = client.get_surf_ele_in_basin_by_time(
                basin_codes=DEFAULT_BASIN_CODES,
                times=times,
                elements="Station_Id_C,Lat,Lon,City,Station_Name,Cnty,Province,Town,PRE_1h",
            )
        except Exception as exc:
            print(f"[last_year_max_daily_rainfall] {day_start:%Y-%m-%d} 站点降雨查询失败：{exc}")
            continue
        if not records:
            continue

        station_map: dict[str, dict] = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            sid = _station_id(record)
            if not sid:
                continue
            rain = _safe_float(record.get("PRE_1h"))
            if rain is None:
                continue
            if sid not in station_map:
                station_map[sid] = {
                    "station_id": sid,
                    "station_name": _station_name(record),
                    **_station_location(record),
                    "daily_rainfall_mm": 0.0,
                }
            station_map[sid]["daily_rainfall_mm"] += float(rain)

        day_candidates = []
        for item in station_map.values():
            daily = round(float(item.get("daily_rainfall_mm") or 0.0), 2)
            if daily <= 0:
                continue
            candidate = {
                "date": f"{day_start:%Y-%m-%d}",
                "daily_rainfall_mm": daily,
                "station_id": item.get("station_id"),
                "station_name": item.get("station_name"),
                "province": item.get("province"),
                "city": item.get("city"),
                "county": item.get("county"),
                "town": item.get("town"),
                "lon": item.get("lon"),
                "lat": item.get("lat"),
            }
            day_candidates.append(candidate)

        if not day_candidates:
            continue
        days_with_data += 1
        total_station_day_count += len(day_candidates)
        day_candidates.sort(key=lambda x: float(x.get("daily_rainfall_mm") or 0.0), reverse=True)
        for candidate in day_candidates[:top_n]:
            top_records = _update_top_records(top_records, candidate, top_n)

    return top_records, {
        "processed_days": processed_days,
        "days_with_data": days_with_data,
        "station_day_count": total_station_day_count,
    }


def register_last_year_max_daily_rainfall_tool(mcp: FastMCP) -> None:
    @mcp.tool()
    def query_last_year_max_daily_rainfall(top_n: int = 10) -> dict:
        """
        查询上一个自然年海河流域最大日降雨量。

        适用于：“去年最大日降雨量是多少”“去年哪个站日降雨最大”“去年最大单日降雨”。
        统计口径：优先使用天擎日资料 PRE_Time_0808 最大值；统计接口不可用时，用逐小时 PRE_1h 累加成自然日降雨量兜底。
        """
        start, end, readable, music_range, year_label = _previous_calendar_year_range()
        top_n = max(1, min(int(top_n or 10), 50))
        client = _get_music_client()

        records = _query_fast_stat(client, music_range, top_n)
        summary_extra = {
            "processed_days": 0,
            "days_with_data": 0,
            "station_day_count": 0,
            "method": "stat_daily_pre_0808",
        }

        if not records:
            records, fallback_summary = _query_by_daily_hourly_sum(client, start, end, top_n)
            summary_extra.update(fallback_summary)
            summary_extra["method"] = "hourly_pre_1h_sum"

        max_record = records[0] if records else None
        if not max_record:
            return {
                "status": "no_data",
                "query_type": "last_year_max_daily_rainfall",
                "year": year_label,
                "time_range_readable": readable,
                "message": f"{year_label}年海河流域暂无有效日降雨量数据。",
                "records": [],
                "summary": {
                    **summary_extra,
                    "max_record": None,
                },
            }

        return {
            "status": "ok",
            "query_type": "last_year_max_daily_rainfall",
            "year": year_label,
            "time_range_readable": readable,
            "statistic_label": "最大日降雨量",
            "max_record": max_record,
            "records": records,
            "summary": {
                **summary_extra,
                "max_record": max_record,
            },
        }
