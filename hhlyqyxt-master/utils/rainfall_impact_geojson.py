"""暴雨影响河流专题图核心方法。

主入口：
- build_rainstorm_impact_thematic_map：同事/智能体传入暴雨站点列表，直接得到专题图数据。
- build_rain24h_impact_river_geojson：本地 CSV 入口，先把 5 分钟 PRE 聚合为 24h 后再走同一套逻辑。
"""
from __future__ import annotations

import heapq
import json
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
DEFAULT_RIVER_TABLE = "haihe_river_directed_full_v5"
DEFAULT_GEOM_COLUMN = "geom"
DEFAULT_OBJECTID_COLUMN = "objectid"
DEFAULT_RIVER_NAME_COLUMN = "src_name"

_GRAPH_CACHE = None
_GRAPH_CACHE_PATH: str | None = None
_GRAPH_CACHE_MTIME: float | None = None
_GRAPH_LOCK = threading.RLock()


def aggregate_5min_station_pre_to_24h(csv_path: str | os.PathLike) -> pd.DataFrame:
    """把 5 分钟站点降水 CSV 聚合成站点 24h 累计雨量。"""
    header = pd.read_csv(csv_path, nrows=0)
    required = {"Station_Id_C", "Datetime", "PRE", "Lon", "Lat"}
    missing = required - set(header.columns)
    if missing:
        raise ValueError(f"CSV 缺少必要字段：{sorted(missing)}")

    usecols = [
        c for c in [
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
    direct_match_km: float = 3.0,
    max_segments: int = 0,
    extra_summary: dict | None = None,
) -> dict:
    """根据暴雨站点生成影响河流专题图数据。"""
    _validate_params(rainfall_threshold_mm, station_buffer_km, downstream_km)
    schema, river_table = _resolve_table(pg_conf, schema, river_table)
    rainstorm_stations = _normalize_stations(stations, rainfall_threshold_mm)
    result = _empty_result(
        rainstorm_stations,
        rainfall_threshold_mm,
        station_buffer_km,
        downstream_km,
        schema,
        river_table,
        graph_path,
        extra_summary,
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
                cur, schema, river_table, geom_column, objectid_column, river_name_column, station_buffer_km
            )
            start_nodes, direct_keys = _find_direct_graph_starts(
                rainstorm_stations, direct_rows, graph_path, station_buffer_km, direct_match_km
            )
            downstream_edges = _collect_downstream_edges(start_nodes, graph_path, direct_keys, downstream_km)
            downstream_rows = _query_downstream_rows(
                cur, schema, river_table, geom_column, objectid_column, river_name_column,
                downstream_edges, station_buffer_km
            )
    finally:
        if should_close:
            conn.close()

    river_geojson = _build_river_geojson(direct_rows, downstream_rows)
    segments = geojson_to_plot_segments(river_geojson, rainstorm_stations)
    if max_segments and max_segments > 0:
        segments = segments[:max_segments]

    result.update({
        "river_geojson": river_geojson,
        "segments": segments,
        "direct_rivers": _sorted_river_names(direct_rows),
        "downstream_rivers": _sorted_river_names(downstream_rows),
        "affected_rivers": sorted({s["rivername"] for s in segments if s.get("rivername")}),
        "downstream_edges": downstream_edges,
        "river_summary": {
            "direct_feature_count": len(direct_rows),
            "downstream_edge_count": len(downstream_edges),
            "downstream_feature_count": len(downstream_rows),
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
    """把河流 GeoJSON 转成前端专题图常用的线段结构。"""
    result = []
    for feature in river_geojson.get("features", []):
        props = feature.get("properties") or {}
        for index, path in enumerate(_geometry_lines(feature.get("geometry") or {})):
            result.append(_make_plot_segment(path, props, index, stations or []))
    return result


def _validate_params(threshold: float, buffer_km: float, downstream_km: float) -> None:
    if threshold < 0:
        raise ValueError("rainfall_threshold_mm 不能为负数")
    if buffer_km <= 0:
        raise ValueError("station_buffer_km 必须大于 0")
    if downstream_km < 0:
        raise ValueError("downstream_km 不能为负数")


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


def _empty_result(stations: list[dict], threshold: float, buffer_km: float, downstream_km: float, schema: str, table: str, graph_path, extra: dict | None) -> dict:
    result = {
        "status": "ok",
        "params": {
            "rainfall_threshold_mm": float(threshold),
            "station_buffer_km": float(buffer_km),
            "downstream_km": float(downstream_km),
            "river_table": f"{schema}.{table}",
            "graph_path": str(graph_path or _default_graph_path()),
        },
        "impact_stations": stations,
        "station_geojson": _make_station_geojson(stations),
        "direct_rivers": [],
        "downstream_rivers": [],
        "affected_rivers": [],
        "downstream_edges": [],
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


def _normalize_stations(stations: list[dict], threshold_mm: float) -> list[dict]:
    normalized = []
    for station in stations or []:
        lon = _safe_float(station.get("lon"))
        lat = _safe_float(station.get("lat"))
        rainfall = _safe_float(station.get("rain_24h", station.get("rainfall")))
        if lon is None or lat is None or rainfall is None or rainfall < threshold_mm:
            continue
        normalized.append({
            "station_id": station.get("station_id") or station.get("Station_Id_C") or "",
            "station_name": station.get("station_name") or station.get("name") or "",
            "lon": lon,
            "lat": lat,
            "rain_24h": rainfall,
            "rainfall": rainfall,
            "level": station.get("level", ""),
        })
    return sorted(normalized, key=lambda item: item["rain_24h"], reverse=True)


def _ensure_river_columns(cur, schema: str, table: str, geom_col: str, objectid_col: str, river_name_col: str) -> None:
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s", (schema, table))
    columns = {str(row["column_name"]) for row in cur.fetchall()}
    missing = {geom_col, objectid_col, river_name_col} - columns
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
    execute_values(cur, "INSERT INTO tmp_rain24h_impact_stations VALUES %s", rows, template="(%s,%s,%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326))")


def _query_direct_rows(cur, schema: str, table: str, geom_col: str, objectid_col: str, river_name_col: str, buffer_km: float) -> list[dict]:
    cur.execute(f"""
        WITH river_parts AS (
            SELECT r.{_qi(objectid_col)}::text AS objectid,
                   COALESCE(NULLIF(TRIM(r.{_qi(river_name_col)}::text), ''), '未知') AS river_name,
                   (ST_Dump(r.{_qi(geom_col)})).geom AS geom
            FROM {_qi(schema)}.{_qi(table)} r
            WHERE r.{_qi(geom_col)} IS NOT NULL AND NOT ST_IsEmpty(r.{_qi(geom_col)})
        )
        SELECT p.objectid, p.objectid AS id, p.river_name,
               ST_AsGeoJSON(p.geom) AS geom_json,
               ST_Length(p.geom::geography) / 1000.0 AS length_km,
               MIN(ST_Distance(p.geom::geography, s.geom::geography) / 1000.0) AS min_station_distance_km,
               COUNT(DISTINCT s.station_id) AS trigger_station_count,
               jsonb_agg(DISTINCT jsonb_build_object('station_id',s.station_id,'station_name',s.station_name,'lon',s.lon,'lat',s.lat,'rain_24h',s.rain_24h)) AS trigger_stations
        FROM river_parts p
        JOIN tmp_rain24h_impact_stations s ON ST_DWithin(p.geom::geography, s.geom::geography, %(buffer_m)s)
        GROUP BY p.objectid, p.river_name, p.geom
        ORDER BY min_station_distance_km, p.river_name, p.objectid
    """, {"buffer_m": float(buffer_km) * 1000.0})
    return list(cur.fetchall())


def _query_downstream_rows(cur, schema: str, table: str, geom_col: str, objectid_col: str, river_name_col: str, edges: list[dict], buffer_km: float) -> list[dict]:
    if not edges:
        return []
    _create_downstream_temp(cur, edges)
    cur.execute(f"""
        WITH river_parts AS (
            SELECT r.{_qi(objectid_col)}::text AS objectid,
                   COALESCE(NULLIF(TRIM(r.{_qi(river_name_col)}::text), ''), '未知') AS db_river_name,
                   (ST_Dump(r.{_qi(geom_col)})).geom AS geom
            FROM {_qi(schema)}.{_qi(table)} r
            WHERE r.{_qi(geom_col)} IS NOT NULL AND NOT ST_IsEmpty(r.{_qi(geom_col)})
        ), candidates AS (
            SELECT e.*, p.db_river_name, ST_LineMerge(p.geom) AS line_geom,
                   ST_Distance(p.geom::geography, e.pkl_line::geography) / 1000.0 AS match_distance_km
            FROM river_parts p
            JOIN tmp_downstream_edges e ON p.objectid = e.objectid
            WHERE e.pkl_line IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM tmp_rain24h_impact_stations s WHERE ST_DWithin(p.geom::geography, s.geom::geography, %(buffer_m)s))
        ), best_part AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY edge_key ORDER BY match_distance_km) AS rn
            FROM candidates
        ), located AS (
            SELECT *,
                   ST_LineLocatePoint(line_geom, ST_SetSRID(ST_MakePoint(from_x, from_y),4326)) AS from_frac,
                   ST_LineLocatePoint(line_geom, ST_SetSRID(ST_MakePoint(to_x, to_y),4326)) AS to_frac,
                   ST_Length(line_geom::geography) / 1000.0 AS line_km
            FROM best_part
            WHERE rn = 1 AND GeometryType(line_geom) = 'LINESTRING'
        ), clipped AS (
            SELECT edge_key, objectid, COALESCE(NULLIF(TRIM(db_river_name), ''), river_name) AS river_name,
                   min_distance_km, end_distance_km, keep_km, clip_fraction, is_direct_graph_edge, match_distance_km,
                   from_x, from_y, to_x, to_y,
                   ST_Multi(ST_LineSubstring(
                       line_geom,
                       LEAST(from_frac, GREATEST(0.0, LEAST(1.0, from_frac + CASE WHEN to_frac >= from_frac THEN keep_km / line_km ELSE -keep_km / line_km END))),
                       GREATEST(from_frac, GREATEST(0.0, LEAST(1.0, from_frac + CASE WHEN to_frac >= from_frac THEN keep_km / line_km ELSE -keep_km / line_km END)))
                   )) AS geom
            FROM located
            WHERE line_km > 0
        )
        SELECT edge_key, objectid, objectid AS id, river_name,
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
    return list(cur.fetchall())


def _create_downstream_temp(cur, edges: list[dict]) -> None:
    from psycopg2.extras import execute_values
    cur.execute("DROP TABLE IF EXISTS tmp_downstream_edges")
    cur.execute("""
        CREATE TEMP TABLE tmp_downstream_edges(
            edge_key text, objectid text, river_name text,
            min_distance_km double precision, end_distance_km double precision,
            keep_km double precision, clip_fraction double precision, is_direct_graph_edge boolean,
            from_x double precision, from_y double precision, to_x double precision, to_y double precision,
            pkl_line geometry(LineString,4326)
        ) ON COMMIT DROP
    """)
    rows = []
    for e in edges:
        fx, fy, tx, ty = e["from_x"], e["from_y"], e["to_x"], e["to_y"]
        rows.append((e["edge_key"], e["objectid"], e["river_name"], e["min_distance_km"], e["end_distance_km"], e["keep_km"], e["clip_fraction"], bool(e["is_direct_graph_edge"]), fx, fy, tx, ty, fx, fy, tx, ty, fx, fy, tx, ty))
    execute_values(cur, "INSERT INTO tmp_downstream_edges VALUES %s", rows, template="""
        (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
         CASE WHEN %s IS NULL OR %s IS NULL OR %s IS NULL OR %s IS NULL THEN NULL
              ELSE ST_SetSRID(ST_MakeLine(ST_MakePoint(%s,%s), ST_MakePoint(%s,%s)),4326) END)
    """)


def _find_direct_graph_starts(stations: list[dict], direct_rows: list[dict], graph_path, buffer_km: float, direct_match_km: float) -> tuple[dict[Any, float], set[str]]:
    """从真实直接影响河段匹配到的 pkl 边启动下游追踪。

    直接影响河段仍由 PostGIS 按暴雨站 30km 查询；下游追踪起点不能再直接使用
    “pkl 边距离暴雨站 30km”条件，否则一个暴雨站会把周边不相连的河系一并起追。
    """
    graph = get_graph(graph_path)
    direct_refs = _direct_refs(direct_rows)
    if not direct_refs:
        return {}, set()

    starts: dict[Any, float] = {}
    direct_keys: set[str] = set()
    for u, v, key, attr in iter_graph_edges(graph):
        p1, p2 = _edge_points(u, v)
        if p1 is None or p2 is None:
            continue
        if not _edge_matches_direct_part(attr, p1, p2, direct_refs, direct_match_km):
            continue
        direct_keys.add(_edge_key(u, v, key, attr))
        starts[v] = 0.0
    return starts, direct_keys


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


def _save_downstream_edge(edges: dict[str, dict], u, v, key, attr: dict, start_km: float, limit_km: float, direct_keys: set[str]) -> float:
    objectid = _edge_objectid_key(attr)
    river_name = get_edge_river_name(attr)
    length_km = get_edge_length_km(attr)
    if not objectid or not river_name:
        return start_km + length_km
    keep_km = max(min(limit_km - start_km, length_km), 0.0) if length_km > 0 else 0.0
    if keep_km <= 0:
        return start_km + length_km
    edge_key = _edge_key(u, v, key, attr)
    old = edges.get(edge_key)
    if old and old["min_distance_km"] <= start_km:
        return start_km + length_km
    from_x, from_y = _parse_node_xy(u)
    to_x, to_y = _parse_node_xy(v)
    edges[edge_key] = {
        "edge_key": edge_key,
        "objectid": objectid,
        "river_name": river_name,
        "min_distance_km": round(float(start_km), 3),
        "end_distance_km": round(float(start_km + keep_km), 3),
        "keep_km": round(float(keep_km), 3),
        "clip_fraction": round(float(keep_km / length_km), 8) if length_km > 0 else 1.0,
        "is_direct_graph_edge": edge_key in direct_keys,
        "from_x": from_x,
        "from_y": from_y,
        "to_x": to_x,
        "to_y": to_y,
    }
    return start_km + length_km


def _build_river_geojson(direct_rows: list[dict], downstream_rows: list[dict]) -> dict:
    features, seen = [], set()
    for row, impact_type in [(r, "direct_buffer") for r in direct_rows] + [(r, "downstream_50km") for r in downstream_rows]:
        feature = _river_feature(row, impact_type)
        if not feature:
            continue
        key = (impact_type, feature["properties"].get("edge_key"), feature["properties"].get("objectid"), json.dumps(feature["geometry"], ensure_ascii=False, sort_keys=True))
        if key not in seen:
            seen.add(key)
            features.append(feature)
    features.sort(key=lambda f: (0 if f["properties"]["impact_type"] == "direct_buffer" else 1, f["properties"].get("river_name") or ""))
    return {"type": "FeatureCollection", "features": features}


def _river_feature(row: dict, impact_type: str) -> dict | None:
    geometry = _geometry_from_row(row)
    if not geometry:
        return None
    if impact_type == "downstream_50km":
        geometry = _orient_downstream_geometry_to_flow(geometry, row)
    props = {
        "impact_type": impact_type,
        "river_name": row.get("river_name") or "未知",
        "id": row.get("id"),
        "objectid": row.get("objectid"),
        "edge_key": row.get("edge_key"),
        "length_km": round(float(row.get("length_km") or 0.0), 3),
    }
    if impact_type == "direct_buffer":
        props.update({
            "min_station_distance_km": round(float(row.get("min_station_distance_km") or 0.0), 3),
            "trigger_station_count": int(row.get("trigger_station_count") or 0),
            "trigger_stations": row.get("trigger_stations") or [],
            "geometry_source": "full_v5_direct_30km_uncut",
        })
    else:
        from_point = _row_point(row, "from")
        to_point = _row_point(row, "to")
        props.update({
            "min_downstream_distance_km": row.get("min_downstream_distance_km"),
            "end_downstream_distance_km": row.get("end_downstream_distance_km"),
            "keep_km": row.get("keep_km"),
            "clip_fraction": row.get("clip_fraction"),
            "is_direct_graph_edge": row.get("is_direct_graph_edge"),
            "match_distance_km": _round(row.get("match_distance_km")),
            "flow_direction": "pkl_from_to",
            "flow_from": {"lon": from_point[0], "lat": from_point[1]} if from_point else None,
            "flow_to": {"lon": to_point[0], "lat": to_point[1]} if to_point else None,
            "geometry_source": "full_v5_downstream_50km_clipped_flow_oriented",
        })
    return {"type": "Feature", "geometry": geometry, "properties": props}


def _orient_downstream_geometry_to_flow(geometry: dict, row: dict) -> dict:
    """把 downstream_50km 的 GeoJSON 坐标顺序校正为 pkl from->to 流向。"""
    from_point = _row_point(row, "from")
    to_point = _row_point(row, "to")
    if not from_point or not to_point or not isinstance(geometry, dict):
        return geometry

    geom_type = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if geom_type == "LineString":
        oriented = _orient_line_to_flow(coords, from_point, to_point)
        return {**geometry, "coordinates": oriented} if oriented else geometry
    if geom_type == "MultiLineString":
        oriented = _orient_multiline_to_flow(coords, from_point, to_point)
        return {**geometry, "coordinates": oriented} if oriented else geometry
    return geometry


def _row_point(row: dict, prefix: str) -> tuple[float, float] | None:
    lon = _safe_float(row.get(f"{prefix}_x"))
    lat = _safe_float(row.get(f"{prefix}_y"))
    if lon is None or lat is None or not math.isfinite(lon) or not math.isfinite(lat):
        return None
    return lon, lat


def _orient_line_to_flow(line: list, from_point: tuple[float, float], to_point: tuple[float, float]) -> list | None:
    if not isinstance(line, list) or len(line) < 2:
        return None
    forward = _line_flow_score(line, from_point, to_point)
    reverse = _line_flow_score(list(reversed(line)), from_point, to_point)
    return list(reversed(line)) if reverse < forward else line


def _orient_multiline_to_flow(lines: list, from_point: tuple[float, float], to_point: tuple[float, float]) -> list | None:
    valid_lines = [line for line in lines if isinstance(line, list) and len(line) >= 2]
    if not valid_lines:
        return None
    forward = _multiline_flow_score(valid_lines, from_point, to_point)
    reversed_lines = [list(reversed(line)) for line in reversed(valid_lines)]
    reverse = _multiline_flow_score(reversed_lines, from_point, to_point)
    return reversed_lines if reverse < forward else valid_lines


def _line_flow_score(line: list, from_point: tuple[float, float], to_point: tuple[float, float]) -> float:
    if len(line) < 2 or not _is_xy(line[0]) or not _is_xy(line[-1]):
        return math.inf
    start = (float(line[0][0]), float(line[0][1]))
    end = (float(line[-1][0]), float(line[-1][1]))
    return _haversine_km(start[0], start[1], from_point[0], from_point[1]) + _haversine_km(end[0], end[1], to_point[0], to_point[1])


def _multiline_flow_score(lines: list, from_point: tuple[float, float], to_point: tuple[float, float]) -> float:
    first = lines[0][0]
    last = lines[-1][-1]
    if not _is_xy(first) or not _is_xy(last):
        return math.inf
    start = (float(first[0]), float(first[1]))
    end = (float(last[0]), float(last[1]))
    return _haversine_km(start[0], start[1], from_point[0], from_point[1]) + _haversine_km(end[0], end[1], to_point[0], to_point[1])


def _is_xy(value: Any) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return False
    try:
        x = float(value[0])
        y = float(value[1])
    except (TypeError, ValueError):
        return False
    return math.isfinite(x) and math.isfinite(y)


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
        "flow_from": props.get("flow_from"),
        "flow_to": props.get("flow_to"),
        "geometry_source": props.get("geometry_source"),
    }


def _direct_refs(rows: list[dict]) -> list[dict]:
    refs = []
    for row in rows:
        geometry = _geometry_from_row(row)
        if geometry:
            refs.append({"objectid": str(row.get("objectid") or ""), "river_name": str(row.get("river_name") or ""), "lines": _geometry_lines(geometry)})
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
    if geometry.get("type") == "LineString":
        coords = geometry.get("coordinates") or []
        return [coords] if len(coords) >= 2 else []
    if geometry.get("type") == "MultiLineString":
        return [line for line in geometry.get("coordinates") or [] if len(line) >= 2]
    return []


def _edge_to_line_km(p1: tuple[float, float], p2: tuple[float, float], line: list[list[float]]) -> float:
    values = [_point_to_line_km(p1[0], p1[1], line), _point_to_line_km(p2[0], p2[1], line)]
    values.extend(_point_to_segment_km(float(p[0]), float(p[1]), p1, p2) for p in line)
    return min(values)


def _point_to_line_km(lon: float, lat: float, line: list[list[float]]) -> float:
    return min(_point_to_segment_km(lon, lat, (float(line[i][0]), float(line[i][1])), (float(line[i + 1][0]), float(line[i + 1][1]))) for i in range(len(line) - 1)) if len(line) >= 2 else math.inf


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
    distances = [_point_to_line_km(float(s["lon"]), float(s["lat"]), line) for s in stations if s.get("lon") is not None and s.get("lat") is not None]
    return min(distances) if distances else None


def _line_length_km(line: list[list[float]]) -> float:
    return sum(_haversine_km(float(line[i][0]), float(line[i][1]), float(line[i + 1][0]), float(line[i + 1][1])) for i in range(max(len(line) - 1, 0)))


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
    except Exception:
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
    return {field: _jsonable(row.get(field)) for field in ["station_id", "station_name", "province", "city", "cnty", "town", "lon", "lat", "rain_24h", "obs_count", "valid_pre_count", "start_time", "end_time"] if field in row.index}


def _make_station_geojson(stations: list[dict]) -> dict:
    features = []
    for s in stations:
        if s.get("lon") is None or s.get("lat") is None:
            continue
        props = dict(s)
        props.pop("lon", None)
        props.pop("lat", None)
        features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [float(s["lon"]), float(s["lat"])]}, "properties": props})
    return {"type": "FeatureCollection", "features": features}


def _jsonable(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
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


def _sorted_river_names(rows: list[dict]) -> list[str]:
    return sorted({str(row.get("river_name") or "").strip() for row in rows if row.get("river_name")})


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
        if value is None or not str(value).strip():
            continue
        text = str(value).strip()
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
        here.parents[1] / "Service" / "river_directed_v5.pkl",
        here.parents[1] / "Service" / "river_directed_v4_asis.pkl",
        Path.cwd() / "Service" / "river_directed_v5.pkl",
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
