"""修正版暴雨影响河流工具。

业务口径：
- 30km 缓冲区只用于判断直接影响河流，不截断直接河流；
- 直接影响河流输出 full_v5 真实 geom 完整线；
- 下游影响按 river_directed_v5.pkl 追踪 50km；
- 下游回 full_v5 取真实 geom，最后一段按 50km 截断；
- 同名、同 objectid 的不同拓扑边不提前误删。
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
KM_PER_DEG = 111.32
TOOL_NAME = "get_affected_river_network_by_rainfall"


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
    except Exception:
        return None


def _parse_node_xy(node: Any) -> tuple[float | None, float | None]:
    try:
        if isinstance(node, str) and "," in node:
            x, y = node.split(",", 1)
            return float(x), float(y)
        if isinstance(node, (tuple, list)) and len(node) >= 2:
            return float(node[0]), float(node[1])
    except Exception:
        pass
    return None, None


def _geom_to_paths(geom: dict) -> list[list[list[float]]]:
    if not isinstance(geom, dict):
        return []
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "LineString":
        line = []
        for pt in coords:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                x = _safe_float(pt[0])
                y = _safe_float(pt[1])
                if x is not None and y is not None:
                    line.append([x, y])
        return [line] if len(line) >= 2 else []
    if gtype == "MultiLineString":
        out = []
        for raw_line in coords:
            line = []
            if not isinstance(raw_line, list):
                continue
            for pt in raw_line:
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    x = _safe_float(pt[0])
                    y = _safe_float(pt[1])
                    if x is not None and y is not None:
                        line.append([x, y])
            if len(line) >= 2:
                out.append(line)
        return out
    return []


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _line_length_km(path: list[list[float]]) -> float:
    if len(path) < 2:
        return 0.0
    total = 0.0
    for i in range(len(path) - 1):
        total += _haversine_km(path[i][0], path[i][1], path[i + 1][0], path[i + 1][1])
    return total


def _station_to_segment_km(lon: float, lat: float, a: list[float], b: list[float]) -> float:
    lon1, lat1 = float(a[0]), float(a[1])
    lon2, lat2 = float(b[0]), float(b[1])
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


def _min_station_distance_km(path: list[list[float]], stations: list[dict]) -> float | None:
    if len(path) < 2 or not stations:
        return None
    best = math.inf
    for station in stations:
        lon = _safe_float(station.get("lon"))
        lat = _safe_float(station.get("lat"))
        if lon is None or lat is None:
            continue
        for i in range(len(path) - 1):
            best = min(best, _station_to_segment_km(lon, lat, path[i], path[i + 1]))
    return None if best == math.inf else best


def _clip_path_to_length(path: list[list[float]], keep_km: float) -> list[list[float]]:
    if len(path) < 2 or keep_km <= 0:
        return []
    out = [path[0]]
    remaining = float(keep_km)
    for i in range(len(path) - 1):
        a = path[i]
        b = path[i + 1]
        seg_len = _haversine_km(a[0], a[1], b[0], b[1])
        if seg_len <= 0:
            continue
        if remaining >= seg_len:
            out.append(b)
            remaining -= seg_len
            continue
        ratio = max(0.0, min(1.0, remaining / seg_len))
        out.append([a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio])
        break
    return out if len(out) >= 2 else []


def _edge_key(source: str, u: Any, v: Any, key: Any, attr: dict, base_tools) -> str:
    oid = base_tools._edge_objectid_key(attr) or ""
    rn = base_tools.get_edge_river_name(attr) or ""
    return f"{source}|{u}|{v}|{key}|{oid}|{rn}"


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
    if not rows:
        return
    execute_values(
        cur,
        "INSERT INTO tmp_rain_impact_stations VALUES %s",
        rows,
        template="(%s,%s,%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326))",
    )


def _table_columns(cur, schema: str, table: str) -> set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
        (schema, table),
    )
    return {str(row["column_name"]) for row in cur.fetchall()}


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
                'rainfall', s.rainfall
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


def _collect_downstream_segments(source_rivers: set[str], *, downstream_km: float, base_tools) -> tuple[dict[str, dict], list[dict]]:
    limit = float(downstream_km)
    if not source_rivers or limit <= 0:
        return {}, []
    graph = base_tools.get_graph()
    end_nodes_by_river = base_tools._get_end_nodes_by_river_map()
    downstream_map: dict[str, dict] = {}
    segment_map: dict[str, dict] = {}

    for source in sorted(source_rivers):
        start_nodes = end_nodes_by_river.get(source, set())
        if not start_nodes:
            continue
        best_dist = {node: 0.0 for node in start_nodes}
        seq = count()
        heap = [(0.0, next(seq), node) for node in start_nodes]
        heapq.heapify(heap)

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
                    from_x, from_y = _parse_node_xy(u)
                    to_x, to_y = _parse_node_xy(v)
                    ek = _edge_key(source, u, v, key, attr, base_tools)
                    old = segment_map.get(ek)
                    if old is None or curr_dist < old["min_distance_km"] or clip_fraction > old["clip_fraction"]:
                        segment_map[ek] = {
                            "edge_key": ek,
                            "source_river": source,
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
                    item = downstream_map.setdefault(river_name, {"river_name": river_name, "min_distance_km": math.inf, "source_rivers": []})
                    if curr_dist < item["min_distance_km"]:
                        item["min_distance_km"] = curr_dist
                    if source not in item["source_rivers"]:
                        item["source_rivers"].append(source)
                if next_dist <= limit and next_dist < best_dist.get(v, math.inf):
                    best_dist[v] = next_dist
                    heapq.heappush(heap, (next_dist, next(seq), v))

    for item in downstream_map.values():
        item["min_distance_km"] = round(float(item["min_distance_km"]), 3)
    return downstream_map, sorted(segment_map.values(), key=lambda x: (x["min_distance_km"], x["river_name"], x["edge_key"]))


def _create_downstream_temp(cur, segments: list[dict]) -> None:
    cur.execute("DROP TABLE IF EXISTS tmp_downstream_segments")
    cur.execute("""
        CREATE TEMP TABLE tmp_downstream_segments(
            edge_key text,
            source_river text,
            objectid text,
            river_name text,
            min_distance_km double precision,
            end_distance_km double precision,
            keep_km double precision,
            clip_fraction double precision,
            from_x double precision,
            from_y double precision,
            to_x double precision,
            to_y double precision,
            pkl_line geometry(LineString,4326)
        ) ON COMMIT DROP
    """)
    rows = []
    for s in segments:
        fx, fy, tx, ty = s.get("from_x"), s.get("from_y"), s.get("to_x"), s.get("to_y")
        rows.append((
            s["edge_key"], s["source_river"], s["objectid"], s["river_name"], s["min_distance_km"],
            s["end_distance_km"], s["keep_km"], s["clip_fraction"], fx, fy, tx, ty,
            fx, fy, tx, ty, fx, fy, tx, ty,
        ))
    if not rows:
        return
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
    _create_downstream_temp(cur, segments)
    objectid_col = _first_col(columns, ("objectid", "OBJECTID", "id", "gid"))
    id_col = _first_col(columns, ("id", "gid"))
    if not objectid_col:
        return []
    objectid_expr = f"r.{_quote_ident(objectid_col)}::text"
    id_expr = f"r.{_quote_ident(id_col)}::text" if id_col else objectid_expr
    river_expr = _river_name_expr(columns, alias="r")
    q_schema = _quote_ident(schema)
    q_table = _quote_ident(table)
    q_geom = _quote_ident(geom_col)
    q_objectid = _quote_ident(objectid_col)
    cur.execute(f"""
        WITH candidates AS (
            SELECT
                ds.edge_key,
                ds.source_river,
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
                ds.pkl_line,
                r.{q_geom} AS original_geom,
                ST_LineMerge(r.{q_geom}) AS merged_geom,
                CASE WHEN ds.pkl_line IS NULL THEN NULL
                     ELSE ST_Distance(r.{q_geom}::geography, ds.pkl_line::geography) / 1000.0 END AS match_distance_km
            FROM {q_schema}.{q_table} r
            JOIN tmp_downstream_segments ds
              ON r.{q_objectid}::text = ds.objectid
            WHERE r.{q_geom} IS NOT NULL
              AND NOT ST_IsEmpty(r.{q_geom})
              AND NOT EXISTS (
                  SELECT 1 FROM tmp_rain_impact_stations s
                  WHERE ST_DWithin(r.{q_geom}::geography, s.geom::geography, %(buffer_m)s)
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
                source_river,
                id,
                objectid,
                COALESCE(NULLIF(TRIM(db_river_name), ''), graph_river_name) AS river_name,
                min_distance_km,
                end_distance_km,
                keep_km,
                clip_fraction,
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
            source_river,
            id,
            objectid,
            river_name,
            min_distance_km AS min_downstream_distance_km,
            end_distance_km AS end_downstream_distance_km,
            keep_km,
            clip_fraction,
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
    out: list[dict] = []
    for row in rows:
        geom_json = row.get("geom_json")
        if not geom_json:
            continue
        try:
            geom = json.loads(geom_json) if isinstance(geom_json, str) else geom_json
        except Exception:
            continue
        paths = _geom_to_paths(geom)
        for idx, path in enumerate(paths):
            if len(path) < 2:
                continue
            min_station_km = _min_station_distance_km(path, stations)
            length_km = _line_length_km(path)
            seg = {
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
                "edge_key": row.get("edge_key") or f"{row.get('objectid')}:{idx}",
                "is_affected": True,
                "impact_type": impact_type,
                "min_station_distance_km": round(min_station_km, 3) if min_station_km is not None else None,
            }
            if impact_type == "downstream_50km":
                seg.update({
                    "source_river": row.get("source_river"),
                    "min_downstream_distance_km": row.get("min_downstream_distance_km"),
                    "end_downstream_distance_km": row.get("end_downstream_distance_km"),
                    "keep_km": row.get("keep_km"),
                    "clip_fraction": row.get("clip_fraction"),
                    "match_distance_km": row.get("match_distance_km"),
                    "geometry_source": "full_v5_downstream_50km_clipped",
                })
            else:
                seg.update({
                    "trigger_station_count": row.get("trigger_station_count"),
                    "trigger_stations": row.get("trigger_stations") or [],
                    "geometry_source": "full_v5_direct_30km_uncut",
                })
            out.append(seg)
    return out


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
    ) -> dict:
        """分析暴雨影响河流并返回真实河流线段。直接河流不裁剪，下游 50km 裁剪。"""
        import tools as base_tools

        pg_conf = base_tools.config["postgres"]
        custom_timerange = f"[{start_time},{end_time}]" if start_time and end_time else ""
        rainfall_result = base_tools._analyze_rainfall_core(time_str, pg_conf, custom_timerange)
        level_to_threshold = {name: lo for name, lo, _hi in base_tools.RAIN_LEVELS}

        affected_zone_77_regions: set[str] = set()
        affected_admin_divisions: set[str] = set()
        stations: list[dict] = []
        for level_item in rainfall_result.get("level_analysis", []) or []:
            level = level_item.get("level", "")
            if level_to_threshold.get(level, float("inf")) < rainfall_threshold_mm:
                continue
            affected_zone_77_regions.update(str(x).strip() for x in level_item.get("zone_77_regions", []) or [] if x)
            affected_admin_divisions.update(str(x).strip() for x in level_item.get("admin_divisions", []) or [] if x)
            for s in level_item.get("stations", []) or []:
                if isinstance(s, dict):
                    stations.append({
                        "station_id": s.get("station_id"),
                        "name": s.get("name"),
                        "lon": s.get("lon"),
                        "lat": s.get("lat"),
                        "rainfall": s.get("rainfall"),
                        "level": level,
                    })

        if not stations:
            return {
                "time_range_readable": rainfall_result.get("time_range_readable", ""),
                "rainfall_threshold_mm": rainfall_threshold_mm,
                "affected_rivers": [],
                "affected_zone_77_regions": sorted(affected_zone_77_regions),
                "affected_admin_divisions": sorted(affected_admin_divisions),
                "stations": [],
                "total_segments": 0,
                "affected_segments": 0,
                "segments": [],
                "summary": f"统计时段 {rainfall_result.get('time_range_readable', '')} 内，未达到 {rainfall_threshold_mm}mm 降雨阈值的河系数据。",
            }

        schema = pg_conf.get("schema", "public")
        river_table = (pg_conf.get("river_table_full", "haihe_river_directed_full_v5").strip() or "haihe_river_directed_full_v5")
        station_buffer_km = 30.0
        direct_rows: list[dict] = []
        downstream_rows: list[dict] = []
        downstream_map: dict[str, dict] = {}
        downstream_segments: list[dict] = []

        with _connect_pg(pg_conf) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                columns = _table_columns(cur, schema, river_table)
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
                direct_names = {str(row.get("river_name") or "").strip() for row in direct_rows if row.get("river_name")}
                downstream_map, downstream_segments = _collect_downstream_segments(
                    direct_names,
                    downstream_km=downstream_km,
                    base_tools=base_tools,
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

        affected_rivers = sorted({str(s.get("rivername") or "").strip() for s in segments if s.get("rivername")})
        affected_segments = len(segments)
        return {
            "time_range_readable": rainfall_result.get("time_range_readable", ""),
            "rainfall_threshold_mm": rainfall_threshold_mm,
            "affected_rivers": affected_rivers,
            "affected_zone_77_regions": sorted(affected_zone_77_regions),
            "affected_admin_divisions": sorted(affected_admin_divisions),
            "stations": sorted(stations, key=lambda x: x.get("rainfall", 0) or 0, reverse=True),
            "total_segments": len(segments),
            "affected_segments": affected_segments,
            "segments": segments[:max_edges] if max_edges and max_edges > 0 else segments,
            "summary": (
                f"统计时段 {rainfall_result.get('time_range_readable', '')} 内，"
                f"降雨量≥{rainfall_threshold_mm}mm 的站点共影响 {len(affected_rivers)} 条河流。"
                "直接河流按 30km 命中完整输出，下游河流按 50km 范围截断。"
            ),
            "rules": {
                "direct": "ST_DWithin 30km 命中，真实河流完整输出，不截断",
                "downstream": "从直接河流下游端点拓扑追踪 downstream_km，最后一段截断",
                "dedupe": "按拓扑 edge_key 区分，不按 river_name/objectid 提前误删",
            },
        }

    logger.info("已注册修正版 %s 工具", TOOL_NAME)
