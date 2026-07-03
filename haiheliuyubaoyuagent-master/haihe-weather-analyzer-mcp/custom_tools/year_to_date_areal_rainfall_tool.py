"""今年以来累计面雨量 MCP 工具。

业务口径：今年 1 月 1 日 00:00 到当前时刻，按海河9分区返回累计面雨量。
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


def _year_to_date_range(now: datetime | None = None) -> tuple[str, str, str]:
    now = now or datetime.now()
    start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return (
        start.strftime("%Y%m%d%H%M%S"),
        now.strftime("%Y%m%d%H%M%S"),
        f"{start:%Y-%m-%d %H:%M:%S} ~ {now:%Y-%m-%d %H:%M:%S}",
    )


def _month_ranges(start_s: str, end_s: str) -> list[str]:
    start = datetime.strptime(start_s, "%Y%m%d%H%M%S")
    end = datetime.strptime(end_s, "%Y%m%d%H%M%S")
    ranges: list[str] = []
    cur = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cur <= end:
        next_month = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        month_start = max(cur, start)
        month_end = min(next_month - timedelta(seconds=1), end)
        ranges.append(f"[{month_start:%Y%m%d%H%M%S},{month_end:%Y%m%d%H%M%S}]")
        cur = next_month
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


def _merge_zone_rows(month_rows: list[list[dict]]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for rows in month_rows:
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


def _no_data_payload(time_range: str, readable: str, reason: str = "") -> dict:
    return {
        "status": "no_data",
        "query_type": "year_to_date_areal_rainfall",
        "time_range": time_range,
        "time_range_readable": readable,
        "zone_type": "9",
        "zone_label": "海河9分区",
        "records": [],
        "summary": _summarize_rows([]),
        "message": "今年以来暂无有效海河9分区累计降雨量数据。",
        "debug_reason": reason[:300] if reason else "",
    }


def register_year_to_date_areal_rainfall_tool(mcp: FastMCP) -> None:
    @mcp.tool()
    def query_year_to_date_areal_rainfall(zone_type: str = "9") -> dict:
        """查询今年以来海河9分区累计面雨量。"""
        zone_type = str(zone_type or "9").strip()
        start_s, end_s, readable = _year_to_date_range()
        time_range = f"[{start_s},{end_s}]"
        pg_conf = _get_postgres_conf()
        if not pg_conf:
            return _no_data_payload(time_range, readable, reason="postgres_config_missing")

        month_rows: list[list[dict]] = []
        errors: list[str] = []
        for tr in _month_ranges(start_s, end_s):
            try:
                rows = _aggregate_areal_rainfall_from_stations(tr, zone_type, pg_conf) or []
            except Exception as exc:
                errors.append(str(exc)[:120])
                rows = []
            if rows:
                month_rows.append(rows)

        rows = _merge_zone_rows(month_rows)
        if not rows:
            return _no_data_payload(time_range, readable, reason="; ".join(errors[:3]) if errors else "empty_rows")

        # 业务问题要求海河9分区；如果站点落区结果不是9分区口径，宁可返回无数据，也不误展示细分区。
        if zone_type == "9" and len(rows) > 12:
            return _no_data_payload(time_range, readable, reason=f"unexpected_zone_count:{len(rows)}")

        return {
            "status": "ok",
            "query_type": "year_to_date_areal_rainfall",
            "time_range": time_range,
            "time_range_readable": readable,
            "zone_type": zone_type,
            "zone_label": _ZONE_LABELS.get(zone_type, zone_type),
            "records": rows,
            "summary": _summarize_rows(rows),
        }
