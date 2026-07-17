"""暴雨影响河流专题图核心方法。

主入口：
- build_rainstorm_impact_thematic_map：按暴雨站点生成专题图数据；
- build_rain24h_impact_river_geojson：从 5 分钟 CSV 聚合 24h 雨量后复用同一套逻辑。
"""
from __future__ import annotations

import heapq
import json
import logging
import math
import os
import pickle
import re
import threading
from itertools import count
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

KM_PER_DEG = 111.32
logger = logging.getLogger(__name__)
DIRECTED_GRAPH_FILENAME = "river_directed_v6.pkl"
RIVER_TABLE_VERSION = "v6"
DEFAULT_RIVER_TABLE = f"haihe_river_directed_full_{RIVER_TABLE_VERSION}"
DEFAULT_GEOM_COLUMN = "geom"
DEFAULT_OBJECTID_COLUMN = "objectid"
DEFAULT_RIVER_NAME_COLUMN = "src_name"

# 滦河系在合并后的 pkl/full_v6 中只保存了单字缩写，这里维护 objectid -> 全名的映射。
# 若 graph 文件同目录存在 `{stem}_luan_names.json`，会以外部文件内容覆盖/扩展本默认映射。
_DEFAULT_LUAN_NAME_MAPPING: dict[str, str] = {
    "1": "滦河",
    "2": "兴州河",
    "3": "闪电河",
    "4": "洒河",
    "5": "洒河",
    "6": "洋河",
    "7": "洋河",
    "8": "东河",
    "9": "陡河",
    "10": "二滦河",
    "11": "大石河",
    "12": "冷口沙河",
    "13": "青龙河",
    "14": "瀑河",
    "15": "老牛河",
    "16": "伊逊河",
    "17": "蚁蚂吐河",
    "18": "武烈河",
    "19": "滦河",
    "20": "小滦河",
    "21": "柳河",
}


def _load_luan_name_mapping(graph_path: str | os.PathLike | None) -> dict[str, str]:
    """加载滦河系 objectid -> 全名映射，外部 JSON 优先于内置默认。"""
    mapping = _DEFAULT_LUAN_NAME_MAPPING.copy()
    if not graph_path:
        return mapping
    try:
        path = Path(graph_path)
        json_path = path.parent / f"{path.stem}_luan_names.json"
        if json_path.is_file():
            with open(json_path, encoding="utf-8") as f:
                mapping.update({str(k): str(v) for k, v in json.load(f).items()})
    except Exception:
        logger.warning("加载滦河系名称映射文件失败，使用内置默认映射", exc_info=True)
    return mapping


def _apply_luan_names(features: list[dict], mapping: dict[str, str]) -> None:
    for feature in features:
        props = feature.get("properties") or {}
        if not props.get("is_luan"):
            continue
        objectid = str(props.get("objectid") or "")
        props["river_name"] = mapping.get(objectid, _normalize_river_name(props.get("river_name")))

_GRAPH_CACHE = None
_GRAPH_CACHE_PATH: str | None = None
_GRAPH_CACHE_MTIME: float | None = None
_GRAPH_LOCK = threading.RLock()


def aggregate_5min_station_pre_to_24h(csv_path: str | os.PathLike) -> pd.DataFrame:
    """把 5 分钟站点降水 CSV 聚合为站点 24h 累计雨量。"""
    header = pd.read_csv(csv_path, nrows=0)
    required = {"Station_Id_C", "Datetime", "PRE", "Lon", "Lat"}
    missing = required - set(header.columns)
    if missing:
        raise ValueError(f"CSV 缺少必要字段：{sorted(missing)}")

    usecols = [
        c
        for c in [
            "Station_Id_C", "Datetime", "PRE", "Lat", "Lon", "City",
            "Station_Name", "Cnty", "Province", "Town",
        ]
        if c in header.columns
    ]
    df = pd.read_csv(csv_path, usecols=usecols, dtype={"Station_Id_C": "string"}, low_memory=False)
    df["Datetime"] = pd.to_datetime(df["Datetime"], errors="coerce")
    df["PRE"] = pd.to_numeric(df["PRE"], errors="coerce")
    df["Lon"] = pd.to_numeric(df["Lon"], errors="coerce")
    df["Lat"] = pd.to_numeric(df["Lat"], errors="coerce")

    valid_pre = df["PRE"].notna() & (df["PRE"] >= 0) & (df["PRE"] <= 9999.0)
    df["_pre_valid"] = valid_pre
    df.loc[~valid_pre, "PRE"] = 0.0

    aggs: dict[str, tuple[str, Any]] = {
        "station_id": ("Station_Id_C", _first_not_empty),
        "rain_24h": ("PRE", "sum"),
        "lon": ("Lon", _first_not_empty),
        "lat": ("Lat", _first_not_empty),
        "obs_count": ("PRE", "count"),
        "valid_pre_count": ("_pre_valid", "sum"),
        "start_time": ("Datetime", "min"),
        "end_time": ("Datetime", "max"),
    }
    for source, target in {
        "City": "city",
        "Station_Name": "station_name",
        "Cnty": "cnty",
        "Province": "province",
        "Town": "town",
    }.items():
        if source in df.columns:
            aggs[target] = (source, _first_not_empty)

    result = df.groupby("Station_Id_C", dropna=False).agg(**aggs).reset_index(drop=True)
    result["rain_24h"] = result["rain_24h"].round(3)
    result["obs_count"] = result["obs_count"].astype(int)
    result["valid_pre_count"] = result["valid_pre_count"].astype(int)
    return result.sort_values("rain_24h", ascending=False).reset_index(drop=True)


