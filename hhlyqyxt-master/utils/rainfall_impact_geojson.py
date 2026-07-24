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
DEFAULT_FLOW_VELOCITY_MPS = 2.0  # 经验洪水波传播速度，≈7.2 km/h

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


_GRAPH_CACHE = None
_GRAPH_CACHE_PATH: str | None = None
_GRAPH_CACHE_MTIME: float | None = None
_GRAPH_LOCK = threading.RLock()


def aggregate_5min_station_pre_to_24h(csv_path: str | os.PathLike) -> pd.DataFrame:
    """把 5 分钟站点降水 CSV 聚合为站点 24h 累计雨量；空 CSV 返回空表。"""
    try:
        header = pd.read_csv(csv_path, nrows=0)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=[
            "station_id", "rain_24h", "lon", "lat",
            "obs_count", "valid_pre_count", "start_time", "end_time",
        ])
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
    max_segments: int = 0,
    flow_velocity_mps: float = DEFAULT_FLOW_VELOCITY_MPS,
    extra_summary: dict | None = None,
) -> dict:
    """根据暴雨站点生成影响河流专题图数据。"""
    _validate_params(rainfall_threshold_mm, station_buffer_km, downstream_km, flow_velocity_mps)
    schema, river_table = _resolve_table(pg_conf, schema, river_table)
    rainstorm_stations = _normalize_stations(stations, rainfall_threshold_mm)
    result = _empty_result(
        stations=rainstorm_stations,
        threshold=rainfall_threshold_mm,
        buffer_km=station_buffer_km,
        downstream_km=downstream_km,
        direct_match_km=direct_match_km,
        schema=schema,
        table=river_table,
        graph_path=graph_path,
        extra=extra_summary,
        flow_velocity_mps=flow_velocity_mps,
    )
    if not rainstorm_stations:
        result["message"] = f"未找到降雨量≥{rainfall_threshold_mm}mm 的站点。"
        return result

    # 先加载 pkl 图再打开数据库连接，避免图加载失败时泄漏连接。
    graph = get_graph(graph_path)
    conn, should_close = _open_connection(pg_conf, db_connection)
    try:
        from psycopg2.extras import RealDictCursor
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            _ensure_river_columns(cur, schema, river_table, geom_column, objectid_column, river_name_column)
            _create_station_temp(cur, rainstorm_stations)
            candidate_rows = _query_candidate_edge_rows(
                cur,
                schema,
                river_table,
                geom_column,
                objectid_column,
                river_name_column,
                station_buffer_km,
            )
            direct_edges, start_nodes, downstream_start_stats = _classify_graph_edges(
                candidate_rows,
                graph,
                rainstorm_stations,
                station_buffer_km,
                direct_match_km,
            )
            direct_keys = set(direct_edges)
            downstream_edges = _collect_downstream_edges(
                {node: 0.0 for node in start_nodes}, graph, direct_keys, downstream_km
            )
            geometry_rows = _fetch_missing_edge_rows(
                cur,
                schema,
                river_table,
                geom_column,
                objectid_column,
                river_name_column,
                candidate_rows,
                downstream_edges,
            )
    finally:
        if should_close:
            conn.close()

    river_geojson = _build_river_geojson(direct_edges, downstream_edges, geometry_rows,
                                         graph_path=graph_path, flow_velocity_mps=flow_velocity_mps)
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
        "river_propagation": _build_river_propagation(
            direct_edges, downstream_edges, flow_velocity_mps,
            luan_mapping=_load_luan_name_mapping(graph_path),
            candidate_rows=geometry_rows,
        ),
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


