from __future__ import annotations

import argparse
import configparser
import io
import json
import os
import re
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

from constants import DEFAULT_BASIN_CODES

from emergency_response_interface import (
    DEFAULT_HAIHE_BASIN_CODES,
    DEFAULT_EC_OUTPUT_PATH,
    query_haihe_emergency_forecast,
    query_haihe_emergency_observation,
    resolve_emergency_basin_codes,
)
from emergency_event_store import EmergencyEventStore
from emergency_management_store import EmergencyManagementStore
from rainfall_ranking_service import station_rainfall_ranking
from tools import estimate_river_impact_time_core
from scenario_response_builder import (
    TableColumn,
    ScenarioTable,
    build_feature,
    build_response_payload,
    feature_collection,
)
from gis_wms_sql_registry import (
    build_admin_boundaries_by_names_wms_sql,
    build_partition_stats_wms_sql_for_rows,
    build_region_rivers_scene_wms_sql,
    build_river_names_geojson_wms_sql,
    build_zone256_rivers_wms_sql,
    register_wms_sql_text,
    load_pg_for_registry,
)

try:
    from haihe_mcp_tools import BusinessException, MusicApiError
except Exception:

    class BusinessException(Exception):
        pass

    class MusicApiError(Exception):
        pass

from constants import DEFAULT_OBS_ELEMENTS
from haihe_mcp_tools import collect_ec_forecast_precip_files
from haihe_mcp_tools import (
    MusicClient,
    deduplicate_latest_records,
    filter_records_by_station_levels,
    normalize_station_level,
    safe_float,
    station_id_of,
)
from forecast_product_queue import (
    enqueue_forecast_product_job,
    forecast_product_queue_status,
    get_forecast_product_job,
    job_to_dict,
    read_manifest,
    resolve_png_path,
    products_root,
    set_forecast_product_queue_paused,
)
from observation_product_queue import (
    enqueue_observation_product_job,
    enqueue_observation_product_jobs_range,
    get_observation_product_job,
    obs_job_to_dict,
    observation_product_queue_status,
    read_observation_manifest,
    resolve_observation_png_path,
    resolve_observation_shapefile_path,
    set_observation_product_queue_paused,
    times_compact_from_times,
)

