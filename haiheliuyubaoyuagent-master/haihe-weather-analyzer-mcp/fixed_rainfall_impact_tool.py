"""问答智能体暴雨影响河流工具。

业务口径与牵引智能体保持一致：
- 30km 缓冲区只用于判断直接影响，不截断直接命中的真实河段；
- 直接河流按 full_v5 的 ST_Dump 后单个真实线段判断，避免 MultiLineString 远处碎线被带出；
- 下游 50km 按 river_directed_v5.pkl 拓扑追踪；
- 下游起点同时来自 30km 内 pkl 边、以及直接命中真实河段附近匹配到的 pkl 边；
- 下游回 full_v5 时按 pkl 边位置匹配最近真实线段，并只输出 50km 范围内的截断线段。
"""
from __future__ import annotations

import heapq
import json
import logging
import math
import re
from itertools import count
from typing import Any, Iterable

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

logger = logging.getLogger(__name__)
TOOL_NAME = "get_affected_river_network_by_rainfall"
KM_PER_DEG = 111.32
DEFAULT_DIRECT_GRAPH_MATCH_KM = 15.0


def _quote_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name or "")):
        raise ValueError(f"非法 SQL 标识符：{name!r}")
    return f'"{name}"'


def _first_col(columns: set[str], names: Iterable[str]) -> str | None:
    for name in names:
        if name in columns:
            return name
    return None


def _river_name_expr(columns: set[str], alias: str = "r") -> str:
    fields = [c for c in ("river_name", "rivername", "src_name", "name") if c in columns]
    if not fields:
        return "'未知'"
    prefix = f"{_quote_ident(alias)}."
    parts = [f"NULLIF(TRIM({prefix}{_quote_ident(c)}::text), '')" for c in fields]
    return f"COALESCE({', '.join(parts)}, '未知')"


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _connect_pg(pg_conf: dict):
    return psycopg2.connect(
        host=pg_conf.get("host"),
        port=int(pg_conf.get("port", 5432)),
        dbname=pg_conf.get("dbname"),
        user=pg_conf.get("user"),
        password=pg_conf.get("password"),
        sslmode=pg_conf.get("sslmode", "prefer"),
        connect_timeout=int(pg_conf.get("connect_timeout", "5")),
    )


def _parse_node_xy(node: Any) -> tuple[float | None, float | None]:
    try:
        if isinstance(node, str) and "," in node:
            x, y = node.split(",", 1)
            return float(x), float(y)
        if isinstance(node, (tuple, list)) and len(node) >= 2:
            return float(node[0]), float(node[1])
    except (TypeError, ValueError):
        pass
    return None, None


def _point_to_segment_km(lon: float, lat: float, p1: tuple[float, float], p2: tuple[float, float]) -> float:
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
    ratio = ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)
    ratio = max(0.0, min(1.0, ratio))
    return math.hypot(x - (x1 + ratio * dx), y - (y1 + ratio * dy))


def _point_to_line_km(lon: float, lat: float, line: list[list[float]]) -> float:
    if len(line) < 2:
        return math.inf
    return min(
        _point_to_segment_km(lon, lat, (float(line[i][0]), float(line[i][1])), (float(line[i + 1][0]), float(line[i + 1][1])))
        for i in range(len(line) - 1)
    )


def _edge_to_line_km(p1: tuple[float, float], p2: tuple[float, float], line: list[list[float]]) -> float:
    if len(line) < 2:
        return math.inf
    distances = [_point_to_line_km(p1[0], p1[1], line), _point_to_line_km(p2[0], p2[1], line)]
    distances.extend(_point_to_segment_km(float(point[0]), float(point[1]), p1, p2) for point in line)
    return min(distances)


def _geometry_lines(geometry: dict) -> list[list[list[float]]]:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "LineString":
        return [coords] if len(coords) >= 2 else []
    if gtype == "MultiLineString":
        return [line for line in coords if len(line) >= 2]
    return []


def _line_length_km(line: list[list[float]]) -> float:
    if len(line) < 2:
        return 0.0
    total = 0.0
    for index in range(len(line) - 1):
        lon1, lat1 = line[index]
        lon2, lat2 = line[index + 1]
        total += _haversine_km(float(lon1), float(lat1), float(lon2), float(lat2))
    return total


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _min_station_distance_km(line: list[list[float]], stations: list[dict]) -> float | None:
    best = math.inf
    for station in stations:
        lon = _safe_float(station.get("lon"))
        lat = _safe_float(station.get("lat"))
        if lon is None or lat is None:
            continue
        best = min(best, _point_to_line_km(lon, lat, line))
    return None if best == math.inf else best


