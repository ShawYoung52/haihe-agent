"""去年最大日降雨量 MCP 工具。

用于回答“去年最大日降雨量是多少”这类明确历史统计问题。
按用户指定接口 getSurfEleInBasinByTime 查询海河流域日资料，统计上一个自然年最大日降雨量。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastmcp import FastMCP

from constants import DEFAULT_BASIN_CODES
from tools import _get_music_client


DAILY_DATA_CODE = "SURF_CHN_MUL_DAY"
DAILY_RAIN_FIELD = "PRE_Time_0808"


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
    for key in (DAILY_RAIN_FIELD, "PRE_Time_2020", "PRE_Time_0808", "daily_rainfall_mm"):
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


def _month_time_groups(start: datetime, end: datetime) -> list[tuple[str, str]]:
    """按月生成 getSurfEleInBasinByTime 的 times，避免一次请求 URL 过长。"""
    groups: list[tuple[str, str]] = []
    cur = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cur <= end:
        next_month = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        month_end = min(next_month - timedelta(days=1), end)
        times: list[str] = []
        day = cur
        while day <= month_end:
            # 日资料的 Datetime 通常为每日 00:00:00。
            times.append(day.strftime("%Y%m%d000000"))
            day += timedelta(days=1)
        if times:
            groups.append((cur.strftime("%Y-%m"), ",".join(times)))
        cur = next_month
    return groups


def _query_daily_by_basin_time(client, start: datetime, end: datetime, top_n: int) -> tuple[list[dict], dict]:
    """使用用户指定的 getSurfEleInBasinByTime 查询日资料并统计最大日降雨量。"""
    top_records: list[dict] = []
    queried_months = 0
    months_with_data = 0
    source_record_count = 0
    error_messages: list[str] = []

    for month_label, times in _month_time_groups(start, end):
        queried_months += 1
        try:
            records = client.get_surf_ele_in_basin_by_time(
                basin_codes=DEFAULT_BASIN_CODES,
                times=times,
                elements="Station_Id_C,Station_Name,Lat,Lon,City,Cnty,Province,Town,Datetime,PRE_Time_0808",
                data_code=DAILY_DATA_CODE,
                ele_value_ranges="PRE_Time_0808:(,9999)",
                order_by="PRE_Time_0808:desc",
                limit_cnt=max(20, int(top_n or 10)),
            )
        except Exception as exc:
            msg = f"{month_label}: {str(exc)[:120]}"
            print(f"[last_year_max_daily_rainfall] getSurfEleInBasinByTime 查询失败：{msg}")
            error_messages.append(msg)
            continue

        if not records:
            continue
        months_with_data += 1
        source_record_count += len(records)

        for record in records:
            if not isinstance(record, dict):
                continue
            candidate = _candidate_from_record(record)
            if candidate:
                top_records = _update_top_records(top_records, candidate, top_n)

    return top_records, {
        "method": "getSurfEleInBasinByTime_daily",
        "data_code": DAILY_DATA_CODE,
        "rain_field": DAILY_RAIN_FIELD,
        "queried_months": queried_months,
        "months_with_data": months_with_data,
        "source_record_count": source_record_count,
        "errors": error_messages[:5],
    }


def register_last_year_max_daily_rainfall_tool(mcp: FastMCP) -> None:
    @mcp.tool()
    def query_last_year_max_daily_rainfall(top_n: int = 10, allow_slow_fallback: bool = False) -> dict:
        """
        查询上一个自然年海河流域最大日降雨量。

        适用于：“去年最大日降雨量是多少”“去年哪个站日降雨最大”“去年最大单日降雨”。
        统计口径：使用 getSurfEleInBasinByTime + SURF_CHN_MUL_DAY + PRE_Time_0808 查询日资料。
        allow_slow_fallback 参数为兼容旧前端保留，当前不再执行逐小时慢查询。
        """
        start, end, readable, year_label = _previous_calendar_year_range()
        top_n = max(1, min(int(top_n or 10), 50))
        client = _get_music_client()

        records, summary_extra = _query_daily_by_basin_time(client, start, end, top_n)
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