def build_rainstorm_impact_thematic_map(
    stations: list[dict],
    *,
    pg_conf: dict | None = None,
    db_connection=None,
    graph_path: str | os.PathLike | None = None,
    rainfall_threshold_mm: float = 50.0,
    station_buffer_km: float = 30.0,
    downstream_km: float = 50.0,
    river_table: str = DEFAULT_RIVER_TABLE,
    schema: str = "public",
    geom_column: str = DEFAULT_GEOM_COLUMN,
    objectid_column: str = DEFAULT_OBJECTID_COLUMN,
    river_name_column: str = DEFAULT_RIVER_NAME_COLUMN,
    direct_match_km: float = 10.0,
    direct_station_top_n: int = 0,
    max_segments: int = 0,
    extra_summary: dict | None = None,
) -> dict:
    """根据暴雨站点生成影响河流专题图数据。

    direct_station_top_n=0 表示保留站点 30km 内全部直接河段；
    direct_station_top_n>0 表示每个暴雨站只保留最近 N 条直接河段，适合实况问答场景。
    """
    _validate_params(rainfall_threshold_mm, station_buffer_km, downstream_km, direct_station_top_n)
    schema, river_table = _resolve_table(pg_conf, schema, river_table)
    rainstorm_stations = _normalize_stations(stations, rainfall_threshold_mm)
    result = _empty_result(
        stations=rainstorm_stations,
        threshold=rainfall_threshold_mm,
        buffer_km=station_buffer_km,
        downstream_km=downstream_km,
        direct_match_km=direct_match_km,
        direct_station_top_n=direct_station_top_n,
        schema=schema,
        table=river_table,
        graph_path=graph_path,
        extra=extra_summary,
    )
    if not rainstorm_stations:
        result["message"] = f"未找到降雨量≥{rainfall_threshold_mm}mm 的站点。"
        return result

    conn, should_close = _open_connection(pg_conf, db_connection)
    try:
        from psycopg2.extras import RealDictCursor
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            _ensure_river_columns(cur, schema, river_table, geom_column, objectid_column, river_name_column)
            _create_station_temp(cur, rainstorm_stations)
            direct_rows = _query_direct_rows(
                cur,
                schema,
                river_table,
                geom_column,
                objectid_column,
                river_name_column,
                station_buffer_km,
                direct_station_top_n,
            )
            start_nodes, direct_keys, downstream_start_stats = _find_direct_graph_starts(
                rainstorm_stations,
                direct_rows,
                graph_path,
                station_buffer_km,
                direct_match_km,
            )
            downstream_edges = _collect_downstream_edges(start_nodes, graph_path, direct_keys, downstream_km)
            downstream_rows = _query_downstream_rows(
                cur,
                schema,
                river_table,
                geom_column,
                objectid_column,
                river_name_column,
                downstream_edges,
                station_buffer_km,
            )
    finally:
        if should_close:
            conn.close()

    river_geojson = _build_river_geojson(direct_rows, downstream_rows, graph_path=graph_path)
    segments = geojson_to_plot_segments(river_geojson, rainstorm_stations)
    if max_segments and max_segments > 0:
        segments = segments[:max_segments]

    result.update({
        "river_geojson": river_geojson,
        "segments": segments,
        "direct_rivers": _sorted_feature_river_names(river_geojson, "direct_buffer"),
        "downstream_rivers": _sorted_feature_river_names(river_geojson, "downstream_50km"),
        "affected_rivers": sorted({s["rivername"] for s in segments if s.get("rivername")}),
        "downstream_edges": downstream_edges,
        "downstream_start_stats": downstream_start_stats,
        "impact_stations": rainstorm_stations,
        "station_geojson": _make_station_geojson(rainstorm_stations),
        "river_summary": {
            "direct_feature_count": _count_features(river_geojson, "direct_buffer"),
            "downstream_edge_count": len(downstream_edges),
            "downstream_feature_count": _count_features(river_geojson, "downstream_50km"),
            "geojson_feature_count": len(river_geojson["features"]),
            "plot_segment_count": len(segments),
        },
    })
    return result


def build_rain24h_impact_river_geojson(
    csv_path: str | os.PathLike,
    *,
    rain_threshold_mm: float = 50.0,
    station_buffer_km: float = 30.0,
    downstream_km: float = 50.0,
    river_table: str = DEFAULT_RIVER_TABLE,
    schema: str = "public",
    graph_path: str | os.PathLike | None = None,
    top_station_limit: int = 100,
    **kwargs,
) -> dict:
    """从 5 分钟降水 CSV 生成暴雨影响河流专题图数据。"""
    station_df = aggregate_5min_station_pre_to_24h(csv_path)
    stations = [_station_record(row) for _, row in station_df.iterrows()]
    result = build_rainstorm_impact_thematic_map(
        stations,
        rainfall_threshold_mm=rain_threshold_mm,
        station_buffer_km=station_buffer_km,
        downstream_km=downstream_km,
        river_table=river_table,
        schema=schema,
        graph_path=graph_path,
        **kwargs,
    )
    result.update({
        "rainfall_24h_top_stations": [
            _station_record(row) for _, row in station_df.head(max(int(top_station_limit), 0)).iterrows()
        ],
        "time_range": {
            "start_time": _jsonable(station_df["start_time"].min()) if len(station_df) else None,
            "end_time": _jsonable(station_df["end_time"].max()) if len(station_df) else None,
        },
        "station_summary": {
            "total_station_count": int(len(station_df)),
            "impact_station_count": len(result["impact_stations"]),
            "max_rain_24h": float(station_df["rain_24h"].max() or 0.0) if len(station_df) else 0.0,
        },
    })
    return result


def geojson_to_plot_segments(river_geojson: dict, stations: list[dict] | None = None) -> list[dict]:
    """把河流 GeoJSON 转成前端专题图常用线段结构。"""
    result = []
    for feature in river_geojson.get("features", []) or []:
        props = feature.get("properties") or {}
        for index, path in enumerate(_geometry_lines(feature.get("geometry") or {})):
            result.append(_make_plot_segment(path, props, index, stations or []))
    return result


def _validate_params(threshold: float, buffer_km: float, downstream_km: float, direct_station_top_n: int) -> None:
    if threshold < 0:
        raise ValueError("rainfall_threshold_mm 不能为负数")
    if buffer_km <= 0:
        raise ValueError("station_buffer_km 必须大于 0")
    if downstream_km < 0:
        raise ValueError("downstream_km 不能为负数")
    if int(direct_station_top_n or 0) < 0:
        raise ValueError("direct_station_top_n 不能为负数")


def _resolve_table(pg_conf: dict | None, schema: str, river_table: str) -> tuple[str, str]:
    if pg_conf:
        schema = pg_conf.get("schema", schema)
        river_table = pg_conf.get("river_table_full", river_table)
    if "." in str(river_table):
        schema, river_table = str(river_table).split(".", 1)
    return schema or "public", river_table or DEFAULT_RIVER_TABLE


def _open_connection(pg_conf: dict | None, db_connection):
    if db_connection is not None:
        return db_connection, False
    if pg_conf:
        import psycopg2
        return psycopg2.connect(
            host=pg_conf.get("host"),
            port=int(pg_conf.get("port", 5432)),
            dbname=pg_conf.get("dbname"),
            user=pg_conf.get("user"),
            password=pg_conf.get("password"),
            sslmode=pg_conf.get("sslmode", "prefer"),
            connect_timeout=int(pg_conf.get("connect_timeout", "5")),
        ), True
    try:
        from utils.db import engine  # type: ignore
    except Exception:
        from .db import engine  # type: ignore
    return engine.raw_connection(), True


