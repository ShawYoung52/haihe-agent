"""24 小时降水影响河流 GeoJSON。

代码评审版只保留当前业务链路：
1. 5 分钟站点 PRE 聚合为站点 24h 累计雨量；
2. rain_24h >= 阈值的站点作为触发站；
3. PostGIS 查触发站 30km 内真实河段，直接河段不截断；
4. pkl 河网拓扑向下游追踪 50km；
5. 下游边回 full_v5 时按 pkl 边位置匹配最近真实河段，并按 50km 截断。
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
from typing import Any, Iterable, Iterator

import pandas as pd

_GRAPH_CACHE = None
_GRAPH_CACHE_PATH: str | None = None
_GRAPH_CACHE_MTIME: float | None = None
_GRAPH_LOCK = threading.RLock()

KM_PER_DEG = 111.32
DEFAULT_RIVER_TABLE = "haihe_river_directed_full_v5"
DEFAULT_GEOM_COLUMN = "geom"
DEFAULT_OBJECTID_COLUMN = "objectid"
DEFAULT_RIVER_NAME_COLUMN = "src_name"


def _first_not_empty(values: pd.Series) -> Any:
    valid = values.dropna()
    if valid.empty:
        return None
    for item in valid:
        if str(item).strip():
            return item
    return valid.iloc[0]


def aggregate_5min_station_pre_to_24h(
    csv_path: str | os.PathLike,
    *,
    station_id_col: str = "Station_Id_C",
    datetime_col: str = "Datetime",
    pre_col: str = "PRE",
    lon_col: str = "Lon",
    lat_col: str = "Lat",
    invalid_pre_upper_mm: float = 9999.0,
) -> pd.DataFrame:
    header = pd.read_csv(csv_path, nrows=0)
    required = {station_id_col, datetime_col, pre_col, lon_col, lat_col}
    missing = required - set(header.columns)
    if missing:
        raise ValueError(f"CSV 缺少必要字段：{sorted(missing)}")

    preferred_cols = [
        station_id_col,
        datetime_col,
        pre_col,
        lat_col,
        lon_col,
        "City",
        "Station_Name",
        "Cnty",
        "Province",
        "Town",
    ]
    usecols = [col for col in preferred_cols if col in header.columns]
    df = pd.read_csv(csv_path, usecols=usecols, dtype={station_id_col: "string"}, low_memory=False)

    df[datetime_col] = pd.to_datetime(df[datetime_col], errors="coerce")
    df[pre_col] = pd.to_numeric(df[pre_col], errors="coerce")
    df[lon_col] = pd.to_numeric(df[lon_col], errors="coerce")
    df[lat_col] = pd.to_numeric(df[lat_col], errors="coerce")

    valid_pre = df[pre_col].notna() & (df[pre_col] >= 0) & (df[pre_col] <= invalid_pre_upper_mm)
    df["_pre_valid"] = valid_pre
    df.loc[~valid_pre, pre_col] = 0.0

    aggs: dict[str, tuple[str, Any]] = {
        "station_id": (station_id_col, _first_not_empty),
        "rain_24h": (pre_col, "sum"),
        "lon": (lon_col, _first_not_empty),
        "lat": (lat_col, _first_not_empty),
        "obs_count": (pre_col, "count"),
        "valid_pre_count": ("_pre_valid", "sum"),
        "start_time": (datetime_col, "min"),
        "end_time": (datetime_col, "max"),
    }
    optional_cols = {
        "City": "city",
        "Station_Name": "station_name",
        "Cnty": "cnty",
        "Province": "province",
        "Town": "town",
    }
    for source, target in optional_cols.items():
        if source in df.columns:
            aggs[target] = (source, _first_not_empty)

    grouped = df.groupby(station_id_col, dropna=False).agg(**aggs).reset_index(drop=True)
    grouped["rain_24h"] = grouped["rain_24h"].round(3)
    grouped["obs_count"] = grouped["obs_count"].astype(int)
    grouped["valid_pre_count"] = grouped["valid_pre_count"].astype(int)
    return grouped.sort_values("rain_24h", ascending=False).reset_index(drop=True)


def _jsonable(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def _station_record(row: pd.Series) -> dict:
    fields = [
        "station_id",
        "station_name",
        "province",
        "city",
        "cnty",
        "town",
        "lon",
        "lat",
        "rain_24h",
        "obs_count",
        "valid_pre_count",
        "start_time",
        "end_time",
    ]
    return {field: _jsonable(row.get(field)) for field in fields if field in row.index}


def _make_station_geojson(stations: list[dict]) -> dict:
    features = []
    for station in stations:
        lon = station.get("lon")
        lat = station.get("lat")
        if lon is None or lat is None:
            continue
        props = dict(station)
        props.pop("lon", None)
        props.pop("lat", None)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
            "properties": props,
        })
    return {"type": "FeatureCollection", "features": features}


def _default_graph_path() -> str:
    here = Path(__file__).resolve()
    candidates = [
        here.parents[1] / "Service" / "river_directed_v5.pkl",
        here.parents[1] / "Service" / "river_directed_v4_asis.pkl",
        Path.cwd() / "Service" / "river_directed_v5.pkl",
        Path.cwd() / "Service" / "river_directed_v4_asis.pkl",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
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
    value_m = attr.get("length_m")
    try:
        return max(float(value_m) / 1000.0, 0.0)
    except (TypeError, ValueError):
        return 0.0


def _edge_objectid_key(attr: dict) -> str:
    if not isinstance(attr, dict):
        return ""
    for key in ("objectid", "OBJECTID", "id", "ID", "gid"):
        raw = attr.get(key)
        if raw is None or not str(raw).strip():
            continue
        text = str(raw).strip()
        try:
            value = float(text)
            if value.is_integer():
                return str(int(value))
        except (TypeError, ValueError):
            pass
        return text
    return ""


def _quote_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name or "")):
        raise ValueError(f"非法 SQL 标识符：{name!r}")
    return f'"{name}"'


def _split_table_name(table_name: str, default_schema: str = "public") -> tuple[str, str]:
    value = str(table_name or "").strip()
    if "." in value:
        schema, table = value.split(".", 1)
        return schema.strip() or default_schema, table.strip()
    return default_schema, value or DEFAULT_RIVER_TABLE


def _get_engine():
    try:
        from utils.db import engine  # type: ignore
    except Exception:
        from .db import engine  # type: ignore
    return engine


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
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))


def _edge_key(u: Any, v: Any, key: Any, attr: dict) -> str:
    return f"{u}|{v}|{key}|{_edge_objectid_key(attr)}|{get_edge_river_name(attr)}"


def _geometry_from_row(row: dict) -> dict | None:
    raw = row.get("geom_json")
    if not raw:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw


def _geometry_lines(geometry: dict) -> list[list[list[float]]]:
    if geometry.get("type") == "LineString":
        coords = geometry.get("coordinates") or []
        return [coords] if len(coords) >= 2 else []
    if geometry.get("type") == "MultiLineString":
        return [line for line in geometry.get("coordinates") or [] if len(line) >= 2]
    return []


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
    values = [_point_to_line_km(p1[0], p1[1], line), _point_to_line_km(p2[0], p2[1], line)]
    values.extend(_point_to_segment_km(float(pt[0]), float(pt[1]), p1, p2) for pt in line)
    return min(values)


def _create_station_temp(cur, stations: list[dict]) -> None:
    cur.execute("DROP TABLE IF EXISTS tmp_rain24h_impact_stations")
    cur.execute("""
        CREATE TEMP TABLE tmp_rain24h_impact_stations(
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
        rows.append((str(station.get("station_id") or ""), str(station.get("station_name") or ""), lon, lat, float(station.get("rain_24h") or 0.0), lon, lat))
    if rows:
        from psycopg2.extras import execute_values
        execute_values(
            cur,
            "INSERT INTO tmp_rain24h_impact_stations VALUES %s",
            rows,
            template="(%s,%s,%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326))",
        )


def _query_direct_parts(cur, *, schema: str, table: str, geom_col: str, objectid_col: str, river_name_col: str, buffer_km: float) -> list[dict]:
    cur.execute(f"""
        WITH river_parts AS (
            SELECT
                r.{_quote_ident(objectid_col)}::text AS objectid,
                COALESCE(NULLIF(TRIM(r.{_quote_ident(river_name_col)}::text), ''), '未知') AS river_name,
                (ST_Dump(r.{_quote_ident(geom_col)})).geom AS geom
            FROM {_quote_ident(schema)}.{_quote_ident(table)} r
            WHERE r.{_quote_ident(geom_col)} IS NOT NULL
              AND NOT ST_IsEmpty(r.{_quote_ident(geom_col)})
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
        JOIN tmp_rain24h_impact_stations s
          ON ST_DWithin(p.geom::geography, s.geom::geography, %(buffer_m)s)
        GROUP BY p.objectid, p.river_name, p.geom
        ORDER BY min_station_distance_km, p.river_name, p.objectid
    """, {"buffer_m": float(buffer_km) * 1000.0})
    return list(cur.fetchall())


def _direct_part_refs(direct_rows: list[dict]) -> list[dict]:
    refs = []
    for row in direct_rows:
        geometry = _geometry_from_row(row)
        if not geometry:
            continue
        refs.append({
            "objectid": str(row.get("objectid") or "").strip(),
            "river_name": str(row.get("river_name") or "").strip(),
            "lines": _geometry_lines(geometry),
        })
    return refs


def _find_direct_graph_starts(stations: list[dict], direct_rows: list[dict], graph_path: str | os.PathLike | None, buffer_km: float, direct_match_km: float) -> tuple[dict[Any, float], set[str]]:
    graph = get_graph(graph_path)
    station_points = [(float(s["lon"]), float(s["lat"])) for s in stations]
    refs = _direct_part_refs(direct_rows)
    start_nodes: dict[Any, float] = {}
    direct_edge_keys: set[str] = set()

    for u, v, key, attr in iter_graph_edges(graph):
        ux, uy = _parse_node_xy(u)
        vx, vy = _parse_node_xy(v)
        if ux is None or uy is None or vx is None or vy is None:
            continue
        p1 = (ux, uy)
        p2 = (vx, vy)
        objectid = str(_edge_objectid_key(attr) or "").strip()
        river_name = str(get_edge_river_name(attr) or "").strip()
        station_hit = min(_point_to_segment_km(lon, lat, p1, p2) for lon, lat in station_points) <= float(buffer_km)
        direct_part_hit = any(
            ((objectid and objectid == ref["objectid"]) or (river_name and river_name == ref["river_name"]))
            and any(_edge_to_line_km(p1, p2, line) <= float(direct_match_km) for line in ref["lines"])
            for ref in refs
        )
        if station_hit or direct_part_hit:
            direct_edge_keys.add(_edge_key(u, v, key, attr))
            start_nodes[v] = 0.0
    return start_nodes, direct_edge_keys


def _collect_downstream_segments(start_nodes: dict[Any, float], graph_path: str | os.PathLike | None, direct_edge_keys: set[str], downstream_km: float) -> list[dict]:
    limit = float(downstream_km)
    if not start_nodes or limit <= 0:
        return []

    graph = get_graph(graph_path)
    best_dist = dict(start_nodes)
    seq = count()
    heap = [(float(dist), next(seq), node) for node, dist in start_nodes.items()]
    heapq.heapify(heap)
    segments: dict[str, dict] = {}

    while heap:
        curr_dist, _seq, node = heapq.heappop(heap)
        if curr_dist > best_dist.get(node, math.inf) or curr_dist >= limit:
            continue
        for u, v, key, attr in iter_out_edges(graph, node):
            objectid = str(_edge_objectid_key(attr) or "").strip()
            river_name = str(get_edge_river_name(attr) or "").strip()
            if not objectid or not river_name:
                continue

            edge_len = max(float(get_edge_length_km(attr) or 0.0), 0.0)
            next_dist = curr_dist + edge_len
            keep_km = max(min(limit - curr_dist, edge_len), 0.0) if edge_len > 0 else 0.0
            clip_fraction = 1.0 if edge_len <= 0 else max(min(keep_km / edge_len, 1.0), 0.0)
            edge_id = _edge_key(u, v, key, attr)

            if clip_fraction > 0:
                from_x, from_y = _parse_node_xy(u)
                to_x, to_y = _parse_node_xy(v)
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
        fx, fy = segment.get("from_x"), segment.get("from_y")
        tx, ty = segment.get("to_x"), segment.get("to_y")
        rows.append((segment["edge_key"], segment["objectid"], segment["river_name"], segment["min_distance_km"], segment["end_distance_km"], segment["keep_km"], segment["clip_fraction"], bool(segment.get("is_direct_graph_edge")), fx, fy, tx, ty, fx, fy, tx, ty, fx, fy, tx, ty))
    if rows:
        from psycopg2.extras import execute_values
        execute_values(
            cur,
            "INSERT INTO tmp_downstream_segments VALUES %s",
            rows,
            template="""
            (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
             CASE WHEN %s IS NULL OR %s IS NULL OR %s IS NULL OR %s IS NULL THEN NULL
                  ELSE ST_SetSRID(ST_MakeLine(ST_MakePoint(%s,%s), ST_MakePoint(%s,%s)),4326) END)
            """,
        )


def _query_downstream_parts(cur, *, schema: str, table: str, geom_col: str, objectid_col: str, river_name_col: str, segments: list[dict], buffer_km: float) -> list[dict]:
    if not segments:
        return []
    _create_downstream_temp(cur, segments)
    cur.execute(f"""
        WITH river_parts AS (
            SELECT
                r.{_quote_ident(objectid_col)}::text AS objectid,
                COALESCE(NULLIF(TRIM(r.{_quote_ident(river_name_col)}::text), ''), '未知') AS db_river_name,
                (ST_Dump(r.{_quote_ident(geom_col)})).geom AS geom
            FROM {_quote_ident(schema)}.{_quote_ident(table)} r
            WHERE r.{_quote_ident(geom_col)} IS NOT NULL
              AND NOT ST_IsEmpty(r.{_quote_ident(geom_col)})
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
            WHERE ds.pkl_line IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM tmp_rain24h_impact_stations s
                  WHERE ST_DWithin(p.geom::geography, s.geom::geography, %(buffer_m)s)
              )
        ),
        ranked AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY edge_key ORDER BY match_distance_km) AS rn
            FROM candidates
        ),
        fractions AS (
            SELECT *,
                ST_LineLocatePoint(line_geom, ST_SetSRID(ST_MakePoint(from_x, from_y),4326)) AS from_frac,
                ST_LineLocatePoint(line_geom, ST_SetSRID(ST_MakePoint(to_x, to_y),4326)) AS to_frac,
                ST_Length(line_geom::geography) / 1000.0 AS line_km
            FROM ranked
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
    """, {"buffer_m": float(buffer_km) * 1000.0})
    return list(cur.fetchall())


def _river_feature(row: dict, impact_type: str) -> dict | None:
    geometry = _geometry_from_row(row)
    if not geometry:
        return None
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
            "geometry_source": "full_v5_dump_part_direct_30km_uncut",
        })
    else:
        match_distance = row.get("match_distance_km")
        props.update({
            "min_downstream_distance_km": row.get("min_downstream_distance_km"),
            "end_downstream_distance_km": row.get("end_downstream_distance_km"),
            "keep_km": row.get("keep_km"),
            "clip_fraction": row.get("clip_fraction"),
            "is_direct_graph_edge": row.get("is_direct_graph_edge"),
            "match_distance_km": round(float(match_distance), 3) if match_distance is not None else None,
            "geometry_source": "full_v5_dump_part_downstream_50km_clipped",
        })
    return {"type": "Feature", "geometry": geometry, "properties": props}


