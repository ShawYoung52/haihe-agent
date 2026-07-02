"""上月面雨量 MCP 工具。

该模块单独注册“上个月面雨量”查询能力，避免改动体量较大的 tools.py。
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

import psycopg2
from fastmcp import FastMCP
from psycopg2.extras import RealDictCursor

from tools import config, _aggregate_areal_rainfall_from_stations


_ZONE_TABLES = {
    "11": "haihe_zone_11",
    "77": "haihe_zone_77",
    "246": "haihe_246_zone",
    "32": "haihe_zone_32",
    "9": "haihe_zone_9",
}

_ZONE_LABELS = {
    "11": "海河11分区",
    "77": "海河77分区",
    "246": "海河246分区",
    "32": "海河32分区",
    "9": "海河9分区",
}


def _previous_calendar_month_range(now: datetime | None = None) -> tuple[str, str, str, str]:
    """返回上一个自然月的起止时间。"""
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


def _load_zone_name_map(zone_type: str) -> dict[str, str]:
    """加载指定分区表的 zone_code -> zone_name 映射。"""
    table_name = _ZONE_TABLES.get(zone_type)
    if not table_name or "postgres" not in config:
        return {}

    pg_conf = config["postgres"]
    timeout = int(pg_conf.get("connect_timeout", "5")) if str(pg_conf.get("connect_timeout", "5")).isdigit() else 5
    zone_map: dict[str, str] = {}
    try:
        with psycopg2.connect(
            host=pg_conf["host"],
            port=pg_conf["port"],
            dbname=pg_conf["dbname"],
            user=pg_conf["user"],
            password=pg_conf["password"],
            sslmode=pg_conf.get("sslmode", "prefer"),
            connect_timeout=timeout,
        ) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(f"SELECT zone_code, zone_name FROM {table_name} WHERE zone_name IS NOT NULL")
                for row in cur.fetchall():
                    code = str(row.get("zone_code") or "").strip()
                    name = str(row.get("zone_name") or "").strip()
                    if not code or not name:
                        continue
                    zone_map[code] = name
                    num_part = code.split("_")[-1]
                    if num_part.isdigit():
                        zone_map[num_part] = name
                        zone_map[str(int(num_part))] = name
    except Exception:
        return {}
    return zone_map


def _zone_name(zone_map: dict[str, str], zone_id: str) -> str:
    zone_id = str(zone_id or "").strip()
    if not zone_id:
        return "未知分区"
    if zone_id in zone_map:
        return zone_map[zone_id]
    if zone_id.lstrip("0").isdigit():
        normalized = str(int(zone_id))
        if normalized in zone_map:
            return zone_map[normalized]
    for pad in range(2, 6):
        padded = zone_id.zfill(pad)
        if padded in zone_map:
            return zone_map[padded]
    return f"分区{zone_id}"


def _aggregate_raw_areal_rows(raw: list[dict], rain_field: str, zone_type: str) -> list[dict]:
    """将天擎面雨量原始行聚合为分区累计面雨量。"""
    zone_map = _load_zone_name_map(zone_type)
    grouped: dict[str, list[float]] = defaultdict(list)

    for item in raw or []:
        if not isinstance(item, dict):
            continue
        area_id = str(item.get("V_AREA_ID", "")).strip()
        rainfall = _safe_float(item.get(rain_field))
        if not area_id or rainfall is None:
            continue
        grouped[area_id].append(rainfall)

    rows: list[dict] = []
    for area_id, values in grouped.items():
        if not values:
            continue
        cumulative = sum(values)
        rows.append(
            {
                "zone_id": area_id,
                "zone_name": _zone_name(zone_map, area_id),
                "avg_rainfall_mm": round(cumulative, 2),
                "max_rainfall_mm": round(max(values), 2),
                "record_count": len(values),
            }
        )
    rows.sort(key=lambda x: x["avg_rainfall_mm"], reverse=True)
    return rows


def _summarize_rows(rows: list[dict]) -> dict:
    if not rows:
        return {
            "zone_count": 0,
            "max_zone": None,
            "simple_mean_of_zone_rainfall_mm": None,
        }
    vals = [float(row.get("avg_rainfall_mm") or 0.0) for row in rows]
    return {
        "zone_count": len(rows),
        "max_zone": rows[0],
        "simple_mean_of_zone_rainfall_mm": round(sum(vals) / len(vals), 2),
        "note": "simple_mean_of_zone_rainfall_mm 为各分区累计面雨量的算术平均，不等同于面积加权全流域面雨量。",
    }


def register_last_month_areal_rainfall_tool(mcp: FastMCP) -> None:
    @mcp.tool()
    def query_last_month_areal_rainfall(zone_type: str = "9") -> dict:
        """
        查询上一个自然月的海河流域分区累计面雨量。

        当用户询问“上个月面雨量是多少”“上月各子流域面雨量”“上一个月哪个分区雨量最大”时使用。
        默认按海河9分区返回，适合领导快速查看主要子流域；如需更细可传 11/32/77/246。
        """
        zone_type = str(zone_type or "9").strip()
        if zone_type not in _ZONE_TABLES:
            return {
                "status": "error",
                "error": f"zone_type 仅支持 {', '.join(_ZONE_TABLES.keys())}，当前为 {zone_type}",
            }

        start_s, end_s, readable, month_label = _previous_calendar_month_range()
        time_range = f"[{start_s},{end_s}]"

        raw = None
        rain_field = None
        data_source = "天擎面雨量实况"

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

        if raw and rain_field:
            rows = _aggregate_raw_areal_rows(raw, rain_field, zone_type)
        else:
            rows = []

        if not rows:
            try:
                pg_conf = config["postgres"]
                rows = _aggregate_areal_rainfall_from_stations(time_range, zone_type, pg_conf) or []
                if rows:
                    data_source = "天擎自动站降雨聚合面雨量"
                    rain_field = "PRE_1h_station_agg"
            except Exception:
                rows = []

        payload = {
            "status": "ok" if rows else "no_data",
            "query_type": "last_month_areal_rainfall",
            "month": month_label,
            "time_range": time_range,
            "time_range_readable": readable,
            "zone_type": zone_type,
            "zone_label": _ZONE_LABELS.get(zone_type, zone_type),
            "data_source": data_source,
            "rain_field": rain_field,
            "records": rows,
            "summary": _summarize_rows(rows),
        }
        if not rows:
            payload["message"] = "上一个自然月面雨量无数据或数据源暂不可用。"
        return payload