def _downstream_start_stats(
    *,
    direct_part_matched_edge_count: int = 0,
    station_buffer_fallback_edge_count: int = 0,
    direct_match_km: float = 0.0,
    station_buffer_km: float = 0.0,
) -> dict:
    return {
        "direct_part_matched_edge_count": direct_part_matched_edge_count,
        "station_buffer_fallback_used": station_buffer_fallback_edge_count > 0,
        "station_buffer_fallback_edge_count": station_buffer_fallback_edge_count,
        "direct_match_km": float(direct_match_km),
        "station_buffer_km": float(station_buffer_km),
    }


def _empty_result(
    *,
    stations: list[dict],
    threshold: float,
    buffer_km: float,
    downstream_km: float,
    direct_match_km: float,
    direct_station_top_n: int,
    schema: str,
    table: str,
    graph_path,
    extra: dict | None,
) -> dict:
    result = {
        "status": "ok",
        "params": {
            "rainfall_threshold_mm": float(threshold),
            "station_buffer_km": float(buffer_km),
            "downstream_km": float(downstream_km),
            "direct_match_km": float(direct_match_km),
            "direct_station_top_n": int(direct_station_top_n or 0),
            "river_table": f"{schema}.{table}",
            "graph_path": str(graph_path or _default_graph_path()),
        },
        "impact_stations": stations,
        "station_geojson": _make_station_geojson(stations),
        "direct_rivers": [],
        "downstream_rivers": [],
        "affected_rivers": [],
        "downstream_edges": [],
        "downstream_start_stats": _downstream_start_stats(
            direct_match_km=direct_match_km,
            station_buffer_km=buffer_km,
        ),
        "segments": [],
        "river_geojson": {"type": "FeatureCollection", "features": []},
        "river_summary": {
            "direct_feature_count": 0,
            "downstream_edge_count": 0,
            "downstream_feature_count": 0,
            "geojson_feature_count": 0,
            "plot_segment_count": 0,
        },
    }
    if extra:
        result.update(extra)
    return result


def _normalize_station(station: dict, threshold_mm: float) -> dict | None:
    lon = _safe_float(station.get("lon"))
    lat = _safe_float(station.get("lat"))
    rainfall = _safe_float(station.get("rain_24h", station.get("rainfall")))
    if lon is None or lat is None or rainfall is None or rainfall < threshold_mm:
        return None
    return {
        "station_id": station.get("station_id") or station.get("Station_Id_C") or "",
        "station_name": station.get("station_name") or station.get("name") or "",
        "lon": lon,
        "lat": lat,
        "rain_24h": rainfall,
        "rainfall": rainfall,
        "level": station.get("level", ""),
    }


def _normalize_stations(stations: list[dict], threshold_mm: float) -> list[dict]:
    normalized = [
        s for s in (_normalize_station(st, threshold_mm) for st in stations or []) if s
    ]
    return sorted(normalized, key=lambda item: item["rain_24h"], reverse=True)


def _ensure_river_columns(cur, schema: str, table: str, geom_col: str, objectid_col: str, river_name_col: str) -> None:
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s", (schema, table))
    columns = {str(row["column_name"]) for row in cur.fetchall()}
    missing = {geom_col, objectid_col, river_name_col, "is_luan"} - columns
    if missing:
        raise RuntimeError(f"河流表 {schema}.{table} 缺少字段：{sorted(missing)}")


def _create_station_temp(cur, stations: list[dict]) -> None:
    from psycopg2.extras import execute_values
    cur.execute("DROP TABLE IF EXISTS tmp_rain24h_impact_stations")
    cur.execute("""
        CREATE TEMP TABLE tmp_rain24h_impact_stations(
            station_id text, station_name text, lon double precision, lat double precision,
            rain_24h double precision, geom geometry(Point,4326)
        ) ON COMMIT DROP
    """)
    rows = [(s["station_id"], s["station_name"], s["lon"], s["lat"], s["rain_24h"], s["lon"], s["lat"]) for s in stations]
    execute_values(
        cur,
        "INSERT INTO tmp_rain24h_impact_stations VALUES %s",
        rows,
        template="(%s,%s,%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326))",
    )


def _query_direct_rows(
    cur,
    schema: str,
    table: str,
    geom_col: str,
    objectid_col: str,
    river_name_col: str,
    buffer_km: float,
    direct_station_top_n: int,
) -> list[dict]:
    """查询暴雨站点缓冲区内直接命中的河段，按 objectid 聚合为 MultiLineString。"""
    cur.execute(f"""
        WITH river_parts AS (
            SELECT r.{_qi(objectid_col)}::text AS objectid,
                   COALESCE(NULLIF(TRIM(r.{_qi(river_name_col)}::text), ''), '未知') AS river_name,
                   COALESCE(r.is_luan, false) AS is_luan,
                   (ST_Dump(r.{_qi(geom_col)})).geom AS geom
            FROM {_qi(schema)}.{_qi(table)} r
            WHERE r.{_qi(geom_col)} IS NOT NULL AND NOT ST_IsEmpty(r.{_qi(geom_col)})
        ), station_hits AS (
            SELECT p.objectid, p.river_name, p.is_luan, p.geom,
                   s.station_id, s.station_name, s.lon, s.lat, s.rain_24h,
                   ST_Distance(p.geom::geography, s.geom::geography) / 1000.0 AS station_distance_km,
                   ROW_NUMBER() OVER (
                       PARTITION BY s.station_id
                       ORDER BY ST_Distance(p.geom::geography, s.geom::geography), p.river_name, p.objectid
                   ) AS station_rank
            FROM river_parts p
            JOIN tmp_rain24h_impact_stations s ON ST_DWithin(p.geom::geography, s.geom::geography, %(buffer_m)s)
        ), selected_hits AS (
            SELECT * FROM station_hits
            WHERE %(top_n)s <= 0 OR station_rank <= %(top_n)s
        )
        SELECT objectid, objectid AS id, river_name, is_luan,
               ST_AsGeoJSON(ST_Multi(ST_Collect(geom))) AS geom_json,
               SUM(ST_Length(geom::geography)) / 1000.0 AS length_km,
               MIN(station_distance_km) AS min_station_distance_km,
               COUNT(DISTINCT station_id) AS trigger_station_count,
               jsonb_agg(DISTINCT jsonb_build_object(
                   'station_id',station_id,'station_name',station_name,'lon',lon,'lat',lat,'rain_24h',rain_24h
               )) AS trigger_stations
        FROM selected_hits
        GROUP BY objectid, river_name, is_luan
        ORDER BY min_station_distance_km, river_name, objectid
    """, {"buffer_m": float(buffer_km) * 1000.0, "top_n": int(direct_station_top_n or 0)})
    return list(cur.fetchall())