def _validate_params(threshold: float, buffer_km: float, downstream_km: float, flow_velocity_mps: float) -> None:
    if threshold < 0:
        raise ValueError("rainfall_threshold_mm 不能为负数")
    if buffer_km <= 0:
        raise ValueError("station_buffer_km 必须大于 0")
    if downstream_km < 0:
        raise ValueError("downstream_km 不能为负数")
    velocity = float(flow_velocity_mps)
    if not math.isfinite(velocity) or velocity <= 0:
        raise ValueError("flow_velocity_mps 必须为大于 0 的有限数值")


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
    schema: str,
    table: str,
    graph_path,
    extra: dict | None,
    flow_velocity_mps: float = DEFAULT_FLOW_VELOCITY_MPS,
) -> dict:
    result = {
        "status": "ok",
        "params": {
            "rainfall_threshold_mm": float(threshold),
            "station_buffer_km": float(buffer_km),
            "downstream_km": float(downstream_km),
            "direct_match_km": float(direct_match_km),
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
        "river_propagation": {"flow_velocity_mps": float(flow_velocity_mps), "rivers": []},
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
    required = {
        geom_col, objectid_col, river_name_col, "is_luan",
        # 查询中硬编码引用的列
        "id", "src_name", "len_km", "from_x", "from_y", "to_x", "to_y",
    }
    missing = required - columns
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


def _edge_lookup_key(objectid: Any, from_x: Any, from_y: Any, to_x: Any, to_y: Any) -> tuple[str, float, float, float, float]:
    """pkl 边与 full_v6 行的匹配键；坐标统一保留 6 位小数（约 0.1m）吸收精度差异。"""

    def _num(value: Any) -> float:
        return round(float(value or 0.0), 6)

    return (str(objectid or ""), _num(from_x), _num(from_y), _num(to_x), _num(to_y))


def _build_edge_lookup(rows: list[dict]) -> dict[tuple[str, float, float, float, float], dict]:
    """Build a lookup from (objectid, from_x, from_y, to_x, to_y) to a full_v6 row.

    每行同时按 (objectid, from, to) 和 (objectid, to, from) 两个方向建索引，
    吸收 pkl 流向与 full_v6 数字化方向不一致导致的匹配失败。
    """
    lookup: dict[tuple[str, float, float, float, float], dict] = {}
    for row in rows or []:
        objectid = str(row.get("objectid") or "")
        if not objectid:
            continue
        key = _edge_lookup_key(objectid, row.get("from_x"), row.get("from_y"), row.get("to_x"), row.get("to_y"))
        lookup.setdefault(key, row)
        reverse = _edge_lookup_key(objectid, row.get("to_x"), row.get("to_y"), row.get("from_x"), row.get("from_y"))
        lookup.setdefault(reverse, row)
    return lookup


def _build_spatial_lookup(rows: list[dict]) -> dict[str, list[dict]]:
    """按 objectid 索引候选行，供精确键失配时的空间兜底匹配使用。"""
    by_oid: dict[str, list[dict]] = {}
    for row in rows or []:
        oid = str(row.get("objectid") or "")
        if oid:
            by_oid.setdefault(oid, []).append(row)
    return by_oid


def _point_to_lines_km(lon: float, lat: float, lines: list[list[list[float]]]) -> float:
    if not lines:
        return math.inf
    return min(_point_to_line_km(lon, lat, line) for line in lines)


def _match_edge_spatially(
    objectid: str,
    from_xy: tuple[float, float],
    to_xy: tuple[float, float],
    spatial_lookup: dict[str, list[dict]],
    tolerance_km: float = 0.1,
) -> dict | None:
    """精确端点键失配时的空间兜底：在同 objectid 候选行中找几何同时经过 pkl 两端点的行。

    tolerance_km 默认 100m，容忍 shapefile 简化/坐标漂移；要求两端点都落在几何附近，
    避免误匹配同 objectid 的平行河道。
    """
    candidates = spatial_lookup.get(objectid)
    if not candidates:
        return None
    best_row = None
    best_dist = math.inf
    for row in candidates:
        geom = _geometry_from_row(row)
        lines = _geometry_lines(geom or {})
        if not lines:
            continue
        d_from = _point_to_lines_km(float(from_xy[0]), float(from_xy[1]), lines)
        d_to = _point_to_lines_km(float(to_xy[0]), float(to_xy[1]), lines)
        max_d = max(d_from, d_to)
        if max_d <= tolerance_km and max_d < best_dist:
            best_dist = max_d
            best_row = row
    return best_row


def _resolve_edge_row(
    objectid,
    from_xy,
    to_xy,
    lookup: dict[tuple[str, float, float, float, float], dict],
    spatial_lookup: dict[str, list[dict]],
) -> dict | None:
    """按端点键解析 full_v6 行：正向键 -> 反向键 -> 空间兜底。供 GeoJSON 与传播时间命名共用。"""
    if objectid is None or from_xy is None or to_xy is None:
        return None
    row = lookup.get(_edge_lookup_key(objectid, from_xy[0], from_xy[1], to_xy[0], to_xy[1]))
    if row is None:
        row = lookup.get(_edge_lookup_key(objectid, to_xy[0], to_xy[1], from_xy[0], from_xy[1]))
    if row is None:
        row = _match_edge_spatially(objectid, from_xy, to_xy, spatial_lookup)
    return row


def _query_candidate_edge_rows(
    cur,
    schema: str,
    table: str,
    geom_col: str,
    objectid_col: str,
    river_name_col: str,
    buffer_km: float,
) -> list[dict]:
    """查询暴雨站点缓冲区内的 full_v6 行，每行对应一条 pkl 边，不做 Dump/聚合。"""
    cur.execute(f"""
        SELECT
            r.{_qi(objectid_col)}::text AS objectid,
            COALESCE(NULLIF(TRIM(r.{_qi(river_name_col)}::text), ''), '未知') AS river_name,
            COALESCE(NULLIF(TRIM(r.src_name::text), ''), '未知') AS src_name,
            COALESCE(r.is_luan, false) AS is_luan,
            r.from_x, r.from_y, r.to_x, r.to_y,
            r.len_km,
            ST_AsGeoJSON(r.{_qi(geom_col)}) AS geom_json,
            MIN(ST_Distance(r.{_qi(geom_col)}::geography, s.geom::geography) / 1000.0) AS min_station_distance_km,
            COUNT(DISTINCT s.station_id) AS trigger_station_count,
            jsonb_agg(DISTINCT jsonb_build_object(
                'station_id', s.station_id,
                'station_name', s.station_name,
                'lon', s.lon,
                'lat', s.lat,
                'rain_24h', s.rain_24h
            )) AS trigger_stations
        FROM {_qi(schema)}.{_qi(table)} r
        JOIN tmp_rain24h_impact_stations s
          ON ST_DWithin(r.{_qi(geom_col)}::geography, s.geom::geography, %(buffer_m)s)
        WHERE r.{_qi(geom_col)} IS NOT NULL AND NOT ST_IsEmpty(r.{_qi(geom_col)})
        GROUP BY r.id, r.{_qi(objectid_col)}, r.{_qi(river_name_col)}, r.src_name, r.is_luan,
                 r.from_x, r.from_y, r.to_x, r.to_y, r.len_km, r.{_qi(geom_col)}
        ORDER BY min_station_distance_km, r.{_qi(objectid_col)}
    """, {"buffer_m": float(buffer_km) * 1000.0})
    return list(cur.fetchall())


def _classify_graph_edges(
    candidate_rows: list[dict],
    graph,
    stations: list[dict],
    station_buffer_km: float,
    direct_match_km: float,
) -> tuple[dict[str, dict], set[Any], dict]:
    """
    把 full_v6 候选行匹配到 pkl 边并分类。站点缓冲区（station_buffer_km）内的所有边
    都作为 direct_buffer 输出，其中距站点 ≤ direct_match_km 的标记 is_direct_graph_edge=true。
    返回 (direct_edges, downstream_start_nodes, stats)。
    """
    lookup = _build_edge_lookup(candidate_rows)
    spatial_lookup = _build_spatial_lookup(candidate_rows)
    used_row_ids: set[int] = set()
    direct_edges: dict[str, dict] = {}
    direct_match_count = 0
    buffer_only_count = 0
    start_nodes: set[Any] = set()

    for u, v, key, attr, p1, p2 in _iter_edges_with_points(graph):
        edge_key = _edge_key(u, v, key, attr)
        objectid = _edge_objectid_key(attr)
        row_key = _edge_lookup_key(objectid, p1[0], p1[1], p2[0], p2[1])
        row = lookup.get(row_key)
        if row is None:
            # 精确端点键失配时，按 objectid + 几何空间邻近兜底匹配
            row = _match_edge_spatially(objectid, p1, p2, spatial_lookup)
            if row is None:
                continue
        used_row_ids.add(id(row))

        # 优先使用 SQL 计算的真实几何最近距离；缺失时退化为 pkl 端点弦距。
        min_dist = _safe_float(row.get("min_station_distance_km"))
        if min_dist is None:
            min_dist = min(
                _point_to_segment_km(s["lon"], s["lat"], p1, p2)
                for s in stations
                if s.get("lon") is not None and s.get("lat") is not None
            )
        if min_dist > station_buffer_km:
            continue

        is_direct = min_dist <= direct_match_km
        edge_info = {
            "edge_key": edge_key,
            "objectid": objectid,
            "river_name": get_edge_river_name(attr),
            "from_x": p1[0],
            "from_y": p1[1],
            "to_x": p2[0],
            "to_y": p2[1],
            "length_km": get_edge_length_km(attr, from_xy=(p1[0], p1[1]), to_xy=(p2[0], p2[1])),
            "is_direct_graph_edge": is_direct,
            "is_luan": bool(attr.get("is_luan")),
            "min_station_distance_km": round(float(min_dist), 3),
            "trigger_stations": row.get("trigger_stations") or [],
            "trigger_station_count": int(row.get("trigger_station_count") or 0),
            "row": row,
        }
        direct_edges[edge_key] = edge_info
        if is_direct:
            direct_match_count += 1
        else:
            buffer_only_count += 1
        start_nodes.add(v)

    unmatched_rows = sum(1 for r in candidate_rows if id(r) not in used_row_ids)
    if unmatched_rows:
        logger.warning("full_v6 候选行 %d 条未能匹配到 pkl 边（已跳过）", unmatched_rows)

    stats = _downstream_start_stats(
        direct_part_matched_edge_count=direct_match_count,
        station_buffer_fallback_edge_count=buffer_only_count,
        direct_match_km=direct_match_km,
        station_buffer_km=station_buffer_km,
    )
    return direct_edges, start_nodes, stats


def _fetch_missing_edge_rows(
    cur,
    schema: str,
    table: str,
    geom_col: str,
    objectid_col: str,
    river_name_col: str,
    candidate_rows: list[dict],
    downstream_edges: list[dict],
) -> list[dict]:
    """为不在站点缓冲区候选行中的下游边补查 full_v6 几何行，返回合并后的行列表。

    匹配键方向无关；精确端点键失配时按 objectid + 几何空间邻近兜底。
    """
    lookup = _build_edge_lookup(candidate_rows)

    def _has_exact_match(oid, fx, fy, tx, ty):
        return (
            _edge_lookup_key(oid, fx, fy, tx, ty) in lookup
            or _edge_lookup_key(oid, tx, ty, fx, fy) in lookup
        )

    missing_edges = [
        e for e in downstream_edges
        if not _has_exact_match(e["objectid"], e["from_x"], e["from_y"], e["to_x"], e["to_y"])
    ]
    if not missing_edges:
        return candidate_rows

    objectids = sorted({e["objectid"] for e in missing_edges if e["objectid"]})
    if not objectids:
        return candidate_rows
    cur.execute(f"""
        SELECT
            r.{_qi(objectid_col)}::text AS objectid,
            COALESCE(NULLIF(TRIM(r.{_qi(river_name_col)}::text), ''), '未知') AS river_name,
            COALESCE(NULLIF(TRIM(r.src_name::text), ''), '未知') AS src_name,
            COALESCE(r.is_luan, false) AS is_luan,
            r.from_x, r.from_y, r.to_x, r.to_y,
            r.len_km,
            ST_AsGeoJSON(r.{_qi(geom_col)}) AS geom_json
        FROM {_qi(schema)}.{_qi(table)} r
        WHERE r.{_qi(objectid_col)}::text = ANY(%(objectids)s)
          AND r.{_qi(geom_col)} IS NOT NULL AND NOT ST_IsEmpty(r.{_qi(geom_col)})
    """, {"objectids": objectids})
    queried = list(cur.fetchall())

    # 先用精确端点键匹配
    matched_rows: list[dict] = []
    still_missing: list[dict] = list(missing_edges)
    for row in queried:
        oid = row.get("objectid")
        fwd = _edge_lookup_key(oid, row.get("from_x"), row.get("from_y"), row.get("to_x"), row.get("to_y"))
        rev = _edge_lookup_key(oid, row.get("to_x"), row.get("to_y"), row.get("from_x"), row.get("from_y"))
        for e in still_missing[:]:
            e_fwd = _edge_lookup_key(e["objectid"], e["from_x"], e["from_y"], e["to_x"], e["to_y"])
            e_rev = _edge_lookup_key(e["objectid"], e["to_x"], e["to_y"], e["from_x"], e["from_y"])
            if fwd in (e_fwd, e_rev) or rev in (e_fwd, e_rev):
                matched_rows.append(row)
                still_missing.remove(e)
                break

    # 精确键仍未匹配的边，用空间兜底（同 objectid + 几何经过两端点）
    if still_missing:
        spatial_lookup = _build_spatial_lookup(queried)
        for e in still_missing[:]:
            row = _match_edge_spatially(
                e["objectid"],
                (e["from_x"], e["from_y"]),
                (e["to_x"], e["to_y"]),
                spatial_lookup,
            )
            if row is not None:
                matched_rows.append(row)
                still_missing.remove(e)

    if still_missing:
        logger.warning(
            "full_v6 中未找到 %d 条下游边的几何（精确键+空间兜底均失败），使用直线兜底",
            len(still_missing),
        )
    return candidate_rows + matched_rows


def _iter_edges_with_points(graph):
    for u, v, key, attr in iter_graph_edges(graph):
        p1, p2 = _edge_points(u, v)
        if p1 is None or p2 is None:
            continue
        yield u, v, key, attr, p1, p2


def _collect_downstream_edges(starts: dict[Any, float], graph, direct_keys: set[str], downstream_km: float) -> list[dict]:
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
    length_km = get_edge_length_km(attr, from_xy=_parse_node_xy(u), to_xy=_parse_node_xy(v))
    end_km = start_km + length_km
    if not objectid or not river_name or not (length_km > 0):
        return end_km

    keep_km = limit_km - start_km
    if keep_km <= 0:
        return end_km
    keep_km = min(keep_km, length_km)

    edge_key = _edge_key(u, v, key, attr)
    if edge_key in direct_keys:
        # 直接边已作为 direct_buffer 输出，遍历继续但不再重复记录为下游。
        return end_km
    old = edges.get(edge_key)
    if old and old["min_distance_km"] <= start_km:
        return end_km

    from_x, from_y = _parse_node_xy(u)
    to_x, to_y = _parse_node_xy(v)
    edges[edge_key] = {
        "edge_key": edge_key,
        "objectid": objectid,
        "river_name": river_name,
        "min_distance_km": _round(start_km, 3) or 0.0,
        "end_distance_km": _round(start_km + keep_km, 3) or 0.0,
        "keep_km": _round(keep_km, 3) or 0.0,
        "clip_fraction": _round(keep_km / length_km, 8) or 0.0,
        "is_direct_graph_edge": edge_key in direct_keys,
        "is_luan": bool(attr.get("is_luan")),
        "from_x": from_x,
        "from_y": from_y,
        "to_x": to_x,
        "to_y": to_y,
    }
    return end_km


def _build_river_geojson(
    direct_edges: dict[str, dict],
    downstream_edges: list[dict],
    candidate_rows: list[dict],
    graph_path=None,
    flow_velocity_mps: float = DEFAULT_FLOW_VELOCITY_MPS,
) -> dict:
    """生成河流 GeoJSON；直接边与下游边按 edge_key 天然互斥，每条 pkl 边至多一个 feature。"""
    lookup = _build_edge_lookup(candidate_rows)
    spatial_lookup = _build_spatial_lookup(candidate_rows)
    luan_mapping = _load_luan_name_mapping(graph_path)

    direct_features = _resolve_edge_features(
        list(direct_edges.values()), lookup, spatial_lookup, "direct_buffer", luan_mapping,
        flow_velocity_mps=flow_velocity_mps,
    )
    downstream_features = _resolve_edge_features(
        downstream_edges, lookup, spatial_lookup, "downstream_50km", luan_mapping,
        flow_velocity_mps=flow_velocity_mps,
    )

    features = direct_features + downstream_features
    features.sort(
        key=lambda f: (
            0 if f["properties"]["impact_type"] == "direct_buffer" else 1,
            f["properties"].get("river_name") or "",
            f["properties"].get("min_downstream_distance_km", 0.0),
        )
    )
    return {"type": "FeatureCollection", "features": features}


def _resolve_edge_features(
    edges: list[dict],
    lookup: dict[tuple[str, float, float, float, float], dict],
    spatial_lookup: dict[str, list[dict]],
    impact_type: str,
    luan_mapping: dict[str, str],
    flow_velocity_mps: float = DEFAULT_FLOW_VELOCITY_MPS,
) -> list[dict]:
    """把 pkl 边列表解析为 GeoJSON features，几何/名称来自 full_v6 lookup。每条 feature 含 per-edge 传播时间。"""
    velocity_kmh = float(flow_velocity_mps) * 3.6
    features = []
    for edge in edges:
        objectid = edge["objectid"]
        # 直接边在 _classify_graph_edges 构建时已带 full_v6 "row"，
        # 优先用已存储的 row 而非重新 lookup，与 _build_river_propagation._resolve_row
        # 同口径，避免两次 lookup 在滦河命名等路径产出不同行→不同河名→per-edge 与 summary 分组错位。
        row = edge.get("row")
        if row is None:
            row = _resolve_edge_row(
                objectid,
                (edge["from_x"], edge["from_y"]),
                (edge["to_x"], edge["to_y"]),
                lookup,
                spatial_lookup,
            )
        geometry = _geometry_from_row(row) if row else None
        geometry = _unwrap_geometry(geometry)
        if not _geometry_lines(geometry or {}):
            geometry = {
                "type": "LineString",
                "coordinates": [[edge["from_x"], edge["from_y"]], [edge["to_x"], edge["to_y"]]],
            }
            from_db = False
            logger.warning("full_v6 缺少边几何，使用直线兜底: %s", edge["edge_key"])
        else:
            from_db = True

        if impact_type == "downstream_50km":
            geometry = _clip_geometry_to_keep_km(
                geometry,
                float(edge.get("keep_km") or 0.0),
                (edge.get("from_x"), edge.get("from_y")),
            )

        river_name = _pick_river_name(row, edge, luan_mapping)
        is_direct = impact_type == "direct_buffer"
        # 传播距离
        prop_distance = (
            float(edge.get("end_distance_km") or 0)
            if not is_direct
            else _feature_length_km(row, edge, impact_type)
        )
        if velocity_kmh > 0 and math.isfinite(prop_distance):
            prop_time = round(prop_distance / velocity_kmh, 1)
        else:
            prop_distance = 0.0
            prop_time = 0.0

        feature = {
            "type": "Feature",
            "properties": {
                # 基础信息
                "objectid": objectid,
                "id": objectid,
                "river_name": river_name,
                "is_luan": edge.get("is_luan", False),
                "impact_type": impact_type,
                "length_km": _feature_length_km(row, edge, impact_type),
                "edge_key": edge["edge_key"],
                "flow_direction": "database_geometry_order",
                "direction_source": f"full_{RIVER_TABLE_VERSION}_original_geometry",
                "geometry_source": (
                    f"full_{RIVER_TABLE_VERSION}_downstream_clipped" if not is_direct and from_db
                    else f"full_{RIVER_TABLE_VERSION}_direct_uncut" if is_direct and from_db
                    else "pkl_edge_straight_fallback"
                ),
                # 直接河段属性（下游段填默认值，保证属性表统一无 NULL 列）
                "min_station_distance_km": (edge.get("min_station_distance_km") or 0.0) if is_direct else 0.0,
                "trigger_station_count": (edge.get("trigger_station_count") or 0) if is_direct else 0,
                "trigger_stations": (edge.get("trigger_stations") or []) if is_direct else [],
                # 下游河段属性（直接段填默认值）
                "min_downstream_distance_km": (edge.get("min_distance_km") or 0.0) if not is_direct else 0.0,
                "end_downstream_distance_km": (edge.get("end_distance_km") or 0.0) if not is_direct else 0.0,
                "keep_km": (edge.get("keep_km") or 0.0) if not is_direct else 0.0,
                "clip_fraction": (edge.get("clip_fraction") or 1.0) if not is_direct else 0.0,
                "is_direct_graph_edge": edge.get("is_direct_graph_edge") if not is_direct else True,
                # 传播时间（所有河段统一）
                "propagation_distance_km": round(prop_distance, 3),
                "propagation_time_hours": prop_time,
            },
            "geometry": geometry,
        }
        features.append(feature)
    return features


def _feature_length_km(row: dict | None, edge: dict, impact_type: str) -> float:
    """下游裁剪段报告 keep_km；直接段优先用 full_v6 len_km，缺失时退化为 pkl 边长。"""
    if impact_type == "downstream_50km":
        return round(float(edge.get("keep_km") or 0.0), 3)
    if row and row.get("len_km") is not None:
        return round(float(row["len_km"]), 3)
    return round(float(edge.get("length_km") or 0.0), 3)


def _pick_river_name(row: dict | None, edge: dict, luan_mapping: dict[str, str]) -> str:
    """名称优先级：full_v6.src_name → full_v6.river_name → pkl 名称 → 滦河映射 → 未知。

    滦河静态映射只在名称为单字缩写或所有来源都失败时启用，不覆盖合法全名。
    """
    candidates = []
    if row:
        candidates.append(row.get("src_name"))
        candidates.append(row.get("river_name"))
    candidates.append(edge.get("river_name"))
    is_luan = bool(edge.get("is_luan"))
    objectid = str(edge.get("objectid") or "")
    for name in candidates:
        text = str(name or "").strip()
        if not text or text == "未知":
            continue
        if is_luan and len(text) == 1:
            mapped = luan_mapping.get(objectid)
            return mapped if mapped else _normalize_river_name(text)
        return text
    if is_luan:
        mapped = luan_mapping.get(objectid)
        if mapped:
            return mapped
    return "未知"


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


def _clip_geometry_to_keep_km(geometry: dict, keep_km: float, from_xy: tuple[Any, Any]) -> dict:
    """把几何从上游端裁剪到 keep_km 长度（纯 Python，无 Shapely 依赖）。

    - 多 part 几何取最长 part；
    - 按 pkl from 节点判定数字化方向，反向时先翻转再裁剪；
    - keep_km 覆盖全长时原样返回。
    """
    lines = _geometry_lines(geometry)
    if not lines:
        return geometry
    line = max(lines, key=_line_length_km)
    total_km = _line_length_km(line)
    if total_km <= 0 or keep_km >= total_km:
        return {"type": "LineString", "coordinates": line}

    fx, fy = _safe_float(from_xy[0]), _safe_float(from_xy[1])
    if fx is not None and fy is not None:
        head = _haversine_km(float(line[0][0]), float(line[0][1]), fx, fy)
        tail = _haversine_km(float(line[-1][0]), float(line[-1][1]), fx, fy)
        if tail < head:
            line = list(reversed(line))

    clipped: list[list[float]] = [list(line[0])]
    acc = 0.0
    for i in range(len(line) - 1):
        seg = _haversine_km(float(line[i][0]), float(line[i][1]), float(line[i + 1][0]), float(line[i + 1][1]))
        if acc + seg >= keep_km:
            remain = keep_km - acc
            t = (remain / seg) if seg > 0 else 0.0
            clipped.append([
                float(line[i][0]) + (float(line[i + 1][0]) - float(line[i][0])) * t,
                float(line[i][1]) + (float(line[i + 1][1]) - float(line[i][1])) * t,
            ])
            break
        clipped.append(list(line[i + 1]))
        acc += seg
    return {"type": "LineString", "coordinates": clipped}


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


def _geometry_from_row(row: dict) -> dict | None:
    raw = row.get("geom_json")
    return json.loads(raw) if isinstance(raw, str) else raw


def _unwrap_geometry(geometry: dict | None) -> dict | None:
    """单 part 的 MultiLineString 解包为 LineString，方便前端渲染。"""
    if not isinstance(geometry, dict):
        return geometry
    if geometry.get("type") == "MultiLineString":
        lines = geometry.get("coordinates") or []
        if len(lines) == 1:
            return {"type": "LineString", "coordinates": lines[0]}
    return geometry


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
        result = round(float(value), digits)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


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


def _propagation_readable(hours: float) -> str:
    """传播时间的可读表述：<1 小时显示分钟，否则显示小时。"""
    if hours < 1:
        return f"约{max(int(round(hours * 60)), 1)}分钟"
    return f"约{hours:.1f}小时"


def _build_river_propagation(
    direct_edges: dict[str, dict],
    downstream_edges: list[dict],
    flow_velocity_mps: float,
    luan_mapping: dict[str, str] | None = None,
    candidate_rows: list[dict] | None = None,
) -> dict:
    """按河流聚合暴雨影响传播时间估算。

    口径：下游边取 Dijkstra 累计 end_distance_km 最大值（暴雨入口视为 0km）；
    仅有直接边、无下游边的河流取直接边中最长 length_km（影响就地发生）。
    河名经 _pick_river_name 解析，与 GeoJSON/affected_rivers 的命名口径一致：
    下游边由 _save_downstream_edge 构造、不带 full_v6 "row"，需经 full_v6 lookup
    解析 row（与 _resolve_edge_features 同款），避免滦河单字等命名偏差。
    """
    mapping = luan_mapping or {}
    lookup = _build_edge_lookup(candidate_rows or [])
    spatial_lookup = _build_spatial_lookup(candidate_rows or [])
    velocity_kmh = float(flow_velocity_mps) * 3.6
    direct_len: dict[str, float] = {}
    downstream_dist: dict[str, float] = {}

    def _resolve_row(edge: dict):
        # 直接边已带 full_v6 "row"；下游边按端点键查 full_v6，与 GeoJSON 同口径。
        # 端点键缺失（如单元测试构造的极简下游边）时回退 None，由 _pick_river_name 走 edge 名。
        if edge.get("row") is not None:
            return edge["row"]
        return _resolve_edge_row(
            edge.get("objectid"),
            (edge.get("from_x"), edge.get("from_y")),
            (edge.get("to_x"), edge.get("to_y")),
            lookup,
            spatial_lookup,
        )

    for edges, field, acc in (
        ((direct_edges or {}).values(), "length_km", direct_len),
        (downstream_edges or [], "end_distance_km", downstream_dist),
    ):
        for edge in edges:
            name = _pick_river_name(_resolve_row(edge), edge, mapping)
            value = _safe_float(edge.get(field))
            if name == "未知" or value is None or value <= 0:
                continue
            acc[name] = max(acc.get(name, 0.0), value)

    rivers = []
    for name in sorted(set(direct_len) | set(downstream_dist)):
        has_downstream = name in downstream_dist
        distance_km = downstream_dist[name] if has_downstream else direct_len[name]
        raw_hours = distance_km / velocity_kmh
        rivers.append({
            "river_name": name,
            "propagation_distance_km": round(distance_km, 3),
            "propagation_time_hours": round(raw_hours, 1),
            "arrival_estimate_readable": _propagation_readable(raw_hours),
            "has_downstream": has_downstream,
        })
    rivers.sort(key=lambda r: r["propagation_time_hours"], reverse=True)
    return {"flow_velocity_mps": float(flow_velocity_mps), "rivers": rivers}


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


def get_edge_length_km(
    attr: dict,
    attr_name: str = "length_km",
    *,
    from_xy: tuple[Any, Any] | None = None,
    to_xy: tuple[Any, Any] | None = None,
) -> float:
    """获取 pkl 边长度（km）。属性缺失或 NaN 时回退到端点 haversine 距离。

    滦河系边的 len_km 在数据中可能为 NaN，必须兜底，否则会污染下游 Dijkstra 距离累积。
    """
    if isinstance(attr, dict):
        for key in (attr_name, "length_km", "len_km", "length"):
            value = attr.get(key)
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number) and number > 0:
                return number
        try:
            length_m = float(attr.get("len_m"))
            if math.isfinite(length_m) and length_m > 0:
                return max(length_m / 1000.0, 0.0)
        except (TypeError, ValueError):
            pass
    if from_xy is not None and to_xy is not None:
        fx, fy = _safe_float(from_xy[0]), _safe_float(from_xy[1])
        tx, ty = _safe_float(to_xy[0]), _safe_float(to_xy[1])
        if fx is not None and fy is not None and tx is not None and ty is not None:
            return max(_haversine_km(fx, fy, tx, ty), 0.0)
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
