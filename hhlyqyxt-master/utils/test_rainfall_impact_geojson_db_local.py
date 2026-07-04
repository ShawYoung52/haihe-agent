r"""本地 DB 版测试：5 分钟 CSV -> 24 小时降水 -> 真实影响河流 GeoJSON。

固定口径：
- 30km 缓冲区只用于判断直接影响，不截断直接命中的真实河段；
- 直接河流按 full_v5 的 ST_Dump 后单个真实线段判断，避免同一 MultiLineString 里的远处碎线被带出；
- 下游 50km 按 river_directed_v5.pkl 拓扑追踪；
- 下游起点同时来自 30km 内 pkl 边、以及直接命中真实河段附近匹配到的 pkl 边；
- 下游回 full_v5 时按 pkl 边位置匹配最近真实线段，并只输出 50km 范围内的截断线段。
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
import os
import re
import sys
from itertools import count
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote_plus

import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for path in (PROJECT_ROOT, CURRENT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import rainfall_impact_geojson as rig  # noqa: E402

DEFAULT_CSV_PATH = r"C:\Users\gaozr\Downloads\24hourmindata.csv"
DEFAULT_GRAPH_PATH = r"E:\tj\line\result\river_directed_v5.pkl"
DEFAULT_OUTPUT_DIR = r"C:\Users\gaozr\Downloads\24hourmindata_db_impact_output"
DEFAULT_DB_HOST = "211.157.132.19"
DEFAULT_DB_PORT = 48091
DEFAULT_DB_NAME = "hhly"
DEFAULT_DB_USER = "postgres"
DEFAULT_DB_SCHEMA = "public"
DEFAULT_RIVER_TABLE = "haihe_river_directed_full_v5"
DEFAULT_RIVER_GEOM_COLUMN = "geom"
KM_PER_DEG = 111.32


def quote_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name or "")):
        raise ValueError(f"非法 SQL 标识符：{name!r}")
    return f'"{name}"'


def first_col(columns: set[str], names: Iterable[str]) -> str | None:
    for name in names:
        if name in columns:
            return name
    return None


def river_name_expr(columns: set[str], alias: str = "r") -> str:
    fields = [c for c in ("river_name", "rivername", "src_name", "name") if c in columns]
    if not fields:
        return "'未知'"
    prefix = f"{quote_ident(alias)}."
    parts = [f"NULLIF(TRIM({prefix}{quote_ident(c)}::text), '')" for c in fields]
    return f"COALESCE({', '.join(parts)}, '未知')"


def json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)


def connect_db(args: argparse.Namespace):
    if not args.db_password:
        raise ValueError("缺少数据库密码：请设置 HHLY_DB_PASSWORD 或传入 --db-password")
    return psycopg2.connect(
        host=args.db_host,
        port=args.db_port,
        dbname=args.db_name,
        user=args.db_user,
        password=args.db_password,
        sslmode=args.db_sslmode,
        connect_timeout=args.db_connect_timeout,
    )


def table_columns(cur, schema: str, table: str) -> set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
        (schema, table),
    )
    return {str(row["column_name"]) for row in cur.fetchall()}


def create_station_temp(cur, stations: list[dict], srid: int) -> None:
    cur.execute("DROP TABLE IF EXISTS tmp_rain_impact_stations")
    cur.execute(f"""
        CREATE TEMP TABLE tmp_rain_impact_stations(
            station_id text,
            station_name text,
            lon double precision,
            lat double precision,
            rain_24h double precision,
            geom geometry(Point,{int(srid)})
        ) ON COMMIT DROP
    """)
    rows = []
    for station in stations:
        lon = float(station["lon"])
        lat = float(station["lat"])
        rows.append((
            str(station.get("station_id") or ""),
            str(station.get("station_name") or ""),
            lon,
            lat,
            float(station.get("rain_24h") or 0.0),
            lon,
            lat,
        ))
    if rows:
        execute_values(
            cur,
            "INSERT INTO tmp_rain_impact_stations VALUES %s",
            rows,
            template=f"(%s,%s,%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),{int(srid)}))",
        )


def query_direct_river_parts(cur, *, schema: str, table: str, columns: set[str], geom_col: str, buffer_km: float) -> list[dict]:
    objectid_col = first_col(columns, ("objectid", "OBJECTID", "id", "gid"))
    id_col = first_col(columns, ("id", "gid"))
    objectid_expr = f"r.{quote_ident(objectid_col)}::text" if objectid_col else "NULL::text"
    id_expr = f"r.{quote_ident(id_col)}::text" if id_col else objectid_expr
    name_expr = river_name_expr(columns, alias="r")
    q_schema = quote_ident(schema)
    q_table = quote_ident(table)
    q_geom = quote_ident(geom_col)
    cur.execute(f"""
        WITH river_parts AS (
            SELECT
                {id_expr} AS id,
                {objectid_expr} AS objectid,
                {name_expr} AS river_name,
                (ST_Dump(r.{q_geom})).geom AS geom
            FROM {q_schema}.{q_table} r
            WHERE r.{q_geom} IS NOT NULL
              AND NOT ST_IsEmpty(r.{q_geom})
        )
        SELECT
            p.id,
            p.objectid,
            p.river_name,
            ST_AsGeoJSON(p.geom) AS geom_json,
            ST_Length(p.geom::geography) / 1000.0 AS length_km,
            MIN(ST_Distance(p.geom::geography, s.geom::geography) / 1000.0) AS min_station_distance_km,
            COUNT(DISTINCT s.station_id) AS trigger_station_count,
            jsonb_agg(DISTINCT jsonb_build_object(
                'station_id', s.station_id,
                'station_name', s.station_name,
                'lon', s.lon,
                'lat', s.lat,
                'rain_24h', s.rain_24h
            )) AS trigger_stations
        FROM river_parts p
        JOIN tmp_rain_impact_stations s
          ON ST_DWithin(p.geom::geography, s.geom::geography, %(buffer_m)s)
        WHERE p.geom IS NOT NULL
          AND NOT ST_IsEmpty(p.geom)
        GROUP BY p.id, p.objectid, p.river_name, p.geom
        ORDER BY min_station_distance_km, p.river_name, p.objectid
    """, {"buffer_m": float(buffer_km) * 1000.0})
    return list(cur.fetchall())


def parse_node_xy(node: Any) -> tuple[float | None, float | None]:
    try:
        if isinstance(node, str) and "," in node:
            x, y = node.split(",", 1)
            return float(x), float(y)
        if isinstance(node, (tuple, list)) and len(node) >= 2:
            return float(node[0]), float(node[1])
    except Exception:
        pass
    return None, None


def point_to_segment_km(lon: float, lat: float, p1: tuple[float, float], p2: tuple[float, float]) -> float:
    lon1, lat1 = p1
    lon2, lat2 = p2
    mean_lat = math.radians((lat + lat1 + lat2) / 3.0)
    x = lon * math.cos(mean_lat) * KM_PER_DEG
    y = lat * KM_PER_DEG
    x1 = lon1 * math.cos(mean_lat) * KM_PER_DEG
    y1 = lat1 * KM_PER_DEG
    x2 = lon2 * math.cos(mean_lat) * KM_PER_DEG
    y2 = lat2 * KM_PER_DEG
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x - x1, y - y1)
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))


def point_to_line_km(lon: float, lat: float, line: list[list[float]]) -> float:
    if len(line) < 2:
        return math.inf
    return min(
        point_to_segment_km(lon, lat, (float(line[i][0]), float(line[i][1])), (float(line[i + 1][0]), float(line[i + 1][1])))
        for i in range(len(line) - 1)
    )


def edge_to_line_km(p1: tuple[float, float], p2: tuple[float, float], line: list[list[float]]) -> float:
    if len(line) < 2:
        return math.inf
    candidates = [
        point_to_line_km(p1[0], p1[1], line),
        point_to_line_km(p2[0], p2[1], line),
    ]
    candidates.extend(point_to_segment_km(float(pt[0]), float(pt[1]), p1, p2) for pt in line)
    return min(candidates)


def geometry_lines(geometry: dict) -> list[list[list[float]]]:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "LineString":
        return [coords] if len(coords) >= 2 else []
    if gtype == "MultiLineString":
        return [line for line in coords if len(line) >= 2]
    return []


def direct_part_refs(direct_rows: list[dict]) -> list[dict]:
    refs = []
    for row in direct_rows:
        geometry = geometry_from_row(row)
        if not geometry:
            continue
        lines = geometry_lines(geometry)
        if not lines:
            continue
        refs.append({
            "objectid": str(row.get("objectid") or "").strip(),
            "river_name": str(row.get("river_name") or "").strip(),
            "lines": lines,
        })
    return refs


def graph_edge_key(source_node: Any, target_node: Any, key: Any, attr: dict) -> str:
    objectid = rig._edge_objectid_key(attr) or ""
    river_name = rig.get_edge_river_name(attr) or ""
    return f"{source_node}|{target_node}|{key}|{objectid}|{river_name}"


def matches_direct_part(
    edge_objectid: str,
    edge_river_name: str,
    p1: tuple[float, float],
    p2: tuple[float, float],
    refs: list[dict],
    max_distance_km: float,
) -> bool:
    for ref in refs:
        ref_objectid = ref["objectid"]
        ref_river_name = ref["river_name"]
        same_objectid = bool(edge_objectid and ref_objectid and edge_objectid == ref_objectid)
        same_name = bool(edge_river_name and ref_river_name and edge_river_name == ref_river_name)
        if not same_objectid and not same_name:
            continue
        if any(edge_to_line_km(p1, p2, line) <= max_distance_km for line in ref["lines"]):
            return True
    return False


def find_direct_graph_starts(
    stations: list[dict],
    direct_rows: list[dict],
    graph_path: str,
    buffer_km: float,
    direct_match_km: float,
) -> tuple[dict[Any, float], set[str], dict]:
    graph = rig.get_graph(graph_path)
    station_points = [(float(s["lon"]), float(s["lat"])) for s in stations]
    refs = direct_part_refs(direct_rows)
    start_nodes: dict[Any, float] = {}
    direct_edge_keys: set[str] = set()
    station_edge_count = 0
    matched_part_edge_count = 0

    for u, v, key, attr in rig.iter_graph_edges(graph):
        ux, uy = parse_node_xy(u)
        vx, vy = parse_node_xy(v)
        if ux is None or uy is None or vx is None or vy is None:
            continue
        p1 = (ux, uy)
        p2 = (vx, vy)
        edge_key = graph_edge_key(u, v, key, attr)
        station_dist = min(point_to_segment_km(lon, lat, p1, p2) for lon, lat in station_points)
        station_hit = station_dist <= float(buffer_km)
        edge_objectid = str(rig._edge_objectid_key(attr) or "").strip()
        edge_river_name = str(rig.get_edge_river_name(attr) or "").strip()
        direct_part_hit = matches_direct_part(edge_objectid, edge_river_name, p1, p2, refs, float(direct_match_km))

        if station_hit or direct_part_hit:
            direct_edge_keys.add(edge_key)
            start_nodes[v] = 0.0
            station_edge_count += int(station_hit)
            matched_part_edge_count += int(direct_part_hit and not station_hit)

    stats = {
        "direct_graph_edge_count": len(direct_edge_keys),
        "station_buffer_graph_edge_count": station_edge_count,
        "direct_part_matched_graph_edge_count": matched_part_edge_count,
        "direct_part_match_km": float(direct_match_km),
    }
    return start_nodes, direct_edge_keys, stats


def collect_downstream_segments(start_nodes: dict[Any, float], *, graph_path: str, direct_edge_keys: set[str], downstream_km: float) -> tuple[dict[str, dict], list[dict]]:
    limit = float(downstream_km)
    if not start_nodes or limit <= 0:
        return {}, []

    graph = rig.get_graph(graph_path)
    best_dist = dict(start_nodes)
    seq = count()
    heap = [(float(dist), next(seq), node) for node, dist in start_nodes.items()]
    heapq.heapify(heap)
    river_map: dict[str, dict] = {}
    segment_map: dict[str, dict] = {}

    while heap:
        curr_dist, _seq, node = heapq.heappop(heap)
        if curr_dist > best_dist.get(node, math.inf) or curr_dist >= limit:
            continue
        for u, v, key, attr in rig.iter_out_edges(graph, node):
            objectid = str(rig._edge_objectid_key(attr) or "").strip()
            river_name = str(rig.get_edge_river_name(attr) or "").strip()
            if not objectid or not river_name:
                continue

            edge_len = max(float(rig.get_edge_length_km(attr, attr_name="length_km") or 0.0), 0.0)
            next_dist = curr_dist + edge_len
            keep_km = max(min(limit - curr_dist, edge_len), 0.0) if edge_len > 0 else 0.0
            clip_fraction = 1.0 if edge_len <= 0 else max(min(keep_km / edge_len, 1.0), 0.0)
            if clip_fraction > 0:
                edge_key = graph_edge_key(u, v, key, attr)
                from_x, from_y = parse_node_xy(u)
                to_x, to_y = parse_node_xy(v)
                old = segment_map.get(edge_key)
                if old is None or curr_dist < old["min_distance_km"] or clip_fraction > old["clip_fraction"]:
                    segment_map[edge_key] = {
                        "edge_key": edge_key,
                        "objectid": objectid,
                        "river_name": river_name,
                        "min_distance_km": round(float(curr_dist), 3),
                        "end_distance_km": round(float(curr_dist + keep_km), 3),
                        "keep_km": round(float(keep_km), 3),
                        "clip_fraction": round(float(clip_fraction), 8),
                        "is_direct_graph_edge": edge_key in direct_edge_keys,
                        "from_x": from_x,
                        "from_y": from_y,
                        "to_x": to_x,
                        "to_y": to_y,
                    }
                item = river_map.setdefault(river_name, {"river_name": river_name, "min_distance_km": math.inf})
                item["min_distance_km"] = min(item["min_distance_km"], curr_dist)

            if next_dist <= limit and next_dist < best_dist.get(v, math.inf):
                best_dist[v] = next_dist
                heapq.heappush(heap, (next_dist, next(seq), v))

    for item in river_map.values():
        item["min_distance_km"] = round(float(item["min_distance_km"]), 3)
    segments = sorted(segment_map.values(), key=lambda x: (x["min_distance_km"], x["river_name"], x["edge_key"]))
    return river_map, segments


def create_downstream_temp(cur, segments: list[dict]) -> None:
    cur.execute("DROP TABLE IF EXISTS tmp_downstream_segments")
    cur.execute("""
        CREATE TEMP TABLE tmp_downstream_segments(
            edge_key text,
            objectid text,
            river_name text,
            min_distance_km double precision,
            end_distance_km double precision,
            keep_km double precision,
            clip_fraction double precision,
            is_direct_graph_edge boolean,
            from_x double precision,
            from_y double precision,
            to_x double precision,
            to_y double precision,
            pkl_line geometry(LineString,4326)
        ) ON COMMIT DROP
    """)
    rows = []
    for segment in segments:
        fx, fy, tx, ty = segment.get("from_x"), segment.get("from_y"), segment.get("to_x"), segment.get("to_y")
        rows.append((
            segment["edge_key"], segment["objectid"], segment["river_name"], segment["min_distance_km"],
            segment["end_distance_km"], segment["keep_km"], segment["clip_fraction"], bool(segment.get("is_direct_graph_edge")),
            fx, fy, tx, ty, fx, fy, tx, ty, fx, fy, tx, ty,
        ))
    if rows:
        cur.executemany("""
            INSERT INTO tmp_downstream_segments VALUES(
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                CASE WHEN %s IS NULL OR %s IS NULL OR %s IS NULL OR %s IS NULL THEN NULL
                     ELSE ST_SetSRID(ST_MakeLine(ST_MakePoint(%s,%s), ST_MakePoint(%s,%s)),4326) END
            )
        """, rows)


def query_downstream_river_parts(cur, *, schema: str, table: str, columns: set[str], geom_col: str, segments: list[dict], buffer_km: float) -> list[dict]:
    if not segments:
        return []
    objectid_col = first_col(columns, ("objectid", "OBJECTID", "id", "gid"))
    id_col = first_col(columns, ("id", "gid"))
    if not objectid_col:
        return []

    create_downstream_temp(cur, segments)
    objectid_expr = f"r.{quote_ident(objectid_col)}::text"
    id_expr = f"r.{quote_ident(id_col)}::text" if id_col else objectid_expr
    name_expr = river_name_expr(columns, alias="r")
    q_schema = quote_ident(schema)
    q_table = quote_ident(table)
    q_geom = quote_ident(geom_col)
    cur.execute(f"""
        WITH river_parts AS (
            SELECT
                {id_expr} AS id,
                {objectid_expr} AS objectid,
                {name_expr} AS db_river_name,
                (ST_Dump(r.{q_geom})).geom AS geom
            FROM {q_schema}.{q_table} r
            WHERE r.{q_geom} IS NOT NULL
              AND NOT ST_IsEmpty(r.{q_geom})
        ),
        candidates AS (
            SELECT
                ds.edge_key,
                ds.objectid,
                ds.river_name AS graph_river_name,
                ds.min_distance_km,
                ds.end_distance_km,
                ds.keep_km,
                ds.clip_fraction,
                ds.is_direct_graph_edge,
                ds.from_x,
                ds.from_y,
                ds.to_x,
                ds.to_y,
                ds.pkl_line,
                p.id,
                p.db_river_name,
                p.geom AS original_geom,
                ST_LineMerge(p.geom) AS merged_geom,
                CASE WHEN ds.pkl_line IS NULL THEN NULL
                     ELSE ST_Distance(p.geom::geography, ds.pkl_line::geography) / 1000.0 END AS match_distance_km
            FROM river_parts p
            JOIN tmp_downstream_segments ds ON p.objectid = ds.objectid
            WHERE p.geom IS NOT NULL
              AND NOT ST_IsEmpty(p.geom)
              AND NOT EXISTS (
                  SELECT 1 FROM tmp_rain_impact_stations s
                  WHERE ST_DWithin(p.geom::geography, s.geom::geography, %(buffer_m)s)
              )
        ),
        ranked AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY edge_key ORDER BY match_distance_km NULLS LAST) AS rn
            FROM candidates
        ),
        located AS (
            SELECT *, CASE WHEN GeometryType(merged_geom) = 'LINESTRING' THEN merged_geom ELSE NULL END AS line_geom
            FROM ranked
            WHERE rn = 1
        ),
        fractions AS (
            SELECT *,
                CASE WHEN line_geom IS NULL OR from_x IS NULL OR from_y IS NULL THEN NULL
                     ELSE ST_LineLocatePoint(line_geom, ST_SetSRID(ST_MakePoint(from_x, from_y),4326)) END AS from_frac,
                CASE WHEN line_geom IS NULL OR to_x IS NULL OR to_y IS NULL THEN NULL
                     ELSE ST_LineLocatePoint(line_geom, ST_SetSRID(ST_MakePoint(to_x, to_y),4326)) END AS to_frac,
                CASE WHEN line_geom IS NULL THEN NULL ELSE ST_Length(line_geom::geography) / 1000.0 END AS line_km
            FROM located
        ),
        clipped AS (
            SELECT
                edge_key,
                id,
                objectid,
                COALESCE(NULLIF(TRIM(db_river_name), ''), graph_river_name) AS river_name,
                min_distance_km,
                end_distance_km,
                keep_km,
                clip_fraction,
                is_direct_graph_edge,
                match_distance_km,
                CASE
                    WHEN line_geom IS NOT NULL AND from_frac IS NOT NULL AND to_frac IS NOT NULL AND line_km > 0 THEN
                        ST_Multi(ST_LineSubstring(
                            line_geom,
                            LEAST(
                                from_frac,
                                GREATEST(0.0, LEAST(1.0, from_frac + CASE WHEN to_frac >= from_frac THEN keep_km / line_km ELSE -keep_km / line_km END))
                            ),
                            GREATEST(
                                from_frac,
                                GREATEST(0.0, LEAST(1.0, from_frac + CASE WHEN to_frac >= from_frac THEN keep_km / line_km ELSE -keep_km / line_km END))
                            )
                        ))
                    WHEN line_geom IS NOT NULL AND clip_fraction < 0.999999 THEN ST_Multi(ST_LineSubstring(line_geom, 0, clip_fraction))
                    WHEN line_geom IS NOT NULL THEN line_geom
                    ELSE original_geom
                END AS clipped_geom
            FROM fractions
        )
        SELECT
            edge_key,
            id,
            objectid,
            river_name,
            min_distance_km AS min_downstream_distance_km,
            end_distance_km AS end_downstream_distance_km,
            keep_km,
            clip_fraction,
            is_direct_graph_edge,
            match_distance_km,
            ST_AsGeoJSON(clipped_geom) AS geom_json,
            ST_Length(clipped_geom::geography) / 1000.0 AS length_km
        FROM clipped
        WHERE clipped_geom IS NOT NULL
          AND NOT ST_IsEmpty(clipped_geom)
        ORDER BY min_distance_km, river_name, edge_key
    """, {"buffer_m": float(buffer_km) * 1000.0})
    return list(cur.fetchall())


def geometry_from_row(row: dict) -> dict | None:
    raw = row.get("geom_json")
    if not raw:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw


def feature_from_direct_row(row: dict) -> dict | None:
    geometry = geometry_from_row(row)
    if not geometry:
        return None
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "impact_type": "direct_buffer",
            "river_name": row.get("river_name") or "未知",
            "id": row.get("id"),
            "objectid": row.get("objectid"),
            "length_km": round(float(row.get("length_km") or 0.0), 3),
            "min_station_distance_km": round(float(row.get("min_station_distance_km") or 0.0), 3),
            "trigger_station_count": int(row.get("trigger_station_count") or 0),
            "trigger_stations": row.get("trigger_stations") or [],
            "geometry_source": "full_v5_dump_part_direct_30km_uncut",
        },
    }


def feature_from_downstream_row(row: dict) -> dict | None:
    geometry = geometry_from_row(row)
    if not geometry:
        return None
    match_distance = row.get("match_distance_km")
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "impact_type": "downstream_50km",
            "river_name": row.get("river_name") or "未知",
            "id": row.get("id"),
            "objectid": row.get("objectid"),
            "edge_key": row.get("edge_key"),
            "length_km": round(float(row.get("length_km") or 0.0), 3),
            "min_downstream_distance_km": row.get("min_downstream_distance_km"),
            "end_downstream_distance_km": row.get("end_downstream_distance_km"),
            "keep_km": row.get("keep_km"),
            "clip_fraction": row.get("clip_fraction"),
            "is_direct_graph_edge": row.get("is_direct_graph_edge"),
            "match_distance_km": round(float(match_distance), 3) if match_distance is not None else None,
            "geometry_source": "full_v5_dump_part_downstream_50km_clipped_by_pkl_edge",
        },
    }


def build_outputs(args: argparse.Namespace) -> dict:
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    station_df = rig.aggregate_5min_station_pre_to_24h(args.csv)
    impact_df = station_df[
        (station_df["rain_24h"] >= args.rain_threshold_mm)
        & station_df["lon"].notna()
        & station_df["lat"].notna()
    ].copy()
    impact_stations = [rig._station_record(row) for _, row in impact_df.iterrows()]

    station_geojson_path = output / "impact_stations.geojson"
    river_geojson_path = output / "impact_rivers_postgis.geojson"
    top_csv_path = output / "rain24h_top_stations.csv"
    summary_path = output / "summary.json"

    station_geojson_path.write_text(json.dumps(rig._make_station_geojson(impact_stations), ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    station_df.head(max(args.top_station_limit, 0)).to_csv(top_csv_path, index=False, encoding="utf-8-sig")

    direct_rows: list[dict] = []
    downstream_rows: list[dict] = []
    downstream_segments: list[dict] = []
    direct_start_stats = {
        "direct_graph_edge_count": 0,
        "station_buffer_graph_edge_count": 0,
        "direct_part_matched_graph_edge_count": 0,
        "direct_part_match_km": args.direct_graph_match_km,
    }

    if impact_stations:
        conn = connect_db(args)
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                columns = table_columns(cur, args.db_schema, args.river_table)
                if not columns:
                    raise ValueError(f"未找到河流表：{args.db_schema}.{args.river_table}")
                geom_col = args.river_geom_column if args.river_geom_column in columns else first_col(columns, ("geom", "geometry", "wkb_geometry", "the_geom"))
                if not geom_col:
                    raise ValueError("河流表未找到几何字段")

                create_station_temp(cur, impact_stations, args.db_srid)
                direct_rows = query_direct_river_parts(
                    cur,
                    schema=args.db_schema,
                    table=args.river_table,
                    columns=columns,
                    geom_col=geom_col,
                    buffer_km=args.station_buffer_km,
                )
                start_nodes, direct_edge_keys, direct_start_stats = find_direct_graph_starts(
                    impact_stations,
                    direct_rows,
                    args.graph,
                    args.station_buffer_km,
                    args.direct_graph_match_km,
                )
                _downstream_map, downstream_segments = collect_downstream_segments(
                    start_nodes,
                    graph_path=args.graph,
                    direct_edge_keys=direct_edge_keys,
                    downstream_km=args.downstream_km,
                )
                downstream_rows = query_downstream_river_parts(
                    cur,
                    schema=args.db_schema,
                    table=args.river_table,
                    columns=columns,
                    geom_col=geom_col,
                    segments=downstream_segments,
                    buffer_km=args.station_buffer_km,
                )
        finally:
            conn.close()

    features: list[dict] = []
    seen = set()
    for row in direct_rows:
        feature = feature_from_direct_row(row)
        if not feature:
            continue
        key = ("direct", feature["properties"].get("objectid"), json.dumps(feature["geometry"], sort_keys=True))
        if key not in seen:
            seen.add(key)
            features.append(feature)
    for row in downstream_rows:
        feature = feature_from_downstream_row(row)
        if not feature:
            continue
        key = ("downstream", feature["properties"].get("edge_key"))
        if key not in seen:
            seen.add(key)
            features.append(feature)

    features.sort(key=lambda item: (
        0 if item["properties"].get("impact_type") == "direct_buffer" else 1,
        item["properties"].get("river_name") or "",
        item["properties"].get("edge_key") or item["properties"].get("objectid") or "",
    ))
    river_geojson_path.write_text(json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")

    direct_rivers = sorted({str(row.get("river_name") or "") for row in direct_rows if row.get("river_name")})
    downstream_rivers = sorted({str(row.get("river_name") or "") for row in downstream_rows if row.get("river_name")})
    summary = {
        "status": "ok",
        "params": {
            "river_table": args.river_table,
            "rain_threshold_mm": args.rain_threshold_mm,
            "station_buffer_km": args.station_buffer_km,
            "downstream_km": args.downstream_km,
            "direct_graph_match_km": args.direct_graph_match_km,
            "direct_rule": "ST_Dump full_v5 geom, only direct-hit line parts within 30km are output uncut",
            "downstream_rule": "trace pkl topology 50km from station-hit or direct-part-matched graph starts, output clipped dump parts only",
        },
        "station_summary": {
            "total_station_count": int(len(station_df)),
            "impact_station_count": int(len(impact_stations)),
            "max_rain_24h": float(station_df["rain_24h"].max() or 0.0) if len(station_df) else 0.0,
        },
        "river_summary": {
            "direct_feature_count": len(direct_rows),
            "direct_river_count": len(direct_rivers),
            "direct_graph_edge_count": direct_start_stats["direct_graph_edge_count"],
            "station_buffer_graph_edge_count": direct_start_stats["station_buffer_graph_edge_count"],
            "direct_part_matched_graph_edge_count": direct_start_stats["direct_part_matched_graph_edge_count"],
            "downstream_graph_segment_count": len(downstream_segments),
            "downstream_feature_count": len(downstream_rows),
            "downstream_river_count": len(downstream_rivers),
            "geojson_feature_count": len(features),
        },
        "direct_rivers": direct_rivers,
        "downstream_rivers": downstream_rivers,
        "downstream_segments": downstream_segments,
        "outputs": {
            "river_geojson": str(river_geojson_path),
            "station_geojson": str(station_geojson_path),
            "top_stations_csv": str(top_csv_path),
            "summary_json": str(summary_path),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="真实河流 24 小时降水影响 GeoJSON")
    parser.add_argument("--csv", default=DEFAULT_CSV_PATH)
    parser.add_argument("--graph", default=DEFAULT_GRAPH_PATH)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--rain-threshold-mm", type=float, default=50.0)
    parser.add_argument("--station-buffer-km", type=float, default=30.0)
    parser.add_argument("--downstream-km", type=float, default=50.0)
    parser.add_argument("--direct-graph-match-km", type=float, default=15.0)
    parser.add_argument("--top-station-limit", type=int, default=100)
    parser.add_argument("--db-host", default=os.getenv("HHLY_DB_HOST", DEFAULT_DB_HOST))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("HHLY_DB_PORT", DEFAULT_DB_PORT)))
    parser.add_argument("--db-name", default=os.getenv("HHLY_DB_NAME", DEFAULT_DB_NAME))
    parser.add_argument("--db-user", default=os.getenv("HHLY_DB_USER", DEFAULT_DB_USER))
    parser.add_argument("--db-password", default=os.getenv("HHLY_DB_PASSWORD", ""))
    parser.add_argument("--db-schema", default=os.getenv("HHLY_DB_SCHEMA", DEFAULT_DB_SCHEMA))
    parser.add_argument("--db-srid", type=int, default=int(os.getenv("HHLY_DB_SRID", "4326")))
    parser.add_argument("--db-sslmode", default=os.getenv("HHLY_DB_SSLMODE", "disable"))
    parser.add_argument("--db-connect-timeout", type=int, default=int(os.getenv("HHLY_DB_CONNECT_TIMEOUT", "5")))
    parser.add_argument("--river-table", default=os.getenv("HHLY_RIVER_TABLE_FULL", DEFAULT_RIVER_TABLE))
    parser.add_argument("--river-geom-column", default=os.getenv("HHLY_RIVER_GEOM_COLUMN", DEFAULT_RIVER_GEOM_COLUMN))
    return parser.parse_args()


def main() -> None:
    summary = build_outputs(parse_args())
    print("DB 版本地测试完成")
    print(f"渲染河流表：{summary['params']['river_table']}")
    print(f"总站数：{summary['station_summary']['total_station_count']}")
    print(f"触发站数：{summary['station_summary']['impact_station_count']}")
    print(f"直接河流要素：{summary['river_summary']['direct_feature_count']}")
    print(f"pkl直接起点边：{summary['river_summary']['direct_graph_edge_count']}")
    print(f"  - 站点30km命中：{summary['river_summary']['station_buffer_graph_edge_count']}")
    print(f"  - 真实直接河段匹配：{summary['river_summary']['direct_part_matched_graph_edge_count']}")
    print(f"下游图边段：{summary['river_summary']['downstream_graph_segment_count']}")
    print(f"下游河流要素：{summary['river_summary']['downstream_feature_count']}")
    print(f"GeoJSON要素数：{summary['river_summary']['geojson_feature_count']}")
    print("输出文件：")
    for name, path in summary["outputs"].items():
        print(f"  - {name}: {path}")


if __name__ == "__main__":
    main()