def _query_downstream_rows(cur, schema: str, table: str, geom_col: str, objectid_col: str, river_name_col: str, edges: list[dict], buffer_km: float) -> list[dict]:
    """根据下游追踪边 keys 查询数据库几何，并裁剪到保留长度。

    对于在 full_v6 表中找不到匹配 objectid 几何的下游边，回退使用 pkl 边的直线几何，
    保证每条有下游的 direct_buffer 都能在输出中看到 50km 下游追踪结果。
    """
    if not edges:
        return []
    _create_downstream_temp(cur, edges)
    cur.execute(f"""
        WITH river_parts AS (
            SELECT r.{_qi(objectid_col)}::text AS objectid,
                   COALESCE(NULLIF(TRIM(r.{_qi(river_name_col)}::text), ''), '未知') AS db_river_name,
                   COALESCE(r.is_luan, false) AS is_luan,
                   (ST_Dump(r.{_qi(geom_col)})).geom AS geom
            FROM {_qi(schema)}.{_qi(table)} r
            WHERE r.{_qi(geom_col)} IS NOT NULL AND NOT ST_IsEmpty(r.{_qi(geom_col)})
        ), candidates AS (
            SELECT e.edge_key, e.objectid, e.river_name,
                   e.min_distance_km, e.end_distance_km, e.keep_km, e.clip_fraction,
                   e.is_direct_graph_edge, e.from_x, e.from_y, e.to_x, e.to_y, e.pkl_line,
                   p.db_river_name, p.is_luan AS db_is_luan, e.is_luan AS pkl_is_luan, p.geom AS part_geom,
                   ST_Distance(p.geom::geography, e.pkl_line::geography) / 1000.0 AS match_distance_km,
                   CASE WHEN p.is_luan = e.is_luan THEN 0 ELSE 1 END AS match_priority
            FROM river_parts p
            JOIN tmp_downstream_edges e ON p.objectid = e.objectid
            WHERE e.pkl_line IS NOT NULL
              AND GeometryType(p.geom) = 'LINESTRING'
        ), evaluated_parts AS (
            SELECT edge_key, objectid, river_name, db_river_name, db_is_luan, pkl_is_luan,
                   min_distance_km, end_distance_km, keep_km, clip_fraction, is_direct_graph_edge,
                   from_x, from_y, to_x, to_y, match_distance_km, match_priority,
                   part_geom,
                   ST_Length(part_geom::geography) / 1000.0 AS part_length_km,
                   ST_LineLocatePoint(part_geom, ST_SetSRID(ST_MakePoint(from_x, from_y),4326)) AS from_frac,
                   ST_LineLocatePoint(part_geom, ST_SetSRID(ST_MakePoint(to_x, to_y),4326)) AS to_frac
            FROM candidates
            WHERE match_distance_km <= %(buffer_m)s / 1000.0
        ), ranked_parts AS (
            SELECT *,
                   CASE
                       WHEN from_frac IS NOT NULL AND ABS(to_frac - from_frac) > 1e-6
                       THEN SIGN(to_frac - from_frac)
                       ELSE 1.0
                   END AS direction,
                   ROW_NUMBER() OVER (
                       PARTITION BY edge_key
                       ORDER BY
                           CASE WHEN to_frac IS NOT NULL THEN 0 ELSE 1 END,
                           CASE WHEN from_frac IS NOT NULL AND ABS(to_frac - from_frac) > 1e-6 THEN 0 ELSE 1 END,
                           CASE WHEN part_length_km >= keep_km THEN 0 ELSE 1 END,
                           match_priority,
                           match_distance_km
                   ) AS rn
            FROM evaluated_parts
            WHERE part_length_km > 0
        ), clipped AS (
            SELECT edge_key, objectid, pkl_is_luan AS is_luan,
                   CASE
                       WHEN COALESCE(TRIM(db_river_name), '') IN ('', '未知') THEN river_name
                       ELSE db_river_name
                   END AS river_name,
                   min_distance_km, end_distance_km, keep_km, clip_fraction, is_direct_graph_edge, match_distance_km,
                   from_x, from_y, to_x, to_y,
                   ST_Multi(ST_LineSubstring(
                       part_geom,
                       LEAST(from_frac, target_frac),
                       GREATEST(from_frac, target_frac)
                   )) AS geom
            FROM (
                SELECT edge_key, objectid, river_name, db_river_name, pkl_is_luan, part_geom,
                       min_distance_km, end_distance_km, keep_km, clip_fraction, is_direct_graph_edge,
                       from_x, from_y, to_x, to_y, match_distance_km,
                       from_frac, to_frac,
                       GREATEST(0.0, LEAST(1.0, from_frac + direction * keep_km / part_length_km)) AS target_frac
                FROM ranked_parts
                WHERE rn = 1 AND to_frac IS NOT NULL AND part_length_km > 0
            ) t
        )
        SELECT edge_key, objectid, objectid AS id, river_name, is_luan,
               min_distance_km AS min_downstream_distance_km,
               end_distance_km AS end_downstream_distance_km,
               keep_km, clip_fraction, is_direct_graph_edge, match_distance_km,
               from_x, from_y, to_x, to_y,
               ST_AsGeoJSON(geom) AS geom_json,
               ST_Length(geom::geography) / 1000.0 AS length_km
        FROM clipped
        WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
        ORDER BY min_distance_km, river_name, edge_key
    """, {"buffer_m": float(buffer_km) * 1000.0})
    rows = list(cur.fetchall())
    return _fill_unmatched_downstream_edges(rows, edges)


def _fill_unmatched_downstream_edges(rows: list[dict], edges: list[dict]) -> list[dict]:
    matched_keys = {r["edge_key"] for r in rows}
    unmatched = [e for e in edges if e["edge_key"] not in matched_keys]
    if not unmatched:
        return rows
    logger.info("下游边回退直线几何数量=%d / 总下游边=%d", len(unmatched), len(edges))
    for e in unmatched:
        row = _build_fallback_downstream_row(e)
        if row:
            rows.append(row)
    return rows