EVENTS_FILE_PATH = os.getenv("EMERGENCY_EVENTS_FILE", os.path.join(os.path.dirname(__file__), "emergency_events.json"))
_EVENTS_LOCK = threading.Lock()
_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.ini")
_EVENT_STORE = EmergencyEventStore(_CONFIG_PATH)
_MANAGEMENT_STORE = EmergencyManagementStore(_CONFIG_PATH)
MONITOR_OBS_ELEMENTS = ",".join(
    sorted(
        {
            *[x.strip() for x in DEFAULT_OBS_ELEMENTS.split(",") if x.strip()],
            "TEM",
            "RHU",
            "WIN_S_Avg_2mi",
            "PRS",
        }
    )
)
_DEFAULT_LOCAL_SCENARIO_JSON = os.getenv(
    "LOCAL_SCENARIO_JSON",
    r"C:\Users\gaozr\Desktop\fsdownload\merged_6h_all_ranges.json",
)
_DEFAULT_LOCAL_ZONE256_JSON = os.getenv(
    "LOCAL_ZONE256_JSON",
    r"D:\tj\export\zone246_rivers_wms_sql.json",
)
_SERVER_STARTED_AT = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
_SESSION_BASELINE_LOCK = threading.Lock()
_SESSION_BASELINES: Dict[str, Tuple[str, datetime]] = {}
_SESSION_BASELINE_TTL_SECONDS = max(60, int(os.getenv("EMERGENCY_RESPONSE_BOARD_SESSION_TTL_SECONDS", "86400")))
_SESSION_BASELINE_MAX_ENTRIES = max(100, int(os.getenv("EMERGENCY_RESPONSE_BOARD_SESSION_MAX_ENTRIES", "5000")))


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _server_version_info() -> Dict[str, Any]:
    """用于确认当前进程是否加载了最新代码（只读信息，不影响业务逻辑）。"""
    try:
        src = os.path.abspath(__file__)
        mtime = datetime.fromtimestamp(os.path.getmtime(src)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        src = os.path.abspath(__file__)
        mtime = None
    return {
        "started_at": _SERVER_STARTED_AT,
        "source_file": src,
        "source_mtime": mtime,
        "pid": os.getpid(),
    }


def _round2(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except Exception:
        return None


def _format_time_compact(times: str) -> str:
    txt = str(times or "").strip()
    if len(txt) == 14 and txt.isdigit():
        return datetime.strptime(txt, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    if len(txt) == 10 and txt.isdigit():
        return datetime.strptime(txt, "%Y%m%d%H").strftime("%Y-%m-%d %H:00:00")
    return txt


def _load_pg_config(config_path: str) -> Dict[str, Any]:
    cp = configparser.ConfigParser()
    cp.read(config_path, encoding="utf-8")
    if not cp.has_section("postgres"):
        raise ValueError("config.ini 缺少 [postgres] 配置")
    pg = cp["postgres"]
    return {
        "host": pg.get("host", "127.0.0.1"),
        "port": pg.getint("port", 5432),
        "dbname": pg.get("dbname", "postgres"),
        "user": pg.get("user", "postgres"),
        "password": pg.get("password", ""),
        "sslmode": pg.get("sslmode", "prefer"),
        "connect_timeout": pg.getint("connect_timeout", 5),
        "schema": pg.get("schema", "public").strip() or "public",
        "river_table": (
            pg.get("river_table_full", "").strip()
            or pg.get("river_table", "haihe_river_directed_full_v2").strip()
            or "haihe_river_directed_full_v2"
        ),
        "admin_table": pg.get("admin_table", "haihe_admin_division").strip() or "haihe_admin_division",
        "zone256_table": (pg.get("zone256_table") or "haihe_246_zone").strip() or "haihe_246_zone",
    }


def _query_region_rivers_scene(region_name: str, max_rivers: int = 200) -> Dict[str, Any]:
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except Exception as exc:
        raise ValueError(f"缺少 psycopg2 依赖，无法查询分区河流: {exc}") from exc
    pg = _load_pg_config(_CONFIG_PATH)
    sql_boundary = f"""
        SELECT
          id,
          adcode,
          name,
          city_name,
          county_name,
          ST_AsGeoJSON(geom) AS geom_json
        FROM {pg["schema"]}.{pg["admin_table"]}
        WHERE
          COALESCE(adcode::text,'') = %(name)s
          OR COALESCE(name,'') ILIKE %(name)s
          OR COALESCE(city_name,'') ILIKE %(name)s
          OR COALESCE(county_name,'') ILIKE %(name)s
          OR COALESCE(name,'') ILIKE ('%%' || %(name)s || '%%')
          OR COALESCE(city_name,'') ILIKE ('%%' || %(name)s || '%%')
          OR COALESCE(county_name,'') ILIKE ('%%' || %(name)s || '%%')
        ORDER BY id
        LIMIT 50
    """
    sql_rivers = f"""
        WITH region_match AS (
            SELECT ST_UnaryUnion(ST_Collect(geom)) AS geom
            FROM {pg["schema"]}.{pg["admin_table"]}
            WHERE
              COALESCE(adcode::text,'') = %(name)s
              OR COALESCE(name,'') ILIKE %(name)s
              OR COALESCE(city_name,'') ILIKE %(name)s
              OR COALESCE(county_name,'') ILIKE %(name)s
              OR COALESCE(name,'') ILIKE ('%%' || %(name)s || '%%')
              OR COALESCE(city_name,'') ILIKE ('%%' || %(name)s || '%%')
              OR COALESCE(county_name,'') ILIKE ('%%' || %(name)s || '%%')
        )
        SELECT
          COALESCE(NULLIF(TRIM(r.river_name), ''), NULLIF(TRIM(r.src_name), '')) AS river_name,
          SUM(
            COALESCE(
              CASE WHEN to_jsonb(r) ? 'length_km' THEN (to_jsonb(r)->>'length_km')::double precision END,
              CASE WHEN to_jsonb(r) ? 'length_val' THEN (to_jsonb(r)->>'length_val')::double precision END,
              CASE WHEN to_jsonb(r) ? 'len_km' THEN (to_jsonb(r)->>'len_km')::double precision END,
              0
            )
          ) AS river_length_km,
          ST_AsGeoJSON(ST_LineMerge(ST_UnaryUnion(ST_Collect(r.geom)))) AS geom_json
        FROM {pg["schema"]}.{pg["river_table"]} r
        JOIN region_match rm ON ST_Intersects(r.geom, rm.geom)
        GROUP BY COALESCE(NULLIF(TRIM(r.river_name), ''), NULLIF(TRIM(r.src_name), ''))
        ORDER BY river_length_km DESC
        LIMIT %(max_rivers)s
    """
    conn = psycopg2.connect(
        host=pg["host"],
        port=pg["port"],
        dbname=pg["dbname"],
        user=pg["user"],
        password=pg["password"],
        sslmode=pg["sslmode"],
        connect_timeout=pg["connect_timeout"],
    )
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql_boundary, {"name": region_name})
                boundaries = cur.fetchall() or []
                cur.execute(sql_rivers, {"name": region_name, "max_rivers": max_rivers})
                rivers = cur.fetchall() or []
    finally:
        conn.close()
    return {"boundaries": boundaries, "rivers": rivers}


def _query_partition_stats_from_ranking(ranking_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    将站点降水明细按 9/11 分区做空间聚合，输出分区统计和分区面几何。
    """
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor, Json
    except Exception:
        return []
    rows = []
    for r in ranking_list or []:
        lon = _nullable_float(r.get("lon"))
        lat = _nullable_float(r.get("lat"))
        if lon is None or lat is None:
            continue
        rows.append(
            {
                "station_id": str(r.get("station_id") or ""),
                "lon": lon,
                "lat": lat,
                "rainfall_mm": float(r.get("rainfall_mm") or 0.0),
            }
        )
    if not rows:
        return []
    pg = _load_pg_config(_CONFIG_PATH)
    sql = f"""
        WITH s AS (
            SELECT *
            FROM json_to_recordset(%s::json) AS x(
                station_id text,
                lon double precision,
                lat double precision,
                rainfall_mm double precision
            )
        ),
        z AS (
            SELECT
              'haihe_zone_9'::text AS partition_layer,
              gid,
              zone_code::text AS zone_code,
              zone_name::text AS zone_name,
              geom
            FROM {pg["schema"]}.haihe_zone_9
            UNION ALL
            SELECT
              'haihe_zone_11'::text AS partition_layer,
              gid,
              zone_code::text AS zone_code,
              zone_name::text AS zone_name,
              geom
            FROM {pg["schema"]}.haihe_zone_11
        )
        SELECT
            z.partition_layer,
            z.gid AS partition_id,
            COALESCE(z.zone_code::text, z.gid::text) AS partition_code,
            COALESCE(z.zone_name, z.gid::text) AS partition_name,
            SUM(s.rainfall_mm) AS station_precip_sum_mm,
            MAX(s.rainfall_mm) AS station_precip_max_mm,
            COUNT(*) AS station_count,
            ST_AsGeoJSON(z.geom) AS geom_json
        FROM z
        JOIN s
          ON ST_Intersects(z.geom, ST_SetSRID(ST_Point(s.lon, s.lat), 4326))
        GROUP BY z.partition_layer, z.gid, z.zone_code, z.zone_name, z.geom
        ORDER BY z.partition_layer, station_precip_sum_mm DESC
    """
    conn = psycopg2.connect(
        host=pg["host"],
        port=pg["port"],
        dbname=pg["dbname"],
        user=pg["user"],
        password=pg["password"],
        sslmode=pg["sslmode"],
        connect_timeout=pg["connect_timeout"],
    )
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, (Json(rows),))
                return cur.fetchall() or []
    except Exception:
        return []
    finally:
        conn.close()


def _query_zone256_rivers_from_ranking(
    ranking_list: List[Dict[str, Any]],
    *,
    max_rivers: int = 800,
    station_buffer_km: float = 30.0,
) -> List[Dict[str, Any]]:
    """
    降雨站点落区所覆盖的 246 分区并集与河系求交，返回河线 GeoJSON 与河长（与 WMS SQL 一致的空间逻辑）。
    默认以站点为中心 30 km 缓冲区求交，避免仅因站点未精确落在河道上就漏掉受影响河系。
    """
    try:
        import psycopg2
        from psycopg2.extras import Json, RealDictCursor
    except Exception:
        return []
    rows: List[Dict[str, Any]] = []
    for r in ranking_list or []:
        lon = _nullable_float(r.get("lon"))
        lat = _nullable_float(r.get("lat"))
        if lon is None or lat is None:
            continue
        rows.append(
            {
                "station_id": str(r.get("station_id") or ""),
                "lon": lon,
                "lat": lat,
                "rainfall_mm": float(r.get("rainfall_mm") or 0.0),
            }
        )
    if not rows:
        return []
    pg = _load_pg_config(_CONFIG_PATH)
    zt = str(pg.get("zone256_table") or "haihe_246_zone").strip()
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", zt):
        return []
    rt = str(pg.get("river_table") or "haihe_river_directed_full_v2").strip()
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", rt):
        return []
    lim = max(1, min(int(max_rivers), 5000))
    buf_m = max(0.0, float(station_buffer_km)) * 1000.0
    schema = pg["schema"]
    sql = f"""
        WITH s AS (
            SELECT *
            FROM json_to_recordset(%(rows)s::json) AS x(
                station_id text,
                lon double precision,
                lat double precision,
                rainfall_mm double precision
            )
        ),
        buffered AS (
            SELECT
                station_id,
                ST_Transform(
                    ST_Buffer(
                        ST_Transform(ST_SetSRID(ST_Point(lon, lat), 4326), 3857),
                        %(buf_m)s
                    ),
                    4326
                ) AS geom
            FROM s
        ),
        union_buffer AS (
            SELECT ST_UnaryUnion(ST_Collect(geom)) AS g FROM buffered
        ),
        rivers AS (
            SELECT
                COALESCE(NULLIF(TRIM(r.river_name), ''), NULLIF(TRIM(r.src_name), '')) AS river_name,
                ST_LineMerge(ST_UnaryUnion(ST_Collect(r.geom))) AS geom_union,
                SUM(
                    COALESCE(
                        CASE WHEN to_jsonb(r) ? 'length_km' THEN (to_jsonb(r)->>'length_km')::double precision END,
                        CASE WHEN to_jsonb(r) ? 'length_val' THEN (to_jsonb(r)->>'length_val')::double precision END,
                        CASE WHEN to_jsonb(r) ? 'len_km' THEN (to_jsonb(r)->>'len_km')::double precision END,
                        0
                    )
                ) AS river_length_km
            FROM {schema}.{rt} r
            CROSS JOIN union_buffer u
            WHERE u.g IS NOT NULL AND ST_Intersects(r.geom, u.g)
            GROUP BY 1
            ORDER BY river_length_km DESC NULLS LAST
            LIMIT %(lim)s
        )
        SELECT river_name, ST_AsGeoJSON(geom_union) AS geom_json, river_length_km
        FROM rivers
        WHERE geom_union IS NOT NULL
    """
    conn = psycopg2.connect(
        host=pg["host"],
        port=pg["port"],
        dbname=pg["dbname"],
        user=pg["user"],
        password=pg["password"],
        sslmode=pg["sslmode"],
        connect_timeout=pg["connect_timeout"],
    )
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, {"rows": Json(rows), "buf_m": buf_m, "lim": lim})
                return cur.fetchall() or []
    except Exception:
        return []
    finally:
        conn.close()


def _query_admin_boundaries_by_region_names(region_names: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    按“表格中的行政区名称”精确取行政区边界，避免模糊匹配下钻到街道级。
    返回 key=region_name（入参原文），value 包含 adcode/name/geom。
    """
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except Exception:
        return {}
    cleaned = [str(x).strip() for x in region_names if str(x).strip()]
    if not cleaned:
        return {}
    pg = _load_pg_config(_CONFIG_PATH)
    # 固定使用备份行政区表，避免旧表异常影响导出质量
    admin_table = "haihe_admin_division_bak"

    sql_tpl = """
        WITH q AS (
            SELECT DISTINCT NULLIF(TRIM(unnest(%s::text[])), '') AS region_name
        ),
        m AS (
            SELECT
                q.region_name AS query_name,
                a.adcode,
                a.name,
                a.level,
                a.geom,
                CASE
                    WHEN COALESCE(a.name,'') = q.region_name THEN 1
                    WHEN REPLACE(COALESCE(a.name,''), '市', '') = REPLACE(q.region_name, '市', '') THEN 2
                    WHEN REPLACE(COALESCE(a.name,''), '区', '') = REPLACE(q.region_name, '区', '') THEN 3
                    ELSE 99
                END AS hit_rank
            FROM q
            JOIN {schema}.{table_name} a
              ON q.region_name IS NOT NULL
             AND (
                COALESCE(a.name,'') = q.region_name
                OR REPLACE(COALESCE(a.name,''), '市', '') = REPLACE(q.region_name, '市', '')
                OR REPLACE(COALESCE(a.name,''), '区', '') = REPLACE(q.region_name, '区', '')
             )
            WHERE
              -- 行政区划最细到区县；排除乡镇/街道级
              NOT (
                LOWER(COALESCE(a.level, '')) LIKE 'town%%'
                OR COALESCE(a.level, '') IN ('town', 'street', '乡镇', '街道')
              )
        ),
        picked AS (
            SELECT DISTINCT ON (query_name)
                query_name, adcode, name, level, geom
            FROM m
            ORDER BY
              query_name,
              hit_rank,
              CASE
                WHEN LOWER(COALESCE(level,'')) IN ('district', 'county') OR COALESCE(level,'') IN ('区', '区县') THEN 1
                WHEN LOWER(COALESCE(level,'')) = 'city' OR COALESCE(level,'') IN ('市') THEN 2
                ELSE 9
              END,
              adcode
        )
        SELECT
            query_name,
            adcode,
            name,
            level,
            ST_AsGeoJSON(geom) AS geom_json
        FROM picked
        ORDER BY query_name
    """
    conn = psycopg2.connect(
        host=pg["host"],
        port=pg["port"],
        dbname=pg["dbname"],
        user=pg["user"],
        password=pg["password"],
        sslmode=pg["sslmode"],
        connect_timeout=pg["connect_timeout"],
    )
    out: Dict[str, Dict[str, Any]] = {}
    try:
        rows = []
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                sql = sql_tpl.format(schema=pg["schema"], table_name=admin_table)
                cur.execute(sql, (cleaned,))
                rows = cur.fetchall() or []
        for row in rows:
            qn = str(row.get("query_name") or "").strip()
            if not qn:
                continue
            geom = None
            gtxt = row.get("geom_json")
            if gtxt:
                try:
                    geom = json.loads(gtxt)
                except Exception:
                    geom = None
            region_name = str(row.get("name") or "").strip() or qn
            out[qn] = {
                "adcode": row.get("adcode"),
                "name": region_name,
                "level": row.get("level"),
                "geometry": geom,
            }
    finally:
        conn.close()
    return out


def _fetch_station_obs_records(times: str, basin_codes: str, allowed_station_levels: str) -> List[Dict[str, Any]]:
    levels = [x.strip() for x in str(allowed_station_levels or "").split(",") if x.strip()]
    client = MusicClient()
    records = client.get_surf_ele_in_basin_by_time(
        basin_codes=basin_codes,
        times=times,
        elements=MONITOR_OBS_ELEMENTS,
    )
    records = filter_records_by_station_levels(records, levels if levels else None)
    records = deduplicate_latest_records(records)
    return records


def _build_rainfall_timeline_list(params: Dict[str, Any]) -> Dict[str, Any]:
    times = str(_as_scalar(params.get("times")) or "").strip()
    if not times:
        raise ValueError("times 必填，例如 20250723080000")

    _scope, basin_codes = _resolve_scope_and_basin(params)
    allowed_station_levels = (
        str(_as_scalar(params.get("allowed_station_levels")))
        if params.get("allowed_station_levels") is not None
        else "11,12,13,16"
    )

    hours_raw = str(_as_scalar(params.get("accum_hours")) or "").strip()
    if hours_raw:
        parsed_hours = [int(x.strip()) for x in hours_raw.split(",") if x.strip().isdigit()]
    else:
        parsed_hours = [6, 12, 24]
    hours_list = sorted({h for h in parsed_hours if h > 0})
    if not hours_list:
        raise ValueError("accum_hours 无效，请使用逗号分隔正整数，例如 6,12,24")

    min_mm_raw = _as_scalar(params.get("min_mm"))
    min_mm = float(min_mm_raw) if min_mm_raw not in (None, "") else 0.0
    per_hours_limit = max(1, min(_parse_int(params.get("per_hours_limit"), 500), 2000))

    try:
        records = _fetch_station_obs_records(
            times=times,
            basin_codes=basin_codes,
            allowed_station_levels=allowed_station_levels,
        )
    except MusicApiError as exc:
        msg = str(exc)
        lower_msg = msg.lower()
        # 内网接口在无数据时会返回 "query success, but no record in database"
        # 此场景对前端联调应返回空数组而不是 502。
        if ("no record" in lower_msg) or ("rowcount" in lower_msg and '"0"' in lower_msg):
            records = []
        else:
            raise

    timeline_list: List[Dict[str, Any]] = []
    for h in hours_list:
        field = f"PRE_{h}h"
        rows: List[Dict[str, Any]] = []
        for rec in records:
            sid = station_id_of(rec)
            if not sid:
                continue
            mm = safe_float(rec.get(field))
            if mm < min_mm:
                continue
            rows.append(
                {
                    "station_id": sid,
                    "station_name": (
                        (rec.get("Station_Name") or rec.get("Station_Name_C") or "").strip() or sid
                    ),
                    "rainfall_mm": round(mm, 2),
                    "station_level": normalize_station_level(rec.get("Station_levl")),
                    "lat": _round2(safe_float(rec.get("Lat"))),
                    "lon": _round2(safe_float(rec.get("Lon"))),
                    "city": rec.get("City"),
                    "cnty": rec.get("Cnty"),
                }
            )
        rows.sort(key=lambda x: float(x.get("rainfall_mm") or 0.0), reverse=True)
        if len(rows) > per_hours_limit:
            rows = rows[:per_hours_limit]

        total_mm = round(sum(float(x.get("rainfall_mm") or 0.0) for x in rows), 2)
        timeline_list.append(
            {
                "accum_hours": h,
                "field": field,
                "count": len(rows),
                "total_mm": total_mm,
                "max_mm": rows[0]["rainfall_mm"] if rows else 0.0,
                "list": rows,
            }
        )

    return {
        "times": times,
        "basin_codes": basin_codes,
        "min_mm": min_mm,
        "per_hours_limit": per_hours_limit,
        "accum_hours": hours_list,
        "list": timeline_list,
    }


def _load_local_rainfall_timeline_payload(times: str, local_json_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    path = str(local_json_path or "").strip()
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return None

    if isinstance(doc, dict):
        if str(doc.get("times") or "").strip() == str(times).strip() and isinstance(doc.get("list"), list):
            return doc
        slots = doc.get("slots")
        if isinstance(slots, list):
            for slot in slots:
                if not isinstance(slot, dict):
                    continue
                if str(slot.get("times") or "").strip() != str(times).strip():
                    continue
                candidate = slot.get("rainfall_timeline")
                if isinstance(candidate, dict) and isinstance(candidate.get("list"), list):
                    if not candidate.get("times"):
                        candidate["times"] = str(times).strip()
                    return candidate
    return None


def _nullable_float(value: Any) -> Optional[float]:
    if value in (None, "", "None"):
        return None
    text = str(value).strip()
    if text in {"999999", "999999.0", "999990", "999990.0", "-9999", "-9999.0"}:
        return None
    try:
        return float(text)
    except Exception:
        return None


def _estimate_avg_propagation_human(distance_km: Any) -> Optional[str]:
    """缺少传播时间文案时，按平均流速 5.84 km/h 进行兜底估算。"""
    d = _nullable_float(distance_km)
    if d is None:
        return None
    d = max(0.0, float(d))
    if d <= 0.0:
        return "0 小时"
    return f"{round(d / 5.84, 2)} 小时"


def _load_local_merged_slot(times: str, local_json_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    def _norm_times_candidates(value: Any) -> List[str]:
        digits = "".join(c for c in str(value or "") if c.isdigit())
        if not digits:
            return []
        cands = {digits}
        if len(digits) >= 10:
            cands.add(digits[:10])
        if len(digits) >= 12:
            cands.add(digits[:12])
        if len(digits) >= 14:
            cands.add(digits[:14])
        return [x for x in cands if x]

    path = str(local_json_path or _DEFAULT_LOCAL_SCENARIO_JSON).strip()
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return None
    slots = doc.get("slots")
    if not isinstance(slots, list):
        return None
    target_times = set(_norm_times_candidates(times))
    for slot in slots:
        slot_times = set(_norm_times_candidates(slot.get("times")))
        if target_times and slot_times and target_times.intersection(slot_times):
            if not isinstance(slot, dict):
                return None
            # 兼容 station_rain_api 导出的 slots[*].payload 结构
            if isinstance(slot.get("station_ranking"), dict):
                return slot
            payload = slot.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("list"), list):
                return {
                    "times": str(slot.get("times") or times).strip(),
                    "station_ranking": payload,
                }
            return slot
    return None


def _load_latest_local_merged_slot(local_json_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    当请求 times 与本地 mock 槽位不一致时，回退到最新一个可用槽位。
    """
    path = str(local_json_path or _DEFAULT_LOCAL_SCENARIO_JSON).strip()
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return None
    slots = doc.get("slots")
    if not isinstance(slots, list):
        return None

    def _slot_score(slot: Dict[str, Any]) -> int:
        ts = "".join(c for c in str(slot.get("times") or "") if c.isdigit())
        try:
            return int(ts)
        except Exception:
            return -1

    candidates: List[Dict[str, Any]] = []
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        if isinstance(slot.get("station_ranking"), dict):
            candidates.append(slot)
            continue
        payload = slot.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("list"), list):
            candidates.append(
                {
                    "times": str(slot.get("times") or "").strip(),
                    "station_ranking": payload,
                }
            )
    if not candidates:
        return None
    candidates.sort(key=_slot_score, reverse=True)
    return candidates[0]


def _load_local_emergency_rivers_payload(times: str) -> Optional[Dict[str, Any]]:
    """
    本地联调兜底：从 scene_exports/emergency_rivers.json 读取一条可用 payload。
    优先按 times 命中槽位；未命中则回退第一个可用槽位。
    返回格式：{"success": True, "message": "ok", "data": {...}}
    """
    base_dir = os.getenv("GIS_SCENE_EXPORT_DIR", r"C:\Users\gaozr\Desktop\fsdownload\scene_exports")
    path = os.path.join(base_dir, "emergency_rivers.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            root = json.load(f)
    except Exception:
        return None
    slots = root.get("slots")
    if not isinstance(slots, list) or not slots:
        return None

    target = _normalize_times_compact(times)
    chosen: Optional[Dict[str, Any]] = None
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        st = _normalize_times_compact(slot.get("times") or slot.get("slot_times") or slot.get("time_compact"))
        if target and st and st == target:
            chosen = slot
            break
    if chosen is None:
        chosen = next((s for s in slots if isinstance(s, dict)), None)
    if not isinstance(chosen, dict):
        return None

    er = chosen.get("emergency_rivers") if isinstance(chosen.get("emergency_rivers"), dict) else {}
    data = er.get("data")
    if isinstance(data, dict):
        return {"success": True, "message": "ok", "data": data}
    items = er.get("items") if isinstance(er.get("items"), list) else []
    merged_features: List[Dict[str, Any]] = []
    tables_by_key: Dict[str, Dict[str, Any]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        payload = it.get("payload") if isinstance(it.get("payload"), dict) else {}
        one = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(one, dict):
            continue
        mg = one.get("map_geojson")
        if isinstance(mg, str):
            try:
                mg = json.loads(mg)
            except Exception:
                mg = None
        if isinstance(mg, dict) and isinstance(mg.get("features"), list):
            merged_features.extend([f for f in mg.get("features", []) if isinstance(f, dict)])
        for t in (one.get("tables") if isinstance(one.get("tables"), list) else []):
            if not isinstance(t, dict):
                continue
            tk = str(t.get("table_key") or t.get("id") or "").strip() or "table"
            if tk not in tables_by_key:
                tables_by_key[tk] = {
                    "table_key": tk,
                    "table_name": t.get("table_name") or t.get("title") or tk,
                    "columns": t.get("columns") if isinstance(t.get("columns"), list) else [],
                    "rows": [],
                }
            rows = t.get("rows") if isinstance(t.get("rows"), list) else []
            tables_by_key[tk]["rows"].extend([r for r in rows if isinstance(r, dict)])
    if tables_by_key or merged_features:
        data_out = {
            "scenario": "emergency_rivers",
            "map_geojson": json.dumps(feature_collection(merged_features), ensure_ascii=False),
            "tables": list(tables_by_key.values()),
            "query_time": _now_iso(),
        }
        return {"success": True, "message": "ok", "data": data_out}
    return None


def _to_rows_from_local_tables(tables: List[Dict[str, Any]], key_names: List[str]) -> List[Dict[str, Any]]:
    for t in tables:
        if not isinstance(t, dict):
            continue
        tk = str(t.get("table_key") or t.get("id") or "").strip()
        if tk in key_names:
            rows = t.get("rows")
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)]
    return []


def _load_local_zone256_payload(times: str, local_json_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    path = str(local_json_path or _DEFAULT_LOCAL_ZONE256_JSON).strip()
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return None
    slots = doc.get("slots") if isinstance(doc, dict) else None
    if not isinstance(slots, list):
        return None
    target = "".join(c for c in str(times or "") if c.isdigit())
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        slot_times = "".join(c for c in str(slot.get("times") or "") if c.isdigit())
        if target and slot_times and slot_times != target:
            continue
        payload = slot.get("payload")
        if isinstance(payload, dict) and payload.get("success") is True and isinstance(payload.get("data"), dict):
            return payload
    return None


def _build_station_scene_from_local_slot(times: str, slot: Dict[str, Any]) -> Dict[str, Any]:
    ranking = slot.get("station_ranking") or {}
    station_list = ranking.get("list") if isinstance(ranking, dict) else []
    rows: List[Dict[str, Any]] = []
    features: List[Dict[str, Any]] = []
    for row in station_list or []:
        sid = str(row.get("station_id") or "").strip() or str(row.get("station") or "").strip()
        if not sid:
            continue
        lon = safe_float(row.get("lon"))
        lat = safe_float(row.get("lat"))
        if abs(lon) < 0.01 or abs(lat) < 0.01:
            continue
        fid = f"station_{sid}"
        one = {
            "feature_id": fid,
            "station_id": sid,
            "station_name": row.get("station"),
            "temperature": row.get("temperature"),
            "humidity": row.get("humidity"),
            "wind_speed": row.get("wind_speed"),
            "pressure": row.get("pressure"),
            "rainfall_mm": row.get("rainfall_mm"),
            "station_level": row.get("station_level"),
        }
        rows.append(one)
        features.append(
            build_feature(
                feature_id=fid,
                geometry={"type": "Point", "coordinates": [lon, lat]},
                style_type="national_station",
                props=one,
            )
        )
    payload = build_response_payload(
        scenario="monitor_stations",
        geojson_obj=feature_collection(features),
        tables=[
            ScenarioTable(
                table_key="station_obs",
                table_name="站点观测",
                columns=[
                    TableColumn("station_name", "站点名称"),
                    TableColumn("temperature", "温度"),
                    TableColumn("humidity", "湿度"),
                    TableColumn("wind_speed", "风速"),
                    TableColumn("pressure", "气压"),
                    TableColumn("rainfall_mm", "降水量(mm)"),
                    TableColumn("feature_id", "定位ID"),
                ],
                rows=rows,
            )
        ],
        query_time=_now_iso(),
    )
    payload["data"]["meta"]["data_source"] = "local_json"
    payload["data"]["meta"]["times"] = times
    return payload


def _query_river_geometries_by_names(river_names: List[str]) -> Dict[str, Dict[str, Any]]:
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except Exception as exc:
        raise ValueError(f"缺少 psycopg2 依赖，无法查询河流几何: {exc}") from exc
    cleaned = [str(x).strip() for x in river_names if str(x).strip()]
    if not cleaned:
        return {}
    pg = _load_pg_config(_CONFIG_PATH)
    sql = f"""
        SELECT
          river_name_key,
          river_name,
          river_length_km,
          ST_AsGeoJSON(geom_union) AS geom_json
        FROM (
          SELECT
            COALESCE(NULLIF(TRIM(r.river_name), ''), NULLIF(TRIM(r.src_name), '')) AS river_name_key,
            MIN(COALESCE(NULLIF(TRIM(r.river_name), ''), NULLIF(TRIM(r.src_name), ''))) AS river_name,
            SUM(
              COALESCE(
                CASE WHEN to_jsonb(r) ? 'length_km' THEN (to_jsonb(r)->>'length_km')::double precision END,
                CASE WHEN to_jsonb(r) ? 'length_val' THEN (to_jsonb(r)->>'length_val')::double precision END,
                CASE WHEN to_jsonb(r) ? 'len_km' THEN (to_jsonb(r)->>'len_km')::double precision END,
                0
              )
            ) AS river_length_km,
            ST_LineMerge(ST_UnaryUnion(ST_Collect(r.geom))) AS geom_union
          FROM {pg["schema"]}.{pg["river_table"]} r
          WHERE COALESCE(NULLIF(TRIM(r.river_name), ''), NULLIF(TRIM(r.src_name), '')) = ANY(%s)
          GROUP BY COALESCE(NULLIF(TRIM(r.river_name), ''), NULLIF(TRIM(r.src_name), ''))
        ) t
    """
    conn = psycopg2.connect(
        host=pg["host"],
        port=pg["port"],
        dbname=pg["dbname"],
        user=pg["user"],
        password=pg["password"],
        sslmode=pg["sslmode"],
        connect_timeout=pg["connect_timeout"],
    )
    out: Dict[str, Dict[str, Any]] = {}
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, (cleaned,))
                rows = cur.fetchall() or []
        for row in rows:
            key = str(row.get("river_name_key") or "").strip()
            if not key:
                continue
            geom = None
            gtxt = row.get("geom_json")
            if gtxt:
                try:
                    geom = json.loads(gtxt)
                except Exception:
                    geom = None
            out[key] = {
                "river_name": row.get("river_name"),
                "river_length_km": round(float(row.get("river_length_km") or 0.0), 2),
                "geometry": geom,
            }
    finally:
        conn.close()
    return out


def _to_iso_time(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y-%m-%d %H",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d %H",
        # 必须优先 10 位 YYYYMMDDHH，避免 2023073000 被 14 位格式误吞为错误日期。
        "%Y%m%d%H",
        "%Y%m%d%H%M%S",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def _to_hour_iso_time(value: Optional[str]) -> Optional[str]:
    iso = _to_iso_time(value)
    if not iso:
        return None
    try:
        dt = datetime.strptime(iso, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return dt.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _parse_int(value: Any, default: int) -> int:
    try:
        if isinstance(value, list):
            value = value[0]
        return int(value)
    except Exception:
        return default


def _parse_bool(value: Any, default: bool = False) -> bool:
    scalar = _as_scalar(value)
    if scalar is None:
        return default
    if isinstance(scalar, bool):
        return scalar
    txt = str(scalar).strip().lower()
    if txt in {"1", "true", "yes", "y", "on"}:
        return True
    if txt in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_float(value: Any, default: float) -> float:
    try:
        if isinstance(value, list):
            value = value[0]
        return float(value)
    except Exception:
        return default


def _as_scalar(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _normalize_times_compact(value: Any) -> str:
    """
    归一化业务时次参数为 14 位紧凑串（YYYYMMDDHHMMSS）。
    兼容前端偶发传参形态：\"['20260508090000']\"、\"2026-05-08 09:00:00\" 等。
    """
    raw = _as_scalar(value)
    if raw is None:
        return ""
    txt = str(raw).strip()
    if not txt:
        return ""
    m14 = re.search(r"(\d{14})", txt)
    if m14:
        return m14.group(1)
    digits = "".join(ch for ch in txt if ch.isdigit())
    if len(digits) >= 14:
        return digits[:14]
    if len(digits) == 12:
        return digits + "00"
    return txt


def _wants_wms_sql(params: Dict[str, Any]) -> bool:
    """map_render / map_mode = wms_sql|wms|sql_id 时只登记 SQL 模板并返回 map_sql_id，避免直传大体量 GeoJSON。"""
    v = _as_scalar(params.get("map_render") or params.get("map_mode"))
    return str(v or "").strip().lower() in {"wms_sql", "wms", "sql_id"}


def _normalize_level(level: Any) -> Optional[str]:
    level = _as_scalar(level)
    if level is None:
        return None
    text = str(level).strip().upper()
    return text or None


def _normalize_status(reached: Any) -> str:
    return "triggered" if bool(reached) else "not_triggered"


def _resolve_scope_and_basin(params: Dict[str, Any]) -> Tuple[str, str]:
    scope_raw = _as_scalar(params.get("scope"))
    basin_raw = _as_scalar(params.get("basin_codes"))
    scope = str(scope_raw).strip() if scope_raw is not None else "haihe"
    basin = resolve_emergency_basin_codes(
        basin_codes=(str(basin_raw).strip() if basin_raw is not None else None),
        scope=scope,
    )
    return scope, basin


def _load_events() -> List[Dict[str, Any]]:
    if not os.path.isfile(EVENTS_FILE_PATH):
        return []
    try:
        with open(EVENTS_FILE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    except Exception:
        return []
    return []


def _save_events(events: List[Dict[str, Any]]) -> None:
    parent = os.path.dirname(EVENTS_FILE_PATH)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(EVENTS_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)


def _build_product_list(event_type: str, response_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    products: List[Dict[str, Any]] = []
    evidence = response_payload.get("evidence")
    if not isinstance(evidence, dict):
        return products
    if event_type == "forecast":
        ec_files = evidence.get("ec_files")
        if isinstance(ec_files, dict):
            for key in ("6h", "12h", "24h", "48h", "72h"):
                p = ec_files.get(key)
                if p:
                    products.append(
                        {
                            "product_id": f"ec_{key}",
                            "product_type": "forecast_grib",
                            "title": f"EC预报累计降水 {key}",
                            "path": str(p),
                        }
                    )
    return products


def _append_event(
    event_type: str,
    query_time: Optional[str],
    request_params: Dict[str, Any],
    response_payload: Dict[str, Any],
    trace_id: Optional[str] = None,
) -> str:
    req = dict(request_params or {})
    if trace_id:
        req.setdefault("trace_id", trace_id)
    return _EVENT_STORE.append_event(
        event_type=event_type,
        query_time=query_time,
        request_params=req,
        response_payload=response_payload,
    )


def _sync_forecast_result_to_management_timeline(
    *,
    event_id: str,
    start_time: str,
    request_params: Dict[str, Any],
    forecast_result: Dict[str, Any],
    trace_id: Optional[str] = None,
) -> None:
    """
    将预报触发结果同步到 hh_emergency_event（response-board 数据源）。
    - 只同步 reached=true 的结果，避免未触发噪声刷屏。
    - 历史起报时次（如 2023073000）在本地联调会被判为过去；此处转成「当前+1小时」放入未来分组。
    """
    if not bool(forecast_result.get("reached")):
        return
    level = str(forecast_result.get("level") or "IV").strip().upper() or "IV"
    # 管理主表时间轴统一按整点落库，避免出现 22:03 这类非整点时间。
    now_dt = datetime.now().replace(minute=0, second=0, microsecond=0)
    req_start_dt = None
    for fmt in ("%Y%m%d%H", "%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            req_start_dt = datetime.strptime(str(start_time).strip(), fmt)
            break
        except Exception:
            continue
    if req_start_dt and req_start_dt > now_dt:
        timeline_start = req_start_dt
    else:
        timeline_start = now_dt + timedelta(hours=1)

    evidence = forecast_result.get("evidence") if isinstance(forecast_result.get("evidence"), dict) else {}
    window_hours = int(evidence.get("window_hours") or 24)
    timeline_end = timeline_start + timedelta(hours=max(1, min(window_hours, 240)))
    title = f"预报触发应急响应（{level}级）"
    norm_start_time = _to_hour_iso_time(str(start_time)) or str(start_time)
    ext = {
        "source": "forecast_trigger_http",
        "trace_id": trace_id,
        "monitor_event_id": event_id,
        "request_start_time": norm_start_time,
        "effective_start_time": timeline_start.strftime("%Y-%m-%d %H:%M:%S"),
        "effective_end_time": timeline_end.strftime("%Y-%m-%d %H:%M:%S"),
        "request_params": request_params,
        "forecast_response": forecast_result,
    }
    _MANAGEMENT_STORE.upsert_event(
        event_code=f"FORECAST-{event_id}",
        # 前端大多沿用 rainstorm 过滤口径；这里按 rainstorm 入库，避免被 event_type 过滤掉。
        event_type="rainstorm",
        event_level=level,
        title=title,
        status="active",
        start_time=timeline_start.strftime("%Y-%m-%d %H:%M:%S"),
        end_time=timeline_end.strftime("%Y-%m-%d %H:%M:%S"),
        ext=ext,
    )


def _sync_observation_result_to_management_timeline(
    *,
    event_id: str,
    times: str,
    request_params: Dict[str, Any],
    observation_result: Dict[str, Any],
    trace_id: Optional[str] = None,
) -> None:
    """
    将实况触发结果同步到 hh_emergency_event（response-board 数据源）。
    规则：仅同步 reached=true；实况触发提示窗口默认保持 2 小时。
    """
    if not bool(observation_result.get("reached")):
        return
    level = str(observation_result.get("level") or "IV").strip().upper() or "IV"
    # 实况同步同样按整点对齐，保证 hh_emergency_event 口径一致。
    now_dt = datetime.now().replace(minute=0, second=0, microsecond=0)
    obs_dt = None
    for fmt in ("%Y%m%d%H", "%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            obs_dt = datetime.strptime(str(times).strip(), fmt)
            break
        except Exception:
            continue
    if obs_dt and obs_dt <= now_dt:
        timeline_start = obs_dt
    else:
        timeline_start = now_dt
    timeline_end = timeline_start + timedelta(hours=2)
    title = f"实况触发应急响应（{level}级）"
    norm_times = _to_hour_iso_time(str(times)) or str(times)
    ext = {
        "source": "observation_trigger_http",
        "trace_id": trace_id,
        "monitor_event_id": event_id,
        "request_times": norm_times,
        "effective_start_time": timeline_start.strftime("%Y-%m-%d %H:%M:%S"),
        "effective_end_time": timeline_end.strftime("%Y-%m-%d %H:%M:%S"),
        "request_params": request_params,
        "observation_response": observation_result,
    }
    _MANAGEMENT_STORE.upsert_event(
        event_code=f"OBSERVATION-{event_id}",
        event_type="rainstorm",
        event_level=level,
        title=title,
        status="active",
        start_time=timeline_start.strftime("%Y-%m-%d %H:%M:%S"),
        end_time=timeline_end.strftime("%Y-%m-%d %H:%M:%S"),
        ext=ext,
    )


def _list_events(params: Dict[str, Any]) -> Dict[str, Any]:
    trace_id = str(_as_scalar(params.get("trace_id")) or "").strip()
    if trace_id:
        limit = min(max(1, _parse_int(params.get("limit"), 200)), 500)
        rows = _EVENT_STORE.find_events_by_trace_id(trace_id, limit=limit)
        return {
            "trace_id": trace_id,
            "total": len(rows),
            "list": rows,
        }
    page = max(1, _parse_int(params.get("page"), 1))
    page_size = min(max(1, _parse_int(params.get("page_size"), 20)), 200)
    start_time = _to_iso_time(_as_scalar(params.get("start_time")))
    end_time = _to_iso_time(_as_scalar(params.get("end_time")))
    status_raw = _as_scalar(params.get("status"))
    status = str(status_raw).strip().lower() if status_raw is not None else ""
    level = _normalize_level(params.get("level"))
    event_type_raw = _as_scalar(params.get("event_type"))
    event_type = str(event_type_raw).strip().lower() if event_type_raw is not None else ""

    return _EVENT_STORE.list_events(
        page=page,
        page_size=page_size,
        start_time=start_time,
        end_time=end_time,
        status=status,
        level=level,
        event_type=event_type,
    )


def _get_event_detail(event_id: str) -> Optional[Dict[str, Any]]:
    return _EVENT_STORE.get_event_detail(event_id)


def _expand_date_end_for_range(value: Any) -> Optional[str]:
    """
    筛选「结束日期」若只传 YYYY-MM-DD，则扩展到当日 23:59:59，避免漏掉当天后半日起报的事件。
    """
    raw = _as_scalar(value)
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return f"{text} 23:59:59"
    return _to_iso_time(text)


def _list_management_events(params: Dict[str, Any]) -> Dict[str, Any]:
    trace_id = str(_as_scalar(params.get("trace_id")) or "").strip()
    if trace_id:
        limit = min(max(1, _parse_int(params.get("limit"), 200)), 500)
        rows = _MANAGEMENT_STORE.find_events_by_trace_id(trace_id, limit=limit)
        return {
            "trace_id": trace_id,
            "total": len(rows),
            "list": rows,
        }
    page = max(1, _parse_int(params.get("page"), 1))
    page_size = min(max(1, _parse_int(params.get("page_size"), 20)), 200)
    range_start = _to_iso_time(_as_scalar(params.get("start_time")))
    range_end = _expand_date_end_for_range(params.get("end_time"))
    status_raw = _as_scalar(params.get("status"))
    status = str(status_raw).strip().lower() if status_raw is not None else ""
    event_type_raw = _as_scalar(params.get("event_type"))
    event_type = str(event_type_raw).strip().lower() if event_type_raw is not None else ""

    return _MANAGEMENT_STORE.list_events(
        page=page,
        page_size=page_size,
        range_start=range_start,
        range_end=range_end,
        status=status,
        event_type=event_type,
    )


def _list_management_timeline(params: Dict[str, Any]) -> Dict[str, Any]:
    now_time = _to_iso_time(_as_scalar(params.get("now_time")))
    history_hours = min(max(0, _parse_int(params.get("history_hours"), 24)), 24 * 14)
    future_hours = min(max(1, _parse_int(params.get("future_hours"), 48)), 24 * 14)
    event_type_raw = _as_scalar(params.get("event_type"))
    event_type = str(event_type_raw).strip().lower() if event_type_raw is not None else ""
    include_archived = _parse_bool(params.get("include_archived"), True)
    tick_step_hours = min(max(1, _parse_int(params.get("tick_step_hours"), 12)), 48)

    return _MANAGEMENT_STORE.build_timeline(
        now_time=now_time,
        history_hours=history_hours,
        future_hours=future_hours,
        event_type=event_type,
        include_archived=include_archived,
        tick_step_hours=tick_step_hours,
    )


_GS_COV_CACHE: Dict[str, Tuple[float, List[str]]] = {}
_GS_COV_TTL_SEC = 120.0


def _geoserver_rest_auth() -> Tuple[str, str]:
    gs_user = (os.getenv("GEOSERVER_USER") or "admin").strip() or "admin"
    gs_pwd = (os.getenv("GEOSERVER_PASSWORD") or "geoserver").strip() or "geoserver"
    return gs_user, gs_pwd


def _geoserver_list_workspace_coverages(base_url: str, workspace: str) -> List[str]:
    key = f"{base_url.rstrip('/')}\0{workspace}"
    now = time.time()
    hit = _GS_COV_CACHE.get(key)
    if hit and now - hit[0] < _GS_COV_TTL_SEC:
        return hit[1]
    url = f"{base_url.rstrip('/')}/rest/workspaces/{workspace}/coverages.json"
    try:
        resp = requests.get(url, auth=_geoserver_rest_auth(), timeout=5)
        if resp.status_code != 200:
            _GS_COV_CACHE[key] = (now, [])
            return []
        payload = resp.json() if resp.content else {}
        items = ((payload or {}).get("coverages") or {}).get("coverage") or []
        names = [
            str(one.get("name")).strip()
            for one in items
            if isinstance(one, dict) and str(one.get("name") or "").strip()
        ]
        _GS_COV_CACHE[key] = (now, names)
        return names
    except Exception:
        _GS_COV_CACHE[key] = (now, [])
        return []


def _geoserver_resolve_configured_layer_id(configured: str, base_url: str) -> str:
    """
    与 _load_geoserver_catalog 中 WMS 一致：workspace:basename 若可匹配时间后缀 coverage 则取最新一条。
    """
    raw = str(configured or "").strip()
    if not raw:
        return raw
    m = re.match(r"^([^:]+):(.+)$", raw)
    if not m:
        return raw
    workspace, layer_base = m.group(1), m.group(2)
    if re.search(r"_20\d{8,14}$", layer_base):
        return raw
    names = _geoserver_list_workspace_coverages(base_url, workspace)
    if not names:
        return raw
    ts_names = sorted(
        [n for n in names if n.startswith(layer_base + "_") and re.search(r"_20\d{8,14}$", n)]
    )
    if not ts_names:
        return raw
    return f"{workspace}:{ts_names[-1]}"


def _haihe_boundary_bbox_wgs84(boundary_shp: str, pad_deg: float = 0.02) -> Optional[Tuple[float, float, float, float]]:
    if not boundary_shp or not os.path.isfile(boundary_shp):
        return None
    try:
        import geopandas as gpd  # type: ignore[import-untyped]

        gdf = gpd.read_file(boundary_shp)
        if gdf.empty:
            return None
        if gdf.crs is None:
            gdf = gdf.set_crs(4326)
        else:
            gdf = gdf.to_crs(4326)
        minx, miny, maxx, maxy = (float(x) for x in gdf.total_bounds)
        return (minx - pad_deg, miny - pad_deg, maxx + pad_deg, maxy + pad_deg)
    except Exception:
        return None


def _wcs_raster_size_for_bbox(minx: float, miny: float, maxx: float, maxy: float, max_px: int) -> Tuple[int, int]:
    w_deg = max(float(maxx) - float(minx), 1e-9)
    h_deg = max(float(maxy) - float(miny), 1e-9)
    ar = w_deg / h_deg
    if ar >= 1.0:
        width = max_px
        height = max(2, int(round(max_px / ar)))
    else:
        height = max_px
        width = max(2, int(round(max_px * ar)))
    return width, height


def _wcs_getcoverage_to_temp_geotiff(
    base_url: str,
    coverage_id: str,
    bbox_wgs84: Tuple[float, float, float, float],
    width: int,
    height: int,
    timeout_sec: int = 120,
) -> Tuple[Optional[str], str]:
    """
    WCS 1.0.0 GetCoverage，输出 GeoTIFF，与 GeoServer 上该 coverage 一致（可与 WMS 同源联调）。
    """
    wcs_url = f"{base_url.rstrip('/')}/wcs"
    minx, miny, maxx, maxy = bbox_wgs84
    params = {
        "service": "WCS",
        "version": "1.0.0",
        "request": "GetCoverage",
        "coverage": coverage_id,
        "crs": "EPSG:4326",
        "bbox": f"{minx},{miny},{maxx},{maxy}",
        "width": str(width),
        "height": str(height),
        "format": "GeoTIFF",
    }
    auth = _geoserver_rest_auth()
    try:
        resp = requests.get(wcs_url, params=params, auth=auth, timeout=timeout_sec)
    except Exception as exc:
        return None, f"wcs_request_failed: {exc}"
    if resp.status_code != 200:
        snippet = (resp.text or "")[:500]
        return None, f"wcs_http_{resp.status_code}: {snippet}"
    blob = resp.content or b""
    if len(blob) < 200:
        snippet = blob.decode("utf-8", errors="replace")[:500]
        return None, f"wcs_empty_or_short: {snippet}"
    head = blob[:2500]
    if b"ServiceException" in head or (head.lstrip().startswith(b"<") and b"Exception" in head):
        snippet = blob[:800].decode("utf-8", errors="replace")
        return None, f"wcs_service_exception: {snippet}"
    fd, path = tempfile.mkstemp(suffix=".tif", prefix="haihe_gs_wcs_")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(blob)
    except Exception as exc:
        try:
            os.unlink(path)
        except OSError:
            pass
        return None, f"wcs_write_failed: {exc}"
    return path, ""


def _parse_lead_hours_param(params: Dict[str, Any]) -> List[int]:
    lead_raw = str(_as_scalar(params.get("lead_hours")) or "24,48,72").strip()
    hours: List[int] = []
    for part in re.split(r"[,，\s]+", lead_raw):
        p = part.strip()
        if p.isdigit():
            hours.append(int(p))
    hours = sorted({h for h in hours if h > 0})
    return hours if hours else [24, 48, 72]


def _intensity_stats_common_payload(boundary_shp: str) -> Dict[str, Any]:
    return {
        "boundary_shp": boundary_shp,
        "intensity_definition_mm": {
            "storm": "[50, 100)",
            "heavy_storm": "[100, 250)",
            "extreme_storm": ">=250",
            "denominator": "海河流域掩膜内有效像元（非 NaN）",
        },
    }


def _forecast_precip_intensity_stats_geoserver_http(params: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    cp = configparser.ConfigParser()
    cp.read(_CONFIG_PATH, encoding="utf-8")
    env_base = (os.getenv("GEOSERVER_BASE_URL") or "").strip().rstrip("/")
    base_url = env_base
    if cp.has_section("geoserver"):
        base_url = (cp["geoserver"].get("base_url") or base_url or "").strip().rstrip("/")
    if not base_url:
        return 503, {"message": "未配置 [geoserver] base_url 或 GEOSERVER_BASE_URL，无法用 WCS 与地图同源统计"}
    if not cp.has_section("geoserver"):
        return 503, {"message": "config.ini 缺少 [geoserver] 节"}

    _, boundary_shp = _read_ec_output_and_boundary_shp(_CONFIG_PATH)
    bbox = _haihe_boundary_bbox_wgs84(boundary_shp)
    if not bbox:
        return 400, {"message": "paths.boundary_shp 无效，无法确定 WCS 范围"}

    try:
        max_px = max(128, min(4096, int(_as_scalar(params.get("wcs_max_px")) or 0) or int(os.getenv("FORECAST_PRECIP_WCS_MAX_PX", "900"))))
    except (TypeError, ValueError):
        max_px = 900
    width, height = _wcs_raster_size_for_bbox(*bbox, max_px)

    hours = _parse_lead_hours_param(params)
    sec = cp["geoserver"]
    rows: List[Dict[str, Any]] = []
    try:
        wcs_timeout = max(15, int(os.getenv("FORECAST_PRECIP_WCS_TIMEOUT_SEC", "120")))
    except ValueError:
        wcs_timeout = 120

    for h in hours:
        cfg_key = f"layer_fcst_cumulative_{h}h"
        configured = str(sec.get(cfg_key) or "").strip()
        row: Dict[str, Any] = {"lead_hours": h, "raster_source": "geoserver"}
        if not configured:
            row["note"] = f"config.ini [geoserver] 未配置 {cfg_key}"
            rows.append(row)
            continue
        resolved = _geoserver_resolve_configured_layer_id(configured, base_url)
        row["geoserver_coverage"] = resolved
        tmp_path, err = _wcs_getcoverage_to_temp_geotiff(
            base_url, resolved, bbox, width, height, timeout_sec=wcs_timeout
        )
        if not tmp_path:
            row["note"] = err or "wcs_failed"
            rows.append(row)
            continue
        try:
            stats = _compute_basin_raster_intensity_stats_mm(tmp_path, boundary_shp)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        if not stats.get("ok"):
            row["note"] = str(stats.get("error") or "stats_failed")
            rows.append(row)
            continue
        row.update(
            {
                "storm_area_pct": stats["storm_area_pct_of_basin_valid"],
                "heavy_storm_area_pct": stats["heavy_storm_area_pct_of_basin_valid"],
                "extreme_storm_area_pct": stats["extreme_storm_area_pct_of_basin_valid"],
                "storm_area_km2_approx": stats["storm_50_100_mm_area_km2_approx"],
                "heavy_storm_area_km2_approx": stats["heavy_storm_100_250_mm_area_km2_approx"],
                "extreme_storm_area_km2_approx": stats["extreme_storm_ge_250_mm_area_km2_approx"],
                "basin_valid_area_km2_approx": stats["basin_valid_area_km2_approx"],
            }
        )
        rows.append(row)

    meta_start = str(_as_scalar(params.get("start_time_compact") or params.get("start_time")) or "").strip()
    meta_digits = "".join(c for c in meta_start if c.isdigit())[:10] if meta_start else ""

    out: Dict[str, Any] = {
        **_intensity_stats_common_payload(boundary_shp),
        "raster_source": "geoserver",
        "geoserver_base_url": base_url,
        "wcs_endpoint": f"{base_url.rstrip('/')}/wcs",
        "wcs_bbox_wgs84": [round(x, 6) for x in bbox],
        "wcs_width_height": [width, height],
        "rows": rows,
    }
    if meta_digits:
        out["ref_start_time_compact"] = meta_digits
    out["note"] = (
        "强度占比与 GeoServer 发布的预报累计降水 coverage 同源：WCS GetCoverage 裁海河流域 BBOX 后再按 boundary_shp 精确掩膜统计。"
    )
    return 200, out


def _forecast_precip_intensity_stats_ec_http(params: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    start_raw = str(_as_scalar(params.get("start_time_compact") or params.get("start_time")) or "").strip()
    digits = "".join(c for c in start_raw if c.isdigit())
    if len(digits) < 10:
        return 400, {"message": "raster_source=ec 时 start_time_compact 必填（至少十位起报 YYYYMMDDHH）"}
    start_compact = digits[:10]
    hours = _parse_lead_hours_param(params)

    ec_out, boundary_shp = _read_ec_output_and_boundary_shp(_CONFIG_PATH)
    try:
        bundle = collect_ec_forecast_precip_files(start_compact, ec_out, hours)
    except Exception as exc:
        return 400, {"message": f"起报或路径解析失败: {exc}"}

    rows: List[Dict[str, Any]] = []
    ec_files = bundle.get("ec_files") if isinstance(bundle, dict) else {}
    if not isinstance(ec_files, dict):
        ec_files = {}
    for h in hours:
        path = ec_files.get(f"{h}h")
        path_s = str(path).strip() if path else ""
        row: Dict[str, Any] = {
            "lead_hours": h,
            "raster_source": "ec",
            "source_file": path_s,
        }
        if not path_s or not path_s.lower().endswith((".tif", ".tiff")):
            row["note"] = "未找到对应 GeoTIFF（ec_*_rain_total_*h.tif）；可改用 raster_source=geoserver 与 WMS 同源"
            rows.append(row)
            continue
        stats = _compute_basin_raster_intensity_stats_mm(path_s, boundary_shp)
        if not stats.get("ok"):
            row["note"] = str(stats.get("error") or "stats_failed")
            rows.append(row)
            continue
        row.update(
            {
                "storm_area_pct": stats["storm_area_pct_of_basin_valid"],
                "heavy_storm_area_pct": stats["heavy_storm_area_pct_of_basin_valid"],
                "extreme_storm_area_pct": stats["extreme_storm_area_pct_of_basin_valid"],
                "storm_area_km2_approx": stats["storm_50_100_mm_area_km2_approx"],
                "heavy_storm_area_km2_approx": stats["heavy_storm_100_250_mm_area_km2_approx"],
                "extreme_storm_area_km2_approx": stats["extreme_storm_ge_250_mm_area_km2_approx"],
                "basin_valid_area_km2_approx": stats["basin_valid_area_km2_approx"],
            }
        )
        rows.append(row)

    return 200, {
        **_intensity_stats_common_payload(boundary_shp),
        "raster_source": "ec",
        "start_time_compact": str(bundle.get("start_time_compact") or start_compact),
        "ec_output_path": ec_out,
        "rows": rows,
    }


def _forecast_precip_intensity_stats_http(params: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    src = str(
        _as_scalar(params.get("raster_source"))
        or os.getenv("FORECAST_PRECIP_STATS_RASTER_SOURCE", "geoserver")
        or "geoserver"
    ).strip().lower()
    if src in ("ec", "ec_tif", "ec_output", "tif", "file"):
        return _forecast_precip_intensity_stats_ec_http(params)
    if src in ("auto", "both"):
        code, payload = _forecast_precip_intensity_stats_geoserver_http(params)
        if code == 200 and isinstance(payload.get("rows"), list):
            ok_any = any(
                isinstance(r, dict) and r.get("storm_area_pct") is not None for r in payload["rows"]
            )
            if ok_any:
                payload["raster_source"] = "geoserver"
                return code, payload
        ec_code, ec_payload = _forecast_precip_intensity_stats_ec_http(params)
        if isinstance(ec_payload, dict):
            ec_payload["raster_source"] = "ec_fallback_after_geoserver"
        return ec_code, ec_payload
    return _forecast_precip_intensity_stats_geoserver_http(params)


def _load_geoserver_catalog() -> Dict[str, Any]:
    """
    读取 config.ini [geoserver] 或环境变量 GEOSERVER_BASE_URL，生成累计降水等栅格 WMS 模板链接。
    layer_* 配置值为 GeoServer 工作区与图层名（如 hhly:obs_apcp_24h）。
    """
    cp = configparser.ConfigParser()
    cp.read(_CONFIG_PATH, encoding="utf-8")
    env_base = (os.getenv("GEOSERVER_BASE_URL") or "").strip().rstrip("/")
    base_url = env_base
    wms_path = "/wms"
    if cp.has_section("geoserver"):
        sec = cp["geoserver"]
        base_url = (sec.get("base_url") or base_url or "").strip().rstrip("/")
        wms_path = (sec.get("wms_path") or wms_path).strip()
        if wms_path and not wms_path.startswith("/"):
            wms_path = "/" + wms_path
    if not base_url:
        return {
            "enabled": False,
            "base_url": "",
            "layers": [],
            "note": "未配置 [geoserver] base_url 或环境变量 GEOSERVER_BASE_URL",
        }
    base_wms = base_url + (wms_path or "/wms")

    defs = [
        ("obs_cumulative_12h", "实况累计降水（12h）", "observation", "layer_obs_cumulative_12h"),
        ("obs_cumulative_24h", "实况累计降水（24h）", "observation", "layer_obs_cumulative_24h"),
        ("obs_cumulative_36h", "实况累计降水（36h）", "observation", "layer_obs_cumulative_36h"),
        ("obs_cumulative_48h", "实况累计降水（48h）", "observation", "layer_obs_cumulative_48h"),
        ("fcst_cumulative_6h", "预报累计降水（6h）", "forecast", "layer_fcst_cumulative_6h"),
        ("fcst_cumulative_12h", "预报累计降水（12h）", "forecast", "layer_fcst_cumulative_12h"),
        ("fcst_cumulative_24h", "预报累计降水（24h）", "forecast", "layer_fcst_cumulative_24h"),
        ("fcst_cumulative_36h", "预报累计降水（36h）", "forecast", "layer_fcst_cumulative_36h"),
        ("fcst_cumulative_48h", "预报累计降水（48h）", "forecast", "layer_fcst_cumulative_48h"),
        ("fcst_cumulative_60h", "预报累计降水（60h）", "forecast", "layer_fcst_cumulative_60h"),
        ("fcst_cumulative_72h", "预报累计降水（72h）", "forecast", "layer_fcst_cumulative_72h"),
    ]
    layers_out: List[Dict[str, Any]] = []
    sec_gs = cp["geoserver"] if cp.has_section("geoserver") else None
    for lid, title, kind, cfg_key in defs:
        layer_name = (sec_gs.get(cfg_key) if sec_gs else "") or ""
        layer_name = str(layer_name).strip()
        if not layer_name:
            continue
        layer_name = _geoserver_resolve_configured_layer_id(layer_name, base_url)
        lyr_enc = quote(layer_name, safe=":")
        style_name = ""
        if sec_gs:
            style_name = str(sec_gs.get(f"{cfg_key}_style", "") or "").strip()
        if not style_name:
            # 默认使用与图层同名样式（与专题图发布脚本保持一致）
            style_name = layer_name.split(":")[-1]
        style_enc = quote(style_name, safe=":")
        tmpl = (
            f"{base_wms}?service=WMS&version=1.1.0&request=GetMap"
            f"&layers={lyr_enc}&styles={style_enc}"
            f"&bbox={{minx}},{{miny}},{{maxx}},{{maxy}}&width={{width}}&height={{height}}"
            f"&srs=EPSG:4326&format=image/png&transparent=true"
        )
        layers_out.append(
            {
                "id": lid,
                "title": title,
                "kind": kind,
                "layer": layer_name,
                "style": style_name,
                "wms_getmap_template": tmpl,
            }
        )
    return {
        "enabled": bool(layers_out),
        "base_url": base_url,
        "wms_endpoint": base_wms,
        "layers": layers_out,
        "note": "占位符 minx,miny,maxx,maxy,width,height 由前端地图视口替换；Tomcat 托管 GeoServer WAR 时 base_url 形如 http://主机:端口/geoserver",
    }


def _read_ec_output_and_boundary_shp(config_path: str) -> Tuple[str, str]:
    cp = configparser.ConfigParser()
    cp.read(config_path, encoding="utf-8")
    ec_out = (cp.get("paths", "ecOutput", fallback="") or "").strip() or DEFAULT_EC_OUTPUT_PATH
    boundary = (cp.get("paths", "boundary_shp", fallback="") or "").strip()
    return ec_out, boundary


def _compute_basin_raster_intensity_stats_mm(tif_path: str, boundary_shp: str) -> Dict[str, Any]:
    """
    在海河流域矢量（boundary_shp）掩膜内，对累计降水栅格（mm）做等级面积占比：
    - 暴雨：50.0 <= v < 100.0
    - 大暴雨：100.0 <= v < 250.0
    - 特大暴雨：v >= 250.0
    面积按近似像元面积（经纬度栅格）估算，单位 km²。
    """
    from math import cos, radians

    try:
        import numpy as np  # type: ignore[import-untyped]
        import rasterio  # type: ignore[import-untyped]
        from rasterio.features import geometry_mask  # type: ignore[import-untyped]
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": f"rasterio/numpy 不可用: {exc}"}

    if not tif_path or not os.path.isfile(tif_path):
        return {"ok": False, "error": "tif_not_found"}
    if not str(tif_path).lower().endswith((".tif", ".tiff")):
        return {"ok": False, "error": "only_geotiff_supported"}

    with rasterio.open(tif_path) as src:
        arr = src.read(1).astype("float64")
        transform = src.transform
        nodata = src.nodata
        crs = src.crs
        height, width = arr.shape
        bounds = src.bounds

    if nodata is not None:
        arr = np.where(arr == nodata, np.nan, arr)
    arr = np.where(np.isfinite(arr) & (arr < -1e5), np.nan, arr)

    inside = np.ones((height, width), dtype=bool)
    if boundary_shp and os.path.isfile(boundary_shp):
        try:
            import geopandas as gpd  # type: ignore[import-untyped]

            gdf = gpd.read_file(boundary_shp)
            if not gdf.empty:
                if gdf.crs is not None and crs is not None and gdf.crs != crs:
                    gdf = gdf.to_crs(crs)
                geom = gdf.unary_union
                gi = getattr(geom, "__geo_interface__", None)
                if gi is None:
                    return {"ok": False, "error": "boundary_geometry_invalid"}
                inside = geometry_mask(
                    [gi],
                    out_shape=(height, width),
                    transform=transform,
                    invert=True,
                    all_touched=True,
                )
        except Exception as exc:
            return {"ok": False, "error": f"boundary_mask_failed: {exc}"}

    valid = np.isfinite(arr) & inside
    if not np.any(valid):
        return {"ok": False, "error": "no_valid_pixels_in_basin"}

    masked = np.where(valid, arr, np.nan)
    n_valid = int(np.count_nonzero(valid))
    n_storm = int(np.count_nonzero(valid & (masked >= 50.0) & (masked < 100.0)))
    n_heavy = int(np.count_nonzero(valid & (masked >= 100.0) & (masked < 250.0)))
    n_extreme = int(np.count_nonzero(valid & (masked >= 250.0)))

    lat_c = (bounds.bottom + bounds.top) / 2.0
    res_x = abs(transform[0])
    res_y = abs(transform[4])
    m_per_deg_lat = 110574.0
    m_per_deg_lon = 111320.0 * cos(radians(lat_c))
    cell_area_km2 = abs(res_x * m_per_deg_lon) * abs(res_y * m_per_deg_lat) / 1e6

    def pct(n: int) -> float:
        return round(100.0 * n / n_valid, 4) if n_valid else 0.0

    return {
        "ok": True,
        "tif_path": tif_path,
        "valid_pixel_count": n_valid,
        "cell_area_km2_approx": round(float(cell_area_km2), 8),
        "basin_valid_area_km2_approx": round(float(n_valid * cell_area_km2), 2),
        "storm_50_100_mm_area_km2_approx": round(float(n_storm * cell_area_km2), 2),
        "heavy_storm_100_250_mm_area_km2_approx": round(float(n_heavy * cell_area_km2), 2),
        "extreme_storm_ge_250_mm_area_km2_approx": round(float(n_extreme * cell_area_km2), 2),
        "storm_area_pct_of_basin_valid": pct(n_storm),
        "heavy_storm_area_pct_of_basin_valid": pct(n_heavy),
        "extreme_storm_area_pct_of_basin_valid": pct(n_extreme),
    }


def _extract_cookie_value(cookie_header: Optional[str], name: str) -> Optional[str]:
    if not cookie_header:
        return None
    prefix = f"{name}="
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(prefix):
            return unquote(part[len(prefix) :].strip())
    return None


def _response_board_session_key(handler: BaseHTTPRequestHandler) -> Optional[str]:
    """
    生成 response-board 的会话键（仅接收显式会话标识）：
    - 仅使用前端透传的 X-Session-Id；
    - 未提供则返回 None，避免 NAT 场景下 peer+UA 串会话。
    """
    sid = str(handler.headers.get("X-Session-Id") or "").strip()
    if sid:
        return f"sid:{sid}"
    return None


def _prune_session_baselines(now_dt: datetime) -> None:
    cutoff = now_dt - timedelta(seconds=_SESSION_BASELINE_TTL_SECONDS)
    stale = [k for k, (_, ts) in _SESSION_BASELINES.items() if ts < cutoff]
    for k in stale:
        _SESSION_BASELINES.pop(k, None)
    if len(_SESSION_BASELINES) <= _SESSION_BASELINE_MAX_ENTRIES:
        return
    ordered = sorted(_SESSION_BASELINES.items(), key=lambda kv: kv[1][1], reverse=True)
    keep = dict(ordered[:_SESSION_BASELINE_MAX_ENTRIES])
    _SESSION_BASELINES.clear()
    _SESSION_BASELINES.update(keep)


def _prepare_response_board_request(
    handler: BaseHTTPRequestHandler, params: Dict[str, Any]
) -> Tuple[Dict[str, Any], Optional[str]]:
    """
    会话基线（可选）：首次访问下发 Cookie，仅返回 created_at >= 基线 的事件，
    避免打开页面尚未交互时已堆满历史/联调数据。

    开启环境变量：EMERGENCY_RESPONSE_BOARD_SESSION_BASELINE=1

    跳过过滤：include_all_events=true，或显式传 created_after / baseline_at。
    返回：(合并后的 params, Set-Cookie 整行或 None)
    """
    out = dict(params)
    if _parse_bool(out.get("include_all_events"), False):
        return out, None

    explicit = _to_iso_time(str(_as_scalar(out.get("created_after")) or "").strip())
    if not explicit:
        explicit = _to_iso_time(str(_as_scalar(out.get("baseline_at")) or "").strip())
    if explicit:
        out["_created_after_effective"] = explicit
        return out, None

    if str(os.getenv("EMERGENCY_RESPONSE_BOARD_SESSION_BASELINE", "0")).strip().lower() not in {"1", "true", "yes", "on"}:
        return out, None

    ck = _extract_cookie_value(handler.headers.get("Cookie"), "emergency_baseline_at")
    if ck:
        iso = _to_iso_time(ck.strip())
        if iso:
            sk = _response_board_session_key(handler)
            if sk:
                now_dt = datetime.now()
                with _SESSION_BASELINE_LOCK:
                    _prune_session_baselines(now_dt)
                    _SESSION_BASELINES[sk] = (iso, now_dt)
            out["_created_after_effective"] = iso
        return out, None

    sk = _response_board_session_key(handler)
    if sk:
        now_dt = datetime.now()
        cached: Optional[Tuple[str, datetime]] = None
        with _SESSION_BASELINE_LOCK:
            _prune_session_baselines(now_dt)
            cached = _SESSION_BASELINES.get(sk)
            if cached:
                _SESSION_BASELINES[sk] = (cached[0], now_dt)
        if cached:
            iso = _to_iso_time(cached[0].strip())
            if iso:
                out["_created_after_effective"] = iso
                return out, None

    baseline_now = _now_iso()
    if sk:
        now_dt = datetime.now()
        with _SESSION_BASELINE_LOCK:
            _prune_session_baselines(now_dt)
            _SESSION_BASELINES[sk] = (baseline_now, now_dt)
    out["_created_after_effective"] = baseline_now
    val = quote(baseline_now, safe="")
    return out, f"emergency_baseline_at={val}; Path=/; Max-Age=86400; SameSite=Lax"


def _list_management_response_board(params: Dict[str, Any]) -> Dict[str, Any]:
    now_time = _to_iso_time(_as_scalar(params.get("now_time")))
    history_hours = min(max(0, _parse_int(params.get("history_hours"), 36)), 24 * 14)
    future_hours = min(max(1, _parse_int(params.get("future_hours"), 72)), 24 * 14)
    event_type_raw = _as_scalar(params.get("event_type"))
    event_type = str(event_type_raw).strip().lower() if event_type_raw is not None else ""
    include_archived = _parse_bool(params.get("include_archived"), True)
    try:
        now_window = float(_as_scalar(params.get("now_window_hours")) or 2.0)
    except (TypeError, ValueError):
        now_window = 2.0
    now_window = min(max(now_window, 0.25), 48.0)
    tick_step_hours = min(max(1, _parse_int(params.get("tick_step_hours"), 12)), 48)
    limit = min(max(1, _parse_int(params.get("limit"), 200)), 1000)

    eff = _as_scalar(params.get("_created_after_effective"))
    ca = _as_scalar(params.get("created_after"))
    ba = _as_scalar(params.get("baseline_at"))
    created_after_raw = eff if eff is not None else (ca if ca is not None else ba)
    created_after_iso = _to_iso_time(str(created_after_raw).strip()) if created_after_raw else None

    allow_mutation = _parse_bool(params.get("_allow_mutation"), False)
    trigger_forecast = _parse_bool(params.get("trigger_forecast"), False) and allow_mutation
    trigger_meta = None
    if trigger_forecast:
        trigger_meta = _trigger_forecast_from_response_board_params(params)

    board = _MANAGEMENT_STORE.build_response_board(
        now_time=now_time,
        history_hours=history_hours,
        future_hours=future_hours,
        event_type=event_type,
        include_archived=include_archived,
        now_window_hours=now_window,
        tick_step_hours=tick_step_hours,
        limit=limit,
        created_after=created_after_iso,
    )
    if trigger_forecast and trigger_meta is not None:
        board["trigger_forecast"] = trigger_meta
    return board


def _flow_self_check() -> Dict[str, Any]:
    """
    轻量流程自检：用于快速判断主链路是否可用（管理库、看板构建、产品队列状态）。
    """
    checks: Dict[str, Any] = {}
    ok = True
    try:
        events = _MANAGEMENT_STORE.list_events(page=1, page_size=1)
        checks["management_store"] = {
            "ok": True,
            "event_count_hint": int(events.get("total") or 0),
        }
    except Exception as exc:
        ok = False
        checks["management_store"] = {"ok": False, "error": str(exc)}
    try:
        checks["forecast_queue"] = {"ok": True, **forecast_product_queue_status()}
    except Exception as exc:
        ok = False
        checks["forecast_queue"] = {"ok": False, "error": str(exc)}
    try:
        checks["observation_queue"] = {"ok": True, **observation_product_queue_status()}
    except Exception as exc:
        ok = False
        checks["observation_queue"] = {"ok": False, "error": str(exc)}
    return {
        "ok": ok,
        "checked_at": _now_iso(),
        "checks": checks,
    }


def _trigger_forecast_from_response_board_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    单接口兜底：
    response-board 带 trigger_forecast=true 时，先触发一次 forecast 并同步写入 hh_emergency_event。
    """
    raw_payload = params.get("trigger_payload")
    payload: Dict[str, Any] = {}
    if isinstance(raw_payload, dict):
        payload = dict(raw_payload)
    start_time = str(
        _as_scalar(
            payload.get("start_time")
            or params.get("trigger_start_time")
            or params.get("start_time")
        )
        or ""
    ).strip()
    if not start_time:
        return {"ok": False, "message": "trigger_forecast=true 时需要 start_time（或 trigger_start_time）"}
    start_time_hour = _to_hour_iso_time(start_time) or start_time

    scope = str(_as_scalar(payload.get("scope") or params.get("trigger_scope") or params.get("scope")) or "haihe")
    basin_codes = (
        str(_as_scalar(payload.get("basin_codes") or params.get("trigger_basin_codes") or params.get("basin_codes")))
        if (payload.get("basin_codes") or params.get("trigger_basin_codes") or params.get("basin_codes")) is not None
        else None
    )
    ec_output_path = (
        str(_as_scalar(payload.get("ec_output_path") or params.get("trigger_ec_output_path")))
        if (payload.get("ec_output_path") or params.get("trigger_ec_output_path")) is not None
        else DEFAULT_EC_OUTPUT_PATH
    )
    local_station_json_path = (
        str(_as_scalar(payload.get("local_station_json_path") or params.get("trigger_local_station_json_path")))
        if (payload.get("local_station_json_path") or params.get("trigger_local_station_json_path")) is not None
        else None
    )
    include_evidence = _parse_bool(payload.get("include_evidence"), _parse_bool(params.get("trigger_include_evidence"), True))

    forecast_req = {
        "start_time": start_time_hour,
        "scope": scope,
        "basin_codes": resolve_emergency_basin_codes(basin_codes=basin_codes, scope=scope),
        "ec_output_path": ec_output_path,
        "local_station_json_path": local_station_json_path,
        "include_evidence": include_evidence,
    }

    trace_id = str(_as_scalar(params.get("_trace_id")) or "").strip() or None
    fast_mock = _parse_bool(params.get("trigger_fast_mock"), False) or _parse_bool(params.get("trigger_mock"), False)
    if fast_mock:
        mock_result = {
            "reached": True,
            "level": str(_as_scalar(params.get("trigger_mock_level")) or "IV").strip().upper() or "IV",
            "message": str(_as_scalar(params.get("trigger_mock_message")) or "快速联调：已模拟触发应急响应"),
            "evidence": {
                "mode": "fast_mock",
                "generated_at": _now_iso(),
                "window_hours": 24,
            },
        }
        event_id = _append_event(
            event_type="forecast",
            query_time=start_time_hour,
            request_params={**forecast_req, "fast_mock": True},
            response_payload=mock_result,
            trace_id=trace_id,
        )
        _sync_forecast_result_to_management_timeline(
            event_id=event_id,
            start_time=start_time_hour,
            request_params={**forecast_req, "fast_mock": True},
            forecast_result=mock_result,
            trace_id=trace_id,
        )
        return {
            "ok": True,
            "event_id": event_id,
            "reached": True,
            "level": mock_result["level"],
            "message": mock_result["message"],
            "mode": "fast_mock",
        }

    try:
        sustain_raw = _as_scalar(payload.get("sustain_threshold_6h_mm"))
        if sustain_raw is None:
            sustain_raw = _as_scalar(params.get("trigger_sustain_threshold_6h_mm"))
        result = query_haihe_emergency_forecast(
            start_time=start_time_hour,
            basin_codes=basin_codes,
            scope=scope,
            ec_output_path=ec_output_path,
            allowed_station_levels=str(payload.get("allowed_station_levels") or params.get("trigger_allowed_station_levels") or "11,12,13,16"),
            sample_method=str(payload.get("sample_method") or params.get("trigger_sample_method") or "nearest"),
            sustain_threshold_6h_mm=float(0.1 if sustain_raw is None else sustain_raw),
            typhoon_landing_impact=_parse_bool(payload.get("typhoon_landing_impact"), _parse_bool(params.get("trigger_typhoon_landing_impact"), False)),
            typhoon_impact_increasing=_parse_bool(payload.get("typhoon_impact_increasing"), _parse_bool(params.get("trigger_typhoon_impact_increasing"), False)),
            include_evidence=include_evidence,
            local_station_json_path=local_station_json_path,
        )
        event_id = _append_event(
            event_type="forecast",
            query_time=start_time_hour,
            request_params=forecast_req,
            response_payload=result,
            trace_id=trace_id,
        )
        _sync_forecast_result_to_management_timeline(
            event_id=event_id,
            start_time=start_time_hour,
            request_params=forecast_req,
            forecast_result=result,
            trace_id=trace_id,
        )
        return {
            "ok": True,
            "event_id": event_id,
            "reached": bool(result.get("reached")),
            "level": result.get("level"),
            "message": result.get("message"),
        }
    except Exception as e:
        return {"ok": False, "message": f"trigger_forecast 执行失败: {e}"}


def _read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = handler.headers.get("Content-Length")
    if not length:
        return {}
    try:
        n = int(length)
    except Exception:
        return {}
    if n <= 0:
        return {}
    raw = handler.rfile.read(n).decode("utf-8", errors="replace")
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        snippet = raw.strip().replace("\r", "\\r").replace("\n", "\\n")
        if len(snippet) > 200:
            snippet = snippet[:200] + "...(truncated)"
        raise ValueError(f"POST body 不是合法 JSON: {e}; body_snippet={snippet}")


def _cors_headers(handler: BaseHTTPRequestHandler) -> Dict[str, str]:
    """浏览器 Ajax 轮询跨域：默认开启；EMERGENCY_HTTP_CORS=0 关闭。"""
    if str(os.getenv("EMERGENCY_HTTP_CORS", "1")).strip().lower() in ("0", "false", "no", "off"):
        return {}
    origin = (os.getenv("EMERGENCY_HTTP_CORS_ORIGIN") or "*").strip() or "*"
    if origin == "__mirror__":
        origin = handler.headers.get("Origin") or "*"
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Session-Id, X-Trace-Id",
        "Access-Control-Max-Age": "86400",
    }


def _request_trace_id(handler: BaseHTTPRequestHandler) -> str:
    incoming = str(handler.headers.get("X-Trace-Id") or "").strip()
    if incoming:
        return incoming
    return f"tr-{uuid.uuid4().hex}"


def _json_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    payload: Dict[str, Any],
    extra_headers: Optional[Dict[str, str]] = None,
) -> None:
    body = dict(payload) if isinstance(payload, dict) else {"data": payload}
    trace_id = str(getattr(handler, "_trace_id", "") or "").strip()
    if trace_id and "trace_id" not in body:
        body["trace_id"] = trace_id
    out = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(out)))
        if trace_id:
            handler.send_header("X-Trace-Id", trace_id)
        for ck, cv in _cors_headers(handler).items():
            handler.send_header(ck, cv)
        for hk, hv in (extra_headers or {}).items():
            handler.send_header(str(hk), str(hv))
        handler.end_headers()
        handler.wfile.write(out)
    except (BrokenPipeError, ConnectionResetError):
        # 客户端已主动断开连接（超时/取消请求），服务端无需再报错
        return


def _binary_response(handler: BaseHTTPRequestHandler, status: int, data: bytes, content_type: str) -> None:
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
    except (BrokenPipeError, ConnectionResetError):
        return


def _binary_response_with_headers(
    handler: BaseHTTPRequestHandler,
    status: int,
    data: bytes,
    content_type: str,
    extra_headers: Optional[Dict[str, str]] = None,
) -> None:
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or {}).items():
            handler.send_header(str(k), str(v))
        handler.end_headers()
        handler.wfile.write(data)
    except (BrokenPipeError, ConnectionResetError):
        return


