"""24 小时站点降水影响河流 GeoJSON 工具。

本模块用于把 5 分钟站点降水 CSV 聚合为 24 小时站点累计降水，并复用
“暴雨站点 30km 缓冲区 + 下游河流 50km 追踪”的业务口径，返回前端
可直接渲染的 GeoJSON FeatureCollection。

典型用法：
    from utils.rainfall_impact_geojson import build_rain24h_impact_river_geojson

    result = build_rain24h_impact_river_geojson(
        csv_path="/path/to/24hourmindata.csv",
        rain_threshold_mm=50.0,
        station_buffer_km=30.0,
        downstream_km=50.0,
    )
    river_geojson = result["river_geojson"]
"""

from __future__ import annotations

import heapq
import json
import math
import os
import pickle
import re
import threading
from collections import defaultdict
from itertools import count
from pathlib import Path
from typing import Any, Iterable, Iterator

import pandas as pd


_GRAPH_CACHE = None
_GRAPH_CACHE_PATH: str | None = None
_GRAPH_CACHE_MTIME: float | None = None
_GRAPH_LOCK = threading.RLock()
_END_NODES_BY_RIVER: dict[str, set] | None = None
_END_NODES_INDEX_META: tuple[str, float] | None = None


REQUIRED_CSV_COLUMNS = {
    "Station_Id_C",
    "Datetime",
    "PRE",
    "Lat",
    "Lon",
}


def _first_not_empty(values: pd.Series) -> Any:
    """返回序列中的第一个非空值。"""
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
    """把 5 分钟站点降水 CSV 聚合为站点 24 小时累计降水。

    Args:
        csv_path: CSV 文件路径。
        station_id_col: 站号字段名。
        datetime_col: 时间字段名。
        pre_col: 5 分钟降水字段名。
        lon_col: 经度字段名。
        lat_col: 纬度字段名。
        invalid_pre_upper_mm: 单条 5 分钟降水超过该值时视作无效。

    Returns:
        每个站点一行的 DataFrame，核心字段包括：
        station_id、rain_24h、lon、lat、start_time、end_time、obs_count。
    """
    csv_path = str(csv_path)
    header = pd.read_csv(csv_path, nrows=0)
    missing = {
        station_id_col,
        datetime_col,
        pre_col,
        lon_col,
        lat_col,
    } - set(header.columns)
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
    usecols = [c for c in preferred_cols if c in header.columns]

    df = pd.read_csv(
        csv_path,
        usecols=usecols,
        dtype={station_id_col: "string"},
        low_memory=False,
    )

    df[datetime_col] = pd.to_datetime(df[datetime_col], errors="coerce")
    df[pre_col] = pd.to_numeric(df[pre_col], errors="coerce")
    df[lon_col] = pd.to_numeric(df[lon_col], errors="coerce")
    df[lat_col] = pd.to_numeric(df[lat_col], errors="coerce")

    # 降水质量控制：负值和明显异常大值不参与累计。
    valid_pre_mask = df[pre_col].notna() & (df[pre_col] >= 0) & (df[pre_col] <= invalid_pre_upper_mm)
    df["_pre_valid"] = valid_pre_mask
    df.loc[~valid_pre_mask, pre_col] = 0.0

    meta_aggs: dict[str, tuple[str, Any]] = {
        "station_id": (station_id_col, _first_not_empty),
        "rain_24h": (pre_col, "sum"),
        "lon": (lon_col, _first_not_empty),
        "lat": (lat_col, _first_not_empty),
        "obs_count": (pre_col, "count"),
        "valid_pre_count": ("_pre_valid", "sum"),
        "start_time": (datetime_col, "min"),
        "end_time": (datetime_col, "max"),
    }

    optional_map = {
        "City": "city",
        "Station_Name": "station_name",
        "Cnty": "cnty",
        "Province": "province",
        "Town": "town",
    }
    for source_col, out_col in optional_map.items():
        if source_col in df.columns:
            meta_aggs[out_col] = (source_col, _first_not_empty)

    grouped = (
        df.groupby(station_id_col, dropna=False)
        .agg(**meta_aggs)
        .reset_index(drop=True)
    )

    grouped["rain_24h"] = grouped["rain_24h"].round(3)
    grouped["obs_count"] = grouped["obs_count"].astype(int)
    grouped["valid_pre_count"] = grouped["valid_pre_count"].astype(int)
    grouped = grouped.sort_values("rain_24h", ascending=False).reset_index(drop=True)
    return grouped