def _build_fallback_downstream_row(edge: dict) -> dict | None:
    fx, fy, tx, ty = edge.get("from_x"), edge.get("from_y"), edge.get("to_x"), edge.get("to_y")
    if fx is None or fy is None or tx is None or ty is None:
        return None
    return {
        "edge_key": edge["edge_key"],
        "objectid": edge["objectid"],
        "id": edge["objectid"],
        "river_name": edge["river_name"],
        "is_luan": edge.get("is_luan", False),
        "min_downstream_distance_km": edge["min_distance_km"],
        "end_downstream_distance_km": edge["end_distance_km"],
        "keep_km": edge["keep_km"],
        "clip_fraction": edge["clip_fraction"],
        "is_direct_graph_edge": edge["is_direct_graph_edge"],
        "match_distance_km": None,
        "from_x": fx,
        "from_y": fy,
        "to_x": tx,
        "to_y": ty,
        "geom_json": json.dumps({"type": "LineString", "coordinates": [[fx, fy], [tx, ty]]}),
        "length_km": edge["keep_km"],
    }


def _create_downstream_temp(cur, edges: list[dict]) -> None:
    from psycopg2.extras import execute_values
    cur.execute("DROP TABLE IF EXISTS tmp_downstream_edges")
    cur.execute("""
        CREATE TEMP TABLE tmp_downstream_edges(
            edge_key text, objectid text, river_name text,
            min_distance_km double precision, end_distance_km double precision,
            keep_km double precision, clip_fraction double precision, is_direct_graph_edge boolean,
            is_luan boolean,
            from_x double precision, from_y double precision, to_x double precision, to_y double precision,
            pkl_line geometry(LineString,4326)
        ) ON COMMIT DROP
    """)
    rows = []
    for e in edges:
        fx, fy, tx, ty = e["from_x"], e["from_y"], e["to_x"], e["to_y"]
        rows.append((
            e["edge_key"], e["objectid"], e["river_name"], e["min_distance_km"], e["end_distance_km"],
            e["keep_km"], e["clip_fraction"], bool(e["is_direct_graph_edge"]), bool(e.get("is_luan")),
            fx, fy, tx, ty, _line_wkt(fx, fy, tx, ty),
        ))
    execute_values(cur, "INSERT INTO tmp_downstream_edges VALUES %s", rows, template="""
        (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,ST_GeomFromText(%s,4326))
    """)


def _line_wkt(fx, fy, tx, ty) -> str | None:
    if fx is None or fy is None or tx is None or ty is None:
        return None
    return f"LINESTRING({fx} {fy}, {tx} {ty})"


def _find_direct_graph_starts(
    stations: list[dict],
    direct_rows: list[dict],
    graph_path,
    station_buffer_km: float,
    direct_match_km: float,
) -> tuple[dict[Any, float], set[str], dict]:
    """暴雨站点 30km 缓冲区内的 pkl 边作为下游追踪起点，并标记命中真实直接河段的边。

    业务约束以"暴雨站点 30km 缓冲区"作为直接影响范围。若只采用真实直接河段
    匹配（objectid/name + 几何 proximity）作为起点，会把大量位于缓冲区内、但因
    pkl/full_v6 对齐偏差而未被精确匹配的河系遗漏，导致下游河段断裂、零散。

    - 阶段一：将暴雨站 30km 缓冲区内的所有 pkl 边作为下游追踪起点。
    - 阶段二：在全部 pkl 边中识别真实直接河段匹配，用于 `is_direct_graph_edge`
      标记；若某条直接匹配边因对齐偏差略超 30km 缓冲区，也补充为起点。

    注意：
    - `direct_station_top_n` 只限制 `_query_direct_rows` 返回的直接河段数量，
      不影响 30km 缓冲区起点；这是两个不同维度的控制。
    - `station_buffer_fallback_edge_count` 表示"30km 缓冲区中未被标记为真实直接
      河段的边数"，并不表示"未找到任何直接匹配"。
    """
    graph = get_graph(graph_path)
    direct_refs = _direct_refs(direct_rows)
    station_points = [
        (float(s["lon"]), float(s["lat"]))
        for s in stations
        if s.get("lon") is not None and s.get("lat") is not None
    ]
    buffer_km = float(station_buffer_km)

    starts: dict[Any, float] = {}
    direct_keys: set[str] = set()
    buffer_only_edge_count = 0

    for u, v, key, attr, p1, p2 in _iter_edges_with_points(graph):
        edge_key = _edge_key(u, v, key, attr)
        in_buffer = (
            station_points
            and any(_point_to_segment_km(lon, lat, p1, p2) <= buffer_km for lon, lat in station_points)
        )
        is_direct = _edge_matches_direct_part(attr, p1, p2, direct_refs, direct_match_km)

        if in_buffer or is_direct:
            starts[v] = 0.0
        if is_direct:
            direct_keys.add(edge_key)
        elif in_buffer:
            buffer_only_edge_count += 1

    stats = _downstream_start_stats(
        direct_part_matched_edge_count=len(direct_keys),
        station_buffer_fallback_edge_count=buffer_only_edge_count,
        direct_match_km=direct_match_km,
        station_buffer_km=station_buffer_km,
    )
    return starts, direct_keys, stats


def _iter_edges_with_points(graph):
    for u, v, key, attr in iter_graph_edges(graph):
        p1, p2 = _edge_points(u, v)
        if p1 is None or p2 is None:
            continue
        yield u, v, key, attr, p1, p2


def _collect_downstream_edges(starts: dict[Any, float], graph_path, direct_keys: set[str], downstream_km: float) -> list[dict]:
    graph = get_graph(graph_path)
    best = dict(starts)
    seq = count()
    heap = [(float(dist), next(seq), node) for node, dist in starts.items()]
    heapq.heapify(heap)
    edges: dict[str, dict] = {}
    while heap:
        distance, _seq, node = heapq.heappop(heap)
        if distance > best.get(node, math.inf) or distance >= downstream_km:
            continue
        for u, v, key, attr in iter_out_edges(graph, node):
            next_distance = _save_downstream_edge(edges, u, v, key, attr, distance, downstream_km, direct_keys)
            if next_distance <= downstream_km and next_distance < best.get(v, math.inf):
                best[v] = next_distance
                heapq.heappush(heap, (next_distance, next(seq), v))
    return sorted(edges.values(), key=lambda x: (x["min_distance_km"], x["river_name"], x["edge_key"]))


