r"""代码评审版：24h 降水 -> 直接影响河流 + 下游 50km GeoJSON。

只保留当前业务需要的主流程：
1. CSV 的 5 分钟 PRE 按站点累计为 24h 雨量；
2. rain_24h >= 50mm 的站点作为触发站；
3. PostGIS 查询触发站 30km 内真实河段，直接河段不截断；
4. pkl 拓扑从直接命中边向下游追踪 50km；
5. 下游边回 full_v5 时按 pkl 边位置匹配最近真实河段，并按 50km 截断。

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
from typing import Any

import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for item in (CURRENT_DIR, PROJECT_ROOT):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

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
DEFAULT_GEOM_COLUMN = "geom"
DEFAULT_OBJECTID_COLUMN = "objectid"
DEFAULT_RIVER_NAME_COLUMN = "src_name"
KM_PER_DEG = 111.32


def quote_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name or "")):
        raise ValueError(f"非法 SQL 标识符：{name!r}")
    return f'"{name}"'


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


def load_trigger_stations(csv_path: str, threshold_mm: float) -> tuple[pd.DataFrame, list[dict]]:
    df = rig.aggregate_5min_station_pre_to_24h(csv_path)
    hit = df[(df["rain_24h"] >= threshold_mm) & df["lon"].notna() & df["lat"].notna()].copy()
    return df, [rig._station_record(row) for _, row in hit.iterrows()]


def create_station_temp(cur, stations: list[dict]) -> None:
    cur.execute("DROP TABLE IF EXISTS tmp_rain_impact_stations")
    cur.execute("""
        CREATE TEMP TABLE tmp_rain_impact_stations(
            station_id text,
            station_name text,
            lon double precision,
            lat double precision,
            rain_24h double precision,
            geom geometry(Point,4326)
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
            template="(%s,%s,%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326))",
        )


def query_direct_river_parts(cur, args: argparse.Namespace) -> list[dict]:
    q_schema = quote_ident(args.db_schema)
    q_table = quote_ident(args.river_table)
    q_geom = quote_ident(args.river_geom_column)
    q_objectid = quote_ident(args.river_objectid_column)
    q_name = quote_ident(args.river_name_column)
    cur.execute(f"""
        WITH river_parts AS (
            SELECT
                r.{q_objectid}::text AS objectid,
                COALESCE(NULLIF(TRIM(r.{q_name}::text), ''), '未知') AS river_name,
                (ST_Dump(r.{q_geom})).geom AS geom
            FROM {q_schema}.{q_table} r
            WHERE r.{q_geom} IS NOT NULL
              AND NOT ST_IsEmpty(r.{q_geom})
        )
        SELECT
            p.objectid,
            p.objectid AS id,
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
        GROUP BY p.objectid, p.river_name, p.geom
        ORDER BY min_station_distance_km, p.river_name, p.objectid
    """, {"buffer_m": float(args.station_buffer_km) * 1000.0})
    return list(cur.fetchall())


def parse_node_xy(node: Any) -> tuple[float | None, float | None]:
    try:
        if isinstance(node, str) and "," in node:
            x, y = node.split(",", 1)
            return float(x), float(y)
        if isinstance(node, (tuple, list)) and len(node) >= 2:
            return float(node[0]), float(node[1])
    except Exception:
        return None, None
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


def graph_edge_key(u: Any, v: Any, key: Any, attr: dict) -> str:
    objectid = rig._edge_objectid_key(attr) or ""
    river_name = rig.get_edge_river_name(attr) or ""
    return f"{u}|{v}|{key}|{objectid}|{river_name}"


def find_direct_graph_starts(stations: list[dict], graph_path: str, buffer_km: float) -> tuple[dict[Any, float], set[str]]:
    graph = rig.get_graph(graph_path)
    station_points = [(float(s["lon"]), float(s["lat"])) for s in stations]
    start_nodes: dict[Any, float] = {}
    direct_edge_keys: set[str] = set()

    for u, v, key, attr in rig.iter_graph_edges(graph):
        ux, uy = parse_node_xy(u)
        vx, vy = parse_node_xy(v)
        if ux is None or uy is None or vx is None or vy is None:
            continue
        min_dist = min(point_to_segment_km(lon, lat, (ux, uy), (vx, vy)) for lon, lat in station_points)
        if min_dist <= float(buffer_km):
            direct_edge_keys.add(graph_edge_key(u, v, key, attr))
            start_nodes[v] = 0.0
    return start_nodes, direct_edge_keys