def _default_graph_path() -> str:
    """查找 hhlyqyxt-master/Service/river_directed_v4_asis.pkl。"""
    here = Path(__file__).resolve()
    candidates = [
        here.parents[1] / "Service" / "river_directed_v4_asis.pkl",
        Path.cwd() / "Service" / "river_directed_v4_asis.pkl",
        Path.cwd() / "hhlyqyxt-master" / "Service" / "river_directed_v4_asis.pkl",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    # 返回最常见路径，便于错误信息直接暴露应放置的位置。
    return str(candidates[0])


def get_graph(graph_path: str | os.PathLike | None = None, *, force_reload: bool = False):
    """懒加载并缓存河网拓扑图。"""
    global _GRAPH_CACHE, _GRAPH_CACHE_PATH, _GRAPH_CACHE_MTIME
    path = str(graph_path or _default_graph_path())
    if not os.path.exists(path):
        raise FileNotFoundError(f"河网拓扑文件不存在：{path}")

    mtime = os.path.getmtime(path)
    with _GRAPH_LOCK:
        need_reload = (
            force_reload
            or _GRAPH_CACHE is None
            or _GRAPH_CACHE_PATH != path
            or _GRAPH_CACHE_MTIME != mtime
        )
        if need_reload:
            with open(path, "rb") as f:
                _GRAPH_CACHE = pickle.load(f)
            _GRAPH_CACHE_PATH = path
            _GRAPH_CACHE_MTIME = mtime
            _invalidate_end_node_cache()
        return _GRAPH_CACHE


def _invalidate_end_node_cache() -> None:
    global _END_NODES_BY_RIVER, _END_NODES_INDEX_META
    _END_NODES_BY_RIVER = None
    _END_NODES_INDEX_META = None


def iter_graph_edges(graph) -> Iterator[tuple[Any, Any, Any, dict]]:
    """兼容 DiGraph / MultiDiGraph 的边遍历。"""
    if graph.is_multigraph():
        for u, v, key, attr in graph.edges(keys=True, data=True):
            yield u, v, key, attr
    else:
        for u, v, attr in graph.edges(data=True):
            yield u, v, None, attr


def iter_out_edges(graph, node) -> Iterator[tuple[Any, Any, Any, dict]]:
    """兼容 DiGraph / MultiDiGraph 的出边遍历。"""
    if graph.is_multigraph():
        for u, v, key, attr in graph.out_edges(node, keys=True, data=True):
            yield u, v, key, attr
    else:
        for u, v, attr in graph.out_edges(node, data=True):
            yield u, v, None, attr


def make_edge_id(u, v, key=None):
    return (u, v, key) if key is not None else (u, v)


def get_edge_river_name(attr: dict) -> str:
    """兼容不同版本边属性字段，提取河流名称。"""
    if not isinstance(attr, dict):
        return ""
    for key in ("rivername", "river_name", "src_name", "name"):
        value = attr.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def get_edge_length_km(attr: dict, attr_name: str = "length_km") -> float:
    """兼容 length_km / len_km / length / length_m，返回公里值。"""
    if not isinstance(attr, dict):
        return 0.0

    for key in (attr_name, "length_km", "len_km", "length"):
        if not key:
            continue
        raw = attr.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        return 0.0 if value < 0 else value

    raw_m = attr.get("length_m")
    try:
        value_m = float(raw_m)
        return max(value_m / 1000.0, 0.0)
    except (TypeError, ValueError):
        return 0.0


def _edge_objectid_key(attr: dict) -> str:
    """从图边属性中提取 objectid/id，统一成字符串。"""
    if not isinstance(attr, dict):
        return ""
    for key in ("objectid", "OBJECTID", "id", "ID", "gid"):
        raw = attr.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        try:
            value = float(text)
            if value.is_integer():
                return str(int(value))
        except (TypeError, ValueError):
            pass
        return text
    return ""


def _get_end_nodes_by_river_map(graph_path: str | os.PathLike | None = None) -> dict[str, set]:
    """构建 河名 -> 该河所有边终点节点 的索引。"""
    global _END_NODES_BY_RIVER, _END_NODES_INDEX_META

    path = str(graph_path or _default_graph_path())
    if not os.path.exists(path):
        raise FileNotFoundError(f"河网拓扑文件不存在：{path}")

    mtime = os.path.getmtime(path)
    meta = (path, mtime)
    with _GRAPH_LOCK:
        if _END_NODES_BY_RIVER is not None and _END_NODES_INDEX_META == meta:
            return _END_NODES_BY_RIVER

        graph = get_graph(path)
        by_river: dict[str, set] = defaultdict(set)
        for _u, v, _key, attr in iter_graph_edges(graph):
            river_name = get_edge_river_name(attr)
            if river_name:
                by_river[river_name].add(v)

        _END_NODES_BY_RIVER = dict(by_river)
        _END_NODES_INDEX_META = meta
        return _END_NODES_BY_RIVER


def _collect_downstream_impacts(
    source_rivers: Iterable[str],
    *,
    downstream_km: float,
    graph_path: str | os.PathLike | None = None,
    attr_name: str = "length_km",
) -> tuple[dict[str, dict], set[str]]:
    """从直接受影响河流出发，追踪 downstream_km 内的下游河流。

    Returns:
        downstream_map:
            {
                river_name: {
                    "river_name": str,
                    "min_distance_km": float,
                    "source_rivers": [str, ...],
                }
            }
        downstream_objectids:
            追踪过程中命中的图边 objectid/id，用于回查真实河流几何。
    """
    downstream_km = float(downstream_km)
    if downstream_km <= 0:
        return {}, set()

    graph = get_graph(graph_path)
    end_nodes_map = _get_end_nodes_by_river_map(graph_path)
    downstream_map: dict[str, dict] = {}
    downstream_objectids: set[str] = set()

    for source_river in sorted({str(x).strip() for x in source_rivers if str(x).strip()}):
        start_nodes = end_nodes_map.get(source_river, set())
        if not start_nodes:
            continue

        heap_counter = count()
        best_dist: dict[Any, float] = {node: 0.0 for node in start_nodes}
        heap: list[tuple[float, int, Any]] = [
            (0.0, next(heap_counter), node) for node in start_nodes
        ]
        heapq.heapify(heap)

        while heap:
            curr_dist, _seq, curr_node = heapq.heappop(heap)
            if curr_dist > best_dist.get(curr_node, math.inf):
                continue
            if curr_dist > downstream_km:
                continue

            for _u, next_node, key, attr in iter_out_edges(graph, curr_node):
                edge_len = get_edge_length_km(attr, attr_name=attr_name)
                edge_river = get_edge_river_name(attr)
                edge_id = _edge_objectid_key(attr)

                # 起始河流自身不作为“下游影响河流”重复输出；其他河流按当前节点距离计入。
                if edge_river and edge_river != source_river:
                    if curr_dist <= downstream_km:
                        if edge_id:
                            downstream_objectids.add(edge_id)

                        item = downstream_map.setdefault(
                            edge_river,
                            {
                                "river_name": edge_river,
                                "min_distance_km": math.inf,
                                "source_rivers": [],
                            },
                        )
                        if curr_dist < item["min_distance_km"]:
                            item["min_distance_km"] = curr_dist
                        if source_river not in item["source_rivers"]:
                            item["source_rivers"].append(source_river)

                # 继续向下游传播。源河流自身边不增加到“其他河流起点”的距离。
                next_dist = curr_dist if edge_river == source_river else curr_dist + edge_len
                if next_dist <= downstream_km and next_dist < best_dist.get(next_node, math.inf):
                    best_dist[next_node] = next_dist
                    heapq.heappush(heap, (next_dist, next(heap_counter), next_node))

    for item in downstream_map.values():
        item["min_distance_km"] = round(float(item["min_distance_km"]), 3)

    return downstream_map, downstream_objectids


def _quote_ident(identifier: str) -> str:
    """安全引用 SQL 标识符。仅允许普通 schema/table/column 名。"""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(identifier or "")):
        raise ValueError(f"非法 SQL 标识符：{identifier!r}")
    return f'"{identifier}"'


