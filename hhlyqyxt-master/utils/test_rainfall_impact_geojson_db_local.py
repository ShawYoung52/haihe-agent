r"""本地 DB 版测试：5 分钟 CSV -> 24 小时降水 -> 真实河流 GeoJSON。

当前口径：
- 30km 缓冲区只用于判断直接影响河流，不截断直接河流；
- 直接河流使用 haihe_river_directed_full_v5 的真实 geom 完整输出；
- 下游 50km 使用 river_directed_v5.pkl 做拓扑追踪，最后一段尽量按比例截断；
- 下游追踪按 pkl 拓扑边去重，不按 river_name，也不按 objectid；
  因为同名河段、甚至同 objectid 河段，都可能是上下游不同拓扑边。

运行：
    cd hhlyqyxt-master
    $env:HHLY_DB_PASSWORD="postgres"
    python utils/test_rainfall_impact_geojson_db_local.py
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

import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for item in (CURRENT_DIR, PROJECT_ROOT):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from rainfall_impact_geojson import (  # noqa: E402
    _edge_objectid_key,
    _make_station_geojson,
    _station_record,
    aggregate_5min_station_pre_to_24h,
    get_edge_length_km,
    get_edge_river_name,
    get_graph,
    iter_graph_edges,
    iter_out_edges,
)

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
EARTH_KM_PER_DEG = 111.32


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
    for s in stations:
        lon = float(s["lon"])
        lat = float(s["lat"])
        rows.append((str(s.get("station_id") or ""), str(s.get("station_name") or ""), lon, lat, float(s.get("rain_24h") or 0.0), lon, lat))
    cur.executemany(
        f"INSERT INTO tmp_rain_impact_stations VALUES(%s,%s,%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),{int(srid)}))",
        rows,
    )


def query_direct_rivers(cur, *, schema: str, table: str, columns: set[str], geom_col: str, buffer_km: float) -> list[dict]:
    objectid_col = first_col(columns, ("objectid", "OBJECTID", "id", "gid"))
    id_col = first_col(columns, ("id", "gid"))
    river_expr = river_name_expr(columns)
    objectid_expr = f"r.{quote_ident(objectid_col)}::text" if objectid_col else "NULL::text"
    id_expr = f"r.{quote_ident(id_col)}::text" if id_col else objectid_expr
    q_schema = quote_ident(schema)
    q_table = quote_ident(table)
    q_geom = quote_ident(geom_col)
    cur.execute(f"""
        SELECT
            {id_expr} AS id,
            {objectid_expr} AS objectid,
            {river_expr} AS river_name,
            ST_AsGeoJSON(r.{q_geom}) AS geom_json,
            ST_Length(r.{q_geom}::geography) / 1000.0 AS length_km,
            MIN(ST_Distance(r.{q_geom}::geography, s.geom::geography) / 1000.0) AS min_station_distance_km,
            COUNT(DISTINCT s.station_id) AS trigger_station_count,
            jsonb_agg(DISTINCT jsonb_build_object(
                'station_id', s.station_id,
                'station_name', s.station_name,
                'lon', s.lon,
                'lat', s.lat,
                'rain_24h', s.rain_24h
            )) AS trigger_stations
        FROM {q_schema}.{q_table} r
        JOIN tmp_rain_impact_stations s
          ON ST_DWithin(r.{q_geom}::geography, s.geom::geography, %(buffer_m)s)
        WHERE r.{q_geom} IS NOT NULL
          AND NOT ST_IsEmpty(r.{q_geom})
        GROUP BY r.{q_geom}, {id_expr}, {objectid_expr}, {river_expr}
        ORDER BY min_station_distance_km, river_name, objectid
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


def graph_edge_key(u: Any, v: Any, key: Any, attr: dict) -> str:
    objectid = _edge_objectid_key(attr) or ""
    river_name = get_edge_river_name(attr) or ""
    return f"{u}|{v}|{key}|{objectid}|{river_name}"


def station_to_segment_km(lon: float, lat: float, p1: tuple[float, float], p2: tuple[float, float]) -> float:
    lon1, lat1 = p1
    lon2, lat2 = p2
    mean_lat = math.radians((lat + lat1 + lat2) / 3.0)
    x = lon * math.cos(mean_lat) * EARTH_KM_PER_DEG
    y = lat * EARTH_KM_PER_DEG
    x1 = lon1 * math.cos(mean_lat) * EARTH_KM_PER_DEG
    y1 = lat1 * EARTH_KM_PER_DEG
    x2 = lon2 * math.cos(mean_lat) * EARTH_KM_PER_DEG
    y2 = lat2 * EARTH_KM_PER_DEG
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x - x1, y - y1)
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
    px = x1 + t * dx
    py = y1 + t * dy
    return math.hypot(x - px, y - py)


