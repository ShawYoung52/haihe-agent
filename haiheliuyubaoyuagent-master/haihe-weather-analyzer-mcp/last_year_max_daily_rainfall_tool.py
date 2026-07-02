"""去年最大日降雨量 MCP 工具。

用于回答“去年最大日降雨量是多少”这类明确历史统计问题。
默认统计上一个自然年内海河流域站点逐日累计降雨，并返回最大站点日雨量。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastmcp import FastMCP

from constants import DEFAULT_BASIN_CODES
from tools import _get_music_client


def _previous_calendar_year_range(now: datetime | None = None) -> tuple[datetime, datetime, str, str]:
    now = now or datetime.now()
    year = now.year - 1
    start = datetime(year, 1, 1, 0, 0, 0)
    end = datetime(year, 12, 31, 23, 59, 59)
    readable = f"{start:%Y-%m-%d %H:%M:%S} ~ {end:%Y-%m-%d %H:%M:%S}"
    return start, end, readable, str(year)


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


def _update_top_records(top_records: list[dict], candidate: dict, top_n: int) -> list[dict]:
    top_records.append(candidate)
    top_records.sort(key=lambda x: float(x.get("daily_rainfall_mm") or 0.0), reverse=True)
    return top_records[: max(1, int(top_n or 10))]


def register_last_year_max_daily_rainfall_tool(mcp: FastMCP) -> None:
    @mcp.tool()
    def query_last_year_max_daily_rainfall(top_n: int = 10) -> dict:
        """
        查询上一个自然年海河流域最大日降雨量。

        适用于：“去年最大日降雨量是多少”“去年哪个站日降雨最大”“去年最大单日降雨”。
        统计口径：海河流域站点逐小时 PRE_1h 累加成自然日降雨量，取站点-日期最大值。
        """
        start, end, readable, year_label = _previous_calendar_year_range()
        top_n = max(1, min(int(top_n or 10), 50))
        client = _get_music_client()

        max_record: dict | None = None
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
            day_max = day_candidates[0]
            if max_record is None or float(day_max["daily_rainfall_mm"]) > float(max_record.get("daily_rainfall_mm") or 0.0):
                max_record = day_max
            for candidate in day_candidates[:top_n]:
                top_records = _update_top_records(top_records, candidate, top_n)

        if not max_record:
            return {
                "status": "no_data",
                "query_type": "last_year_max_daily_rainfall",
                "year": year_label,
                "time_range_readable": readable,
                "message": f"{year_label}年海河流域暂无有效日降雨量数据。",
                "records": [],
                "summary": {
                    "processed_days": processed_days,
                    "days_with_data": days_with_data,
                    "station_day_count": total_station_day_count,
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
            "records": top_records,
            "summary": {
                "processed_days": processed_days,
                "days_with_data": days_with_data,
                "station_day_count": total_station_day_count,
                "max_record": max_record,
            },
        }