def _save_downstream_edge(
    edges: dict[str, dict],
    u,
    v,
    key,
    attr: dict,
    start_km: float,
    limit_km: float,
    direct_keys: set[str],
) -> float:
    objectid = _edge_objectid_key(attr)
    river_name = get_edge_river_name(attr)
    length_km = get_edge_length_km(attr)
    end_km = start_km + length_km
    if not objectid or not river_name or length_km <= 0:
        return end_km

    keep_km = limit_km - start_km
    if keep_km <= 0:
        return end_km
    keep_km = min(keep_km, length_km)

    edge_key = _edge_key(u, v, key, attr)
    old = edges.get(edge_key)
    if old and old["min_distance_km"] <= start_km:
        return end_km

    from_x, from_y = _parse_node_xy(u)
    to_x, to_y = _parse_node_xy(v)
    edges[edge_key] = {
        "edge_key": edge_key,
        "objectid": objectid,
        "river_name": river_name,
        "min_distance_km": round(float(start_km), 3),
        "end_distance_km": round(float(start_km + keep_km), 3),
        "keep_km": round(float(keep_km), 3),
        "clip_fraction": round(float(keep_km / length_km), 8),
        "is_direct_graph_edge": edge_key in direct_keys,
        "is_luan": bool(attr.get("is_luan")),
        "from_x": from_x,
        "from_y": from_y,
        "to_x": to_x,
        "to_y": to_y,
    }
    return end_km


def _build_river_geojson(direct_rows: list[dict], downstream_rows: list[dict], graph_path=None) -> dict:
    """生成河流 GeoJSON；直接河段优先，按真实几何去重，并回补水系名称。"""
    rows_with_type = (
        [(r, "direct_buffer") for r in direct_rows]
        + [(r, "downstream_50km") for r in downstream_rows]
    )
    features: list[dict] = []
    seen = set()
    for row, impact_type in rows_with_type:
        feature = _river_feature(row, impact_type)
        if not feature:
            continue
        key = _feature_geometry_key(feature)
        if key in seen:
            continue
        seen.add(key)
        features.append(feature)

    if any(_needs_name_enrichment(f) for f in features):
        name_map = _build_objectid_name_map(graph_path)
        if name_map:
            _enrich_unknown_river_names(features, name_map)

    _apply_luan_names(features, _load_luan_name_mapping(graph_path))

    features = _drop_downstream_covered_by_direct(features)

    features.sort(key=lambda f: (0 if f["properties"]["impact_type"] == "direct_buffer" else 1, f["properties"].get("river_name") or ""))
    return {"type": "FeatureCollection", "features": features}


def _build_objectid_name_map(graph_path) -> dict[str, str]:
    """从 pkl 有向图中建立 objectid -> 河系名 的映射，用于回填数据库缺失名称。"""
    try:
        graph = get_graph(graph_path)
    except Exception:
        logger.exception("加载河网拓扑文件失败，无法构建 objectid 名称映射")
        return {}
    mapping: dict[str, str] = {}
    for _u, _v, _k, attr in iter_graph_edges(graph):
        objectid = _edge_objectid_key(attr)
        if not objectid:
            continue
        name = get_edge_river_name(attr)
        if not name or name == "未知":
            continue
        if mapping.get(objectid) in (None, "未知"):
            mapping[objectid] = name
    return mapping


def _needs_name_enrichment(feature: dict) -> bool:
    name = str((feature.get("properties") or {}).get("river_name") or "").strip()
    return not name or name == "未知"


def _enrich_unknown_river_names(features: list[dict], name_map: dict[str, str]) -> None:
    """将属性中 river_name 为“未知”的要素替换为 pkl 图中的名称。"""
    for feature in features:
        props = feature.get("properties") or {}
        name = str(props.get("river_name") or "").strip()
        if name and name != "未知":
            continue
        objectid = str(props.get("objectid") or "")
        if objectid and objectid in name_map:
            props["river_name"] = name_map[objectid]


_WATER_BODY_SUFFIXES = frozenset({"河", "江", "湖", "海", "渠", "溪", "涧", "沟", "汊"})


def _normalize_river_name(name: Any) -> str:
    text = str(name or "").strip()
    if len(text) != 1:
        return text
    if text in _WATER_BODY_SUFFIXES:
        return text
    if "一" <= text <= "鿿":
        return f"{text}河"
    return text


def _drop_downstream_covered_by_direct(features: list[dict]) -> list[dict]:
    """移除几何上被同 objectid 直接河段覆盖的下游河段，减少重复渲染。"""
    direct_features: list[dict] = []
    direct_by_objectid: dict[str, list[dict]] = {}
    downstream_features: list[dict] = []
    for feature in features:
        props = feature.get("properties") or {}
        objectid = str(props.get("objectid") or "")
        if props.get("impact_type") == "direct_buffer" and objectid:
            direct_features.append(feature)
            direct_by_objectid.setdefault(objectid, []).append(feature)
        else:
            downstream_features.append(feature)

    if not downstream_features or not direct_by_objectid:
        return features

    coverage = _build_direct_coverage_index(direct_by_objectid)
    if not coverage:
        return features

    kept_downstream = [f for f in downstream_features if not _is_downstream_covered(f, coverage)]
    return direct_features + kept_downstream


def _build_direct_coverage_index(direct_by_objectid: dict[str, list[dict]]) -> dict[str, dict]:
    """为每个 objectid 构建直接河段的几何索引（prepared covers + union）。"""
    try:
        from shapely.geometry import shape
        from shapely.prepared import prep
        from shapely.ops import unary_union
    except Exception:  # pragma: no cover - shapely 为可选依赖
        logger.warning("shapely 未安装或无法导入，跳过下游河段几何去重")
        return {}

    coverage: dict[str, dict] = {}
    for objectid, direct_feats in direct_by_objectid.items():
        try:
            geoms = [shape(f.get("geometry") or {}) for f in direct_feats]
            geoms = [g for g in geoms if not g.is_empty]
            if not geoms:
                continue
            union = unary_union(geoms).buffer(1e-4)
            coverage[objectid] = {"prepared": prep(union), "union": union}
        except Exception:
            logger.warning("准备直接河段几何失败 objectid=%s", objectid, exc_info=True)
    return coverage


def _is_downstream_covered(feature: dict, coverage: dict[str, dict]) -> bool:
    """判断下游河段是否被同 objectid 的直接河段几何覆盖。"""
    from shapely.geometry import shape

    props = feature.get("properties") or {}
    objectid = str(props.get("objectid") or "")
    cov = coverage.get(objectid)
    if cov is None:
        return False

    try:
        downstream_shape = shape(feature.get("geometry") or {})
        if downstream_shape.is_empty:
            return True
        if cov["prepared"].covers(downstream_shape):
            return True
        # 若下游段与直接河段重叠比例超过阈值，也视为重复并丢弃
        intersection = cov["union"].intersection(downstream_shape)
        if not intersection.is_empty and downstream_shape.length > 0:
            overlap_ratio = intersection.length / downstream_shape.length
            if overlap_ratio >= 0.9:
                return True
    except Exception:
        logger.warning("下游河段几何覆盖判断失败，保留该河段", exc_info=True)
    return False