def collect_downstream_segments(start_nodes: dict[Any, float], graph_path: str, direct_edge_keys: set[str], downstream_km: float) -> list[dict]:
    limit = float(downstream_km)
    if not start_nodes or limit <= 0:
        return []

    graph = rig.get_graph(graph_path)
    best_dist = dict(start_nodes)
    seq = count()
    heap = [(float(dist), next(seq), node) for node, dist in start_nodes.items()]
    heapq.heapify(heap)
    segments: dict[str, dict] = {}

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
            edge_id = graph_edge_key(u, v, key, attr)

            if clip_fraction > 0:
                from_x, from_y = parse_node_xy(u)
                to_x, to_y = parse_node_xy(v)
                old = segments.get(edge_id)
                if old is None or curr_dist < old["min_distance_km"]:
                    segments[edge_id] = {
                        "edge_key": edge_id,
                        "objectid": objectid,
                        "river_name": river_name,
                        "min_distance_km": round(float(curr_dist), 3),
                        "end_distance_km": round(float(curr_dist + keep_km), 3),
                        "keep_km": round(float(keep_km), 3),
                        "clip_fraction": round(float(clip_fraction), 8),
                        "is_direct_graph_edge": edge_id in direct_edge_keys,
                        "from_x": from_x,
                        "from_y": from_y,
                        "to_x": to_x,
                        "to_y": to_y,
                    }

            if next_dist <= limit and next_dist < best_dist.get(v, math.inf):
                best_dist[v] = next_dist
                heapq.heappush(heap, (next_dist, next(seq), v))

    return sorted(segments.values(), key=lambda x: (x["min_distance_km"], x["river_name"], x["edge_key"]))


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
        fx, fy = segment.get("from_x"), segment.get("from_y")
        tx, ty = segment.get("to_x"), segment.get("to_y")
        rows.append((
            segment["edge_key"],
            segment["objectid"],
            segment["river_name"],
            segment["min_distance_km"],
            segment["end_distance_km"],
            segment["keep_km"],
            segment["clip_fraction"],
            bool(segment.get("is_direct_graph_edge")),
            fx,
            fy,
            tx,
            ty,
            fx,
            fy,
            tx,
            ty,
            fx,
            fy,
            tx,
            ty,
        ))
    if rows:
        execute_values(
            cur,
            """
            INSERT INTO tmp_downstream_segments VALUES %s
            """,
            rows,
            template="""
            (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
             CASE WHEN %s IS NULL OR %s IS NULL OR %s IS NULL OR %s IS NULL THEN NULL
                  ELSE ST_SetSRID(ST_MakeLine(ST_MakePoint(%s,%s), ST_MakePoint(%s,%s)),4326) END)
            """,
        )


