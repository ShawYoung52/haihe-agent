"""上月面雨量 MCP 工具。

恢复为原先可用的快链路：天擎面雨量细分区结果通过空间关系汇总到海河9分区；
不再走整月/逐日站点落区慢链路。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

import psycopg2
from fastmcp import FastMCP
from psycopg2.extras import RealDictCursor

from tools import config


_ZONE_LABELS = {"9": "海河9分区"}
_FINE_ZONE_TABLES_FOR_9 = ["haihe_246_zone", "haihe_zone_77", "haihe_zone_32", "haihe_zone_11"]


def _get_postgres_conf():
    if "postgres" not in config:
        return None
    return config["postgres"]


def _previous_calendar_month_range(now: datetime | None = None) -> tuple[str, str, str, str]:
    now = now or datetime.now()
    first_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end_prev = first_this_month - timedelta(seconds=1)
    start_prev = end_prev.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (
        start_prev.strftime("%Y%m%d%H%M%S"),
        end_prev.strftime("%Y%m%d%H%M%S"),
        f"{start_prev:%Y-%m-%d %H:%M:%S} ~ {end_prev:%Y-%m-%d %H:%M:%S}",
        f"{start_prev:%Y年%m月}",
    )


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, "", "None"):
            return None
        num = float(value)
    except Exception:
        return None
    if num < 0 or num > 9999:
        return None
    return num


def _summarize_rows(rows: list[dict]) -> dict:
    if not rows:
        return {"zone_count": 0, "max_zone": None, "simple_mean_of_zone_rainfall_mm": None}
    vals = [float(row.get("avg_rainfall_mm") or 0.0) for row in rows]
    return {
        "zone_count": len(rows),
        "max_zone": rows[0],
        "simple_mean_of_zone_rainfall_mm": round(sum(vals) / len(vals), 2),
    }


def _no_data_payload(month_label: str, time_range: str, readable: str, reason: str = "") -> dict:
    return {
        "status": "no_data",
        "query_type": "last_month_areal_rainfall",
        "month": month_label,
        "time_range": time_range,
        "time_range_readable": readable,
        "zone_type": "9",
        "zone_label": "海河9分区",
        "records": [],
        "summary": _summarize_rows([]),
        "message": "上一个自然月面雨量暂无有效数据。",
        "debug_reason": reason[:200] if reason else "",
    }


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
        rows.append({
            "zone_id": area_id,
            "zone_name": f"分区{area_id}",
            "avg_rainfall_mm": round(sum(values), 2),
            "max_rainfall_mm": round(max(values), 2),
            "record_count": len(values),
        })
    rows.sort(key=lambda x: float(x.get("avg_rainfall_mm") or 0.0), reverse=True)
    return rows


def _area_id_aliases(area_ids: list[str]) -> list[str]:
    aliases: set[str] = set()
    for raw in area_ids:
        s = str(raw or "").strip()
        if not s:
            continue
        aliases.add(s)
        last = s.split("_")[-1]
        if last:
            aliases.add(last)
        if last.isdigit():
            aliases.add(str(int(last)))
    return sorted(aliases)


def _map_fine_area_ids_to_zone9(area_ids: list[str]) -> dict[str, dict]:
    pg_conf = _get_postgres_conf()
    if not pg_conf:
        return {}
    ids = _area_id_aliases(area_ids)
    if not ids:
        return {}
    schema = pg_conf.get("schema", "public")
    timeout = int(pg_conf.get("connect_timeout", "5")) if str(pg_conf.get("connect_timeout", "5")).isdigit() else 5
    mapping: dict[str, dict] = {}
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
                for table_name in _FINE_ZONE_TABLES_FOR_9:
                    sql = f"""
                        SELECT
                            f.zone_code::text AS fine_code,
                            regexp_replace(f.zone_code::text, '^.*_', '') AS fine_short_code,
                            z9.zone_code::text AS zone9_code,
                            z9.zone_name AS zone9_name,
                            NULLIF(ST_Area(f.geom), 0) AS fine_area
                        FROM {schema}.{table_name} f
                        JOIN {schema}.haihe_zone_9 z9
                          ON ST_Intersects(ST_PointOnSurface(f.geom), z9.geom)
                        WHERE f.zone_code::text = ANY(%(ids)s)
                           OR regexp_replace(f.zone_code::text, '^.*_', '') = ANY(%(ids)s)
                           OR ltrim(regexp_replace(f.zone_code::text, '^.*_', ''), '0') = ANY(%(ids)s)
                    """
                    try:
                        cur.execute(sql, {"ids": ids})
                    except Exception:
                        continue
                    for row in cur.fetchall():
                        fine_code = str(row.get("fine_code") or "").strip()
                        fine_short = str(row.get("fine_short_code") or "").strip()
                        item = {
                            "zone9_code": str(row.get("zone9_code") or "").strip(),
                            "zone9_name": str(row.get("zone9_name") or "").strip(),
                            "weight": float(row.get("fine_area") or 1.0),
                        }
                        for key in {fine_code, fine_short, str(int(fine_short)) if fine_short.isdigit() else fine_short}:
                            if key and key not in mapping:
                                mapping[key] = item
    except Exception:
        return {}
    return mapping


def _aggregate_fine_rows_to_zone9(fine_rows: list[dict]) -> list[dict]:
    area_ids = [str(r.get("zone_id") or "").strip() for r in fine_rows if isinstance(r, dict)]
    fine_to_9 = _map_fine_area_ids_to_zone9(area_ids)
    if not fine_to_9:
        return []

    grouped: dict[str, dict] = {}
    for row in fine_rows:
        if not isinstance(row, dict):
            continue
        fine_id = str(row.get("zone_id") or "").strip()
        key = fine_id
        if key not in fine_to_9 and fine_id.isdigit():
            key = str(int(fine_id))
        map_item = fine_to_9.get(key)
        if not map_item:
            continue
        zone9_code = map_item.get("zone9_code") or map_item.get("zone9_name")
        zone9_name = map_item.get("zone9_name") or f"分区{zone9_code}"
        rain = _safe_float(row.get("avg_rainfall_mm"))
        mx = _safe_float(row.get("max_rainfall_mm"))
        if rain is None:
            continue
        weight = float(map_item.get("weight") or 1.0)
        if zone9_code not in grouped:
            grouped[zone9_code] = {
                "zone_id": zone9_code,
                "zone_name": zone9_name,
                "weighted_sum": 0.0,
                "weight_sum": 0.0,
                "max_rainfall_mm": 0.0,
                "record_count": 0,
            }
        grouped[zone9_code]["weighted_sum"] += rain * weight
        grouped[zone9_code]["weight_sum"] += weight
        if mx is not None:
            grouped[zone9_code]["max_rainfall_mm"] = max(float(grouped[zone9_code]["max_rainfall_mm"]), float(mx))
        grouped[zone9_code]["record_count"] += int(row.get("record_count") or 1)

    out: list[dict] = []
    for item in grouped.values():
        weight_sum = float(item.pop("weight_sum") or 0.0)
        weighted_sum = float(item.pop("weighted_sum") or 0.0)
        if weight_sum <= 0:
            continue
        item["avg_rainfall_mm"] = round(weighted_sum / weight_sum, 2)
        item["max_rainfall_mm"] = round(float(item.get("max_rainfall_mm") or 0.0), 2)
        out.append(item)
    out.sort(key=lambda x: float(x.get("avg_rainfall_mm") or 0.0), reverse=True)
    return out


def register_last_month_areal_rainfall_tool(mcp: FastMCP) -> None:
    @mcp.tool()
    def query_last_month_areal_rainfall(zone_type: str = "9") -> dict:
        """查询上一个自然月的海河9分区累计面雨量。"""
        start_s, end_s, readable, month_label = _previous_calendar_month_range()
        time_range = f"[{start_s},{end_s}]"
        try:
            from utils.TQ_utils import getSevpEleByTimeRangeHistory, statSevpEleByTimeRangeHistory

            raw = None
            rain_field = None
            try:
                raw = getSevpEleByTimeRangeHistory(time_range=time_range, elements="Datetime,V_AREA_ID,V_RAIN_1H")
                if raw:
                    rain_field = "V_RAIN_1H"
            except Exception:
                raw = None

            if not raw:
                try:
                    raw = statSevpEleByTimeRangeHistory(time_range=time_range, elements="V_AREA_ID,Datetime")
                    if raw:
                        rain_field = "SUM_V_RAIN_1H"
                except Exception:
                    raw = None

            if not raw or not rain_field:
                return _no_data_payload(month_label, time_range, readable, reason="raw_empty")

            fine_rows = _aggregate_raw_areal_rows(raw, rain_field)
            rows = fine_rows if len(fine_rows) <= 12 else _aggregate_fine_rows_to_zone9(fine_rows)
            if not rows:
                return _no_data_payload(month_label, time_range, readable, reason="zone9_empty")

            return {
                "status": "ok",
                "query_type": "last_month_areal_rainfall",
                "month": month_label,
                "time_range": time_range,
                "time_range_readable": readable,
                "zone_type": "9",
                "zone_label": "海河9分区",
                "records": rows,
                "summary": _summarize_rows(rows),
            }
        except Exception as exc:
            return _no_data_payload(month_label, time_range, readable, reason=str(exc)[:120])
