"""上月面雨量 MCP 工具。

迁移版：保留 query_last_month_areal_rainfall 工具名和业务返回结构；
优先使用天擎面雨量历史产品聚合上一个自然月数据。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from fastmcp import FastMCP


_ZONE_LABELS = {
    "9": "海河9分区",
    "11": "海河11分区",
    "32": "海河32分区",
    "77": "海河77分区",
    "246": "海河246分区",
}


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


def _aggregate_raw_areal_rows(raw: list[dict], rain_field: str) -> list[dict]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        area_id = str(item.get("V_AREA_ID") or item.get("zone_id") or "").strip()
        rainfall = _safe_float(item.get(rain_field))
        if not area_id or rainfall is None:
            continue
        grouped[area_id].append(rainfall)

    rows: list[dict] = []
    for area_id, values in grouped.items():
        if not values:
            continue
        rows.append(
            {
                "zone_id": area_id,
                "zone_name": f"分区{area_id}",
                "avg_rainfall_mm": round(sum(values), 2),
                "max_rainfall_mm": round(max(values), 2),
                "record_count": len(values),
            }
        )
    rows.sort(key=lambda x: float(x.get("avg_rainfall_mm") or 0.0), reverse=True)
    return rows


def _summarize_rows(rows: list[dict]) -> dict:
    if not rows:
        return {"zone_count": 0, "max_zone": None, "simple_mean_of_zone_rainfall_mm": None}
    vals = [float(row.get("avg_rainfall_mm") or 0.0) for row in rows]
    return {
        "zone_count": len(rows),
        "max_zone": rows[0],
        "simple_mean_of_zone_rainfall_mm": round(sum(vals) / len(vals), 2),
    }


def _no_data_payload(month_label: str, time_range: str, readable: str, zone_type: str) -> dict:
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
        "message": "上一个自然月面雨量暂无有效数据。",
    }


def register_last_month_areal_rainfall_tool(mcp: FastMCP) -> None:
    @mcp.tool()
    def query_last_month_areal_rainfall(zone_type: str = "9") -> dict:
        """查询上一个自然月的海河流域分区累计面雨量。"""
        zone_type = str(zone_type or "9").strip()
        start_s, end_s, readable, month_label = _previous_calendar_month_range()
        time_range = f"[{start_s},{end_s}]"

        raw = None
        rain_field = None
        try:
            from utils.TQ_utils import getSevpEleByTimeRangeHistory, statSevpEleByTimeRangeHistory

            try:
                raw = getSevpEleByTimeRangeHistory(
                    time_range=time_range,
                    elements="Datetime,V_AREA_ID,V_RAIN_1H",
                )
                if raw:
                    rain_field = "V_RAIN_1H"
            except Exception:
                raw = None

            if not raw:
                try:
                    raw = statSevpEleByTimeRangeHistory(
                        time_range=time_range,
                        elements="V_AREA_ID,Datetime",
                    )
                    if raw:
                        rain_field = "SUM_V_RAIN_1H"
                except Exception:
                    raw = None
        except Exception:
            raw = None

        if not raw or not rain_field:
            return _no_data_payload(month_label, time_range, readable, zone_type)

        rows = _aggregate_raw_areal_rows(raw, rain_field)
        if not rows:
            return _no_data_payload(month_label, time_range, readable, zone_type)

        zone_label = _ZONE_LABELS.get(zone_type, zone_type)
        if zone_type == "9" and len(rows) > 12:
            zone_label = "海河流域面雨量分区"

        return {
            "status": "ok",
            "query_type": "last_month_areal_rainfall",
            "month": month_label,
            "time_range": time_range,
            "time_range_readable": readable,
            "zone_type": zone_type,
            "zone_label": zone_label,
            "records": rows,
            "summary": _summarize_rows(rows),
        }