def query_downstream_river_parts(cur, args: argparse.Namespace, segments: list[dict]) -> list[dict]:
    if not segments:
        return []
    create_downstream_temp(cur, segments)

    q_schema = quote_ident(args.db_schema)
    q_table = quote_ident(args.river_table)
    q_geom = quote_ident(args.river_geom_column)
    q_objectid = quote_ident(args.river_objectid_column)
    q_name = quote_ident(args.river_name_column)
    cur.execute(f"""
        WITH river_parts AS (
            SELECT
                r.{q_objectid}::text AS objectid,
                COALESCE(NULLIF(TRIM(r.{q_name}::text), ''), '未知') AS db_river_name,
                (ST_Dump(r.{q_geom})).geom AS geom
            FROM {q_schema}.{q_table} r
            WHERE r.{q_geom} IS NOT NULL
              AND NOT ST_IsEmpty(r.{q_geom})
        ),
        candidates AS (
            SELECT
                ds.*,
                p.db_river_name,
                p.geom AS original_geom,
                ST_LineMerge(p.geom) AS line_geom,
                ST_Distance(p.geom::geography, ds.pkl_line::geography) / 1000.0 AS match_distance_km
            FROM river_parts p
            JOIN tmp_downstream_segments ds ON p.objectid = ds.objectid
            WHERE p.geom IS NOT NULL
              AND NOT ST_IsEmpty(p.geom)
              AND ds.pkl_line IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM tmp_rain_impact_stations s
                  WHERE ST_DWithin(p.geom::geography, s.geom::geography, %(buffer_m)s)
              )
        ),
        best_match AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY edge_key ORDER BY match_distance_km) AS rn
            FROM candidates
        ),
        fractions AS (
            SELECT *,
                ST_LineLocatePoint(line_geom, ST_SetSRID(ST_MakePoint(from_x, from_y),4326)) AS from_frac,
                ST_LineLocatePoint(line_geom, ST_SetSRID(ST_MakePoint(to_x, to_y),4326)) AS to_frac,
                ST_Length(line_geom::geography) / 1000.0 AS line_km
            FROM best_match
            WHERE rn = 1
              AND GeometryType(line_geom) = 'LINESTRING'
        ),
        clipped AS (
            SELECT
                edge_key,
                objectid,
                COALESCE(NULLIF(TRIM(db_river_name), ''), river_name) AS river_name,
                min_distance_km,
                end_distance_km,
                keep_km,
                clip_fraction,
                is_direct_graph_edge,
                match_distance_km,
                ST_Multi(ST_LineSubstring(
                    line_geom,
                    LEAST(from_frac, GREATEST(0.0, LEAST(1.0, from_frac + CASE WHEN to_frac >= from_frac THEN keep_km / line_km ELSE -keep_km / line_km END))),
                    GREATEST(from_frac, GREATEST(0.0, LEAST(1.0, from_frac + CASE WHEN to_frac >= from_frac THEN keep_km / line_km ELSE -keep_km / line_km END)))
                )) AS geom
            FROM fractions
            WHERE line_km > 0
        )
        SELECT
            edge_key,
            objectid,
            objectid AS id,
            river_name,
            min_distance_km AS min_downstream_distance_km,
            end_distance_km AS end_downstream_distance_km,
            keep_km,
            clip_fraction,
            is_direct_graph_edge,
            match_distance_km,
            ST_AsGeoJSON(geom) AS geom_json,
            ST_Length(geom::geography) / 1000.0 AS length_km
        FROM clipped
        WHERE geom IS NOT NULL
          AND NOT ST_IsEmpty(geom)
        ORDER BY min_distance_km, river_name, edge_key
    """, {"buffer_m": float(args.station_buffer_km) * 1000.0})
    return list(cur.fetchall())


def geometry_from_row(row: dict) -> dict | None:
    raw = row.get("geom_json")
    if not raw:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw


