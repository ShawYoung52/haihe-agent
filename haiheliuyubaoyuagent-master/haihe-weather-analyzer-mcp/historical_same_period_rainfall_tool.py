"""历史同期平均降雨量 MCP 工具。

默认口径：近 10 年同一月日、同一小时段内，海河流域自动站累计降雨量的多年平均。
接口：getSurfEleInBasinByTime + SURF_CHN_MUL_HOR + PRE_1h。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastmcp import FastMCP

from constants import DEFAULT_BASIN_CODES
from tools import _get_music_client


HOURLY_DATA_CODE = "SURF_CHN_MUL_HOR"
HOURLY_RAIN_FIELD = "PRE_1h"


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, "", "None"):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    # 过滤缺测和明显异常值。
    if number < 0 or number > 9999:
        return None
    return number


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("<br>", "").replace("<br/>", "").replace("</br>", "").strip()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    return None


def _default_reference_window() -> tuple[datetime, datetime]:
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    start = now.replace(hour=0)
    return start, now


def _safe_replace_year(dt: datetime, year: int) -> datetime:
    try:
        return dt.replace(year=year)
    except ValueError:
        # 2月29日映射到非闰年时，按2月28日处理。
        return dt.replace(year=year, month=2, day=28)


def _hour_times(start: datetime, end: datetime) -> str:
    if end < start:
        start, end = end, start
    cur = start.replace(minute=0, second=0, microsecond=0)
    end = end.replace(minute=0, second=0, microsecond=0)
    times: list[str] = []
    # 防止异常长窗口导致 URL 过长；该工具面向短时段同期比较。
    max_hours = 168
    count = 0
    while cur <= end and count <= max_hours:
        times.append(cur.strftime("%Y%m%d%H%M%S"))
        cur += timedelta(hours=1)
        count += 1
    return ",".join(times)


def _station_id(record: dict) -> str:
    return _safe_text(record.get("Station_Id_C") or record.get("station_id"))


def _station_name(record: dict) -> str:
    return (
        _safe_text(record.get("Station_Name"))
        or _safe_text(record.get("station_name"))
        or _safe_text(record.get("Station_Id_C"))
        or "未知站点"
    )


def _station_location(record: dict) -> dict:
    return {
        "province": _safe_text(record.get("Province")),
        "city": _safe_text(record.get("City")),
        "county": _safe_text(record.get("Cnty")),
        "town": _safe_text(record.get("Town")),
        "lon": _safe_float(record.get("Lon")),
        "lat": _safe_float(record.get("Lat")),
    }


def _summarize_year(records: list[dict], year: int) -> dict | None:
    station_map: dict[str, dict] = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        sid = _station_id(record)
        rain = _safe_float(record.get(HOURLY_RAIN_FIELD))
        if not sid or rain is None:
            continue
        if sid not in station_map:
            station_map[sid] = {
                "station_id": sid,
                "station_name": _station_name(record),
                **_station_location(record),
                "rainfall_mm": 0.0,
                "valid_hour_count": 0,
            }
        station_map[sid]["rainfall_mm"] += float(rain)
        station_map[sid]["valid_hour_count"] += 1

    stations = list(station_map.values())
    if not stations:
        return None
    for item in stations:
        item["rainfall_mm"] = round(float(item.get("rainfall_mm") or 0.0), 2)
    stations.sort(key=lambda x: float(x.get("rainfall_mm") or 0.0), reverse=True)
    average = round(sum(float(s.get("rainfall_mm") or 0.0) for s in stations) / len(stations), 2)
    return {
        "year": year,
        "average_rainfall_mm": average,
        "station_count": len(stations),
        "max_station": stations[0],
        "top_stations": stations[:10],
    }


def _historical_years(reference_end: datetime, years: int) -> list[int]:
    years = max(1, min(int(years or 10), 30))
    return [reference_end.year - i for i in range(1, years + 1)]


def register_historical_same_period_rainfall_tool(mcp: FastMCP) -> None:
    @mcp.tool()
    def query_historical_same_period_avg_rainfall(
        reference_start_time: str | None = None,
        reference_end_time: str | None = None,
        years: int = 10,
    ) -> dict:
        """
        查询历史同期平均降雨量。

        适用于：“历史同期平均降雨量是多少”“历年同期平均雨量是多少”。
        默认参考时段：今天 00:00 到当前整点。
        统计口径：近 years 年同月日同小时段，海河流域自动站逐小时降雨量累加；每年先求自动站平均累计雨量，再对多年取平均。
        """
        ref_start = _parse_time(reference_start_time)
        ref_end = _parse_time(reference_end_time)
        if not ref_start or not ref_end:
            ref_start, ref_end = _default_reference_window()
        if ref_end < ref_start:
            ref_start, ref_end = ref_end, ref_start

        years = max(1, min(int(years or 10), 30))
        client = _get_music_client()
        year_rows: list[dict] = []
        errors: list[str] = []

        for year in _historical_years(ref_end, years):
            hist_start = _safe_replace_year(ref_start, year)
            hist_end = _safe_replace_year(ref_end, year)
            times = _hour_times(hist_start, hist_end)
            if not times:
                continue
            try:
                records = client.get_surf_ele_in_basin_by_time(
                    basin_codes=DEFAULT_BASIN_CODES,
                    times=times,
                    elements="Station_Id_C,Station_Name,Lat,Lon,City,Cnty,Province,Town,Datetime,PRE_1h",
                    data_code=HOURLY_DATA_CODE,
                    ele_value_ranges="PRE_1h:(,9999)",
                    order_by="PRE_1h:desc",
                )
            except Exception as exc:
                msg = f"{year}: {str(exc)[:160]}"
                print(f"[historical_same_period_rainfall] getSurfEleInBasinByTime 查询失败：{msg}")
                errors.append(msg)
                continue

            summary = _summarize_year(records, year)
            if summary:
                summary["time_range_readable"] = f"{hist_start:%Y-%m-%d %H:%M:%S} ~ {hist_end:%Y-%m-%d %H:%M:%S}"
                year_rows.append(summary)

        if not year_rows:
            return {
                "status": "no_data",
                "query_type": "historical_same_period_avg_rainfall",
                "reference_time_range_readable": f"{ref_start:%Y-%m-%d %H:%M:%S} ~ {ref_end:%Y-%m-%d %H:%M:%S}",
                "years": years,
                "valid_year_count": 0,
                "message": "历史同期暂无有效自动站降雨量数据。",
                "yearly_records": [],
                "summary": {
                    "historical_average_rainfall_mm": None,
                    "max_year": None,
                    "min_year": None,
                    "errors": errors[:5],
                },
            }

        avg = round(sum(float(r["average_rainfall_mm"]) for r in year_rows) / len(year_rows), 2)
        max_year = max(year_rows, key=lambda r: float(r.get("average_rainfall_mm") or 0.0))
        min_year = min(year_rows, key=lambda r: float(r.get("average_rainfall_mm") or 0.0))
        years_sorted = sorted(int(r["year"]) for r in year_rows)

        return {
            "status": "ok",
            "query_type": "historical_same_period_avg_rainfall",
            "reference_time_range_readable": f"{ref_start:%Y-%m-%d %H:%M:%S} ~ {ref_end:%Y-%m-%d %H:%M:%S}",
            "historical_year_range": f"{years_sorted[0]}—{years_sorted[-1]}",
            "years": years,
            "valid_year_count": len(year_rows),
            "historical_average_rainfall_mm": avg,
            "yearly_records": sorted(year_rows, key=lambda r: int(r["year"]), reverse=True),
            "summary": {
                "historical_average_rainfall_mm": avg,
                "max_year": max_year,
                "min_year": min_year,
                "errors": errors[:5],
            },
        }