def find_direct_graph_edges(stations: list[dict], graph_path: str, buffer_km: float) -> tuple[dict[Any, float], set[str], int]:
    """用 pkl 拓扑边判断哪些具体边在 30km 内。

    不再用 river_name/objectid 判定具体边，因为它们都可能重复。
    """
    graph = get_graph(graph_path)
    start_nodes: dict[Any, float] = {}
    direct_edge_keys: set[str] = set()
    station_points = [(float(s["lon"]), float(s["lat"])) for s in stations]

    for u, v, key, attr in iter_graph_edges(graph):
        ux, uy = parse_node_xy(u)
        vx, vy = parse_node_xy(v)
        if ux is None or uy is None or vx is None or vy is None:
            continue
        min_dist = min(station_to_segment_km(lon, lat, (ux, uy), (vx, vy)) for lon, lat in station_points)
        if min_dist <= float(buffer_km):
            direct_edge_keys.add(graph_edge_key(u, v, key, attr))
            start_nodes[v] = 0.0
    return start_nodes, direct_edge_keys, len(direct_edge_keys)


def collect_downstream_segments(
    start_nodes: dict[Any, float],
    *,
    graph_path: str,
    direct_edge_keys: set[str],
    downstream_km: float,
) -> tuple[dict[str, dict], list[dict]]:
    """向下游追踪 50km，按 pkl 拓扑边 edge_key 去重。"""
    limit = float(downstream_km)
    if not start_nodes or limit <= 0:
        return {}, []
    graph = get_graph(graph_path)
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

        for u, v, key, attr in iter_out_edges(graph, node):
            edge_key = graph_edge_key(u, v, key, attr)
            objectid = str(_edge_objectid_key(attr) or "").strip()
            river_name = get_edge_river_name(attr)
            length_km = max(float(get_edge_length_km(attr, attr_name="length_km") or 0.0), 0.0)
            next_dist = curr_dist + length_km
            keep_km = max(min(limit - curr_dist, length_km), 0.0) if length_km > 0 else 0.0
            clip_fraction = 1.0 if length_km <= 0 else max(min(keep_km / length_km, 1.0), 0.0)

            if edge_key not in direct_edge_keys and objectid and river_name and clip_fraction > 0:
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
                        "from_x": from_x,
                        "from_y": from_y,
                        "to_x": to_x,
                        "to_y": to_y,
                    }
                item = river_map.setdefault(river_name, {"river_name": river_name, "min_distance_km": math.inf})
                if curr_dist < item["min_distance_km"]:
                    item["min_distance_km"] = curr_dist

            if next_dist <= limit and next_dist < best_dist.get(v, math.inf):
                best_dist[v] = next_dist
                heapq.heappush(heap, (next_dist, next(seq), v))

    for item in river_map.values():
        item["min_distance_km"] = round(float(item["min_distance_km"]), 3)
    return river_map, sorted(segment_map.values(), key=lambda x: (x["min_distance_km"], x["river_name"], x["edge_key"]))


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
            from_x double precision,
            from_y double precision,
            to_x double precision,
            to_y double precision
        ) ON COMMIT DROP
    """)
    cur.executemany(
        "INSERT INTO tmp_downstream_segments VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        [(
            s["edge_key"], s["objectid"], s["river_name"], s["min_distance_km"], s["end_distance_km"],
            s["keep_km"], s["clip_fraction"], s.get("from_x"), s.get("from_y"), s.get("to_x"), s.get("to_y")
        ) for s in segments],
    )


def query_downstream_rivers(cur, *, schema: str, table: str, columns: set[str], geom_col: str, segments: list[dict]) -> list[dict]:
    if not segments:
        return []
    create_downstream_temp(cur, segments)
    objectid_col = first_col(columns, ("objectid", "OBJECTID", "id", "gid"))
    id_col = first_col(columns, ("id", "gid"))
    if not objectid_col:
        return []
    objectid_expr = f"r.{quote_ident(objectid_col)}::text"
    id_expr = f"r.{quote_ident(id_col)}::text" if id_col else objectid_expr
    river_expr = river_name_expr(columns)
    q_schema = quote_ident(schema)
    q_table = quote_ident(table)
    q_geom = quote_ident(geom_col)
    q_objectid = quote_ident(objectid_col)
    cur.execute(f"""
        WITH joined AS (
            SELECT
                ds.edge_key,
                {id_expr} AS id,
                {objectid_expr} AS objectid,
                {river_expr} AS db_river_name,
                ds.river_name AS graph_river_name,
                ds.min_distance_km,
                ds.end_distance_km,
                ds.keep_km,
                ds.clip_fraction,
                ds.from_x,
                ds.from_y,
                ds.to_x,
                ds.to_y,
                r.{q_geom} AS original_geom,
                ST_LineMerge(r.{q_geom}) AS merged_geom
            FROM {q_schema}.{q_table} r
            JOIN tmp_downstream_segments ds ON r.{q_objectid}::text = ds.objectid
            WHERE r.{q_geom} IS NOT NULL
              AND NOT ST_IsEmpty(r.{q_geom})
        ),
        located AS (
            SELECT *,
                CASE WHEN GeometryType(merged_geom) = 'LINESTRING' THEN merged_geom ELSE NULL END AS line_geom
            FROM joined
        ),
        fractions AS (
            SELECT *,
                CASE WHEN line_geom IS NULL OR from_x IS NULL OR from_y IS NULL THEN NULL
                     ELSE ST_LineLocatePoint(line_geom, ST_SetSRID(ST_MakePoint(from_x, from_y), 4326)) END AS from_frac,
                CASE WHEN line_geom IS NULL OR to_x IS NULL OR to_y IS NULL THEN NULL
                     ELSE ST_LineLocatePoint(line_geom, ST_SetSRID(ST_MakePoint(to_x, to_y), 4326)) END AS to_frac,
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
                CASE
                    WHEN line_geom IS NOT NULL AND from_frac IS NOT NULL AND to_frac IS NOT NULL AND line_km > 0 THEN
                        ST_Multi(ST_LineSubstring(
                            line_geom,
                            LEAST(from_frac, GREATEST(0.0, LEAST(1.0, from_frac + CASE WHEN to_frac >= from_frac THEN keep_km / line_km ELSE -keep_km / line_km END))),
                            GREATEST(from_frac, GREATEST(0.0, LEAST(1.0, from_frac + CASE WHEN to_frac >= from_frac THEN keep_km / line_km ELSE -keep_km / line_km END)))
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
            ST_AsGeoJSON(clipped_geom) AS geom_json,
            ST_Length(clipped_geom::geography) / 1000.0 AS length_km
        FROM clipped
        WHERE clipped_geom IS NOT NULL
          AND NOT ST_IsEmpty(clipped_geom)
        ORDER BY min_distance_km, river_name, edge_key
    """)
    return list(cur.fetchall())


def geometry_from_row(row: dict) -> dict | None:
    raw = row.get("geom_json")
    if not raw:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw


def feature_from_row(row: dict, impact_type: str, downstream_map: dict[str, dict] | None = None) -> dict | None:
    geom = geometry_from_row(row)
    if not geom:
        return None
    river_name = str(row.get("river_name") or "未知")
    props = {
        "impact_type": impact_type,
        "river_name": river_name,
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
            "geometry_source": "full_table_direct_30km_hit_uncut",
        })
    else:
        info = (downstream_map or {}).get(river_name, {})
        props.update({
            "min_downstream_distance_km": row.get("min_downstream_distance_km") or info.get("min_distance_km"),
            "end_downstream_distance_km": row.get("end_downstream_distance_km"),
            "keep_km": row.get("keep_km"),
            "clip_fraction": row.get("clip_fraction"),
            "geometry_source": "full_table_downstream_50km_clipped_best_effort",
        })
    return {"type": "Feature", "geometry": geom, "properties": props}


def build_outputs(args: argparse.Namespace) -> dict:
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    station_df = aggregate_5min_station_pre_to_24h(args.csv)
    impact_df = station_df[
        (station_df["rain_24h"] >= args.rain_threshold_mm)
        & station_df["lon"].notna()
        & station_df["lat"].notna()
    ].copy()
    stations = [_station_record(row) for _, row in impact_df.iterrows()]

    station_geojson = output / "impact_stations.geojson"
    river_geojson = output / "impact_rivers_postgis.geojson"
    top_csv = output / "rain24h_top_stations.csv"
    summary_json = output / "summary.json"
    station_geojson.write_text(json.dumps(_make_station_geojson(stations), ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    station_df.head(max(args.top_station_limit, 0)).to_csv(top_csv, index=False, encoding="utf-8-sig")

    direct_rows: list[dict] = []
    downstream_rows: list[dict] = []
    downstream_map: dict[str, dict] = {}
    downstream_segments: list[dict] = []
    direct_graph_edge_count = 0

    if stations:
        conn = connect_db(args)
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                columns = table_columns(cur, args.db_schema, args.river_table)
                if not columns:
                    raise ValueError(f"未找到河流表：{args.db_schema}.{args.river_table}")
                geom_col = args.river_geom_column if args.river_geom_column in columns else first_col(columns, ("geom", "geometry", "wkb_geometry", "the_geom"))
                if not geom_col:
                    raise ValueError("河流表未找到几何字段")
                create_station_temp(cur, stations, args.db_srid)
                direct_rows = query_direct_rivers(cur, schema=args.db_schema, table=args.river_table, columns=columns, geom_col=geom_col, buffer_km=args.station_buffer_km)
                start_nodes, direct_edge_keys, direct_graph_edge_count = find_direct_graph_edges(stations, args.graph, args.station_buffer_km)
                downstream_map, downstream_segments = collect_downstream_segments(
                    start_nodes,
                    graph_path=args.graph,
                    direct_edge_keys=direct_edge_keys,
                    downstream_km=args.downstream_km,
                )
                downstream_rows = query_downstream_rivers(cur, schema=args.db_schema, table=args.river_table, columns=columns, geom_col=geom_col, segments=downstream_segments)
        finally:
            conn.close()

    features = []
    seen = set()
    for row in direct_rows:
        feat = feature_from_row(row, "direct_buffer")
        if feat:
            key = ("direct", feat["properties"].get("id"), feat["properties"].get("objectid"), feat["properties"].get("river_name"))
            if key not in seen:
                seen.add(key)
                features.append(feat)
    for row in downstream_rows:
        feat = feature_from_row(row, "downstream_50km", downstream_map)
        if feat:
            key = ("downstream", feat["properties"].get("edge_key"))
            if key not in seen:
                seen.add(key)
                features.append(feat)
    features.sort(key=lambda f: (0 if f["properties"].get("impact_type") == "direct_buffer" else 1, f["properties"].get("river_name") or "", f["properties"].get("edge_key") or f["properties"].get("objectid") or ""))
    river_geojson.write_text(json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")

    downstream_rivers = sorted({str(s.get("river_name") or "") for s in downstream_segments if s.get("river_name")})
    direct_rivers = sorted({str(r.get("river_name") or "") for r in direct_rows if r.get("river_name")})
    summary = {
        "status": "ok",
        "params": {
            "river_table": args.river_table,
            "rain_threshold_mm": args.rain_threshold_mm,
            "station_buffer_km": args.station_buffer_km,
            "downstream_km": args.downstream_km,
            "dedupe_rule": "downstream dedupe by pkl edge_key, not river_name or objectid",
        },
        "station_summary": {
            "total_station_count": int(len(station_df)),
            "impact_station_count": int(len(stations)),
            "max_rain_24h": float(station_df["rain_24h"].max() or 0.0),
        },
        "river_summary": {
            "direct_db_feature_count": len(direct_rows),
            "direct_graph_edge_count": direct_graph_edge_count,
            "direct_river_count": len(direct_rivers),
            "downstream_graph_segment_count": len(downstream_segments),
            "downstream_db_feature_count": len(downstream_rows),
            "downstream_river_count": len(downstream_rivers),
            "geojson_feature_count": len(features),
        },
        "direct_rivers": direct_rivers,
        "downstream_rivers": downstream_rivers,
        "downstream_segments": downstream_segments,
        "outputs": {
            "river_geojson": str(river_geojson),
            "station_geojson": str(station_geojson),
            "top_stations_csv": str(top_csv),
            "summary_json": str(summary_json),
        },
    }
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="真实河流 24 小时降水影响 GeoJSON")
    parser.add_argument("--csv", default=DEFAULT_CSV_PATH)
    parser.add_argument("--graph", default=DEFAULT_GRAPH_PATH)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--rain-threshold-mm", type=float, default=50.0)
    parser.add_argument("--station-buffer-km", type=float, default=30.0)
    parser.add_argument("--downstream-km", type=float, default=50.0)
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
    print(f"最大24小时雨量：{summary['station_summary']['max_rain_24h']} mm")
    print(f"直接DB河流要素：{summary['river_summary']['direct_db_feature_count']}")
    print(f"直接拓扑边数：{summary['river_summary']['direct_graph_edge_count']}")
    print(f"下游图边段：{summary['river_summary']['downstream_graph_segment_count']}")
    print(f"下游DB要素：{summary['river_summary']['downstream_db_feature_count']}")
    print(f"下游河流数：{summary['river_summary']['downstream_river_count']}")
    print(f"GeoJSON要素数：{summary['river_summary']['geojson_feature_count']}")
    print("输出文件：")
    for name, path in summary["outputs"].items():
        print(f"  - {name}: {path}")


if __name__ == "__main__":
    main()
