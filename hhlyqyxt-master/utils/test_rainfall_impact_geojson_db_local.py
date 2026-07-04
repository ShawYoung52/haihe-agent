r"""本地 DB 版测试：5 分钟 CSV -> 24 小时降水 -> 裁剪后的真实河流 GeoJSON。

业务口径：
- 站点 PRE 为 5 分钟降水，先按站点聚合为 24 小时累计雨量；
- rain_24h >= 阈值的站点作为暴雨触发站；
- 直接河流：真实河流与触发站 30km 缓冲区并集求交，只保留缓冲区内的河段；
- 间接河流：从直接河流沿拓扑下游追踪 50km，最后一条超出 50km 的河段按比例截断；
- 同一 objectid 同时直接/间接出现时，保留直接河流。

运行：
    cd hhlyqyxt-master
    $env:HHLY_DB_PASSWORD="你的数据库密码"
    python utils/test_rainfall_impact_geojson_db_local.py

输出目录默认：
    C:\Users\gaozr\Downloads\24hourmindata_db_impact_output
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import os
import re
import sys
from collections import defaultdict
from itertools import count
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rainfall_impact_geojson import (  # noqa: E402
    _edge_objectid_key,
    _get_end_nodes_by_river_map,
    _make_station_geojson,
    _station_record,
    aggregate_5min_station_pre_to_24h,
    get_edge_length_km,
    get_edge_river_name,
    get_graph,
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
DEFAULT_DB_SRID = 4326
DEFAULT_RIVER_TABLE_FULL = "haihe_river_directed_full_v5"
DEFAULT_RIVER_GEOM_COLUMN = "geom"
DEFAULT_DB_SSLMODE = "disable"
DEFAULT_DB_CONNECT_TIMEOUT = 5


def _env(name: str, default: Any) -> Any:
    return os.getenv(name, default)


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)


def _quote_ident(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(identifier or "")):
        raise ValueError(f"非法 SQL 标识符：{identifier!r}")
    return f'"{identifier}"'


def _pick_first_existing(columns: set[str], candidates: Iterable[str]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _river_name_sql_expr(columns: set[str], alias: str = "r") -> str:
    fields = [c for c in ("river_name", "rivername", "src_name", "name") if c in columns]
    if not fields:
        return "'未知'"
    prefix = f"{_quote_ident(alias)}."
    parts = [f"NULLIF(TRIM({prefix}{_quote_ident(c)}::text), '')" for c in fields]
    return f"COALESCE({', '.join(parts)}, '未知')"


def _get_table_columns(cur, *, schema: str, table: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
        """,
        (schema, table),
    )
    return {str(row["column_name"]) for row in cur.fetchall()}


def _connect_db(args: argparse.Namespace):
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


def _station_sql_rows(stations: list[dict]) -> list[tuple]:
    rows = []
    for station in stations:
        lon = float(station["lon"])
        lat = float(station["lat"])
        rows.append(
            (
                str(station.get("station_id") or ""),
                str(station.get("station_name") or ""),
                str(station.get("province") or ""),
                str(station.get("city") or ""),
                str(station.get("cnty") or ""),
                str(station.get("town") or ""),
                lon,
                lat,
                float(station.get("rain_24h") or 0.0),
                int(station.get("obs_count") or 0),
                str(station.get("start_time") or ""),
                str(station.get("end_time") or ""),
                lon,
                lat,
            )
        )
    return rows


def _station_values_template(srid: int) -> str:
    return (
        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
        f"ST_SetSRID(ST_MakePoint(%s,%s),{int(srid)}))"
    )


def _station_cte_columns() -> str:
    return """
        station_id, station_name, province, city, cnty, town,
        lon, lat, rain_24h, obs_count, start_time, end_time, geom
    """


def _validate_geometry_column(columns: set[str], geom_column: str) -> str:
    if geom_column in columns:
        return geom_column
    fallback = _pick_first_existing(columns, ("geom", "geometry", "wkb_geometry", "the_geom"))
    if fallback:
        return fallback
    raise ValueError(f"河流表未找到几何字段：{geom_column}")