def river_feature(row: dict, impact_type: str) -> dict | None:
    geometry = geometry_from_row(row)
    if not geometry:
        return None

    properties = {
        "impact_type": impact_type,
        "river_name": row.get("river_name") or "未知",
        "id": row.get("id"),
        "objectid": row.get("objectid"),
        "edge_key": row.get("edge_key"),
        "length_km": round(float(row.get("length_km") or 0.0), 3),
    }
    if impact_type == "direct_buffer":
        properties.update({
            "min_station_distance_km": round(float(row.get("min_station_distance_km") or 0.0), 3),
            "trigger_station_count": int(row.get("trigger_station_count") or 0),
            "trigger_stations": row.get("trigger_stations") or [],
            "geometry_source": "full_v5_dump_part_direct_30km_uncut",
        })
    else:
        match_distance = row.get("match_distance_km")
        properties.update({
            "min_downstream_distance_km": row.get("min_downstream_distance_km"),
            "end_downstream_distance_km": row.get("end_downstream_distance_km"),
            "keep_km": row.get("keep_km"),
            "clip_fraction": row.get("clip_fraction"),
            "is_direct_graph_edge": row.get("is_direct_graph_edge"),
            "match_distance_km": round(float(match_distance), 3) if match_distance is not None else None,
            "geometry_source": "full_v5_dump_part_downstream_50km_clipped",
        })
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def write_outputs(args: argparse.Namespace, station_df: pd.DataFrame, stations: list[dict], direct_rows: list[dict], downstream_rows: list[dict], downstream_segments: list[dict]) -> dict:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    features = []
    seen = set()
    for row, impact_type in [(r, "direct_buffer") for r in direct_rows] + [(r, "downstream_50km") for r in downstream_rows]:
        feature = river_feature(row, impact_type)
        if not feature:
            continue
        key = (impact_type, feature["properties"].get("edge_key"), feature["properties"].get("objectid"), json.dumps(feature["geometry"], sort_keys=True))
        if key not in seen:
            seen.add(key)
            features.append(feature)

    features.sort(key=lambda f: (0 if f["properties"]["impact_type"] == "direct_buffer" else 1, f["properties"].get("river_name") or ""))
    station_geojson = rig._make_station_geojson(stations)
    river_geojson = {"type": "FeatureCollection", "features": features}

    river_path = output_dir / "impact_rivers_postgis.geojson"
    station_path = output_dir / "impact_stations.geojson"
    top_csv = output_dir / "rain24h_top_stations.csv"
    summary_path = output_dir / "summary.json"

    river_path.write_text(json.dumps(river_geojson, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    station_path.write_text(json.dumps(station_geojson, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    station_df.head(max(args.top_station_limit, 0)).to_csv(top_csv, index=False, encoding="utf-8-sig")

    summary = {
        "status": "ok",
        "params": {
            "rain_threshold_mm": args.rain_threshold_mm,
            "station_buffer_km": args.station_buffer_km,
            "downstream_km": args.downstream_km,
            "river_table": f"{args.db_schema}.{args.river_table}",
            "review_note": "精简评审版：固定当前表结构，只保留直接河段、下游拓扑、最近真实河段匹配三段主逻辑。",
        },
        "station_summary": {
            "total_station_count": int(len(station_df)),
            "impact_station_count": int(len(stations)),
            "max_rain_24h": float(station_df["rain_24h"].max() or 0.0) if len(station_df) else 0.0,
        },
        "river_summary": {
            "direct_db_feature_count": len(direct_rows),
            "downstream_graph_segment_count": len(downstream_segments),
            "downstream_db_feature_count": len(downstream_rows),
            "geojson_feature_count": len(features),
        },
        "direct_rivers": sorted({str(r.get("river_name") or "") for r in direct_rows if r.get("river_name")}),
        "downstream_rivers": sorted({str(r.get("river_name") or "") for r in downstream_rows if r.get("river_name")}),
        "outputs": {
            "river_geojson": str(river_path),
            "station_geojson": str(station_path),
            "top_stations_csv": str(top_csv),
            "summary_json": str(summary_path),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    return summary


def build_outputs(args: argparse.Namespace) -> dict:
    station_df, stations = load_trigger_stations(args.csv, args.rain_threshold_mm)
    direct_rows: list[dict] = []
    downstream_rows: list[dict] = []
    downstream_segments: list[dict] = []

    if stations:
        conn = connect_db(args)
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                create_station_temp(cur, stations)
                direct_rows = query_direct_river_parts(cur, args)
                start_nodes, direct_edge_keys = find_direct_graph_starts(stations, args.graph, args.station_buffer_km)
                downstream_segments = collect_downstream_segments(start_nodes, args.graph, direct_edge_keys, args.downstream_km)
                downstream_rows = query_downstream_river_parts(cur, args, downstream_segments)
        finally:
            conn.close()

    return write_outputs(args, station_df, stations, direct_rows, downstream_rows, downstream_segments)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="24 小时降水影响河流 GeoJSON 评审版")
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
    parser.add_argument("--db-sslmode", default=os.getenv("HHLY_DB_SSLMODE", "disable"))
    parser.add_argument("--db-connect-timeout", type=int, default=int(os.getenv("HHLY_DB_CONNECT_TIMEOUT", "5")))
    parser.add_argument("--river-table", default=os.getenv("HHLY_RIVER_TABLE_FULL", DEFAULT_RIVER_TABLE))
    parser.add_argument("--river-geom-column", default=os.getenv("HHLY_RIVER_GEOM_COLUMN", DEFAULT_GEOM_COLUMN))
    parser.add_argument("--river-objectid-column", default=os.getenv("HHLY_RIVER_OBJECTID_COLUMN", DEFAULT_OBJECTID_COLUMN))
    parser.add_argument("--river-name-column", default=os.getenv("HHLY_RIVER_NAME_COLUMN", DEFAULT_RIVER_NAME_COLUMN))
    return parser.parse_args()


def main() -> None:
    summary = build_outputs(parse_args())
    print("DB 版本地测试完成")
    print(f"总站数：{summary['station_summary']['total_station_count']}")
    print(f"触发站数：{summary['station_summary']['impact_station_count']}")
    print(f"直接DB河流要素：{summary['river_summary']['direct_db_feature_count']}")
    print(f"下游图边段：{summary['river_summary']['downstream_graph_segment_count']}")
    print(f"下游DB要素：{summary['river_summary']['downstream_db_feature_count']}")
    print(f"GeoJSON要素数：{summary['river_summary']['geojson_feature_count']}")
    print("输出文件：")
    for name, path in summary["outputs"].items():
        print(f"  - {name}: {path}")


if __name__ == "__main__":
    main()