def _split_table_name(table_name: str, default_schema: str = "public") -> tuple[str, str]:
    value = str(table_name or "").strip()
    if "." in value:
        schema, table = value.split(".", 1)
        return schema.strip() or default_schema, table.strip()
    return default_schema, value or "haihe_river_directed_full_v4"


def _get_engine():
    """延迟导入数据库 engine，避免单元测试/离线环境 import 时就连接数据库。"""
    try:
        from utils.db import engine  # type: ignore
    except Exception:
        from .db import engine  # type: ignore
    return engine


def _get_table_columns(cur, schema: str, table: str) -> set[str]:
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


def _pick_first_existing(columns: set[str], candidates: Iterable[str]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _river_name_sql_expr(columns: set[str], alias: str | None = None) -> str:
    fields = [
        c for c in ("river_name", "rivername", "src_name", "name")
        if c in columns
    ]
    if not fields:
        raise ValueError("河流表中未找到河名字段，期望 river_name/rivername/src_name/name 之一")
    prefix = f"{_quote_ident(alias)}." if alias else ""
    parts = [f"NULLIF(TRIM({prefix}{_quote_ident(c)}::text), '')" for c in fields]
    return f"COALESCE({', '.join(parts)}, '未知')"


def _create_temp_station_table(cur, stations: list[dict]) -> None:
    cur.execute(
        """
        CREATE TEMP TABLE tmp_rain24h_impact_stations (
            station_id TEXT,
            station_name TEXT,
            province TEXT,
            city TEXT,
            cnty TEXT,
            town TEXT,
            lon DOUBLE PRECISION,
            lat DOUBLE PRECISION,
            rain_24h DOUBLE PRECISION,
            obs_count INTEGER,
            start_time TEXT,
            end_time TEXT,
            geom geometry(Point, 4326)
        ) ON COMMIT DROP
        """
    )

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

    from psycopg2.extras import execute_values

    execute_values(
        cur,
        """
        INSERT INTO tmp_rain24h_impact_stations (
            station_id, station_name, province, city, cnty, town,
            lon, lat, rain_24h, obs_count, start_time, end_time, geom
        )
        VALUES %s
        """,
        rows,
        template=(
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "ST_SetSRID(ST_MakePoint(%s,%s),4326))"
        ),
    )


def _query_direct_affected_river_segments(
    cur,
    *,
    schema: str,
    table: str,
    columns: set[str],
    buffer_km: float,
) -> list[dict]:
    geom_col = _pick_first_existing(columns, ("geom", "geometry"))
    if not geom_col:
        raise ValueError(f"{schema}.{table} 中未找到 geom/geometry 字段")

    id_col = _pick_first_existing(columns, ("id", "gid"))
    objectid_col = _pick_first_existing(columns, ("objectid", "OBJECTID", "id", "gid"))
    river_expr = _river_name_sql_expr(columns, alias="r")
    q_schema = _quote_ident(schema)
    q_table = _quote_ident(table)
    q_geom = _quote_ident(geom_col)

    id_expr = f"r.{_quote_ident(id_col)}::text" if id_col else "NULL::text"
    objectid_expr = f"r.{_quote_ident(objectid_col)}::text" if objectid_col else id_expr

    cur.execute(
        f"""
        SELECT
            {id_expr} AS id,
            {objectid_expr} AS objectid,
            {river_expr} AS river_name,
            ST_AsGeoJSON(r.{q_geom}) AS geom_json,
            ST_Length(r.{q_geom}::geography) / 1000.0 AS length_km,
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
                    'rain_24h', s.rain_24h
                )
            ) AS trigger_stations
        FROM {q_schema}.{q_table} r
        JOIN tmp_rain24h_impact_stations s
          ON ST_DWithin(r.{q_geom}::geography, s.geom::geography, %s)
        WHERE r.{q_geom} IS NOT NULL
        GROUP BY r.{q_geom}, {id_expr}, {objectid_expr}, {river_expr}
        """,
        (float(buffer_km) * 1000.0,),
    )
    return list(cur.fetchall())


def _query_river_geometries(
    cur,
    *,
    schema: str,
    table: str,
    columns: set[str],
    river_names: Iterable[str],
    objectids: Iterable[str],
) -> list[dict]:
    names = sorted({str(x).strip() for x in river_names if str(x).strip()})
    ids = sorted({str(x).strip() for x in objectids if str(x).strip()})
    if not names and not ids:
        return []

    geom_col = _pick_first_existing(columns, ("geom", "geometry"))
    if not geom_col:
        raise ValueError(f"{schema}.{table} 中未找到 geom/geometry 字段")

    id_col = _pick_first_existing(columns, ("id", "gid"))
    objectid_col = _pick_first_existing(columns, ("objectid", "OBJECTID", "id", "gid"))
    river_expr = _river_name_sql_expr(columns)
    q_schema = _quote_ident(schema)
    q_table = _quote_ident(table)
    q_geom = _quote_ident(geom_col)

    id_expr = f"{_quote_ident(id_col)}::text" if id_col else "NULL::text"
    objectid_expr = f"{_quote_ident(objectid_col)}::text" if objectid_col else id_expr

    where_parts = []
    params: dict[str, list[str]] = {}
    if names:
        where_parts.append(f"{river_expr} = ANY(%(river_names)s)")
        params["river_names"] = names
    if ids and objectid_col:
        where_parts.append(f"{_quote_ident(objectid_col)}::text = ANY(%(objectids)s)")
        params["objectids"] = ids
    if ids and id_col and id_col != objectid_col:
        where_parts.append(f"{_quote_ident(id_col)}::text = ANY(%(objectids)s)")
        params["objectids"] = ids

    where_sql = " OR ".join(f"({part})" for part in where_parts)
    cur.execute(
        f"""
        SELECT
            {id_expr} AS id,
            {objectid_expr} AS objectid,
            {river_expr} AS river_name,
            ST_AsGeoJSON({q_geom}) AS geom_json,
            ST_Length({q_geom}::geography) / 1000.0 AS length_km
        FROM {q_schema}.{q_table}
        WHERE {q_geom} IS NOT NULL
          AND ({where_sql})
        """,
        params,
    )
    return list(cur.fetchall())


def _jsonable(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if pd.isna(value):
        return None
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
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [float(lon), float(lat)],
                },
                "properties": props,
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _build_river_geojson(
    geometry_rows: list[dict],
    *,
    direct_rivers: dict[str, dict],
    direct_objectids: set[str],
    downstream_map: dict[str, dict],
) -> dict:
    features = []
    seen_feature_keys = set()

    for row in geometry_rows:
        geom_text = row.get("geom_json")
        if not geom_text:
            continue
        try:
            geometry = json.loads(geom_text) if isinstance(geom_text, str) else geom_text
        except Exception:
            continue

        river_name = str(row.get("river_name") or "未知").strip()
        objectid = str(row.get("objectid") or "").strip()
        feature_key = (objectid, river_name, json.dumps(geometry, sort_keys=True, ensure_ascii=False))
        if feature_key in seen_feature_keys:
            continue
        seen_feature_keys.add(feature_key)

        is_direct = river_name in direct_rivers or (objectid and objectid in direct_objectids)
        downstream_info = downstream_map.get(river_name, {})

        if is_direct:
            impact_type = "direct_buffer"
            source_rivers = [river_name]
            min_distance_km = 0.0
        else:
            impact_type = "downstream_50km"
            source_rivers = downstream_info.get("source_rivers", [])
            min_distance_km = downstream_info.get("min_distance_km")

        direct_info = direct_rivers.get(river_name, {})
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "id": row.get("id"),
                    "objectid": objectid or None,
                    "river_name": river_name,
                    "impact_type": impact_type,
                    "source_rivers": source_rivers,
                    "min_downstream_distance_km": min_distance_km,
                    "length_km": round(float(row.get("length_km") or 0.0), 3),
                    "trigger_station_count": direct_info.get("trigger_station_count", 0) if is_direct else 0,
                    "trigger_stations": direct_info.get("trigger_stations", []) if is_direct else [],
                },
            }
        )

    features.sort(
        key=lambda f: (
            0 if f["properties"]["impact_type"] == "direct_buffer" else 1,
            f["properties"].get("river_name") or "",
            f["properties"].get("objectid") or "",
        )
    )
    return {"type": "FeatureCollection", "features": features}