def _query_direct_rows_clipped(
    cur,
    *,
    schema: str,
    table: str,
    columns: set[str],
    geom_column: str,
    srid: int,
    stations: list[dict],
    buffer_km: float,
) -> list[dict]:
    """直接河流：河流与 30km 缓冲区并集求交，只返回缓冲区内线段。"""
    if not stations:
        return []

    geom_col = _validate_geometry_column(columns, geom_column)
    objectid_col = _pick_first_existing(columns, ("objectid", "OBJECTID", "id", "gid"))
    id_col = _pick_first_existing(columns, ("id", "gid"))
    river_expr = _river_name_sql_expr(columns, alias="r")

    q_schema = _quote_ident(schema)
    q_table = _quote_ident(table)
    q_geom = _quote_ident(geom_col)
    objectid_expr = f"r.{_quote_ident(objectid_col)}::text" if objectid_col else "NULL::text"
    id_expr = f"r.{_quote_ident(id_col)}::text" if id_col else objectid_expr
    buffer_m = float(buffer_km) * 1000.0

    sql = f"""
        WITH stations ({_station_cte_columns()}) AS (VALUES %s),
        buffer_union AS (
            SELECT ST_UnaryUnion(ST_Collect(ST_Buffer(geom::geography, {buffer_m})::geometry)) AS geom
            FROM stations
        ),
        direct_raw AS (
            SELECT
                {id_expr} AS id,
                {objectid_expr} AS objectid,
                {river_expr} AS river_name,
                r.{q_geom} AS original_geom,
                ST_Multi(ST_CollectionExtract(ST_Intersection(r.{q_geom}, b.geom), 2)) AS clipped_geom
            FROM {q_schema}.{q_table} r
            CROSS JOIN buffer_union b
            WHERE r.{q_geom} IS NOT NULL
              AND NOT ST_IsEmpty(r.{q_geom})
              AND ST_Intersects(r.{q_geom}, b.geom)
        )
        SELECT
            d.id,
            d.objectid,
            d.river_name,
            ST_AsGeoJSON(d.clipped_geom) AS geom_json,
            ST_Length(d.clipped_geom::geography) / 1000.0 AS length_km,
            MIN(ST_Distance(d.original_geom::geography, s.geom::geography) / 1000.0) AS min_station_distance_km,
            COUNT(DISTINCT s.station_id) AS trigger_station_count,
            jsonb_agg(
                DISTINCT jsonb_build_object(
                    'station_id', s.station_id,
                    'station_name', s.station_name,
                    'province', s.province,
                    'city', s.city,
                    'cnty', s.cnty,
                    'town', s.town,
                    'lon', s.lon,
                    'lat', s.lat,
                    'rain_24h', s.rain_24h,
                    'distance_to_river_km', ROUND((ST_Distance(d.original_geom::geography, s.geom::geography) / 1000.0)::numeric, 3)
                )
            ) AS trigger_stations
        FROM direct_raw d
        JOIN stations s
          ON ST_DWithin(d.original_geom::geography, s.geom::geography, {buffer_m})
        WHERE d.clipped_geom IS NOT NULL
          AND NOT ST_IsEmpty(d.clipped_geom)
        GROUP BY d.id, d.objectid, d.river_name, d.clipped_geom
        ORDER BY min_station_distance_km, d.river_name, d.objectid
    """
    result = execute_values(
        cur,
        sql,
        _station_sql_rows(stations),
        template=_station_values_template(srid),
        page_size=max(len(stations), 1),
        fetch=True,
    )
    return list(result or [])