def _river_feature(row: dict, impact_type: str) -> dict | None:
    geometry = _geometry_from_row(row)
    if not _has_line_geometry(geometry):
        return None

    props = {
        "impact_type": impact_type,
        "river_name": row.get("river_name") or "未知",
        "id": row.get("id"),
        "objectid": row.get("objectid"),
        "edge_key": row.get("edge_key"),
        "length_km": round(float(row.get("length_km") or 0.0), 3),
        "flow_direction": "database_geometry_order",
        "direction_source": f"full_{RIVER_TABLE_VERSION}_original_geometry",
        "is_luan": bool(row.get("is_luan")),
    }
    if impact_type == "direct_buffer":
        props.update({
            "min_station_distance_km": round(float(row.get("min_station_distance_km") or 0.0), 3),
            "trigger_station_count": int(row.get("trigger_station_count") or 0),
            "trigger_stations": row.get("trigger_stations") or [],
            "geometry_source": f"full_{RIVER_TABLE_VERSION}_direct_30km_uncut",
        })
    else:
        props.update({
            "min_downstream_distance_km": row.get("min_downstream_distance_km"),
            "end_downstream_distance_km": row.get("end_downstream_distance_km"),
            "keep_km": row.get("keep_km"),
            "clip_fraction": row.get("clip_fraction"),
            "is_direct_graph_edge": row.get("is_direct_graph_edge"),
            "match_distance_km": _round(row.get("match_distance_km")),
            "topology_from": _point_property(row, "from"),
            "topology_to": _point_property(row, "to"),
            "geometry_source": f"full_{RIVER_TABLE_VERSION}_downstream_50km_clipped_database_order",
        })
    return {"type": "Feature", "geometry": geometry, "properties": props}


def _feature_geometry_key(feature: dict) -> tuple[str, str]:
    props = feature.get("properties") or {}
    objectid = str(props.get("objectid") or "")
    geometry_key = json.dumps(feature.get("geometry") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return objectid, geometry_key


def _has_line_geometry(geometry: dict | None) -> bool:
    return bool(isinstance(geometry, dict) and _geometry_lines(geometry))


def _point_property(row: dict, prefix: str) -> dict | None:
    lon = _safe_float(row.get(f"{prefix}_x"))
    lat = _safe_float(row.get(f"{prefix}_y"))
    if lon is None or lat is None:
        return None
    return {"lon": lon, "lat": lat}


def _make_plot_segment(path: list[list[float]], props: dict, index: int, stations: list[dict]) -> dict:
    min_station_km = _min_station_distance_km(path, stations)
    return {
        "from_x": path[0][0],
        "from_y": path[0][1],
        "to_x": path[-1][0],
        "to_y": path[-1][1],
        "path": path,
        "paths": [path],
        "geometry": {"type": "LineString", "coordinates": path},
        "rivername": props.get("river_name") or "未知",
        "length_km": round(_line_length_km(path) or float(props.get("length_km") or 0.0), 3),
        "objectid": props.get("objectid"),
        "edge_key": props.get("edge_key") or f"{props.get('objectid')}:{index}",
        "is_affected": True,
        "impact_type": props.get("impact_type"),
        "min_station_distance_km": _round(min_station_km),
        "trigger_station_count": props.get("trigger_station_count", 0),
        "trigger_stations": props.get("trigger_stations", []),
        "min_downstream_distance_km": props.get("min_downstream_distance_km"),
        "end_downstream_distance_km": props.get("end_downstream_distance_km"),
        "keep_km": props.get("keep_km"),
        "clip_fraction": props.get("clip_fraction"),
        "match_distance_km": props.get("match_distance_km"),
        "flow_direction": props.get("flow_direction"),
        "direction_source": props.get("direction_source"),
        "topology_from": props.get("topology_from"),
        "topology_to": props.get("topology_to"),
        "geometry_source": props.get("geometry_source"),
    }


def _direct_refs(rows: list[dict]) -> list[dict]:
    refs = []
    for row in rows:
        geometry = _geometry_from_row(row)
        if _has_line_geometry(geometry):
            refs.append({
                "objectid": str(row.get("objectid") or ""),
                "river_name": str(row.get("river_name") or ""),
                "lines": _geometry_lines(geometry),
            })
    return refs


def _edge_matches_direct_part(attr: dict, p1: tuple[float, float], p2: tuple[float, float], refs: list[dict], max_km: float) -> bool:
    objectid = _edge_objectid_key(attr)
    river_name = get_edge_river_name(attr)
    for ref in refs:
        same_id = objectid and objectid == ref["objectid"]
        same_name = river_name and river_name == ref["river_name"]
        if (same_id or same_name) and any(_edge_to_line_km(p1, p2, line) <= max_km for line in ref["lines"]):
            return True
    return False


def _geometry_from_row(row: dict) -> dict | None:
    raw = row.get("geom_json")
    return json.loads(raw) if isinstance(raw, str) else raw


def _geometry_lines(geometry: dict) -> list[list[list[float]]]:
    if not isinstance(geometry, dict):
        return []
    if geometry.get("type") == "LineString":
        coords = geometry.get("coordinates") or []
        return [coords] if len(coords) >= 2 else []
    if geometry.get("type") == "MultiLineString":
        return [line for line in geometry.get("coordinates") or [] if len(line) >= 2]
    return []


def _count_features(feature_collection: dict, impact_type: str) -> int:
    return sum(1 for f in feature_collection.get("features", []) if (f.get("properties") or {}).get("impact_type") == impact_type)


def _edge_to_line_km(p1: tuple[float, float], p2: tuple[float, float], line: list[list[float]]) -> float:
    values = [_point_to_line_km(p1[0], p1[1], line), _point_to_line_km(p2[0], p2[1], line)]
    values.extend(_point_to_segment_km(float(p[0]), float(p[1]), p1, p2) for p in line)
    return min(values)


def _point_to_line_km(lon: float, lat: float, line: list[list[float]]) -> float:
    if len(line) < 2:
        return math.inf
    return min(
        _point_to_segment_km(lon, lat, (float(line[i][0]), float(line[i][1])), (float(line[i + 1][0]), float(line[i + 1][1])))
        for i in range(len(line) - 1)
    )


def _point_to_segment_km(lon: float, lat: float, p1: tuple[float, float], p2: tuple[float, float]) -> float:
    lon1, lat1 = p1
    lon2, lat2 = p2
    mean_lat = math.radians((lat + lat1 + lat2) / 3.0)
    x, y = lon * math.cos(mean_lat) * KM_PER_DEG, lat * KM_PER_DEG
    x1, y1 = lon1 * math.cos(mean_lat) * KM_PER_DEG, lat1 * KM_PER_DEG
    x2, y2 = lon2 * math.cos(mean_lat) * KM_PER_DEG, lat2 * KM_PER_DEG
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x - x1, y - y1)
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))