def build_rain24h_impact_river_geojson(
    csv_path: str | os.PathLike,
    *,
    rain_threshold_mm: float = 50.0,
    station_buffer_km: float = 30.0,
    downstream_km: float = 50.0,
    river_table: str = "haihe_river_directed_full_v4",
    schema: str = "public",
    graph_path: str | os.PathLike | None = None,
    top_station_limit: int = 100,
) -> dict:
    """生成 24 小时累计降水影响河流 GeoJSON。

    业务口径：
    1. CSV 中 PRE 为站点 5 分钟降水；
    2. 按 Station_Id_C 聚合为 24 小时累计降水 rain_24h；
    3. rain_24h >= rain_threshold_mm 的站点作为触发站；
    4. 查询触发站 30km 缓冲区内的真实河流；
    5. 从直接受影响河流出发，沿河网拓扑向下游追踪 downstream_km；
    6. 返回真实河流表中的 GeoJSON，供前端渲染。

    Args:
        csv_path: 5 分钟站点降水 CSV 路径。
        rain_threshold_mm: 暴雨阈值，默认 50mm。
        station_buffer_km: 站点影响缓冲半径，默认 30km。
        downstream_km: 下游追踪距离，默认 50km。
        river_table: 真实河流表名，默认 haihe_river_directed_full_v4。
        schema: 默认数据库 schema。
        graph_path: 河网拓扑 pickle 路径；默认自动查找 Service/river_directed_v4_asis.pkl。
        top_station_limit: 返回 rainfall_24h_top_stations 的最大站点数。

    Returns:
        dict，核心字段：
        - river_geojson: 影响河流 FeatureCollection；
        - station_geojson: 触发站点 FeatureCollection；
        - rainfall_24h_top_stations: 24 小时累计降水靠前站点；
        - direct_rivers / downstream_rivers / affected_rivers。
    """
    if rain_threshold_mm < 0:
        raise ValueError("rain_threshold_mm 不能为负数")
    if station_buffer_km <= 0:
        raise ValueError("station_buffer_km 必须大于 0")
    if downstream_km < 0:
        raise ValueError("downstream_km 不能为负数")

    station_24h_df = aggregate_5min_station_pre_to_24h(csv_path)
    total_station_count = int(len(station_24h_df))

    impact_df = station_24h_df[
        (station_24h_df["rain_24h"] >= float(rain_threshold_mm))
        & station_24h_df["lon"].notna()
        & station_24h_df["lat"].notna()
    ].copy()

    impact_stations = [_station_record(row) for _, row in impact_df.iterrows()]
    top_stations = [
        _station_record(row)
        for _, row in station_24h_df.head(max(int(top_station_limit), 0)).iterrows()
    ]

    start_time = station_24h_df["start_time"].min()
    end_time = station_24h_df["end_time"].max()

    base_result = {
        "status": "ok",
        "params": {
            "rain_threshold_mm": float(rain_threshold_mm),
            "station_buffer_km": float(station_buffer_km),
            "downstream_km": float(downstream_km),
            "river_table": river_table,
            "schema": schema,
            "graph_path": str(graph_path or _default_graph_path()),
        },
        "time_range": {
            "start_time": _jsonable(start_time),
            "end_time": _jsonable(end_time),
        },
        "station_summary": {
            "total_station_count": total_station_count,
            "impact_station_count": int(len(impact_stations)),
            "max_rain_24h": float(station_24h_df["rain_24h"].max() or 0.0) if total_station_count else 0.0,
        },
        "rainfall_24h_top_stations": top_stations,
        "impact_stations": impact_stations,
        "station_geojson": _make_station_geojson(impact_stations),
        "direct_rivers": [],
        "downstream_rivers": [],
        "affected_rivers": [],
        "river_geojson": {"type": "FeatureCollection", "features": []},
    }

    if not impact_stations:
        base_result["message"] = "没有站点达到设定降雨阈值，未生成影响河流。"
        return base_result

    table_schema, table_name = _split_table_name(river_table, default_schema=schema)
    engine = _get_engine()
    raw_conn = engine.raw_connection()

    try:
        from psycopg2.extras import RealDictCursor

        with raw_conn.cursor(cursor_factory=RealDictCursor) as cur:
            columns = _get_table_columns(cur, table_schema, table_name)
            if not columns:
                raise ValueError(f"未找到河流表：{table_schema}.{table_name}")

            _create_temp_station_table(cur, impact_stations)
            direct_rows = _query_direct_affected_river_segments(
                cur,
                schema=table_schema,
                table=table_name,
                columns=columns,
                buffer_km=station_buffer_km,
            )

            direct_rivers: dict[str, dict] = {}
            direct_objectids: set[str] = set()
            for row in direct_rows:
                river_name = str(row.get("river_name") or "").strip()
                objectid = str(row.get("objectid") or "").strip()
                if objectid:
                    direct_objectids.add(objectid)
                if not river_name:
                    continue
                item = direct_rivers.setdefault(
                    river_name,
                    {
                        "river_name": river_name,
                        "trigger_station_count": 0,
                        "trigger_stations": [],
                        "objectids": set(),
                    },
                )
                item["trigger_station_count"] += int(row.get("trigger_station_count") or 0)
                if objectid:
                    item["objectids"].add(objectid)
                trigger_stations = row.get("trigger_stations") or []
                if isinstance(trigger_stations, list):
                    seen_station_ids = {
                        str(s.get("station_id"))
                        for s in item["trigger_stations"]
                        if isinstance(s, dict)
                    }
                    for station in trigger_stations:
                        if not isinstance(station, dict):
                            continue
                        station_id = str(station.get("station_id"))
                        if station_id not in seen_station_ids:
                            item["trigger_stations"].append(station)
                            seen_station_ids.add(station_id)

            downstream_map, downstream_objectids = _collect_downstream_impacts(
                direct_rivers.keys(),
                downstream_km=downstream_km,
                graph_path=graph_path,
            )

            direct_names = set(direct_rivers.keys())
            downstream_names = set(downstream_map.keys())
            affected_names = direct_names | downstream_names
            affected_objectids = direct_objectids | downstream_objectids

            geometry_rows = _query_river_geometries(
                cur,
                schema=table_schema,
                table=table_name,
                columns=columns,
                river_names=affected_names,
                objectids=affected_objectids,
            )

            # json 序列化前把 set 转 list。
            for item in direct_rivers.values():
                item["objectids"] = sorted(item.get("objectids", set()))
                item["trigger_station_count"] = len(item.get("trigger_stations", []))

            river_geojson = _build_river_geojson(
                geometry_rows,
                direct_rivers=direct_rivers,
                direct_objectids=direct_objectids,
                downstream_map=downstream_map,
            )

            base_result.update(
                {
                    "direct_rivers": sorted(direct_rivers.values(), key=lambda x: x["river_name"]),
                    "downstream_rivers": sorted(downstream_map.values(), key=lambda x: x["river_name"]),
                    "affected_rivers": sorted(affected_names),
                    "river_geojson": river_geojson,
                    "message": (
                        f"已生成影响河流 GeoJSON：触发站 {len(impact_stations)} 个，"
                        f"直接影响河流 {len(direct_names)} 条，下游影响河流 {len(downstream_names)} 条，"
                        f"GeoJSON 要素 {len(river_geojson.get('features', []))} 个。"
                    ),
                }
            )
            return base_result
    finally:
        raw_conn.close()


# 更贴近“工具”调用语义的别名，便于 Controller 或智能体工具层直接引用。
get_rain24h_impact_river_geojson = build_rain24h_impact_river_geojson