def _collect_downstream_segments(
    source_rivers: Iterable[str],
    *,
    downstream_km: float,
    graph_path: str | os.PathLike | None,
    attr_name: str = "length_km",
) -> tuple[dict[str, dict], list[dict]]:
    """沿拓扑追踪下游河段，并记录每条边在 50km 范围内应保留的比例。"""
    limit_km = float(downstream_km)
    if limit_km <= 0:
        return {}, []

    graph = get_graph(graph_path)
    end_nodes_map = _get_end_nodes_by_river_map(graph_path)
    downstream_map: dict[str, dict] = {}
    segment_by_key: dict[tuple[str, str], dict] = {}

    for source_river in sorted({str(x).strip() for x in source_rivers if str(x).strip()}):
        start_nodes = end_nodes_map.get(source_river, set())
        if not start_nodes:
            continue

        heap_counter = count()
        best_dist: dict[Any, float] = {node: 0.0 for node in start_nodes}
        heap: list[tuple[float, int, Any]] = [(0.0, next(heap_counter), node) for node in start_nodes]
        heapq.heapify(heap)

        while heap:
            curr_dist, _seq, curr_node = heapq.heappop(heap)
            if curr_dist > best_dist.get(curr_node, math.inf) or curr_dist > limit_km:
                continue

            for _u, next_node, _key, attr in iter_out_edges(graph, curr_node):
                edge_river = get_edge_river_name(attr)
                edge_objectid = _edge_objectid_key(attr)
                edge_len = get_edge_length_km(attr, attr_name=attr_name)
                if edge_len <= 0:
                    edge_len = 0.0

                is_source_river = edge_river == source_river
                next_dist = curr_dist if is_source_river else curr_dist + edge_len

                if edge_river and not is_source_river and curr_dist < limit_km:
                    keep_km = max(min(limit_km - curr_dist, edge_len), 0.0) if edge_len > 0 else 0.0
                    clip_fraction = 1.0 if edge_len <= 0 else max(min(keep_km / edge_len, 1.0), 0.0)
                    if edge_objectid and clip_fraction > 0:
                        key = (str(edge_objectid), edge_river)
                        item = segment_by_key.get(key)
                        if item is None or clip_fraction > item["clip_fraction"]:
                            segment_by_key[key] = {
                                "objectid": str(edge_objectid),
                                "river_name": edge_river,
                                "source_river": source_river,
                                "min_distance_km": round(float(curr_dist), 3),
                                "end_distance_km": round(float(curr_dist + keep_km), 3),
                                "clip_fraction": round(float(clip_fraction), 8),
                            }

                    river_item = downstream_map.setdefault(
                        edge_river,
                        {"river_name": edge_river, "min_distance_km": math.inf, "source_rivers": []},
                    )
                    if curr_dist < river_item["min_distance_km"]:
                        river_item["min_distance_km"] = curr_dist
                    if source_river not in river_item["source_rivers"]:
                        river_item["source_rivers"].append(source_river)

                if next_dist <= limit_km and next_dist < best_dist.get(next_node, math.inf):
                    best_dist[next_node] = next_dist
                    heapq.heappush(heap, (next_dist, next(heap_counter), next_node))

    for item in downstream_map.values():
        item["min_distance_km"] = round(float(item["min_distance_km"]), 3)
        item["source_rivers"] = sorted(item["source_rivers"])

    segments = sorted(segment_by_key.values(), key=lambda x: (x["min_distance_km"], x["river_name"], x["objectid"]))
    return downstream_map, segments


def _query_downstream_rows_clipped(
    cur,
    *,
    schema: str,
    table: str,
    columns: set[str],
    geom_column: str,
    downstream_segments: list[dict],
) -> list[dict]:
    """间接河流：按拓扑累计距离截断最后一段下游河流。"""
    if not downstream_segments:
        return []

    geom_col = _validate_geometry_column(columns, geom_column)
    objectid_col = _pick_first_existing(columns, ("objectid", "OBJECTID", "id", "gid"))
    id_col = _pick_first_existing(columns, ("id", "gid"))
    if not objectid_col:
        return []

    river_expr = _river_name_sql_expr(columns, alias="r")
    q_schema = _quote_ident(schema)
    q_table = _quote_ident(table)
    q_geom = _quote_ident(geom_col)
    q_objectid = _quote_ident(objectid_col)
    objectid_expr = f"r.{q_objectid}::text"
    id_expr = f"r.{_quote_ident(id_col)}::text" if id_col else objectid_expr

    rows = [
        (
            str(seg["objectid"]),
            str(seg["river_name"]),
            float(seg["min_distance_km"]),
            float(seg["end_distance_km"]),
            float(seg["clip_fraction"]),
        )
        for seg in downstream_segments
    ]

    sql = f"""
        WITH ds(objectid, river_name, min_distance_km, end_distance_km, clip_fraction) AS (VALUES %s),
        ds_grouped AS (
            SELECT
                objectid,
                river_name,
                MIN(min_distance_km) AS min_distance_km,
                MAX(end_distance_km) AS end_distance_km,
                MAX(clip_fraction) AS clip_fraction
            FROM ds
            GROUP BY objectid, river_name
        ),
        joined AS (
            SELECT
                {id_expr} AS id,
                {objectid_expr} AS objectid,
                {river_expr} AS db_river_name,
                ds.river_name AS graph_river_name,
                ds.min_distance_km,
                ds.end_distance_km,
                ds.clip_fraction,
                r.{q_geom} AS original_geom,
                ST_LineMerge(r.{q_geom}) AS merged_geom
            FROM {q_schema}.{q_table} r
            JOIN ds_grouped ds
              ON r.{q_objectid}::text = ds.objectid
            WHERE r.{q_geom} IS NOT NULL
              AND NOT ST_IsEmpty(r.{q_geom})
        ),
        clipped AS (
            SELECT
                id,
                objectid,
                COALESCE(NULLIF(TRIM(db_river_name), ''), graph_river_name) AS river_name,
                min_distance_km,
                end_distance_km,
                clip_fraction,
                CASE
                    WHEN clip_fraction < 0.999999 AND GeometryType(merged_geom) = 'LINESTRING'
                    THEN ST_Multi(ST_LineSubstring(merged_geom, 0, clip_fraction))
                    ELSE original_geom
                END AS clipped_geom
            FROM joined
        )
        SELECT
            id,
            objectid,
            river_name,
            min_distance_km AS min_downstream_distance_km,
            end_distance_km AS end_downstream_distance_km,
            clip_fraction,
            ST_AsGeoJSON(clipped_geom) AS geom_json,
            ST_Length(clipped_geom::geography) / 1000.0 AS length_km
        FROM clipped
        WHERE clipped_geom IS NOT NULL
          AND NOT ST_IsEmpty(clipped_geom)
        ORDER BY min_distance_km, river_name, objectid
    """
    result = execute_values(
        cur,
        sql,
        rows,
        template="(%s,%s,%s,%s,%s)",
        page_size=max(len(rows), 1),
        fetch=True,
    )
    return list(result or [])