def _zip_observation_shapefile_bytes(shp_path: str) -> bytes:
    base, _ = os.path.splitext(shp_path)
    family = [".shp", ".shx", ".dbf", ".prj", ".cpg"]
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for ext in family:
            p = base + ext
            if os.path.isfile(p):
                zf.write(p, arcname=os.path.basename(p))
    return mem.getvalue()


def _forecast_compact_from_params(params: Dict[str, Any]) -> Optional[str]:
    c = _as_scalar(params.get("start_time_compact"))
    if c:
        return "".join(x for x in str(c) if x.isalnum())
    st = _as_scalar(params.get("start_time"))
    if not st:
        return None
    try:
        from haihe_mcp_tools import _parse_forecast_start_time

        dt = _parse_forecast_start_time(str(st))
        return dt.strftime("%Y%m%d%H")
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    def _handle(self) -> None:
        self._trace_id = _request_trace_id(self)
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        try:
            if route == "/health":
                _json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "service": "emergency_http_server",
                        "server_version": _server_version_info(),
                    },
                )
                return

            if route in ("/emergency/flow/self-check", "/emergency/flow/self-check/"):
                _json_response(self, 200, _flow_self_check())
                return

            if route in ("/scenario/river/downstream", "/scenario/river/downstream/"):
                body = _read_json_body(self) if self.command.upper() == "POST" else {}
                params = {**query, **body}
                river_name = str(_as_scalar(params.get("river_name")) or "").strip()
                if not river_name:
                    _json_response(self, 400, {"message": "river_name 必填"})
                    return
                max_rivers = max(1, _parse_int(params.get("max_rivers"), 20))
                impact = estimate_river_impact_time_core(river_name=river_name, max_rivers=max_rivers)
                downstream = impact.get("downstream", []) or []
                river_names = [river_name] + [
                    str(x.get("downstream_river") or "").strip() for x in downstream if str(x.get("downstream_river") or "").strip()
                ]
                if _wants_wms_sql(params):
                    try:
                        pg = load_pg_for_registry(_CONFIG_PATH)
                        sql_text = build_river_names_geojson_wms_sql(pg, river_names)
                        sql_id = register_wms_sql_text(_CONFIG_PATH, sql_text)
                        rows_wms: List[Dict[str, Any]] = []
                        for idx, item in enumerate(downstream, start=1):
                            fid = f"downstream_{idx}"
                            dn = str(item.get("downstream_river") or "").strip()
                            distance_km = _round2(item.get("impact_distance_km"))
                            rows_wms.append(
                                {
                                    "feature_id": fid,
                                    "river_name": dn,
                                    "river_length_km": None,
                                    "distance_km": distance_km,
                                }
                            )
                        payload = build_response_payload(
                            scenario="river_downstream",
                            geojson_obj=feature_collection([]),
                            tables=[
                                ScenarioTable(
                                    table_key="downstream_rivers",
                                    table_name="下流河系列表",
                                    columns=[
                                        TableColumn("river_name", "河名称"),
                                        TableColumn("river_length_km", "河长(km)"),
                                        TableColumn("distance_km", "距离(km)"),
                                        TableColumn("feature_id", "定位ID"),
                                    ],
                                    rows=rows_wms,
                                )
                            ],
                            query_time=_now_iso(),
                            map_sql_id=sql_id,
                        )
                        _json_response(self, 200, payload)
                        return
                    except Exception as exc:
                        _json_response(self, 500, {"message": f"WMS SQL 登记失败: {exc}"})
                        return
                geom_map = {}
                try:
                    geom_map = _query_river_geometries_by_names(river_names)
                except Exception:
                    geom_map = {}
                current_geom = (geom_map.get(river_name) or {}).get("geometry") or {"type": "GeometryCollection", "geometries": []}
                current_len = (geom_map.get(river_name) or {}).get("river_length_km")
                features = [
                    build_feature(
                        feature_id=f"river_current_{river_name}",
                        geometry=current_geom,
                        style_type="current_river",
                        props={"river_name": river_name, "river_length_km": current_len},
                    )
                ]
                rows: List[Dict[str, Any]] = []
                for idx, item in enumerate(downstream, start=1):
                    fid = f"downstream_{idx}"
                    dn = str(item.get("downstream_river") or "").strip()
                    dn_geom_item = geom_map.get(dn) or {}
                    distance_km = _round2(item.get("impact_distance_km"))
                    rows.append(
                        {
                            "feature_id": fid,
                            "river_name": dn,
                            "river_length_km": _round2(dn_geom_item.get("river_length_km")),
                            "distance_km": distance_km,
                        }
                    )
                    features.append(
                        build_feature(
                            feature_id=fid,
                            geometry=dn_geom_item.get("geometry") or {"type": "GeometryCollection", "geometries": []},
                            style_type="downstream_river",
                            props={
                                "river_name": dn,
                                "river_length_km": _round2(dn_geom_item.get("river_length_km")),
                                "distance_km": distance_km,
                            },
                        )
                    )
                payload = build_response_payload(
                    scenario="river_downstream",
                    geojson_obj=feature_collection(features),
                    tables=[
                        ScenarioTable(
                            table_key="downstream_rivers",
                            table_name="下流河系列表",
                            columns=[
                                TableColumn("river_name", "河名称"),
                                TableColumn("river_length_km", "河长(km)"),
                                TableColumn("distance_km", "距离(km)"),
                                TableColumn("feature_id", "定位ID"),
                            ],
                            rows=rows,
                        )
                    ],
                    query_time=_now_iso(),
                )
                _json_response(self, 200, payload)
                return

            if route in ("/scenario/region/rivers", "/scenario/region/rivers/"):
                body = _read_json_body(self) if self.command.upper() == "POST" else {}
                params = {**query, **body}
                region_name = str(_as_scalar(params.get("region_name")) or "").strip()
                if not region_name:
                    _json_response(self, 400, {"message": "region_name 必填"})
                    return
                max_rivers = max(1, _parse_int(params.get("max_rivers"), 200))
                if _wants_wms_sql(params):
                    try:
                        pg = load_pg_for_registry(_CONFIG_PATH)
                        sql_text = build_region_rivers_scene_wms_sql(pg, region_name, max_rivers)
                        sql_id = register_wms_sql_text(_CONFIG_PATH, sql_text)
                        payload = build_response_payload(
                            scenario="region_rivers",
                            geojson_obj=feature_collection([]),
                            tables=[
                                ScenarioTable(
                                    table_key="region_rivers",
                                    table_name="河系列表",
                                    columns=[
                                        TableColumn("river_name", "河名称"),
                                        TableColumn("river_length_km", "河长(km)"),
                                        TableColumn("feature_id", "定位ID"),
                                    ],
                                    rows=[],
                                )
                            ],
                            query_time=_now_iso(),
                            map_sql_id=sql_id,
                        )
                        _json_response(self, 200, payload)
                        return
                    except Exception as exc:
                        _json_response(self, 500, {"message": f"WMS SQL 登记失败: {exc}"})
                        return
                scene = _query_region_rivers_scene(region_name=region_name, max_rivers=max_rivers)
                features: List[Dict[str, Any]] = []
                for idx, b in enumerate(scene.get("boundaries", []) or [], start=1):
                    g = b.get("geom_json")
                    if not g:
                        continue
                    try:
                        geom = json.loads(g)
                    except Exception:
                        continue
                    features.append(
                        build_feature(
                            feature_id=f"district_{idx}",
                            geometry=geom,
                            style_type="district_boundary",
                            props={
                                "name": b.get("name"),
                                "adcode": b.get("adcode"),
                            },
                        )
                    )
                rows: List[Dict[str, Any]] = []
                river_feature_count = 0
                for r in scene.get("rivers", []) or []:
                    g = r.get("geom_json")
                    if not g:
                        continue
                    try:
                        geom = json.loads(g)
                    except Exception:
                        continue
                    river_feature_count += 1
                    fid = f"river_{river_feature_count}"
                    features.append(
                        build_feature(
                            feature_id=fid,
                            geometry=geom,
                            style_type="river_in_district",
                            props={
                                "river_name": r.get("river_name"),
                                "river_length_km": round(float(r.get("river_length_km") or 0.0), 2),
                            },
                        )
                    )
                    rows.append(
                        {
                            "feature_id": fid,
                            "river_name": r.get("river_name"),
                            "river_length_km": round(float(r.get("river_length_km") or 0.0), 2),
                        }
                    )
                payload = build_response_payload(
                    scenario="region_rivers",
                    geojson_obj=feature_collection(features),
                    tables=[
                        ScenarioTable(
                            table_key="region_rivers",
                            table_name="河系列表",
                            columns=[
                                TableColumn("river_name", "河名称"),
                                TableColumn("river_length_km", "河长(km)"),
                                TableColumn("feature_id", "定位ID"),
                            ],
                            rows=rows,
                        )
                    ],
                    query_time=_now_iso(),
                )
                _json_response(self, 200, payload)
                return

            if route in ("/scenario/monitor/stations", "/scenario/monitor/stations/"):
                body = _read_json_body(self) if self.command.upper() == "POST" else {}
                params = {**query, **body}
                times = _normalize_times_compact(params.get("times"))
                if not times:
                    _json_response(self, 400, {"message": "times 必填，例如 20250723080000"})
                    return
                force_local = _parse_bool(params.get("force_local"), False)
                local_json_path = str(_as_scalar(params.get("local_json_path")) or "").strip() or None
                if force_local:
                    slot = _load_local_merged_slot(times, local_json_path=local_json_path)
                    if not slot:
                        _json_response(self, 404, {"message": f"本地JSON未找到 times={times} 的数据"})
                        return
                    _json_response(self, 200, _build_station_scene_from_local_slot(times, slot))
                    return
                basin_codes = str(_as_scalar(params.get("basin_codes")) or DEFAULT_HAIHE_BASIN_CODES).strip()
                station_levels = str(_as_scalar(params.get("allowed_station_levels")) or "11,12,13,16")
                limit = max(1, min(_parse_int(params.get("limit"), 500), 2000))
                try:
                    records = _fetch_station_obs_records(times=times, basin_codes=basin_codes, allowed_station_levels=station_levels)
                except Exception:
                    slot = _load_local_merged_slot(times, local_json_path=local_json_path)
                    if slot:
                        _json_response(self, 200, _build_station_scene_from_local_slot(times, slot))
                        return
                    raise
                rows: List[Dict[str, Any]] = []
                features: List[Dict[str, Any]] = []
                for rec in records[:limit]:
                    sid = station_id_of(rec)
                    if not sid:
                        continue
                    lon = safe_float(rec.get("Lon"))
                    lat = safe_float(rec.get("Lat"))
                    if abs(lon) < 0.01 or abs(lat) < 0.01:
                        continue
                    fid = f"station_{sid}"
                    row = {
                        "feature_id": fid,
                        "station_id": sid,
                        "station_name": rec.get("Station_Name") or rec.get("Station_Name_C") or sid,
                        "temperature": _nullable_float(rec.get("TEM")),
                        "humidity": _nullable_float(rec.get("RHU")),
                        "wind_speed": _nullable_float(rec.get("WIN_S_Avg_2mi")),
                        "pressure": _nullable_float(rec.get("PRS")),
                        "station_level": normalize_station_level(rec.get("Station_levl")),
                    }
                    rows.append(row)
                    features.append(
                        build_feature(
                            feature_id=fid,
                            geometry={"type": "Point", "coordinates": [lon, lat]},
                            style_type="national_station",
                            props=row,
                        )
                    )
                payload = build_response_payload(
                    scenario="monitor_stations",
                    geojson_obj=feature_collection(features),
                    tables=[
                        ScenarioTable(
                            table_key="station_obs",
                            table_name="站点观测",
                            columns=[
                                TableColumn("station_name", "站点名称"),
                                TableColumn("temperature", "温度"),
                                TableColumn("humidity", "湿度"),
                                TableColumn("wind_speed", "风速"),
                                TableColumn("pressure", "气压"),
                                TableColumn("feature_id", "定位ID"),
                            ],
                            rows=rows,
                        )
                    ],
                    query_time=_now_iso(),
                )
                _json_response(self, 200, payload)
                return

            if route in ("/scenario/emergency/rivers", "/scenario/emergency/rivers/"):
                body = _read_json_body(self) if self.command.upper() == "POST" else {}
                params = {**query, **body}
                river_name = str(_as_scalar(params.get("river_name")) or "").strip()
                river_infer_mode = "manual_river_seed" if river_name else "rainfall_driven_auto"
                times = _normalize_times_compact(params.get("times"))
                if not times:
                    _json_response(self, 400, {"message": "times 必填（用于挂接应急判定）"})
                    return
                force_local = _parse_bool(params.get("force_local"), False)
                local_json_path = str(_as_scalar(params.get("local_json_path")) or "").strip() or None
                max_rivers = max(1, _parse_int(params.get("max_rivers"), 20))
                max_distance_km = max(0.0, _parse_float(params.get("max_distance_km"), 50.0))
                station_buffer_km = max(0.0, _parse_float(params.get("station_buffer_km"), 30.0))
                judgment: Optional[Dict[str, Any]] = None
                ranking: Optional[Dict[str, Any]] = None
                if force_local:
                    slot_j = _load_local_merged_slot(times, local_json_path=local_json_path)
                    if not slot_j:
                        # 前端可能传当前时刻，mock 槽位不一致时回退到最新本地数据
                        slot_j = _load_latest_local_merged_slot(local_json_path=local_json_path)
                    judgment = slot_j.get("judgment") if slot_j else None
                    ranking = slot_j.get("station_ranking") if slot_j else None
                else:
                    try:
                        judgment = query_haihe_emergency_observation(
                            times=times,
                            basin_codes=(
                                str(_as_scalar(params.get("basin_codes"))) if params.get("basin_codes") is not None else None
                            ),
                            scope=(str(_as_scalar(params.get("scope"))) if params.get("scope") is not None else "haihe"),
                            config_path=_CONFIG_PATH,
                            include_evidence=_parse_bool(params.get("include_evidence"), True),
                        )
                    except Exception as exc:
                        slot_j = _load_local_merged_slot(times, local_json_path=local_json_path)
                        if not slot_j:
                            # 实况接口不可用时，优先兜底到最新 mock 槽位，避免直接 500
                            slot_j = _load_latest_local_merged_slot(local_json_path=local_json_path)
                        judgment = slot_j.get("judgment") if slot_j else None
                        if not judgment:
                            # 本地联调兜底：即便 MUSIC 与本地 mock 都不可用，也继续返回河流影响结构，
                            # 避免前端场景直接失败。
                            judgment = {
                                "reached": False,
                                "level": None,
                                "message": f"MUSIC 查询失败，已降级仅返回河流影响结构：{exc}",
                            }

                    try:
                        basin_codes = resolve_emergency_basin_codes(
                            basin_codes=(str(_as_scalar(params.get("basin_codes"))) if params.get("basin_codes") is not None else None),
                            scope=(str(_as_scalar(params.get("scope"))) if params.get("scope") is not None else "haihe"),
                            config_path=_CONFIG_PATH,
                        )
                        ranking = station_rainfall_ranking(
                            times=times,
                            basin_codes=basin_codes,
                            sort_by="PRE_24h",
                            allowed_station_levels=(
                                str(_as_scalar(params.get("allowed_station_levels")))
                                if params.get("allowed_station_levels") is not None
                                else "11,12,13,16"
                            ),
                            limit=max(1, min(_parse_int(params.get("limit"), 2000), 2000)),
                            min_mm=float(_as_scalar(params.get("min_mm")) or 0.0),
                        )
                    except Exception:
                        slot_r = _load_local_merged_slot(times, local_json_path=local_json_path)
                        ranking = slot_r.get("station_ranking") if slot_r else None
                        if not ranking:
                            slot_latest_r = _load_latest_local_merged_slot(local_json_path=local_json_path)
                            ranking = slot_latest_r.get("station_ranking") if slot_latest_r else None
                            if ranking:
                                times = str(slot_latest_r.get("times") or times).strip()

                # 先由降雨落区映射“直接影响河流”
                direct_rows_zone = _query_zone256_rivers_from_ranking(
                    (ranking.get("list", []) if isinstance(ranking, dict) else []),
                    max_rivers=max(1, min(_parse_int(params.get("max_direct_rivers"), 200), 800)),
                    station_buffer_km=station_buffer_km,
                )
                fallback_used = False
                # 线上/联调常见问题：MUSIC 排行接口返回成功但 list 为空，导致直接影响河流被判空。
                # 这里增加一次“最新本地 merged 槽位”回退，避免明明有可用样例却返回空表。
                if (not direct_rows_zone) and (not force_local):
                    slot_latest_fb = _load_latest_local_merged_slot(local_json_path=local_json_path)
                    ranking_fb = slot_latest_fb.get("station_ranking") if slot_latest_fb else None
                    if isinstance(ranking_fb, dict) and isinstance(ranking_fb.get("list"), list) and ranking_fb.get("list"):
                        ranking = ranking_fb
                        times = str(slot_latest_fb.get("times") or times).strip()
                        direct_rows_zone = _query_zone256_rivers_from_ranking(
                            ranking.get("list", []) or [],
                            max_rivers=max(1, min(_parse_int(params.get("max_direct_rivers"), 200), 800)),
                            station_buffer_km=station_buffer_km,
                        )
                        fallback_used = bool(direct_rows_zone)
                direct_names = [
                    str(r.get("river_name") or "").strip()
                    for r in (direct_rows_zone or [])
                    if str(r.get("river_name") or "").strip()
                ]
                # 显式输入 river_name 时，兼容作为手动入口
                if river_name:
                    if river_name not in direct_names:
                        direct_names = [river_name] + direct_names
                    else:
                        direct_names = [river_name] + [n for n in direct_names if n != river_name]
                if not direct_names:
                    # 通用兜底：未能从站点落区映射河流时，尝试本地应急河流导出（当前时次 -> 最新时次）。
                    local_payload = _load_local_emergency_rivers_payload(times) or _load_local_emergency_rivers_payload("")
                    if isinstance(local_payload, dict):
                        # local + wms_sql：将大 GeoJSON 兜底包转为轻量 sql_id 包，避免前端 message handler 卡顿。
                        if _wants_wms_sql(params):
                            try:
                                lp_data = local_payload.get("data") if isinstance(local_payload.get("data"), dict) else {}
                                lp_tables = lp_data.get("tables") if isinstance(lp_data.get("tables"), list) else []
                                direct_rows_raw = _to_rows_from_local_tables(lp_tables, ["direct_rivers"])
                                indirect_rows_raw = _to_rows_from_local_tables(lp_tables, ["indirect_rivers"])

                                direct_rows: List[Dict[str, Any]] = []
                                indirect_rows: List[Dict[str, Any]] = []
                                river_names: List[str] = []

                                for i, r in enumerate(direct_rows_raw, start=1):
                                    name = str(r.get("river_name") or "").strip()
                                    if not name:
                                        continue
                                    river_names.append(name)
                                    direct_rows.append(
                                        {
                                            "feature_id": str(r.get("feature_id") or r.get("locate_id") or f"direct_{i}"),
                                            "river_name": name,
                                        }
                                    )
                                for i, r in enumerate(indirect_rows_raw, start=1):
                                    name = str(r.get("river_name") or "").strip()
                                    if not name:
                                        continue
                                    dist_km = _round2(r.get("distance_km"))
                                    if dist_km is not None and dist_km > max_distance_km:
                                        continue
                                    eta_txt = r.get("estimated_propagation_time")
                                    if eta_txt in (None, "", "None"):
                                        eta_txt = _estimate_avg_propagation_human(dist_km)
                                    river_names.append(name)
                                    indirect_rows.append(
                                        {
                                            "feature_id": str(r.get("feature_id") or r.get("locate_id") or f"indirect_{i}"),
                                            "river_name": name,
                                            "distance_km": dist_km,
                                            "estimated_propagation_time": eta_txt,
                                        }
                                    )
                                river_names = [n for n in dict.fromkeys(river_names) if n]

                                if river_names:
                                    pg = load_pg_for_registry(_CONFIG_PATH)
                                    sql_text = build_river_names_geojson_wms_sql(pg, river_names)
                                    sql_id = register_wms_sql_text(_CONFIG_PATH, sql_text)
                                    payload_sql = build_response_payload(
                                        scenario="emergency_rivers",
                                        geojson_obj=feature_collection([]),
                                        tables=[
                                            ScenarioTable(
                                                table_key="direct_rivers",
                                                table_name="直接影响河流",
                                                columns=[
                                                    TableColumn("river_name", "河流名称"),
                                                    TableColumn("feature_id", "定位"),
                                                ],
                                                rows=direct_rows,
                                            ),
                                            ScenarioTable(
                                                table_key="indirect_rivers",
                                                table_name="间接影响河流",
                                                columns=[
                                                    TableColumn("river_name", "河流名称"),
                                                    TableColumn("distance_km", "距暴雨河流距离(km)"),
                                                    TableColumn("estimated_propagation_time", "预计传播时间"),
                                                ],
                                                rows=indirect_rows,
                                            ),
                                        ],
                                        query_time=_now_iso(),
                                        map_sql_id=sql_id,
                                    )
                                    payload_sql["data"]["meta"]["times"] = times
                                    payload_sql["data"]["meta"]["direct_source"] = "local_scene_export"
                                    payload_sql["data"]["meta"]["fallback_mode"] = "wms_sql_compact"
                                    payload_sql["data"]["meta"]["river_infer_mode"] = river_infer_mode
                                    payload_sql["data"]["meta"]["max_distance_km"] = max_distance_km
                                    payload_sql["data"]["meta"]["station_buffer_km"] = station_buffer_km
                                    _json_response(self, 200, payload_sql)
                                    return
                            except Exception:
                                pass
                        _json_response(self, 200, local_payload)
                        return
                        fallback_seed = river_name or str(_as_scalar(params.get("fallback_river_name")) or "").strip()
                        if fallback_seed:
                            direct_names = [fallback_seed]
                    if not direct_names:
                        _json_response(
                            self,
                            200,
                            {
                                "success": True,
                                "message": "ok",
                                "data": {
                                    "scenario": "emergency_rivers",
                                    "map_geojson": json.dumps(feature_collection([]), ensure_ascii=False),
                                    "tables": [
                                        {
                                            "table_key": "direct_rivers",
                                            "table_name": "直接影响河流",
                                            "columns": [
                                                {"key": "river_name", "label": "河流名称"},
                                                {"key": "feature_id", "label": "定位"},
                                            ],
                                            "rows": [],
                                        },
                                        {
                                            "table_key": "indirect_rivers",
                                            "table_name": "间接影响河流",
                                            "columns": [
                                                {"key": "river_name", "label": "河流名称"},
                                                {"key": "distance_km", "label": "距暴雨河流距离(km)"},
                                                {"key": "estimated_propagation_time", "label": "预计传播时间"},
                                            ],
                                            "rows": [],
                                        },
                                    ],
                                    "query_time": _now_iso(),
                                    "meta": {
                                        "judgment": judgment,
                                        "times": times,
                                        "direct_source": "rainfall_zone_intersection",
                                        "river_infer_mode": river_infer_mode,
                                        "fallback_used": fallback_used,
                                        "note": "未识别到可用于映射河流的降雨落区。",
                                    },
                                },
                            },
                        )
                        return

                # 再从直接影响河流推导“间接影响河流”，并按最小距离去重
                indirect_agg: Dict[str, Dict[str, Any]] = {}
                for seed in direct_names:
                    try:
                        impact_seed = estimate_river_impact_time_core(
                            river_name=seed,
                            max_rivers=max_rivers,
                            max_distance_km=max_distance_km,
                        )
                    except Exception:
                        continue
                    for item in (impact_seed.get("downstream", []) or []):
                        dn = str(item.get("downstream_river") or "").strip()
                        if not dn or dn in direct_names:
                            continue
                        dist = _round2(item.get("impact_distance_km"))
                        t_avg = (
                            (((item.get("time_estimates") or {}).get("avg") or {}).get("duration") or {}).get("human")
                            or (item.get("descriptions") or {}).get("avg")
                            or _estimate_avg_propagation_human(dist)
                        )
                        old = indirect_agg.get(dn)
                        old_dist = _nullable_float(old.get("distance_km")) if old else None
                        new_dist = _nullable_float(dist)
                        if old is None or (new_dist is not None and (old_dist is None or new_dist < old_dist)):
                            indirect_agg[dn] = {
                                "river_name": dn,
                                "distance_km": dist,
                                "estimated_propagation_time": t_avg,
                            }

                downstream_names = list(indirect_agg.keys())
                if _wants_wms_sql(params):
                    try:
                        pg = load_pg_for_registry(_CONFIG_PATH)
                        names_sql = direct_names + downstream_names
                        sql_text = build_river_names_geojson_wms_sql(pg, names_sql)
                        sql_id = register_wms_sql_text(_CONFIG_PATH, sql_text)
                        direct_rows_wms = []
                        for idx, name in enumerate(direct_names, start=1):
                            direct_rows_wms.append({"feature_id": f"direct_{idx}", "river_name": name})
                        indirect_rows_wms: List[Dict[str, Any]] = []
                        for idx, name in enumerate(downstream_names, start=1):
                            fid = f"indirect_{idx}"
                            row_i = indirect_agg.get(name) or {}
                            indirect_rows_wms.append(
                                {
                                    "feature_id": fid,
                                    "river_name": name,
                                    "distance_km": row_i.get("distance_km"),
                                    "estimated_propagation_time": row_i.get("estimated_propagation_time"),
                                }
                            )
                        payload = build_response_payload(
                            scenario="emergency_rivers",
                            geojson_obj=feature_collection([]),
                            tables=[
                                ScenarioTable(
                                    table_key="direct_rivers",
                                    table_name="直接影响河流",
                                    columns=[
                                        TableColumn("river_name", "河流名称"),
                                        TableColumn("feature_id", "定位"),
                                    ],
                                    rows=direct_rows_wms,
                                ),
                                ScenarioTable(
                                    table_key="indirect_rivers",
                                    table_name="间接影响河流",
                                    columns=[
                                        TableColumn("river_name", "河流名称"),
                                        TableColumn("distance_km", "距暴雨河流距离(km)"),
                                        TableColumn("estimated_propagation_time", "预计传播时间"),
                                    ],
                                    rows=indirect_rows_wms,
                                ),
                            ],
                            query_time=_now_iso(),
                            map_sql_id=sql_id,
                        )
                        payload["data"]["meta"]["judgment"] = judgment
                        payload["data"]["meta"]["times"] = times
                        payload["data"]["meta"]["direct_source"] = "rainfall_zone_intersection"
                        payload["data"]["meta"]["river_infer_mode"] = river_infer_mode
                        payload["data"]["meta"]["fallback_used"] = fallback_used
                        payload["data"]["meta"]["max_distance_km"] = max_distance_km
                        payload["data"]["meta"]["station_buffer_km"] = station_buffer_km
                        _json_response(self, 200, payload)
                        return
                    except Exception as exc:
                        _json_response(self, 500, {"message": f"WMS SQL 登记失败: {exc}"})
                        return
                geom_map = {}
                try:
                    geom_map = _query_river_geometries_by_names(direct_names + downstream_names)
                except Exception:
                    geom_map = {}
                features = []
                direct_rows = []
                for idx, name in enumerate(direct_names, start=1):
                    fid = f"direct_{idx}"
                    geom_item = geom_map.get(name) or {}
                    features.append(
                        build_feature(
                            feature_id=fid,
                            geometry=geom_item.get("geometry") or {"type": "GeometryCollection", "geometries": []},
                            style_type="direct_impact_river",
                            props={
                                "river_name": name,
                                "impact_type": "direct",
                                "river_length_km": geom_item.get("river_length_km"),
                            },
                        )
                    )
                    direct_rows.append({"feature_id": fid, "river_name": name})
                indirect_rows: List[Dict[str, Any]] = []
                for idx, name in enumerate(downstream_names, start=1):
                    fid = f"indirect_{idx}"
                    dn_geom_item = geom_map.get(name) or {}
                    row_i = indirect_agg.get(name) or {}
                    features.append(
                        build_feature(
                            feature_id=fid,
                            geometry=dn_geom_item.get("geometry") or {"type": "GeometryCollection", "geometries": []},
                            style_type="indirect_impact_river",
                            props={
                                "river_name": name,
                                "impact_type": "indirect",
                                "river_length_km": _round2(dn_geom_item.get("river_length_km")),
                                "distance_km": row_i.get("distance_km"),
                                "estimated_propagation_time": row_i.get("estimated_propagation_time"),
                            },
                        )
                    )
                    indirect_rows.append(
                        {
                            "feature_id": fid,
                            "river_name": name,
                            "distance_km": row_i.get("distance_km"),
                            "estimated_propagation_time": row_i.get("estimated_propagation_time"),
                        }
                    )
                payload = build_response_payload(
                    scenario="emergency_rivers",
                    geojson_obj=feature_collection(features),
                    tables=[
                        ScenarioTable(
                            table_key="direct_rivers",
                            table_name="直接影响河流",
                            columns=[
                                TableColumn("river_name", "河流名称"),
                                TableColumn("feature_id", "定位"),
                            ],
                            rows=direct_rows,
                        ),
                        ScenarioTable(
                            table_key="indirect_rivers",
                            table_name="间接影响河流",
                            columns=[
                                TableColumn("river_name", "河流名称"),
                                TableColumn("distance_km", "距暴雨河流距离(km)"),
                                TableColumn("estimated_propagation_time", "预计传播时间"),
                            ],
                            rows=indirect_rows,
                        ),
                    ],
                    query_time=_now_iso(),
                )
                payload["data"]["meta"]["judgment"] = judgment
                payload["data"]["meta"]["times"] = times
                payload["data"]["meta"]["direct_source"] = "rainfall_zone_intersection"
                payload["data"]["meta"]["river_infer_mode"] = river_infer_mode
                payload["data"]["meta"]["fallback_used"] = fallback_used
                payload["data"]["meta"]["max_distance_km"] = max_distance_km
                payload["data"]["meta"]["station_buffer_km"] = station_buffer_km
                _json_response(self, 200, payload)
                return

            if route in (
                "/scenario/emergency/regions",
                "/scenario/emergency/regions/",
                "/scenario/emergency/admin_regions",
                "/scenario/emergency/admin_regions/",
                "/scenario/emergency/partitions",
                "/scenario/emergency/partitions/",
            ):
                body = _read_json_body(self) if self.command.upper() == "POST" else {}
                params = {**query, **body}
                times = str(_as_scalar(params.get("times")) or "").strip()
                if not times:
                    _json_response(self, 400, {"message": "times 必填"})
                    return
                force_local = _parse_bool(params.get("force_local"), False)
                local_json_path = str(_as_scalar(params.get("local_json_path")) or "").strip() or None
                basin_codes = resolve_emergency_basin_codes(
                    basin_codes=(str(_as_scalar(params.get("basin_codes"))) if params.get("basin_codes") is not None else None),
                    scope=(str(_as_scalar(params.get("scope"))) if params.get("scope") is not None else "haihe"),
                    config_path=_CONFIG_PATH,
                )
                if force_local:
                    slot = _load_local_merged_slot(times, local_json_path=local_json_path)
                    ranking = slot.get("station_ranking") if slot else None
                    if not ranking:
                        _json_response(self, 404, {"message": f"本地JSON未找到 times={times} 的站点明细"})
                        return
                else:
                    try:
                        ranking = station_rainfall_ranking(
                            times=times,
                            basin_codes=basin_codes,
                            sort_by="PRE_24h",
                            allowed_station_levels=(
                                str(_as_scalar(params.get("allowed_station_levels")))
                                if params.get("allowed_station_levels") is not None
                                else "11,12,13,16"
                            ),
                            limit=max(1, min(_parse_int(params.get("limit"), 2000), 2000)),
                            min_mm=float(_as_scalar(params.get("min_mm")) or 0.0),
                        )
                    except Exception:
                        slot = _load_local_merged_slot(times, local_json_path=local_json_path)
                        ranking = slot.get("station_ranking") if slot else None
                        if not ranking:
                            raise
                need_admin_regions = route in (
                    "/scenario/emergency/regions",
                    "/scenario/emergency/regions/",
                    "/scenario/emergency/admin_regions",
                    "/scenario/emergency/admin_regions/",
                )
                need_partitions = route in (
                    "/scenario/emergency/regions",
                    "/scenario/emergency/regions/",
                    "/scenario/emergency/partitions",
                    "/scenario/emergency/partitions/",
                )

                by_county: Dict[str, Dict[str, Any]] = {}
                for row in ranking.get("list", []) or []:
                    county = str(row.get("cnty") or row.get("city") or "").strip()
                    if not county:
                        continue
                    if county not in by_county:
                        by_county[county] = {"name": county, "sum_mm": 0.0, "max_mm": 0.0, "station_count": 0}
                    mm = float(row.get("rainfall_mm") or 0.0)
                    by_county[county]["sum_mm"] += mm
                    by_county[county]["max_mm"] = max(by_county[county]["max_mm"], mm)
                    by_county[county]["station_count"] += 1
                region_rows = sorted(by_county.values(), key=lambda x: x["sum_mm"], reverse=True)
                partition_rows_raw = _query_partition_stats_from_ranking(ranking.get("list", []) or []) if need_partitions else []
                if _wants_wms_sql(params):
                    try:
                        pg = load_pg_for_registry(_CONFIG_PATH)
                        sql_ids: Dict[str, int] = {}
                        if need_admin_regions:
                            rnames = [str(x.get("name") or "").strip() for x in region_rows if str(x.get("name") or "").strip()]
                            if not rnames:
                                _json_response(self, 400, {"message": "无行政区聚合结果，无法生成边界 WMS SQL"})
                                return
                            admin_sql = build_admin_boundaries_by_names_wms_sql(pg, rnames)
                            sql_ids["admin_regions"] = register_wms_sql_text(_CONFIG_PATH, admin_sql)
                        if need_partitions:
                            station_rows_wms: List[Dict[str, Any]] = []
                            for r in ranking.get("list", []) or []:
                                lon = _nullable_float(r.get("lon"))
                                lat = _nullable_float(r.get("lat"))
                                if lon is None or lat is None:
                                    continue
                                station_rows_wms.append(
                                    {
                                        "station_id": str(r.get("station_id") or ""),
                                        "lon": lon,
                                        "lat": lat,
                                        "rainfall_mm": float(r.get("rainfall_mm") or 0.0),
                                    }
                                )
                            if not station_rows_wms:
                                _json_response(self, 400, {"message": "无有效站点坐标，无法生成分区 WMS SQL"})
                                return
                            part_sql = build_partition_stats_wms_sql_for_rows(pg, station_rows_wms)
                            sql_ids["partitions"] = register_wms_sql_text(_CONFIG_PATH, part_sql)
                        if not sql_ids:
                            _json_response(self, 400, {"message": "当前路由未包含 admin_regions / partitions 图层"})
                            return
                        table_rows_wms: List[Dict[str, Any]] = []
                        if need_admin_regions:
                            for idx, item in enumerate(region_rows, start=1):
                                rn = item["name"]
                                table_rows_wms.append(
                                    {
                                        "feature_id": f"region_{idx}",
                                        "region_name": rn,
                                        "station_precip_sum_mm": round(float(item["sum_mm"]), 2),
                                        "station_precip_max_mm": round(float(item["max_mm"]), 2),
                                        "acc_precip_mm": round(float(item["sum_mm"]), 2),
                                        "max_precip_mm": round(float(item["max_mm"]), 2),
                                        "station_count": int(item["station_count"]),
                                        "time_compact": times,
                                        "time_display": _format_time_compact(times),
                                    }
                                )
                        partition_rows_wms: List[Dict[str, Any]] = []
                        if need_partitions:
                            for idx, p in enumerate(partition_rows_raw or [], start=1):
                                partition_rows_wms.append(
                                    {
                                        "feature_id": f"partition_{idx}",
                                        "partition_layer": p.get("partition_layer"),
                                        "partition_code": p.get("partition_code"),
                                        "partition_name": p.get("partition_name"),
                                        "station_precip_sum_mm": _round2(p.get("station_precip_sum_mm")),
                                        "station_precip_max_mm": _round2(p.get("station_precip_max_mm")),
                                        "station_count": int(p.get("station_count") or 0),
                                        "time_compact": times,
                                        "time_display": _format_time_compact(times),
                                    }
                                )
                        scenario_name_wms = (
                            "emergency_admin_regions"
                            if route in ("/scenario/emergency/admin_regions", "/scenario/emergency/admin_regions/")
                            else (
                                "emergency_partitions"
                                if route in ("/scenario/emergency/partitions", "/scenario/emergency/partitions/")
                                else "emergency_regions"
                            )
                        )
                        tables_wms: List[ScenarioTable] = []
                        if need_admin_regions:
                            tables_wms.append(
                                ScenarioTable(
                                    table_key="emergency_regions",
                                    table_name="应急影响分区",
                                    columns=[
                                        TableColumn("region_name", "行政区"),
                                        TableColumn("station_precip_sum_mm", "站点累计降水和(mm)"),
                                        TableColumn("station_precip_max_mm", "站点最大降水(mm)"),
                                        TableColumn("station_count", "站点数"),
                                        TableColumn("time_display", "时段"),
                                        TableColumn("time_compact", "时次"),
                                        TableColumn("feature_id", "定位ID"),
                                    ],
                                    rows=table_rows_wms,
                                )
                            )
                        if need_partitions:
                            tables_wms.append(
                                ScenarioTable(
                                    table_key="emergency_partitions",
                                    table_name="应急影响分区（9/11）",
                                    columns=[
                                        TableColumn("partition_layer", "分区图层"),
                                        TableColumn("partition_code", "分区代码"),
                                        TableColumn("partition_name", "分区名称"),
                                        TableColumn("station_precip_sum_mm", "站点累计降水和(mm)"),
                                        TableColumn("station_precip_max_mm", "站点最大降水(mm)"),
                                        TableColumn("station_count", "站点数"),
                                        TableColumn("time_display", "时段"),
                                        TableColumn("time_compact", "时次"),
                                        TableColumn("feature_id", "定位ID"),
                                    ],
                                    rows=partition_rows_wms,
                                )
                            )
                        if len(sql_ids) == 1:
                            only_id = next(iter(sql_ids.values()))
                            payload = build_response_payload(
                                scenario=scenario_name_wms,
                                geojson_obj=feature_collection([]),
                                tables=tables_wms,
                                query_time=_now_iso(),
                                map_sql_id=only_id,
                            )
                        else:
                            payload = build_response_payload(
                                scenario=scenario_name_wms,
                                geojson_obj=feature_collection([]),
                                tables=tables_wms,
                                query_time=_now_iso(),
                                map_sql_ids=sql_ids,
                            )
                        payload["data"]["meta"]["boundary_query_ok"] = True
                        if need_admin_regions:
                            payload["data"]["meta"]["admin_region_count_table"] = len(table_rows_wms)
                            payload["data"]["meta"]["admin_region_count_geojson"] = 0
                            payload["data"]["meta"]["admin_region_count_skipped_no_geometry"] = 0
                        if need_partitions:
                            payload["data"]["meta"]["partition_count_table"] = len(partition_rows_wms)
                            payload["data"]["meta"]["partition_count_geojson"] = 0
                        _json_response(self, 200, payload)
                        return
                    except Exception as exc:
                        _json_response(self, 500, {"message": f"WMS SQL 登记失败: {exc}"})
                        return
                features: List[Dict[str, Any]] = []
                boundary_error = None
                region_boundary_map: Dict[str, Dict[str, Any]] = {}
                if need_admin_regions:
                    region_name_list = [str(x.get("name") or "").strip() for x in region_rows if str(x.get("name") or "").strip()]
                    try:
                        region_boundary_map = _query_admin_boundaries_by_region_names(region_name_list)
                    except Exception as exc:
                        region_boundary_map = {}
                        boundary_error = str(exc)
                table_rows = []
                skipped_region_names: List[str] = []
                region_feature_count = 0
                for item in region_rows:
                    region_name = item["name"]
                    if need_admin_regions:
                        boundary = region_boundary_map.get(region_name) or {}
                        geom = boundary.get("geometry")
                        if geom is None:
                            skipped_region_names.append(region_name)
                            continue
                        region_feature_count += 1
                        fid = f"region_{region_feature_count}"
                        features.append(
                            build_feature(
                                feature_id=fid,
                                geometry=geom,
                                style_type="district_impact_area",
                                props={
                                    "name": boundary.get("name") or region_name,
                                    "adcode": boundary.get("adcode"),
                                },
                            )
                        )
                    else:
                        fid = f"region_{len(table_rows) + 1}"
                    table_rows.append(
                        {
                            "feature_id": fid,
                            "region_name": region_name,
                            "station_precip_sum_mm": round(float(item["sum_mm"]), 2),
                            "station_precip_max_mm": round(float(item["max_mm"]), 2),
                            "acc_precip_mm": round(float(item["sum_mm"]), 2),
                            "max_precip_mm": round(float(item["max_mm"]), 2),
                            "station_count": int(item["station_count"]),
                            "time_compact": times,
                            "time_display": _format_time_compact(times),
                        }
                    )
                partition_features: List[Dict[str, Any]] = []
                partition_rows: List[Dict[str, Any]] = []
                partition_feature_count = 0
                for p in partition_rows_raw:
                    g = p.get("geom_json")
                    if not g:
                        continue
                    try:
                        geom = json.loads(g)
                    except Exception:
                        continue
                    partition_feature_count += 1
                    fid = f"partition_{partition_feature_count}"
                    partition_features.append(
                        build_feature(
                            feature_id=fid,
                            geometry=geom,
                            style_type="district_impact_area",
                            props={
                                "partition_layer": p.get("partition_layer"),
                                "partition_code": p.get("partition_code"),
                                "partition_name": p.get("partition_name"),
                            },
                        )
                    )
                    partition_rows.append(
                        {
                            "feature_id": fid,
                            "partition_layer": p.get("partition_layer"),
                            "partition_code": p.get("partition_code"),
                            "partition_name": p.get("partition_name"),
                            "station_precip_sum_mm": _round2(p.get("station_precip_sum_mm")),
                            "station_precip_max_mm": _round2(p.get("station_precip_max_mm")),
                            "station_count": int(p.get("station_count") or 0),
                            "time_compact": times,
                            "time_display": _format_time_compact(times),
                        }
                    )
                if need_partitions:
                    features.extend(partition_features)
                scenario_name = (
                    "emergency_admin_regions"
                    if route in ("/scenario/emergency/admin_regions", "/scenario/emergency/admin_regions/")
                    else (
                        "emergency_partitions"
                        if route in ("/scenario/emergency/partitions", "/scenario/emergency/partitions/")
                        else "emergency_regions"
                    )
                )
                tables: List[ScenarioTable] = []
                if need_admin_regions:
                    tables.append(
                        ScenarioTable(
                            table_key="emergency_regions",
                            table_name="应急影响分区",
                            columns=[
                                TableColumn("region_name", "行政区"),
                                TableColumn("station_precip_sum_mm", "站点累计降水和(mm)"),
                                TableColumn("station_precip_max_mm", "站点最大降水(mm)"),
                                TableColumn("station_count", "站点数"),
                                TableColumn("time_display", "时段"),
                                TableColumn("time_compact", "时次"),
                                TableColumn("feature_id", "定位ID"),
                            ],
                            rows=table_rows,
                        )
                    )
                if need_partitions:
                    tables.append(
                        ScenarioTable(
                            table_key="emergency_partitions",
                            table_name="应急影响分区（9/11）",
                            columns=[
                                TableColumn("partition_layer", "分区图层"),
                                TableColumn("partition_code", "分区代码"),
                                TableColumn("partition_name", "分区名称"),
                                TableColumn("station_precip_sum_mm", "站点累计降水和(mm)"),
                                TableColumn("station_precip_max_mm", "站点最大降水(mm)"),
                                TableColumn("station_count", "站点数"),
                                TableColumn("time_display", "时段"),
                                TableColumn("time_compact", "时次"),
                                TableColumn("feature_id", "定位ID"),
                            ],
                            rows=partition_rows,
                        )
                    )
                payload = build_response_payload(
                    scenario=scenario_name,
                    geojson_obj=feature_collection(features),
                    tables=tables,
                    query_time=_now_iso(),
                )
                payload["data"]["meta"]["boundary_query_ok"] = boundary_error is None
                if boundary_error:
                    payload["data"]["meta"]["boundary_query_error"] = boundary_error
                if need_admin_regions:
                    payload["data"]["meta"]["admin_region_count_table"] = len(table_rows)
                    payload["data"]["meta"]["admin_region_count_geojson"] = region_feature_count
                    payload["data"]["meta"]["admin_region_count_skipped_no_geometry"] = len(skipped_region_names)
                if need_partitions:
                    payload["data"]["meta"]["partition_count_table"] = len(partition_rows)
                    payload["data"]["meta"]["partition_count_geojson"] = partition_feature_count
                _json_response(self, 200, payload)
                return

            if route in ("/scenario/emergency/zone256-rivers", "/scenario/emergency/zone256-rivers/"):
                body = _read_json_body(self) if self.command.upper() == "POST" else {}
                params = {**query, **body}
                times = str(_as_scalar(params.get("times")) or "").strip()
                if not times:
                    _json_response(self, 400, {"message": "times 必填"})
                    return
                force_local = _parse_bool(params.get("force_local"), False)
                local_json_path = str(_as_scalar(params.get("local_json_path")) or "").strip() or None
                basin_codes = resolve_emergency_basin_codes(
                    basin_codes=(str(_as_scalar(params.get("basin_codes"))) if params.get("basin_codes") is not None else None),
                    scope=(str(_as_scalar(params.get("scope"))) if params.get("scope") is not None else "haihe"),
                    config_path=_CONFIG_PATH,
                )
                if force_local:
                    slot = _load_local_merged_slot(times, local_json_path=local_json_path)
                    ranking = slot.get("station_ranking") if slot else None
                    if not ranking:
                        _json_response(self, 404, {"message": f"本地JSON未找到 times={times} 的站点明细"})
                        return
                else:
                    try:
                        ranking = station_rainfall_ranking(
                            times=times,
                            basin_codes=basin_codes,
                            sort_by="PRE_24h",
                            allowed_station_levels=(
                                str(_as_scalar(params.get("allowed_station_levels")))
                                if params.get("allowed_station_levels") is not None
                                else "11,12,13,16"
                            ),
                            limit=max(1, min(_parse_int(params.get("limit"), 2000), 2000)),
                            min_mm=float(_as_scalar(params.get("min_mm")) or 0.0),
                        )
                    except Exception:
                        slot = _load_local_merged_slot(times, local_json_path=local_json_path)
                        ranking = slot.get("station_ranking") if slot else None
                        if not ranking:
                            # times 常来自“当前时刻”，与 mock 数据时次不一致时回退到最新本地槽位
                            slot_latest = _load_latest_local_merged_slot(local_json_path=local_json_path)
                            ranking = slot_latest.get("station_ranking") if slot_latest else None
                            if ranking:
                                times = str(slot_latest.get("times") or times).strip()
                        if not ranking:
                            zone_payload = _load_local_zone256_payload(times, local_json_path=local_json_path)
                            if zone_payload:
                                _json_response(self, 200, zone_payload)
                                return
                            # 再兜底：直接返回本地 zone256 第一条可用 mock 槽位
                            zone_payload_latest = _load_local_zone256_payload("", local_json_path=local_json_path)
                            if zone_payload_latest:
                                _json_response(self, 200, zone_payload_latest)
                                return
                            _json_response(
                                self,
                                502,
                                {"message": f"MUSIC 站点排行不可用且未找到本地 merged 数据 times={times}"},
                            )
                            return
                try:
                    max_rivers = max(1, min(_parse_int(params.get("max_rivers"), 800), 5000))
                except Exception:
                    max_rivers = 800
                river_rows = _query_zone256_rivers_from_ranking(
                    ranking.get("list", []) or [],
                    max_rivers=max_rivers,
                )
                if _wants_wms_sql(params):
                    try:
                        pg = load_pg_for_registry(_CONFIG_PATH)
                        station_rows_wms: List[Dict[str, Any]] = []
                        for r in ranking.get("list", []) or []:
                            lon = _nullable_float(r.get("lon"))
                            lat = _nullable_float(r.get("lat"))
                            if lon is None or lat is None:
                                continue
                            station_rows_wms.append(
                                {
                                    "station_id": str(r.get("station_id") or ""),
                                    "lon": lon,
                                    "lat": lat,
                                    "rainfall_mm": float(r.get("rainfall_mm") or 0.0),
                                }
                            )
                        if not station_rows_wms:
                            _json_response(self, 400, {"message": "无有效站点坐标，无法生成 246 分区河系 WMS SQL"})
                            return
                        sql_text = build_zone256_rivers_wms_sql(pg, station_rows_wms)
                        sql_id = register_wms_sql_text(_CONFIG_PATH, sql_text)
                        table_rows_wms: List[Dict[str, Any]] = []
                        for idx, rw in enumerate(river_rows or [], start=1):
                            table_rows_wms.append(
                                {
                                    "feature_id": f"z256_river_{idx}",
                                    "river_name": rw.get("river_name"),
                                    "river_length_km": _round2(rw.get("river_length_km")),
                                    "time_compact": times,
                                    "time_display": _format_time_compact(times),
                                }
                            )
                        payload = build_response_payload(
                            scenario="emergency_zone256_rivers",
                            geojson_obj=feature_collection([]),
                            tables=[
                                ScenarioTable(
                                    table_key="zone256_rivers",
                                    table_name="降雨落区 246 分区影响河系",
                                    columns=[
                                        TableColumn("river_name", "河流名称"),
                                        TableColumn("river_length_km", "河长(km)"),
                                        TableColumn("time_display", "时段"),
                                        TableColumn("time_compact", "时次"),
                                        TableColumn("feature_id", "定位ID"),
                                    ],
                                    rows=table_rows_wms,
                                )
                            ],
                            query_time=_now_iso(),
                            map_sql_id=sql_id,
                        )
                        payload["data"]["meta"]["zone256_table"] = pg.get("zone256_table")
                        _json_response(self, 200, payload)
                        return
                    except Exception as exc:
                        _json_response(self, 500, {"message": f"246 分区河系 WMS 登记失败: {exc}"})
                        return
                features: List[Dict[str, Any]] = []
                table_rows: List[Dict[str, Any]] = []
                for idx, rw in enumerate(river_rows or [], start=1):
                    g = rw.get("geom_json")
                    if not g:
                        continue
                    try:
                        geom = json.loads(g)
                    except Exception:
                        continue
                    fid = f"z256_river_{idx}"
                    features.append(
                        build_feature(
                            feature_id=fid,
                            geometry=geom,
                            style_type="zone256_impact_river",
                            props={
                                "river_name": rw.get("river_name"),
                                "river_length_km": _round2(rw.get("river_length_km")),
                            },
                        )
                    )
                    table_rows.append(
                        {
                            "feature_id": fid,
                            "river_name": rw.get("river_name"),
                            "river_length_km": _round2(rw.get("river_length_km")),
                            "time_compact": times,
                            "time_display": _format_time_compact(times),
                        }
                    )
                payload = build_response_payload(
                    scenario="emergency_zone256_rivers",
                    geojson_obj=feature_collection(features),
                    tables=[
                        ScenarioTable(
                            table_key="zone256_rivers",
                            table_name="降雨落区 246 分区影响河系",
                            columns=[
                                TableColumn("river_name", "河流名称"),
                                TableColumn("river_length_km", "河长(km)"),
                                TableColumn("time_display", "时段"),
                                TableColumn("time_compact", "时次"),
                                TableColumn("feature_id", "定位ID"),
                            ],
                            rows=table_rows,
                        )
                    ],
                    query_time=_now_iso(),
                )
                pg_meta = _load_pg_config(_CONFIG_PATH)
                payload["data"]["meta"]["zone256_table"] = pg_meta.get("zone256_table")
                payload["data"]["meta"]["river_row_count"] = len(table_rows)
                _json_response(self, 200, payload)
                return

            # 应急预案主表 hh_emergency_event：列表/详情/归档（供前端表格：名称、状态、起止时间、等级）
            if route in ("/emergency/management/events", "/emergency/management/events/"):
                body = _read_json_body(self) if self.command.upper() == "POST" else {}
                params = {**query, **body}
                _json_response(self, 200, _list_management_events(params))
                return

            if route in ("/emergency/management/timeline", "/emergency/management/timeline/"):
                body = _read_json_body(self) if self.command.upper() == "POST" else {}
                params = {**query, **body}
                _json_response(self, 200, _list_management_timeline(params))
                return

            # 四象限应急响应列表 + 时间轴事件（含 timeline_phase）+ 问答区默认文案（供前端 Ajax 轮询）
            if route in ("/emergency/management/response-board", "/emergency/management/response-board/"):
                body = _read_json_body(self) if self.command.upper() == "POST" else {}
                params = {**query, **body}
                params["_trace_id"] = str(getattr(self, "_trace_id", "") or "")
                params["_allow_mutation"] = self.command.upper() == "POST"
                rb_params, set_baseline_cookie = _prepare_response_board_request(self, params)
                extra_h: Dict[str, str] = {}
                if set_baseline_cookie:
                    extra_h["Set-Cookie"] = set_baseline_cookie
                _json_response(self, 200, _list_management_response_board(rb_params), extra_headers=extra_h if extra_h else None)
                return

            if route in ("/emergency/management/workflow/publish-ack", "/emergency/management/workflow/publish-ack/"):
                if self.command.upper() != "POST":
                    _json_response(self, 405, {"message": "请使用 POST"})
                    return
                body = _read_json_body(self)
                ident = _as_scalar(body.get("event_id"))
                if ident is None or str(ident).strip() == "":
                    ident = _as_scalar(body.get("event_code"))
                if ident is None or str(ident).strip() == "":
                    _json_response(self, 400, {"message": "需提供 event_id 或 event_code"})
                    return
                if body.get("published") is None:
                    _json_response(self, 400, {"message": "需提供 published: true/false"})
                    return
                published = _parse_bool(body.get("published"), False)
                note_raw = _as_scalar(body.get("note"))
                updated = _MANAGEMENT_STORE.set_workflow_publish_ack(
                    str(ident).strip(),
                    published=published,
                    note=str(note_raw) if note_raw is not None else None,
                )
                if not updated:
                    _json_response(self, 404, {"message": f"事件不存在: {ident}"})
                    return
                _json_response(self, 200, {"ok": True, "event": updated})
                return

            if route in ("/emergency/gis/geoserver-layers", "/emergency/gis/geoserver-layers/"):
                _json_response(self, 200, _load_geoserver_catalog())
                return

            if route in (
                "/emergency/gis/forecast-precip-intensity-stats",
                "/emergency/gis/forecast-precip-intensity-stats/",
            ):
                body = _read_json_body(self) if self.command.upper() == "POST" else {}
                params = {**query, **body}
                code, payload = _forecast_precip_intensity_stats_http(params)
                _json_response(self, code, payload)
                return

            if route.startswith("/emergency/management/events/"):
                mgmt_tail = route[len("/emergency/management/events/"):].strip("/")
                if not mgmt_tail:
                    _json_response(self, 400, {"message": "id 或 event_code 必填"})
                    return
                if mgmt_tail.endswith("/terminate"):
                    ident = mgmt_tail[: -len("/terminate")].strip("/")
                    if not ident:
                        _json_response(self, 400, {"message": "id 或 event_code 必填"})
                        return
                    if self.command.upper() != "POST":
                        _json_response(self, 405, {"message": "请使用 POST 归档"})
                        return
                    body = _read_json_body(self)
                    end_t = body.get("end_time")
                    new_status = body.get("status") or "archived"
                    updated = _MANAGEMENT_STORE.terminate_event(
                        ident,
                        end_time=str(end_t) if end_t is not None else None,
                        status=str(new_status),
                    )
                    if not updated:
                        _json_response(self, 404, {"message": f"事件不存在: {ident}"})
                        return
                    _json_response(self, 200, {"ok": True, "event": updated})
                    return

                detail = _MANAGEMENT_STORE.get_by_id_or_code(mgmt_tail)
                if not detail:
                    _json_response(self, 404, {"message": f"事件不存在: {mgmt_tail}"})
                    return
                _json_response(self, 200, detail)
                return

            # 站点降雨量排序：排序 / 站点 / 降水量（MUSIC 流域实况）
            if route in (
                "/emergency/rainfall/station-ranking",
                "/emergency/rainfall/station-ranking/",
            ):
                body = _read_json_body(self) if self.command.upper() == "POST" else {}
                params = {**query, **body}
                times = _as_scalar(params.get("times"))
                if not times:
                    _json_response(self, 400, {"message": "times 必填，例如 20250723080000"})
                    return
                try:
                    min_raw = _as_scalar(params.get("min_mm"))
                    min_mm = float(min_raw) if min_raw not in (None, "") else 0.0
                except Exception:
                    min_mm = 0.0
                try:
                    _scope, basin_codes = _resolve_scope_and_basin(params)
                    payload = station_rainfall_ranking(
                        times=str(times),
                        basin_codes=basin_codes,
                        sort_by=str(_as_scalar(params.get("sort_by")) or "PRE_24h"),
                        allowed_station_levels=(
                            str(_as_scalar(params.get("allowed_station_levels")))
                            if params.get("allowed_station_levels") is not None
                            else "11,12,13,16"
                        ),
                        limit=_parse_int(params.get("limit"), 200),
                        min_mm=min_mm,
                    )
                except ValueError as e:
                    _json_response(self, 400, {"message": str(e)})
                    return
                except BusinessException as e:
                    _json_response(self, 503, {"message": str(e)})
                    return
                except MusicApiError as e:
                    _json_response(self, 502, {"message": str(e)})
                    return
                _json_response(self, 200, payload)
                return

            if route in (
                "/emergency/rainfall/timeline",
                "/emergency/rainfall/timeline/",
            ):
                body = _read_json_body(self) if self.command.upper() == "POST" else {}
                params = {**query, **body}
                force_local = _parse_bool(params.get("force_local"), False)
                local_json_path = str(_as_scalar(params.get("local_json_path")) or "").strip() or None
                if force_local:
                    times = str(_as_scalar(params.get("times")) or "").strip()
                    if not times:
                        _json_response(self, 400, {"message": "times 必填，例如 20250723080000"})
                        return
                    payload = _load_local_rainfall_timeline_payload(times, local_json_path=local_json_path)
                    if not payload:
                        _json_response(self, 404, {"message": f"本地JSON未找到 times={times} 的 rainfall_timeline"})
                        return
                    _json_response(self, 200, payload)
                    return
                try:
                    payload = _build_rainfall_timeline_list(params)
                except ValueError as e:
                    _json_response(self, 400, {"message": str(e)})
                    return
                except BusinessException as e:
                    _json_response(self, 503, {"message": str(e)})
                    return
                except MusicApiError as e:
                    _json_response(self, 502, {"message": str(e)})
                    return
                _json_response(self, 200, payload)
                return

            # 河流影响时间：源河 -> 下游河 的沿程距离与三档流速时间估算
            if route in (
                "/emergency/river/impact-time",
                "/emergency/river/impact-time/",
            ):
                body = _read_json_body(self) if self.command.upper() == "POST" else {}
                params = {**query, **body}
                river_name = _as_scalar(params.get("river_name"))
                if not river_name:
                    _json_response(self, 400, {"message": "river_name 必填"})
                    return
                target_downstream_river = _as_scalar(params.get("target_downstream_river"))
                max_rivers = _parse_int(params.get("max_rivers"), 20)
                try:
                    payload = estimate_river_impact_time_core(
                        river_name=str(river_name).strip(),
                        target_downstream_river=(
                            str(target_downstream_river).strip()
                            if target_downstream_river not in (None, "")
                            else None
                        ),
                        max_rivers=max_rivers,
                    )
                except BusinessException as e:
                    _json_response(self, 400, {"message": str(e)})
                    return
                _json_response(self, 200, payload)
                return

            if route == "/emergency/events":
                body = _read_json_body(self) if self.command.upper() == "POST" else {}
                params = {**query, **body}
                _json_response(self, 200, _list_events(params))
                return

            if route.startswith("/emergency/events/"):
                event_tail = route[len("/emergency/events/"):].strip("/")
                if not event_tail:
                    _json_response(self, 400, {"message": "event_id 必填"})
                    return
                if event_tail.endswith("/products"):
                    event_id = event_tail[:-len("/products")].strip("/")
                    detail = _get_event_detail(event_id)
                    if not detail:
                        _json_response(self, 404, {"message": f"事件不存在: {event_id}"})
                        return
                    _json_response(
                        self,
                        200,
                        {
                            "event_id": event_id,
                            "event_type": detail.get("event_type"),
                            "product_list": detail.get("products", []),
                        },
                    )
                    return

                detail = _get_event_detail(event_tail)
                if not detail:
                    _json_response(self, 404, {"message": f"事件不存在: {event_tail}"})
                    return
                _json_response(self, 200, detail)
                return

            if route.startswith("/emergency/observation/products"):
                sub = route[len("/emergency/observation/products") :].strip("/")
                if sub == "queue" or sub.startswith("queue"):
                    if self.command.upper() == "GET":
                        _json_response(self, 200, observation_product_queue_status())
                        return
                    if self.command.upper() == "POST":
                        body = _read_json_body(self)
                        if "paused" not in body:
                            _json_response(self, 400, {"message": "JSON 需提供 paused: true/false"})
                            return
                        set_observation_product_queue_paused(_parse_bool(body.get("paused"), False))
                        _json_response(self, 200, observation_product_queue_status())
                        return
                    _json_response(self, 405, {"message": "请使用 GET 查询或 POST 设置 paused"})
                    return
                if sub == "manifest" or sub.startswith("manifest"):
                    params = {**query, **(_read_json_body(self) if self.command.upper() == "POST" else {})}
                    c_raw = _as_scalar(params.get("times_compact"))
                    compact = "".join(x for x in str(c_raw) if x.isalnum()) if c_raw else ""
                    if not compact and _as_scalar(params.get("times")):
                        try:
                            compact = times_compact_from_times(str(_as_scalar(params.get("times"))))
                        except Exception:
                            compact = ""
                    if not compact:
                        _json_response(
                            self,
                            400,
                            {"message": "请传 times_compact 或与判定时次一致的 times（仅数字亦可）"},
                        )
                        return
                    man = read_observation_manifest(compact)
                    if not man:
                        _json_response(self, 404, {"message": f"未找到该时次 manifest: {compact}"})
                        return
                    _json_response(self, 200, man)
                    return
                if sub == "png" or sub.startswith("png"):
                    params = {**query, **(_read_json_body(self) if self.command.upper() == "POST" else {})}
                    c_raw = _as_scalar(params.get("times_compact"))
                    compact = "".join(x for x in str(c_raw) if x.isalnum()) if c_raw else ""
                    if not compact and _as_scalar(params.get("times")):
                        try:
                            compact = times_compact_from_times(str(_as_scalar(params.get("times"))))
                        except Exception:
                            compact = ""
                    ah = _parse_int(params.get("accum_hours"), -1)
                    if not compact or ah < 0:
                        _json_response(
                            self,
                            400,
                            {"message": "需要 times_compact（或 times）以及 accum_hours（如 12、24）"},
                        )
                        return
                    png_path = resolve_observation_png_path(compact, ah)
                    if not png_path:
                        _json_response(self, 404, {"message": f"图片不存在: {compact} / {ah}h"})
                        return
                    try:
                        with open(png_path, "rb") as f:
                            raw = f.read()
                    except OSError as e:
                        _json_response(self, 500, {"message": str(e)})
                        return
                    _binary_response(self, 200, raw, "image/png")
                    return
                if sub == "shapefile" or sub.startswith("shapefile"):
                    params = {**query, **(_read_json_body(self) if self.command.upper() == "POST" else {})}
                    c_raw = _as_scalar(params.get("times_compact"))
                    compact = "".join(x for x in str(c_raw) if x.isalnum()) if c_raw else ""
                    if not compact and _as_scalar(params.get("times")):
                        try:
                            compact = times_compact_from_times(str(_as_scalar(params.get("times"))))
                        except Exception:
                            compact = ""
                    ah = _parse_int(params.get("accum_hours"), -1)
                    if not compact or ah < 0:
                        _json_response(
                            self,
                            400,
                            {"message": "需要 times_compact（或 times）以及 accum_hours（如 12、24）"},
                        )
                        return
                    shp_path = resolve_observation_shapefile_path(compact, ah)
                    if not shp_path:
                        _json_response(self, 404, {"message": f"shapefile 不存在: {compact} / {ah}h"})
                        return
                    try:
                        zip_bytes = _zip_observation_shapefile_bytes(shp_path)
                    except OSError as e:
                        _json_response(self, 500, {"message": str(e)})
                        return
                    zip_name = f"haihe_obs_station_{compact}_{ah}h.zip"
                    _binary_response_with_headers(
                        self,
                        200,
                        zip_bytes,
                        "application/zip",
                        extra_headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
                    )
                    return
                if sub == "jobs/range" or sub.startswith("jobs/range/"):
                    if self.command.upper() != "POST":
                        _json_response(self, 405, {"message": "Use POST"})
                        return
                    body = _read_json_body(self)
                    st = _as_scalar(body.get("start"))
                    en = _as_scalar(body.get("end"))
                    if not st or not en:
                        _json_response(
                            self,
                            400,
                            {
                                "message": "start and end required (e.g. 20260309080000 or 10-digit YYYYmmddHH)",
                            },
                        )
                        return
                    step_h = max(1, _parse_int(body.get("step_hours"), 6))
                    max_j = _parse_int(body.get("max_jobs"), 200)
                    if max_j < 1:
                        max_j = 200
                    hours_raw = _as_scalar(body.get("accum_hours"))
                    acc_tuple = None
                    if hours_raw:
                        parts = [x.strip() for x in str(hours_raw).split(",") if x.strip().isdigit()]
                        acc_tuple = tuple(int(x) for x in parts)
                    cfg_opt = body.get("config_path")
                    basin_opt = body.get("basin_vector")
                    draw_opt = body.get("draw_options")
                    draw_dict = draw_opt if isinstance(draw_opt, dict) else None
                    try:
                        jobs = enqueue_observation_product_jobs_range(
                            start=str(st),
                            end=str(en),
                            step_hours=step_h,
                            max_jobs=max_j,
                            accum_hours=acc_tuple,
                            basin_codes=str(body.get("basin_codes", DEFAULT_BASIN_CODES)),
                            allowed_station_levels=str(body.get("allowed_station_levels", "11,12,13,16")),
                            config_path=str(cfg_opt) if cfg_opt is not None else None,
                            basin_vector=str(basin_opt) if basin_opt is not None else None,
                            draw_options=draw_dict,
                        )
                    except ValueError as e:
                        _json_response(self, 400, {"message": str(e)})
                        return
                    _json_response(
                        self,
                        202,
                        {
                            "enqueued": len(jobs),
                            "step_hours": step_h,
                            "max_jobs": max_j,
                            "start": st,
                            "end": en,
                            "jobs": [obs_job_to_dict(j) for j in jobs],
                        },
                    )
                    return
                if sub == "jobs":
                    if self.command.upper() != "POST":
                        _json_response(self, 405, {"message": "请使用 POST 创建任务"})
                        return
                    body = _read_json_body(self)
                    tms = _as_scalar(body.get("times"))
                    if not tms:
                        _json_response(self, 400, {"message": "times 必填（实况时次，如 20250723080000）"})
                        return
                    hours_raw = _as_scalar(body.get("accum_hours"))
                    acc_tuple = None
                    if hours_raw:
                        parts = [x.strip() for x in str(hours_raw).split(",") if x.strip().isdigit()]
                        acc_tuple = tuple(int(x) for x in parts)
                    cfg_opt = body.get("config_path")
                    basin_opt = body.get("basin_vector")
                    draw_opt = body.get("draw_options")
                    draw_dict = draw_opt if isinstance(draw_opt, dict) else None
                    try:
                        job = enqueue_observation_product_job(
                            times=str(tms),
                            accum_hours=acc_tuple,
                            basin_codes=str(body.get("basin_codes", DEFAULT_BASIN_CODES)),
                            allowed_station_levels=str(body.get("allowed_station_levels", "11,12,13,16")),
                            config_path=str(cfg_opt) if cfg_opt is not None else None,
                            basin_vector=str(basin_opt) if basin_opt is not None else None,
                            draw_options=draw_dict,
                        )
                    except ValueError as e:
                        _json_response(self, 400, {"message": str(e)})
                        return
                    _json_response(self, 202, obs_job_to_dict(job))
                    return
                if sub.startswith("jobs/"):
                    job_id = sub[len("jobs/") :].strip("/")
                    if not job_id:
                        _json_response(self, 400, {"message": "job_id 必填"})
                        return
                    j = get_observation_product_job(job_id)
                    if not j:
                        _json_response(self, 404, {"message": f"任务不存在: {job_id}"})
                        return
                    _json_response(self, 200, obs_job_to_dict(j))
                    return
                if sub == "" or sub == "info":
                    _json_response(
                        self,
                        200,
                        {
                            "products_root": observation_product_queue_status().get("products_root"),
                            "default_accum_hours": [1, 6, 12, 24],
                            "endpoints": {
                                "enqueue": "POST /emergency/observation/products/jobs",
                                "enqueue_range": "POST /emergency/observation/products/jobs/range (body: start, end, step_hours?, max_jobs?)",
                                "job_status": "GET /emergency/observation/products/jobs/{job_id}",
                                "queue_pause": "GET/POST /emergency/observation/products/queue",
                                "manifest": "GET /emergency/observation/products/manifest?times_compact=...",
                                "png": "GET /emergency/observation/products/png?times_compact=...&accum_hours=24",
                                "shapefile": "GET /emergency/observation/products/shapefile?times_compact=...&accum_hours=24",
                            },
                            "note": "Gridded field is IDW from basin stations (past N-hour accumulation in titles); queue is independent of EC forecast rendering.",
                        },
                    )
                    return
                _json_response(self, 404, {"message": f"未知子路径: /{sub}"})
                return

            if route == "/emergency/observation":
                body = _read_json_body(self) if self.command.upper() == "POST" else {}
                params = {**query, **body}  # query provides fallback defaults
                times = _as_scalar(params.get("times"))
                if not times:
                    _json_response(self, 400, {"reached": False, "level": None, "message": "times 必填"})
                    return
                times_hour = _to_hour_iso_time(str(times)) or str(times)
                scope_val = str(_as_scalar(params.get("scope")) or "haihe")
                basin_val = str(_as_scalar(params.get("basin_codes"))) if params.get("basin_codes") is not None else None
                station_levels_val = str(_as_scalar(params.get("allowed_station_levels")) or "11,12,13,16")
                include_evidence_val = _parse_bool(params.get("include_evidence"), False)
                neighbor_val = float(_as_scalar(params.get("neighbor_km")) or 50.0)
                sustain_hourly_val = float(_as_scalar(params.get("sustain_hourly_threshold_mm")) or 0.1)

                res = query_haihe_emergency_observation(
                    times=times_hour,
                    basin_codes=basin_val,
                    scope=scope_val,
                    config_path=_CONFIG_PATH,
                    neighbor_km=neighbor_val,
                    sustain_hourly_threshold_mm=sustain_hourly_val,
                    allowed_station_levels=station_levels_val,
                    include_evidence=include_evidence_val,
                )
                event_id = _append_event(
                    event_type="observation",
                    query_time=times_hour,
                    request_params={
                        "times": times_hour,
                        "scope": scope_val,
                        "basin_codes": resolve_emergency_basin_codes(
                            basin_codes=basin_val,
                            scope=scope_val,
                        ),
                        "neighbor_km": neighbor_val,
                        "sustain_hourly_threshold_mm": sustain_hourly_val,
                        "allowed_station_levels": station_levels_val,
                    },
                    response_payload=res,
                    trace_id=str(getattr(self, "_trace_id", "") or ""),
                )
                res = {**res, "event_id": event_id}
                try:
                    _sync_observation_result_to_management_timeline(
                        event_id=event_id,
                        times=times_hour,
                        request_params={
                            "times": times_hour,
                            "scope": scope_val,
                            "basin_codes": resolve_emergency_basin_codes(
                                basin_codes=basin_val,
                                scope=scope_val,
                            ),
                        },
                        observation_result=res,
                        trace_id=str(getattr(self, "_trace_id", "") or ""),
                    )
                except Exception as sync_err:
                    print(f"[emergency_http_server] 同步 observation -> hh_emergency_event 失败: {sync_err}", flush=True)
                try:
                    res["times_compact"] = times_compact_from_times(times_hour)
                except Exception:
                    res["times_compact"] = None
                if _parse_bool(params.get("enqueue_product"), False):
                    try:
                        hours_raw = _as_scalar(params.get("product_accum_hours"))
                        acc_tuple = None
                        if hours_raw:
                            parts = [x.strip() for x in str(hours_raw).split(",") if x.strip().isdigit()]
                            acc_tuple = tuple(int(x) for x in parts)
                        cfg_opt = params.get("config_path")
                        basin_opt = params.get("basin_vector")
                        draw_opt = params.get("draw_options")
                        draw_dict = draw_opt if isinstance(draw_opt, dict) else None
                        pj = enqueue_observation_product_job(
                            times=str(times),
                            accum_hours=acc_tuple,
                            basin_codes=resolve_emergency_basin_codes(
                                basin_codes=(str(_as_scalar(params.get("basin_codes"))) if params.get("basin_codes") is not None else None),
                                scope=(str(_as_scalar(params.get("scope"))) if params.get("scope") is not None else "haihe"),
                            ),
                            allowed_station_levels=str(params.get("allowed_station_levels", "11,12,13,16"))
                            if params.get("allowed_station_levels") is not None
                            else "11,12,13,16",
                            config_path=str(cfg_opt) if cfg_opt is not None else None,
                            basin_vector=str(basin_opt) if basin_opt is not None else None,
                            draw_options=draw_dict,
                        )
                        res["product_job"] = obs_job_to_dict(pj)
                    except ValueError as e:
                        res["product_job_error"] = str(e)
                    except Exception as e:
                        res["product_job_error"] = str(e)
                _json_response(self, 200, res)
                return

            # EC 预报累计降水：各时效（默认 12/24/36/48/60/72h）栅格路径，无文件为 null
            if route in ("/emergency/forecast/precip-files", "/emergency/forecast/precip-files/"):
                body = _read_json_body(self) if self.command.upper() == "POST" else {}
                params = {**query, **body}
                st = _as_scalar(params.get("start_time"))
                if not st:
                    _json_response(self, 400, {"message": "start_time 必填"})
                    return
                ec_path = (
                    str(_as_scalar(params.get("ec_output_path")))
                    if params.get("ec_output_path") is not None
                    else DEFAULT_EC_OUTPUT_PATH
                )
                hours_raw = _as_scalar(params.get("hours"))
                if hours_raw:
                    hrs = tuple(
                        int(x.strip())
                        for x in str(hours_raw).split(",")
                        if x.strip().isdigit()
                    )
                    payload = collect_ec_forecast_precip_files(str(st), ec_path, hrs if hrs else None)
                else:
                    payload = collect_ec_forecast_precip_files(str(st), ec_path)
                _json_response(self, 200, payload)
                return

            # 预报降水产品图：队列生成 12/24/36/48/60/72h PNG + 清单 + 取图
            if route.startswith("/emergency/forecast/products"):
                sub = route[len("/emergency/forecast/products") :].strip("/")
                if sub == "queue" or sub.startswith("queue"):
                    if self.command.upper() == "GET":
                        _json_response(self, 200, forecast_product_queue_status())
                        return
                    if self.command.upper() == "POST":
                        body = _read_json_body(self)
                        if "paused" not in body:
                            _json_response(self, 400, {"message": "JSON 需提供 paused: true/false"})
                            return
                        set_forecast_product_queue_paused(_parse_bool(body.get("paused"), False))
                        _json_response(self, 200, forecast_product_queue_status())
                        return
                    _json_response(self, 405, {"message": "请使用 GET 查询或 POST 设置 paused"})
                    return
                if sub == "manifest" or sub.startswith("manifest"):
                    params = {**query, **(_read_json_body(self) if self.command.upper() == "POST" else {})}
                    compact = _forecast_compact_from_params(params)
                    if not compact:
                        _json_response(
                            self,
                            400,
                            {"message": "请传 start_time_compact（如 2026032502）或 start_time（可解析的起报时次）"},
                        )
                        return
                    man = read_manifest(compact)
                    if not man:
                        _json_response(self, 404, {"message": f"未找到该时次 manifest: {compact}"})
                        return
                    _json_response(self, 200, man)
                    return
                if sub == "png" or sub.startswith("png"):
                    params = {**query, **(_read_json_body(self) if self.command.upper() == "POST" else {})}
                    compact = _forecast_compact_from_params(params)
                    lh = _parse_int(params.get("lead_hours"), -1)
                    if not compact or lh < 0:
                        _json_response(
                            self,
                            400,
                            {"message": "需要 start_time_compact 或 start_time，以及 lead_hours（如 24）"},
                        )
                        return
                    png_path = resolve_png_path(compact, lh)
                    if not png_path:
                        _json_response(self, 404, {"message": f"图片不存在: {compact} / {lh}h"})
                        return
                    try:
                        with open(png_path, "rb") as f:
                            raw = f.read()
                    except OSError as e:
                        _json_response(self, 500, {"message": str(e)})
                        return
                    _binary_response(self, 200, raw, "image/png")
                    return
                if sub == "jobs":
                    if self.command.upper() != "POST":
                        _json_response(self, 405, {"message": "请使用 POST 创建任务"})
                        return
                    body = _read_json_body(self)
                    st = _as_scalar(body.get("start_time"))
                    if not st:
                        _json_response(self, 400, {"message": "start_time 必填（起报时次）"})
                        return
                    hours_raw = _as_scalar(body.get("hours"))
                    hours_tuple = None
                    if hours_raw:
                        parts = [x.strip() for x in str(hours_raw).split(",") if x.strip().isdigit()]
                        hours_tuple = tuple(int(x) for x in parts)
                    ec_opt = body.get("ec_output_path")
                    cfg_opt = body.get("config_path")
                    basin_opt = body.get("basin_vector")
                    draw_opt = body.get("draw_options")
                    draw_dict = draw_opt if isinstance(draw_opt, dict) else None
                    try:
                        job = enqueue_forecast_product_job(
                            start_time=str(st),
                            hours=hours_tuple,
                            ec_output_path=str(ec_opt) if ec_opt is not None else None,
                            config_path=str(cfg_opt) if cfg_opt is not None else None,
                            basin_vector=str(basin_opt) if basin_opt is not None else None,
                            draw_options=draw_dict,
                        )
                    except ValueError as e:
                        _json_response(self, 400, {"message": str(e)})
                        return
                    _json_response(self, 202, job_to_dict(job))
                    return
                if sub.startswith("jobs/"):
                    job_id = sub[len("jobs/") :].strip("/")
                    if not job_id:
                        _json_response(self, 400, {"message": "job_id 必填"})
                        return
                    j = get_forecast_product_job(job_id)
                    if not j:
                        _json_response(self, 404, {"message": f"任务不存在: {job_id}"})
                        return
                    _json_response(self, 200, job_to_dict(j))
                    return
                if sub == "" or sub == "info":
                    _json_response(
                        self,
                        200,
                        {
                            "products_root": products_root(),
                            "default_hours": [12, 24, 36, 48, 60, 72],
                            "endpoints": {
                            "enqueue": "POST /emergency/forecast/products/jobs",
                            "job_status": "GET /emergency/forecast/products/jobs/{job_id}",
                            "queue_pause": "GET/POST /emergency/forecast/products/queue （POST body: {\"paused\":true}）",
                            "manifest": "GET /emergency/forecast/products/manifest?start_time_compact=...",
                            "png": "GET /emergency/forecast/products/png?start_time_compact=...&lead_hours=24",
                            },
                        },
                    )
                    return
                _json_response(self, 404, {"message": f"未知子路径: /{sub}"})
                return

            if route == "/emergency/forecast":
                body = _read_json_body(self) if self.command.upper() == "POST" else {}
                params = {**query, **body}
                start_time = _as_scalar(params.get("start_time"))
                if not start_time:
                    _json_response(self, 400, {"reached": False, "level": None, "message": "start_time 必填"})
                    return
                start_time_hour = _to_hour_iso_time(str(start_time)) or str(start_time)
                scope_val = str(_as_scalar(params.get("scope")) or "haihe")
                basin_val = str(_as_scalar(params.get("basin_codes"))) if params.get("basin_codes") is not None else None
                ec_output_val = (
                    str(_as_scalar(params.get("ec_output_path")))
                    if params.get("ec_output_path") is not None
                    else DEFAULT_EC_OUTPUT_PATH
                )
                station_levels_val = str(_as_scalar(params.get("allowed_station_levels")) or "11,12,13,16")
                sample_method_val = str(_as_scalar(params.get("sample_method")) or "nearest")
                sustain_val = float(_as_scalar(params.get("sustain_threshold_6h_mm")) or 0.1)
                typhoon_land_val = _parse_bool(params.get("typhoon_landing_impact"), False)
                typhoon_inc_val = _parse_bool(params.get("typhoon_impact_increasing"), False)
                include_evidence_val = _parse_bool(params.get("include_evidence"), False)
                local_station_json_val = (
                    str(_as_scalar(params.get("local_station_json_path")))
                    if params.get("local_station_json_path") is not None
                    else None
                )

                res = query_haihe_emergency_forecast(
                    start_time=start_time_hour,
                    basin_codes=basin_val,
                    scope=scope_val,
                    ec_output_path=ec_output_val,
                    allowed_station_levels=station_levels_val,
                    sample_method=sample_method_val,
                    sustain_threshold_6h_mm=sustain_val,
                    typhoon_landing_impact=typhoon_land_val,
                    typhoon_impact_increasing=typhoon_inc_val,
                    include_evidence=include_evidence_val,
                    local_station_json_path=local_station_json_val,
                )
                event_id = _append_event(
                    event_type="forecast",
                    query_time=start_time_hour,
                    request_params={
                        "start_time": start_time_hour,
                        "scope": scope_val,
                        "basin_codes": resolve_emergency_basin_codes(
                            basin_codes=basin_val,
                            scope=scope_val,
                        ),
                        "ec_output_path": ec_output_val,
                        "allowed_station_levels": station_levels_val,
                        "sample_method": sample_method_val,
                        "sustain_threshold_6h_mm": sustain_val,
                        "typhoon_landing_impact": typhoon_land_val,
                        "typhoon_impact_increasing": typhoon_inc_val,
                        "local_station_json_path": local_station_json_val,
                    },
                    response_payload=res,
                    trace_id=str(getattr(self, "_trace_id", "") or ""),
                )
                try:
                    _sync_forecast_result_to_management_timeline(
                        event_id=event_id,
                        start_time=start_time_hour,
                        request_params={
                            "start_time": start_time_hour,
                            "scope": scope_val,
                            "basin_codes": resolve_emergency_basin_codes(
                                basin_codes=basin_val,
                                scope=scope_val,
                            ),
                        },
                        forecast_result=res,
                        trace_id=str(getattr(self, "_trace_id", "") or ""),
                    )
                except Exception as sync_err:
                    print(f"[emergency_http_server] 同步 forecast -> hh_emergency_event 失败: {sync_err}", flush=True)
                res = {**res, "event_id": event_id}
                _json_response(self, 200, res)
                return

            _json_response(self, 404, {"reached": False, "level": None, "message": f"Unknown route: {route}"})
        except (BrokenPipeError, ConnectionResetError):
            # 客户端断连导致的写回异常，忽略即可
            return
        except Exception as e:
            _json_response(self, 500, {"reached": False, "level": None, "message": str(e)})

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_OPTIONS(self) -> None:
        try:
            self.send_response(204)
            for ck, cv in _cors_headers(self).items():
                self.send_header(ck, cv)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="海河应急响应判定 HTTP 服务（JSON）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    print(f"[emergency_http_server] start on http://{args.host}:{args.port}")
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.serve_forever()


if __name__ == "__main__":
    main()

