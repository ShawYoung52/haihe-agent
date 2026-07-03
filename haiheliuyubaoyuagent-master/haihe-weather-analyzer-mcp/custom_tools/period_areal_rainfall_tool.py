"""指定时间段海河9分区面雨量 MCP 工具。

用于“哪个子流域降雨最多”“某时段分区面雨量”等短时段问题，强制返回海河9分区结果。
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


def _parse_time_range(time_range: str) -> tuple[str, str] | None:
    text = str(time_range or "").strip().strip("[]")
    if "," not in text:
        return None
    start_s, end_s = [x.strip() for x in text.split(",", 1)]
    if len(start_s) != 14 or len(end_s) != 14 or not start_s.isdigit() or not end_s.isdigit():
        return None
    return start_s, end_s


def _chunk_ranges(start_s: str, end_s: str) -> list[str]:
    """长时段按天拆分，短时段保留原时段。"""
    start = datetime.strptime(start_s, "%Y%m%d%H%M%S")
    end = datetime.strptime(end_s, "%Y%m%d%H%M%S")
    if end < start:
        start, end = end, start
    if (end - start) <= timedelta(days=2):
        return [f"[{start:%Y%m%d%H%M%S},{end:%Y%m%d%H%M%S}]"]
    ranges: list[str] = []
    cur = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while cur <= end:
        chunk_start = max(cur, start)
        chunk_end = min(cur + timedelta(days=1) - timedelta(seconds=1), end)
        ranges.append(f"[{chunk_start:%Y%m%d%H%M%S},{chunk_end:%Y%m%d%H%M%S}]")
        cur += timedelta(days=1)
    return ranges


def _readable(start_s: str, end_s: str) -> str:
    start = datetime.strptime(start_s, "%Y%m%d%H%M%S")
    end = datetime.strptime(end_s, "%Y%m%d%H%M%S")
    if end < start:
        start, end = end, start
    return f"{start:%Y-%m-%d %H:%M:%S} ~ {end:%Y-%m-%d %H:%M:%S}"


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


def _merge_zone_rows(chunks: list[list[dict]]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for rows in chunks:
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
        "query_type": "period_areal_rainfall_9",
        "time_range": time_range,
        "time_range_readable": readable,
        "zone_type": "9",
        "zone_label": "海河9分区",
        "records": [],
        "summary": _summarize_rows([]),
        "message": "当前时段暂无有效海河9分区面雨量数据。",
        "debug_reason": reason[:300] if reason else "",
    }


def register_period_areal_rainfall_tool(mcp: FastMCP) -> None:
    @mcp.tool()
    def query_period_areal_rainfall_9(time_range: str, zone_type: str = "9") -> dict:
        """查询指定时间段海河9分区累计面雨量。"""
        zone_type = str(zone_type or "9").strip()
        parsed = _parse_time_range(time_range)
        if not parsed:
            return _no_data_payload(str(time_range or ""), "-", reason="invalid_time_range")
        start_s, end_s = parsed
        readable = _readable(start_s, end_s)
        canonical_time_range = f"[{start_s},{end_s}]"
        pg_conf = _get_postgres_conf()
        if not pg_conf:
            return _no_data_payload(canonical_time_range, readable, reason="postgres_config_missing")

        chunks: list[list[dict]] = []
        errors: list[str] = []
        for tr in _chunk_ranges(start_s, end_s):
            try:
                rows = _aggregate_areal_rainfall_from_stations(tr, zone_type, pg_conf) or []
            except Exception as exc:
                errors.append(str(exc)[:120])
                rows = []
            if rows:
                chunks.append(rows)

        rows = _merge_zone_rows(chunks)
        if not rows:
            return _no_data_payload(canonical_time_range, readable, reason="; ".join(errors[:3]) if errors else "empty_rows")
        if zone_type == "9" and len(rows) > 12:
            return _no_data_payload(canonical_time_range, readable, reason=f"unexpected_zone_count:{len(rows)}")

        return {
            "status": "ok",
            "query_type": "period_areal_rainfall_9",
            "time_range": canonical_time_range,
            "time_range_readable": readable,
            "zone_type": zone_type,
            "zone_label": _ZONE_LABELS.get(zone_type, zone_type),
            "records": rows,
            "summary": _summarize_rows(rows),
        }