def _row_geometry(row: dict) -> dict | None:
    geom = row.get("geom_json")
    if not geom:
        return None
    try:
        return json.loads(geom) if isinstance(geom, str) else geom
    except Exception:
        return None


def _build_direct_feature(row: dict) -> dict | None:
    geometry = _row_geometry(row)
    if not geometry:
        return None
    trigger_stations = row.get("trigger_stations") or []
    min_distance = row.get("min_station_distance_km")
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "impact_type": "direct_buffer",
            "river_name": row.get("river_name"),
            "id": row.get("id"),
            "objectid": row.get("objectid"),
            "length_km": round(float(row.get("length_km") or 0.0), 3),
            "min_station_distance_km": round(float(min_distance), 3) if min_distance is not None else None,
            "trigger_station_count": int(row.get("trigger_station_count") or len(trigger_stations)),
            "trigger_stations": trigger_stations,
            "geometry_source": "postgis_clipped_30km_buffer",
        },
    }


def _build_downstream_feature(row: dict, downstream_map: dict[str, dict]) -> dict | None:
    geometry = _row_geometry(row)
    if not geometry:
        return None
    river_name = str(row.get("river_name") or "未知")
    info = downstream_map.get(river_name, {})
    min_distance = row.get("min_downstream_distance_km")
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "impact_type": "downstream_50km",
            "river_name": river_name,
            "id": row.get("id"),
            "objectid": row.get("objectid"),
            "length_km": round(float(row.get("length_km") or 0.0), 3),
            "min_downstream_distance_km": round(float(min_distance), 3) if min_distance is not None else info.get("min_distance_km"),
            "end_downstream_distance_km": row.get("end_downstream_distance_km"),
            "clip_fraction": row.get("clip_fraction"),
            "source_rivers": info.get("source_rivers", []),
            "geometry_source": "postgis_clipped_downstream_50km",
        },
    }