def _build_river_geojson(direct_rows: list[dict], downstream_rows: list[dict]) -> dict:
    features = []
    seen = set()
    for row, impact_type in [(r, "direct_buffer") for r in direct_rows] + [(r, "downstream_50km") for r in downstream_rows]:
        feature = _river_feature(row, impact_type)
        if not feature:
            continue
        key = (impact_type, feature["properties"].get("edge_key"), feature["properties"].get("objectid"), json.dumps(feature["geometry"], sort_keys=True))
        if key not in seen:
            seen.add(key)
            features.append(feature)
    features.sort(key=lambda f: (0 if f["properties"]["impact_type"] == "direct_buffer" else 1, f["properties"].get("river_name") or ""))
    return {"type": "FeatureCollection", "features": features}


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
    geom_column: str = DEFAULT_GEOM_COLUMN,
    objectid_column: str = DEFAULT_OBJECTID_COLUMN,
    river_name_column: str = DEFAULT_RIVER_NAME_COLUMN,
    direct_match_km: float = 3.0,
) -> dict:
    if rain_threshold_mm < 0:
        raise ValueError("rain_threshold_mm 不能为负数")
    if station_buffer_km <= 0:
        raise ValueError("station_buffer_km 必须大于 0")
    if downstream_km < 0:
        raise ValueError("downstream_km 不能为负数")

    station_df = aggregate_5min_station_pre_to_24h(csv_path)
    impact_df = station_df[(station_df["rain_24h"] >= float(rain_threshold_mm)) & station_df["lon"].notna() & station_df["lat"].notna()].copy()
    impact_stations = [_station_record(row) for _, row in impact_df.iterrows()]
    top_stations = [_station_record(row) for _, row in station_df.head(max(int(top_station_limit), 0)).iterrows()]
    table_schema, table_name = _split_table_name(river_table, default_schema=schema)

    result = {
        "status": "ok",
        "params": {
            "rain_threshold_mm": float(rain_threshold_mm),
            "station_buffer_km": float(station_buffer_km),
            "downstream_km": float(downstream_km),
            "river_table": f"{table_schema}.{table_name}",
            "graph_path": str(graph_path or _default_graph_path()),
        },
        "time_range": {
            "start_time": _jsonable(station_df["start_time"].min()) if len(station_df) else None,
            "end_time": _jsonable(station_df["end_time"].max()) if len(station_df) else None,
        },
        "station_summary": {
            "total_station_count": int(len(station_df)),
            "impact_station_count": int(len(impact_stations)),
            "max_rain_24h": float(station_df["rain_24h"].max() or 0.0) if len(station_df) else 0.0,
        },
        "rainfall_24h_top_stations": top_stations,
        "impact_stations": impact_stations,
        "station_geojson": _make_station_geojson(impact_stations),
        "direct_rivers": [],
        "downstream_rivers": [],
        "downstream_segments": [],
        "river_geojson": {"type": "FeatureCollection", "features": []},
    }
    if not impact_stations:
        result["message"] = "没有站点达到设定降雨阈值，未生成影响河流。"
        return result

    engine = _get_engine()
    raw_conn = engine.raw_connection()
    try:
        from psycopg2.extras import RealDictCursor
        with raw_conn.cursor(cursor_factory=RealDictCursor) as cur:
            _create_station_temp(cur, impact_stations)
            direct_rows = _query_direct_parts(
                cur,
                schema=table_schema,
                table=table_name,
                geom_col=geom_column,
                objectid_col=objectid_column,
                river_name_col=river_name_column,
                buffer_km=station_buffer_km,
            )
            start_nodes, direct_edge_keys = _find_direct_graph_starts(impact_stations, direct_rows, graph_path, station_buffer_km, direct_match_km)
            downstream_segments = _collect_downstream_segments(start_nodes, graph_path, direct_edge_keys, downstream_km)
            downstream_rows = _query_downstream_parts(
                cur,
                schema=table_schema,
                table=table_name,
                geom_col=geom_column,
                objectid_col=objectid_column,
                river_name_col=river_name_column,
                segments=downstream_segments,
                buffer_km=station_buffer_km,
            )
    finally:
        raw_conn.close()

    result.update({
        "direct_rivers": sorted({str(row.get("river_name") or "") for row in direct_rows if row.get("river_name")}),
        "downstream_rivers": sorted({str(row.get("river_name") or "") for row in downstream_rows if row.get("river_name")}),
        "downstream_segments": downstream_segments,
        "river_geojson": _build_river_geojson(direct_rows, downstream_rows),
    })
    return result