def _min_station_distance_km(line: list[list[float]], stations: list[dict]) -> float | None:
    distances = [
        _point_to_line_km(float(s["lon"]), float(s["lat"]), line)
        for s in stations
        if s.get("lon") is not None and s.get("lat") is not None
    ]
    return min(distances) if distances else None


def _line_length_km(line: list[list[float]]) -> float:
    return sum(
        _haversine_km(float(line[i][0]), float(line[i][1]), float(line[i + 1][0]), float(line[i + 1][1]))
        for i in range(max(len(line) - 1, 0))
    )


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _edge_points(u, v):
    ux, uy = _parse_node_xy(u)
    vx, vy = _parse_node_xy(v)
    if ux is None or uy is None or vx is None or vy is None:
        return None, None
    return (ux, uy), (vx, vy)


def _parse_node_xy(node: Any) -> tuple[float | None, float | None]:
    try:
        if isinstance(node, str) and "," in node:
            x, y = node.split(",", 1)
            return float(x), float(y)
        if isinstance(node, (tuple, list)) and len(node) >= 2:
            return float(node[0]), float(node[1])
    except (TypeError, ValueError):
        logger.debug("解析节点坐标失败: %s", node)
        return None, None
    return None, None


def _first_not_empty(values: pd.Series) -> Any:
    valid = values.dropna()
    if valid.empty:
        return None
    for item in valid:
        if str(item).strip():
            return item
    return valid.iloc[0]


def _station_record(row: pd.Series) -> dict:
    fields = [
        "station_id", "station_name", "province", "city", "cnty", "town",
        "lon", "lat", "rain_24h", "obs_count", "valid_pre_count", "start_time", "end_time",
    ]
    return {field: _jsonable(row.get(field)) for field in fields if field in row.index}


def _make_station_geojson(stations: list[dict]) -> dict:
    features = []
    for s in stations:
        if s.get("lon") is None or s.get("lat") is None:
            continue
        props = dict(s)
        props.pop("lon", None)
        props.pop("lat", None)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [float(s["lon"]), float(s["lat"])]},
            "properties": props,
        })
    return {"type": "FeatureCollection", "features": features}


def _jsonable(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    try:
        if pd.isna(value):
            return None
    except Exception:
        logger.debug("pd.isna 检查失败，保留原值", exc_info=True)
    return value.item() if hasattr(value, "item") else value


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: Any, digits: int = 3) -> float | None:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _sorted_feature_river_names(river_geojson: dict, impact_type: str) -> list[str]:
    """从已生成的 GeoJSON 要素中提取指定影响类型的河系名（保证名称回填后一致）。"""
    names: set[str] = set()
    for feature in river_geojson.get("features", []) or []:
        props = feature.get("properties") or {}
        if props.get("impact_type") != impact_type:
            continue
        name = str(props.get("river_name") or "").strip()
        if name and name != "未知":
            names.add(name)
    return sorted(names)


def _qi(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name or "")):
        raise ValueError(f"非法 SQL 标识符：{name!r}")
    return f'"{name}"'


def get_edge_river_name(attr: dict) -> str:
    for key in ("src_name", "river_name", "rivername", "name"):
        value = attr.get(key) if isinstance(attr, dict) else None
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def get_edge_length_km(attr: dict, attr_name: str = "length_km") -> float:
    if not isinstance(attr, dict):
        return 0.0
    for key in (attr_name, "length_km", "len_km", "length"):
        value = attr.get(key)
        if value is None:
            continue
        try:
            return max(float(value), 0.0)
        except (TypeError, ValueError):
            continue
    try:
        return max(float(attr.get("length_m")) / 1000.0, 0.0)
    except (TypeError, ValueError):
        return 0.0


def _edge_objectid_key(attr: dict) -> str:
    if not isinstance(attr, dict):
        return ""
    for key in ("objectid", "OBJECTID", "id", "ID", "gid"):
        value = attr.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        try:
            number = float(text)
            return str(int(number)) if number.is_integer() else text
        except (TypeError, ValueError):
            return text
    return ""


def _edge_key(u, v, key, attr: dict) -> str:
    return f"{u}|{v}|{key}|{_edge_objectid_key(attr)}|{get_edge_river_name(attr)}"


def _default_graph_path() -> str:
    here = Path(__file__).resolve()
    candidates = [
        here.parents[1] / "Service" / DIRECTED_GRAPH_FILENAME,
        here.parents[1] / "Service" / "river_directed_v4_asis.pkl",
        Path.cwd() / "Service" / DIRECTED_GRAPH_FILENAME,
        Path.cwd() / "Service" / "river_directed_v4_asis.pkl",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    return str(candidates[0])


def get_graph(graph_path: str | os.PathLike | None = None, *, force_reload: bool = False):
    global _GRAPH_CACHE, _GRAPH_CACHE_PATH, _GRAPH_CACHE_MTIME
    path = str(graph_path or _default_graph_path())
    if not os.path.exists(path):
        raise FileNotFoundError(f"河网拓扑文件不存在：{path}")
    mtime = os.path.getmtime(path)
    with _GRAPH_LOCK:
        if force_reload or _GRAPH_CACHE is None or _GRAPH_CACHE_PATH != path or _GRAPH_CACHE_MTIME != mtime:
            with open(path, "rb") as f:
                _GRAPH_CACHE = pickle.load(f)
            _GRAPH_CACHE_PATH = path
            _GRAPH_CACHE_MTIME = mtime
        return _GRAPH_CACHE


def iter_graph_edges(graph) -> Iterator[tuple[Any, Any, Any, dict]]:
    if graph.is_multigraph():
        for u, v, key, attr in graph.edges(keys=True, data=True):
            yield u, v, key, attr
    else:
        for u, v, attr in graph.edges(data=True):
            yield u, v, None, attr


def iter_out_edges(graph, node) -> Iterator[tuple[Any, Any, Any, dict]]:
    if graph.is_multigraph():
        for u, v, key, attr in graph.out_edges(node, keys=True, data=True):
            yield u, v, key, attr
    else:
        for u, v, attr in graph.out_edges(node, data=True):
            yield u, v, None, attr