def _write_empty_result(
    *,
    station_24h_df: pd.DataFrame,
    river_geojson_path: Path,
    station_geojson_path: Path,
    top_csv_path: Path,
    summary_path: Path,
) -> dict:
    river_geojson = {"type": "FeatureCollection", "features": []}
    river_geojson_path.write_text(json.dumps(river_geojson, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "status": "ok",
        "message": "没有站点达到降雨阈值，未生成影响河流。",
        "station_summary": {
            "total_station_count": int(len(station_24h_df)),
            "impact_station_count": 0,
            "max_rain_24h": float(station_24h_df["rain_24h"].max() or 0.0),
        },
        "outputs": {
            "river_geojson": str(river_geojson_path),
            "station_geojson": str(station_geojson_path),
            "top_stations_csv": str(top_csv_path),
            "summary_json": str(summary_path),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def build_db_test_outputs(args: argparse.Namespace) -> dict:
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    station_24h_df = aggregate_5min_station_pre_to_24h(args.csv)
    impact_df = station_24h_df[
        (station_24h_df["rain_24h"] >= args.rain_threshold_mm)
        & station_24h_df["lon"].notna()
        & station_24h_df["lat"].notna()
    ].copy()
    impact_stations = [_station_record(row) for _, row in impact_df.iterrows()]

    river_geojson_path = output / "impact_rivers_postgis.geojson"
    station_geojson_path = output / "impact_stations.geojson"
    top_csv_path = output / "rain24h_top_stations.csv"
    summary_path = output / "summary.json"

    station_geojson_path.write_text(
        json.dumps(_make_station_geojson(impact_stations), ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    station_24h_df.head(max(args.top_station_limit, 0)).to_csv(
        top_csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    if not impact_stations:
        return _write_empty_result(
            station_24h_df=station_24h_df,
            river_geojson_path=river_geojson_path,
            station_geojson_path=station_geojson_path,
            top_csv_path=top_csv_path,
            summary_path=summary_path,
        )

    conn = _connect_db(args)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            columns = _get_table_columns(cur, schema=args.db_schema, table=args.river_table_full)
            if not columns:
                raise ValueError(f"未找到河流表：{args.db_schema}.{args.river_table_full}")

            direct_rows = _query_direct_rows_clipped(
                cur,
                schema=args.db_schema,
                table=args.river_table_full,
                columns=columns,
                geom_column=args.river_geom_column,
                srid=args.db_srid,
                stations=impact_stations,
                buffer_km=args.station_buffer_km,
            )
            direct_rivers = sorted({str(r.get("river_name") or "").strip() for r in direct_rows if r.get("river_name")})
            direct_objectids = {str(r.get("objectid") or "").strip() for r in direct_rows if r.get("objectid")}

            downstream_map, downstream_segments = _collect_downstream_segments(
                direct_rivers,
                downstream_km=args.downstream_km,
                graph_path=args.graph,
            )
            downstream_segments = [
                seg for seg in downstream_segments
                if str(seg.get("objectid") or "").strip() not in direct_objectids
            ]

            downstream_rows = _query_downstream_rows_clipped(
                cur,
                schema=args.db_schema,
                table=args.river_table_full,
                columns=columns,
                geom_column=args.river_geom_column,
                downstream_segments=downstream_segments,
            )
    finally:
        conn.close()

    features = []
    seen = set()
    for row in direct_rows:
        feature = _build_direct_feature(row)
        if not feature:
            continue
        key = (feature["properties"].get("objectid"), feature["properties"].get("river_name"), "direct")
        if key not in seen:
            seen.add(key)
            features.append(feature)

    for row in downstream_rows:
        objectid = str(row.get("objectid") or "").strip()
        if objectid and objectid in direct_objectids:
            continue
        feature = _build_downstream_feature(row, downstream_map)
        if not feature:
            continue
        key = (feature["properties"].get("objectid"), feature["properties"].get("river_name"), "downstream")
        if key not in seen:
            seen.add(key)
            features.append(feature)

    features.sort(
        key=lambda f: (
            0 if f["properties"].get("impact_type") == "direct_buffer" else 1,
            f["properties"].get("river_name") or "",
            f["properties"].get("objectid") or "",
        )
    )
    river_geojson_path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )

    downstream_river_list = sorted(downstream_map.values(), key=lambda x: x["river_name"])
    summary = {
        "status": "ok",
        "params": {
            "csv_path": args.csv,
            "graph_path": args.graph,
            "db_host": args.db_host,
            "db_port": args.db_port,
            "db_name": args.db_name,
            "db_schema": args.db_schema,
            "river_table_full": args.river_table_full,
            "river_geom_column": args.river_geom_column,
            "rain_threshold_mm": args.rain_threshold_mm,
            "station_buffer_km": args.station_buffer_km,
            "downstream_km": args.downstream_km,
        },
        "time_range": {
            "start_time": _json_default(station_24h_df["start_time"].min()),
            "end_time": _json_default(station_24h_df["end_time"].max()),
        },
        "station_summary": {
            "total_station_count": int(len(station_24h_df)),
            "impact_station_count": int(len(impact_stations)),
            "max_rain_24h": float(station_24h_df["rain_24h"].max() or 0.0),
        },
        "river_summary": {
            "direct_segment_count": len(direct_rows),
            "direct_river_count": len(direct_rivers),
            "downstream_graph_segment_count": len(downstream_segments),
            "downstream_db_segment_count": len(downstream_rows),
            "downstream_river_count": len(downstream_map),
            "geojson_feature_count": len(features),
        },
        "direct_rivers": direct_rivers,
        "direct_segments": [
            {
                "river_name": row.get("river_name"),
                "objectid": row.get("objectid"),
                "min_station_distance_km": round(float(row.get("min_station_distance_km") or 0.0), 3),
                "trigger_station_count": int(row.get("trigger_station_count") or 0),
            }
            for row in direct_rows
        ],
        "downstream_rivers": downstream_river_list,
        "downstream_segments": downstream_segments,
        "outputs": {
            "river_geojson": str(river_geojson_path),
            "station_geojson": str(station_geojson_path),
            "top_stations_csv": str(top_csv_path),
            "summary_json": str(summary_path),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DB 版本地测试：24 小时降水影响河流 GeoJSON")
    parser.add_argument("--csv", default=DEFAULT_CSV_PATH, help="5 分钟降水 CSV 路径")
    parser.add_argument("--graph", default=DEFAULT_GRAPH_PATH, help="河网拓扑 pkl 路径")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="输出目录")
    parser.add_argument("--rain-threshold-mm", type=float, default=50.0, help="暴雨阈值，默认 50mm")
    parser.add_argument("--station-buffer-km", type=float, default=30.0, help="站点缓冲半径，默认 30km")
    parser.add_argument("--downstream-km", type=float, default=50.0, help="下游追踪距离，默认 50km")
    parser.add_argument("--top-station-limit", type=int, default=100, help="累计雨量 TOP N 输出")
    parser.add_argument("--db-host", default=_env("HHLY_DB_HOST", DEFAULT_DB_HOST))
    parser.add_argument("--db-port", type=int, default=int(_env("HHLY_DB_PORT", DEFAULT_DB_PORT)))
    parser.add_argument("--db-name", default=_env("HHLY_DB_NAME", DEFAULT_DB_NAME))
    parser.add_argument("--db-user", default=_env("HHLY_DB_USER", DEFAULT_DB_USER))
    parser.add_argument("--db-password", default=_env("HHLY_DB_PASSWORD", ""))
    parser.add_argument("--db-schema", default=_env("HHLY_DB_SCHEMA", DEFAULT_DB_SCHEMA))
    parser.add_argument("--db-srid", type=int, default=int(_env("HHLY_DB_SRID", DEFAULT_DB_SRID)))
    parser.add_argument("--db-sslmode", default=_env("HHLY_DB_SSLMODE", DEFAULT_DB_SSLMODE))
    parser.add_argument("--db-connect-timeout", type=int, default=int(_env("HHLY_DB_CONNECT_TIMEOUT", DEFAULT_DB_CONNECT_TIMEOUT)))
    parser.add_argument("--river-table-full", default=_env("HHLY_RIVER_TABLE_FULL", DEFAULT_RIVER_TABLE_FULL))
    parser.add_argument("--river-geom-column", default=_env("HHLY_RIVER_GEOM_COLUMN", DEFAULT_RIVER_GEOM_COLUMN))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = build_db_test_outputs(args)

    print("DB 版本地测试完成")
    print(f"时间范围：{summary['time_range']['start_time']} -> {summary['time_range']['end_time']}")
    print(f"总站数：{summary['station_summary']['total_station_count']}")
    print(f"触发站数：{summary['station_summary']['impact_station_count']}")
    print(f"最大24小时雨量：{summary['station_summary']['max_rain_24h']} mm")
    print(f"直接影响河段：{summary['river_summary']['direct_segment_count']}")
    print(f"直接影响河流：{summary['river_summary']['direct_river_count']}")
    print(f"下游图边段：{summary['river_summary']['downstream_graph_segment_count']}")
    print(f"下游DB河段：{summary['river_summary']['downstream_db_segment_count']}")
    print(f"GeoJSON要素数：{summary['river_summary']['geojson_feature_count']}")
    if summary.get("direct_segments"):
        print("直接影响河段明细：")
        for row in summary["direct_segments"]:
            print(
                f"  - {row['river_name']} objectid={row['objectid']} "
                f"distance={row['min_station_distance_km']}km stations={row['trigger_station_count']}"
            )
    if summary.get("downstream_segments"):
        print("下游追踪河段明细：")
        for row in summary["downstream_segments"][:20]:
            print(
                f"  - {row['river_name']} objectid={row['objectid']} "
                f"{row['min_distance_km']}~{row['end_distance_km']}km "
                f"clip={row['clip_fraction']}"
            )
    print("输出文件：")
    for name, path in summary["outputs"].items():
        print(f"  - {name}: {path}")


if __name__ == "__main__":
    main()
