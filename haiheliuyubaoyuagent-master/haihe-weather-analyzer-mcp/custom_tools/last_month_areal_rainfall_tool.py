"""上月面雨量 MCP 工具。

业务口径：上一个自然月，海河9分区累计面雨量。
实现方式：复用项目已有站点落区聚合能力，避免把细分区误当成9分区。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastmcp import FastMCP

from tools import config, _aggregate_areal_rainfall_from_stations


_ZONE_LABELS = {"9": "海河9分区"}


def _get_postgres_conf():
    if "postgres" not in config:
        return None
    return config["postgres"]


def _previous_calendar_month_range(now: datetime | None = None) -> tuple[str, str, str, str]:
    now = now or datetime.now()
    first_day_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_second_prev_month = first_day_this_month - timedelta(seconds=1)
    first_day_prev_month = last_second_prev_month.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start_s = first_day_prev_month.strftime("%Y%m%d%H%M%S")
    end_s = last_second_prev_month.strftime("%Y%m%d%H%M%S")
    readable = f"{first_day_prev_month:%Y-%m-%d %H:%M:%S} ~ {last_second_prev_month:%Y-%m-%d %H:%M:%S}"
    month_label = f"{first_day_prev_month:%Y年%m月}"
    return start_s, end_s, readable, month_label


def _day_ranges(start_s: str, end_s: str) -> list[str]:
    start = datetime.strptime(start_s, "%Y%m%d%H%M%S")
    end = datetime.strptime(end_s, "%Y%m%d%H%M%S")
    ranges: list[str] = []
    cur = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while cur <= end:
        day_start = max(cur, start)
        day_end = min(cur + timedelta(days=1) - timedelta(seconds=1), end)
        ranges.append(f"[{day_start:%Y%m%d%H%M%S},{day_end:%Y%m%d%H%M%S}]")
        cur += timedelta(days=1)
    return ranges


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


def _merge_zone_rows(day_rows: list[list[dict]]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for rows in day_rows:
        for row in rows or []:
            if not isinstance(row, dict) or "error" in row:
                continue
            zone_id = str(row.get("zone_id") or row.get("zone_name") or "").strip()
            zone_name = str(row.get("zone_name") or zone_id or "未知分区").strip()
            rain = _safe_float(row.get("avg_rainfall_mm") or row.get("avg") or row.get("average_rainfall_mm"))
            max_rain = _safe_float(row.get("max_rainfall_mm") or row.get("max") or row.get("maximum_rainfall_mm"))
            if not zone_id or rain is None:
                continue
            if zone_id not in grouped:
                grouped[zone_id] = {
                    "zone_id": zone_id,
                    "zone_name": zone_name,
                    "avg_rainfall_mm": 0.0,
                    "max_rainfall_mm": 0.0,
                    "record_count": 0,
                }
            grouped[zone_id]["zone_name"] = zone_name or grouped[zone_id]["zone_name"]
            grouped[zone_id]["avg_rainfall_mm"] += float(rain)
            if max_rain is not None:
                grouped[zone_id]["max_rainfall_mm"] = max(float(grouped[zone_id].get("max_rainfall_mm") or 0.0), float(max_rain))
            grouped[zone_id]["record_count"] += int(row.get("record_count") or 1)

    out = list(grouped.values())
    for row in out:
        row["avg_rainfall_mm"] = round(float(row.get("avg_rainfall_mm") or 0.0), 2)
        row["max_rainfall_mm"] = round(float(row.get("max_rainfall_mm") or 0.0), 2)
    out.sort(key=lambda x: float(x.get("avg_rainfall_mm") or 0.0), reverse=True)
    return out


def _summarize_rows(rows: list[dict]) -> dict:
    if not rows:
        return {
            "zone_count": 0,
            "max_zone": None,
            "min_zone": None,
            "simple_mean_of_zone_rainfall_mm": None,
            "total_of_zone_rainfall_mm": None,
        }
    vals = [float(row.get("avg_rainfall_mm") or 0.0) for row in rows]
    return {
        "zone_count": len(rows),
        "max_zone": rows[0],
        "min_zone": rows[-1],
        "simple_mean_of_zone_rainfall_mm": round(sum(vals) / len(vals), 2),
        "total_of_zone_rainfall_mm": round(sum(vals), 2),
    }


def _no_data_payload(month_label: str, time_range: str, readable: str, zone_type: str, reason: str = "") -> dict:
    return {
        "status": "no_data",
        "query_type": "last_month_areal_rainfall",
        "month": month_label,
        "time_range": time_range,
        "time_range_readable": readable,
        "zone_type": zone_type,
        "zone_label": _ZONE_LABELS.get(zone_type, zone_type),
        "records": [],
        "summary": _summarize_rows([]),
        "message": "上一个自然月暂无有效海河9分区面雨量数据。",
        "debug_reason": reason[:300] if reason else "",
    }


def register_last_month_areal_rainfall_tool(mcp: FastMCP) -> None:
    @mcp.tool()
    def query_last_month_areal_rainfall(zone_type: str = "9") -> dict:
        """查询上一个自然月的海河9分区累计面雨量。"""
        zone_type = str(zone_type or "9").strip()
        start_s, end_s, readable, month_label = _previous_calendar_month_range()
        time_range = f"[{start_s},{end_s}]"
        pg_conf = _get_postgres_conf()
        if not pg_conf:
            return _no_data_payload(month_label, time_range, readable, zone_type, reason="postgres_config_missing")

        day_rows: list[list[dict]] = []
        errors: list[str] = []
        for tr in _day_ranges(start_s, end_s):
            try:
                rows = _aggregate_areal_rainfall_from_stations(tr, zone_type, pg_conf) or []
            except Exception as exc:
                errors.append(str(exc)[:120])
                rows = []
            if rows:
                day_rows.append(rows)

        rows = _merge_zone_rows(day_rows)
        if not rows:
            return _no_data_payload(month_label, time_range, readable, zone_type, reason="; ".join(errors[:3]) if errors else "empty_rows")

        if zone_type == "9" and len(rows) > 12:
            return _no_data_payload(month_label, time_range, readable, zone_type, reason=f"unexpected_zone_count:{len(rows)}")

        return {
            "status": "ok",
            "query_type": "last_month_areal_rainfall",
            "month": month_label,
            "time_range": time_range,
            "time_range_readable": readable,
            "zone_type": zone_type,
            "zone_label": _ZONE_LABELS.get(zone_type, zone_type),
            "records": rows,
            "summary": _summarize_rows(rows),
        }