def _graph_edge_key(u: Any, v: Any, key: Any, attr: dict, base_tools) -> str:
    objectid = base_tools._edge_objectid_key(attr) or ""
    river_name = base_tools.get_edge_river_name(attr) or ""
    return f"{u}|{v}|{key}|{objectid}|{river_name}"


def _table_columns(cur, schema: str, table: str) -> set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
        (schema, table),
    )
    return {str(row["column_name"]) for row in cur.fetchall()}


def _create_station_temp(cur, stations: list[dict]) -> None:
    cur.execute("DROP TABLE IF EXISTS tmp_rain_impact_stations")
    cur.execute("""
        CREATE TEMP TABLE tmp_rain_impact_stations(
            station_id text,
            station_name text,
            lon double precision,
            lat double precision,
            rainfall double precision,
            geom geometry(Point,4326)
        ) ON COMMIT DROP
    """)
    rows = []
    for station in stations:
        lon = _safe_float(station.get("lon"))
        lat = _safe_float(station.get("lat"))
        if lon is None or lat is None:
            continue
        rows.append((
            str(station.get("station_id") or ""),
            str(station.get("name") or station.get("station_name") or ""),
            lon,
            lat,
            float(station.get("rainfall") or 0.0),
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


def _query_direct_rows(cur, *, schema: str, table: str, columns: set[str], geom_col: str, buffer_km: float) -> list[dict]:
    objectid_col = _first_col(columns, ("objectid", "OBJECTID", "id", "gid"))
    id_col = _first_col(columns, ("id", "gid"))
    objectid_expr = f"r.{_quote_ident(objectid_col)}::text" if objectid_col else "NULL::text"
    id_expr = f"r.{_quote_ident(id_col)}::text" if id_col else objectid_expr
    river_expr = _river_name_expr(columns, alias="r")
    q_schema = _quote_ident(schema)
    q_table = _quote_ident(table)
    q_geom = _quote_ident(geom_col)
    cur.execute(f"""
        WITH river_parts AS (
            SELECT
                {id_expr} AS id,
                {objectid_expr} AS objectid,
                {river_expr} AS river_name,
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
                'rainfall', s.rainfall
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


def _geometry_from_row(row: dict) -> dict | None:
    raw = row.get("geom_json")
    if not raw:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw


def _direct_part_refs(direct_rows: list[dict]) -> list[dict]:
    refs = []
    for row in direct_rows:
        geometry = _geometry_from_row(row)
        if not geometry:
            continue
        lines = _geometry_lines(geometry)
        if lines:
            refs.append({
                "objectid": str(row.get("objectid") or "").strip(),
                "river_name": str(row.get("river_name") or "").strip(),
                "lines": lines,
            })
    return refs


def _matches_direct_part(edge_objectid: str, edge_river_name: str, p1: tuple[float, float], p2: tuple[float, float], refs: list[dict], max_distance_km: float) -> bool:
    for ref in refs:
        same_objectid = bool(edge_objectid and ref["objectid"] and edge_objectid == ref["objectid"])
        same_name = bool(edge_river_name and ref["river_name"] and edge_river_name == ref["river_name"])
        if not same_objectid and not same_name:
            continue
        if any(_edge_to_line_km(p1, p2, line) <= max_distance_km for line in ref["lines"]):
            return True
    return False


def _find_downstream_start_nodes(stations: list[dict], direct_rows: list[dict], base_tools, buffer_km: float, direct_match_km: float) -> tuple[dict[Any, float], set[str], dict]:
    graph = base_tools.get_graph()
    station_points = [(float(s["lon"]), float(s["lat"])) for s in stations if _safe_float(s.get("lon")) is not None and _safe_float(s.get("lat")) is not None]
    refs = _direct_part_refs(direct_rows)
    start_nodes: dict[Any, float] = {}
    direct_edge_keys: set[str] = set()
    station_edge_count = 0
    matched_part_edge_count = 0

    for u, v, key, attr in base_tools.iter_graph_edges(graph):
        ux, uy = _parse_node_xy(u)
        vx, vy = _parse_node_xy(v)
        if ux is None or uy is None or vx is None or vy is None:
            continue
        p1 = (ux, uy)
        p2 = (vx, vy)
        edge_key = _graph_edge_key(u, v, key, attr, base_tools)
        station_hit = False
        if station_points:
            station_hit = min(_point_to_segment_km(lon, lat, p1, p2) for lon, lat in station_points) <= float(buffer_km)
        edge_objectid = str(base_tools._edge_objectid_key(attr) or "").strip()
        edge_river_name = str(base_tools.get_edge_river_name(attr) or "").strip()
        direct_part_hit = _matches_direct_part(edge_objectid, edge_river_name, p1, p2, refs, float(direct_match_km))
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


def _collect_downstream_segments(start_nodes: dict[Any, float], *, base_tools, direct_edge_keys: set[str], downstream_km: float) -> tuple[dict[str, dict], list[dict]]:
    limit = float(downstream_km)
    if not start_nodes or limit <= 0:
        return {}, []
    graph = base_tools.get_graph()
    best_dist = dict(start_nodes)
    seq = count()
    heap = [(float(dist), next(seq), node) for node, dist in start_nodes.items()]
    heapq.heapify(heap)
    downstream_map: dict[str, dict] = {}
    segment_map: dict[str, dict] = {}

    while heap:
        curr_dist, _seq, node = heapq.heappop(heap)
        if curr_dist > best_dist.get(node, math.inf) or curr_dist >= limit:
            continue
        for u, v, key, attr in base_tools.iter_out_edges(graph, node):
            objectid = str(base_tools._edge_objectid_key(attr) or "").strip()
            river_name = str(base_tools.get_edge_river_name(attr) or "").strip()
            if not objectid or not river_name:
                continue
            edge_len = max(float(base_tools.get_edge_length_km(attr, attr_name="length_km") or 0.0), 0.0)
            next_dist = curr_dist + edge_len
            keep_km = max(min(limit - curr_dist, edge_len), 0.0) if edge_len > 0 else 0.0
            clip_fraction = 1.0 if edge_len <= 0 else max(min(keep_km / edge_len, 1.0), 0.0)
            if clip_fraction > 0:
                edge_key = _graph_edge_key(u, v, key, attr, base_tools)
                from_x, from_y = _parse_node_xy(u)
                to_x, to_y = _parse_node_xy(v)
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
                item = downstream_map.setdefault(river_name, {"river_name": river_name, "min_distance_km": math.inf, "source_rivers": []})
                item["min_distance_km"] = min(item["min_distance_km"], curr_dist)
            if next_dist <= limit and next_dist < best_dist.get(v, math.inf):
                best_dist[v] = next_dist
                heapq.heappush(heap, (next_dist, next(seq), v))

    for item in downstream_map.values():
        item["min_distance_km"] = round(float(item["min_distance_km"]), 3)
    segments = sorted(segment_map.values(), key=lambda x: (x["min_distance_km"], x["river_name"], x["edge_key"]))
    return downstream_map, segments


def _create_downstream_temp(cur, segments: list[dict]) -> None:
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


def _query_downstream_rows(cur, *, schema: str, table: str, columns: set[str], geom_col: str, segments: list[dict], buffer_km: float) -> list[dict]:
    if not segments:
        return []
    objectid_col = _first_col(columns, ("objectid", "OBJECTID", "id", "gid"))
    id_col = _first_col(columns, ("id", "gid"))
    if not objectid_col:
        return []

    _create_downstream_temp(cur, segments)
    objectid_expr = f"r.{_quote_ident(objectid_col)}::text"
    id_expr = f"r.{_quote_ident(id_col)}::text" if id_col else objectid_expr
    river_expr = _river_name_expr(columns, alias="r")
    q_schema = _quote_ident(schema)
    q_table = _quote_ident(table)
    q_geom = _quote_ident(geom_col)
    cur.execute(f"""
        WITH river_parts AS (
            SELECT
                {id_expr} AS id,
                {objectid_expr} AS objectid,
                {river_expr} AS db_river_name,
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


def _segments_from_rows(rows: list[dict], *, impact_type: str, stations: list[dict]) -> list[dict]:
    segments: list[dict] = []
    for row in rows:
        geometry = _geometry_from_row(row)
        if not geometry:
            continue
        for index, path in enumerate(_geometry_lines(geometry)):
            length_km = _line_length_km(path)
            min_station_km = _min_station_distance_km(path, stations)
            segment = {
                "from_x": path[0][0],
                "from_y": path[0][1],
                "to_x": path[-1][0],
                "to_y": path[-1][1],
                "path": path,
                "paths": [path],
                "geometry": {"type": "LineString", "coordinates": path},
                "rivername": row.get("river_name") or "未知",
                "length_km": round(length_km or float(row.get("length_km") or 0.0), 3),
                "objectid": row.get("objectid"),
                "edge_key": row.get("edge_key") or f"{row.get('objectid')}:{index}",
                "is_affected": True,
                "impact_type": impact_type,
                "min_station_distance_km": round(min_station_km, 3) if min_station_km is not None else None,
            }
            if impact_type == "direct_buffer":
                segment.update({
                    "trigger_station_count": row.get("trigger_station_count"),
                    "trigger_stations": row.get("trigger_stations") or [],
                    "geometry_source": "full_v5_dump_part_direct_30km_uncut",
                })
            else:
                segment.update({
                    "min_downstream_distance_km": row.get("min_downstream_distance_km"),
                    "end_downstream_distance_km": row.get("end_downstream_distance_km"),
                    "keep_km": row.get("keep_km"),
                    "clip_fraction": row.get("clip_fraction"),
                    "is_direct_graph_edge": row.get("is_direct_graph_edge"),
                    "match_distance_km": row.get("match_distance_km"),
                    "geometry_source": "full_v5_dump_part_downstream_50km_clipped_by_pkl_edge",
                })
            segments.append(segment)
    return segments


def _unregister_existing_tool(mcp, name: str) -> None:
    candidates = [mcp, getattr(mcp, "_tool_manager", None), getattr(mcp, "tool_manager", None)]
    for manager in candidates:
        if manager is None:
            continue
        remover = getattr(manager, "remove_tool", None)
        if callable(remover):
            try:
                remover(name)
                return
            except Exception:
                pass
        for attr in ("_tools", "tools"):
            registry = getattr(manager, attr, None)
            if isinstance(registry, dict) and name in registry:
                registry.pop(name, None)
                return


def _extract_rainstorm_stations(rainfall_result: dict, threshold_mm: float, base_tools) -> tuple[list[dict], set[str], set[str]]:
    level_to_threshold = {name: low for name, low, _high in base_tools.RAIN_LEVELS}
    stations: list[dict] = []
    zone_77_regions: set[str] = set()
    admin_divisions: set[str] = set()
    for level_item in rainfall_result.get("level_analysis", []) or []:
        level = level_item.get("level", "")
        if level_to_threshold.get(level, math.inf) < threshold_mm:
            continue
        zone_77_regions.update(str(x).strip() for x in level_item.get("zone_77_regions", []) or [] if x)
        admin_divisions.update(str(x).strip() for x in level_item.get("admin_divisions", []) or [] if x)
        for station in level_item.get("stations", []) or []:
            if isinstance(station, dict):
                stations.append({
                    "station_id": station.get("station_id"),
                    "name": station.get("name"),
                    "lon": station.get("lon"),
                    "lat": station.get("lat"),
                    "rainfall": station.get("rainfall"),
                    "level": level,
                })
    return stations, zone_77_regions, admin_divisions


def register_fixed_rainfall_impact_tool(mcp) -> None:
    """注册同名修正版工具；注册前尽量移除旧同名工具。"""
    _unregister_existing_tool(mcp, TOOL_NAME)

    @mcp.tool()
    def get_affected_river_network_by_rainfall(
        time_str: str,
        start_time: str = "",
        end_time: str = "",
        rainfall_threshold_mm: float = 50.0,
        max_edges: int = 5000,
        include_background: bool = True,
        downstream_km: float = 50.0,
        direct_graph_match_km: float = DEFAULT_DIRECT_GRAPH_MATCH_KM,
    ) -> dict:
        """分析暴雨影响河流并返回真实河流线段。直接按 30km 命中，下游按 50km 截断。"""
        import tools as base_tools

        pg_conf = base_tools.config["postgres"]
        custom_timerange = f"[{start_time},{end_time}]" if start_time and end_time else ""
        rainfall_result = base_tools._analyze_rainfall_core(time_str, pg_conf, custom_timerange)
        stations, zone_77_regions, admin_divisions = _extract_rainstorm_stations(rainfall_result, rainfall_threshold_mm, base_tools)

        if not stations:
            return {
                "time_range_readable": rainfall_result.get("time_range_readable", ""),
                "rainfall_threshold_mm": rainfall_threshold_mm,
                "affected_rivers": [],
                "affected_zone_77_regions": sorted(zone_77_regions),
                "affected_admin_divisions": sorted(admin_divisions),
                "stations": [],
                "total_segments": 0,
                "affected_segments": 0,
                "segments": [],
                "summary": f"统计时段 {rainfall_result.get('time_range_readable', '')} 内，未达到 {rainfall_threshold_mm}mm 降雨阈值的河系数据。",
            }

        schema = pg_conf.get("schema", "public")
        river_table = (pg_conf.get("river_table_full", "haihe_river_directed_full_v5").strip() or "haihe_river_directed_full_v5")
        station_buffer_km = 30.0
        start_stats = {
            "direct_graph_edge_count": 0,
            "station_buffer_graph_edge_count": 0,
            "direct_part_matched_graph_edge_count": 0,
            "direct_part_match_km": float(direct_graph_match_km),
        }

        with _connect_pg(pg_conf) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                columns = _table_columns(cur, schema, river_table)
                if not columns:
                    raise RuntimeError(f"河流表 {schema}.{river_table} 不存在或无字段")
                geom_col = "geom" if "geom" in columns else _first_col(columns, ("geometry", "wkb_geometry", "the_geom"))
                if not geom_col:
                    raise RuntimeError(f"河流表 {schema}.{river_table} 未找到几何字段")

                _create_station_temp(cur, stations)
                direct_rows = _query_direct_rows(
                    cur,
                    schema=schema,
                    table=river_table,
                    columns=columns,
                    geom_col=geom_col,
                    buffer_km=station_buffer_km,
                )
                start_nodes, direct_edge_keys, start_stats = _find_downstream_start_nodes(
                    stations,
                    direct_rows,
                    base_tools,
                    station_buffer_km,
                    direct_graph_match_km,
                )
                _downstream_map, downstream_segments = _collect_downstream_segments(
                    start_nodes,
                    base_tools=base_tools,
                    direct_edge_keys=direct_edge_keys,
                    downstream_km=downstream_km,
                )
                downstream_rows = _query_downstream_rows(
                    cur,
                    schema=schema,
                    table=river_table,
                    columns=columns,
                    geom_col=geom_col,
                    segments=downstream_segments,
                    buffer_km=station_buffer_km,
                )

        direct_segments = _segments_from_rows(direct_rows, impact_type="direct_buffer", stations=stations)
        downstream_plot_segments = _segments_from_rows(downstream_rows, impact_type="downstream_50km", stations=stations)
        segments = direct_segments + downstream_plot_segments
        if max_edges and max_edges > 0:
            segments = segments[:max_edges]

        affected_rivers = sorted({str(segment.get("rivername") or "").strip() for segment in segments if segment.get("rivername")})
        return {
            "time_range_readable": rainfall_result.get("time_range_readable", ""),
            "rainfall_threshold_mm": rainfall_threshold_mm,
            "affected_rivers": affected_rivers,
            "affected_zone_77_regions": sorted(zone_77_regions),
            "affected_admin_divisions": sorted(admin_divisions),
            "stations": sorted(stations, key=lambda item: item.get("rainfall", 0) or 0, reverse=True),
            "total_segments": len(direct_segments) + len(downstream_plot_segments),
            "affected_segments": len(direct_segments) + len(downstream_plot_segments),
            "segments": segments,
            "start_stats": start_stats,
            "summary": (
                f"统计时段 {rainfall_result.get('time_range_readable', '')} 内，"
                f"降雨量≥{rainfall_threshold_mm}mm 的站点共影响 {len(affected_rivers)} 条河流。"
                "直接河流按 full_v5 单线段 30km 命中输出，下游河流按 50km 范围截断。"
            ),
            "rules": {
                "direct": "ST_Dump(full_v5.geom) 后单线段 ST_DWithin 30km 命中，直接段不截断",
                "downstream_start": "下游起点来自站点30km命中 pkl 边 + 直接命中真实河段附近匹配 pkl 边",
                "downstream": "从起点沿 pkl 拓扑追踪 downstream_km，回 full_v5 匹配最近 ST_Dump 线段并截断",
                "dedupe": "按拓扑 edge_key 区分，不按 river_name/objectid 提前误删",
            },
        }

    logger.info("已注册修正版 %s 工具", TOOL_NAME)
