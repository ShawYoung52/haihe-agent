"""降雨数据查询工具集合"""
import os
import threading
import configparser
import logging
import json
import uuid
import re
import math
from collections import defaultdict
from typing import Iterator, Any
import pickle
from datetime import datetime, timedelta
import time

import networkx as nx
from fastmcp import FastMCP

import pandas as pd
from analyzers.RainfallAnalyzer import RainfallAnalyzer
from exception.CustomException import BusinessException
from models import RainfallCityData
import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor

from haihe_mcp_tools import register_haihe_tools
from constants import DEFAULT_BASIN_CODES, DIRECTED_GRAPH_FILENAME, RIVER_TABLE_FULL
from emergency_scenario_client import emergency_http_base_url, fetch_scenario_get

config = configparser.ConfigParser()
config.read('config.ini', encoding='utf-8-sig')
# 创建全局实例
analyzer = RainfallAnalyzer(config)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

_GRAPH_CACHE = None
_GRAPH_CACHE_PATH = None
_GRAPH_CACHE_MTIME = None
_GRAPH_LOCK = threading.RLock()

# 河名 -> 该河在 DAG 中所有边的终点节点（用于下游分析，避免每次全图扫边）
_END_NODES_BY_RIVER: dict[str, set] | None = None
_END_NODES_INDEX_META: tuple | None = None
_RIVER_LOCATE_CACHE: dict[tuple, dict] = {}

# ===============================
# PostgreSQL 连接池 + MUSIC 客户端复用 + 降雨分析结果缓存
# ===============================
_PG_POOL: psycopg2.pool.ThreadedConnectionPool | None = None
_PG_POOL_LOCK = threading.Lock()
_PG_POOL_KEY: tuple | None = None

_MUSIC_CLIENT = None
_MUSIC_CLIENT_LOCK = threading.Lock()

_RAINFALL_CACHE: dict[tuple, dict] = {}
_RAINFALL_CACHE_TTL_SEC = int(os.getenv("RAINFALL_CACHE_TTL_SEC", "120"))
_RAINFALL_CACHE_MAX_SIZE = int(os.getenv("RAINFALL_CACHE_MAX_SIZE", "50"))
_RAINFALL_CACHE_LOCK = threading.Lock()


def _pg_pool_key(pg_conf: dict) -> tuple:
    return (
        pg_conf.get("host", "127.0.0.1"),
        int(pg_conf.get("port", 5432)),
        pg_conf.get("dbname", ""),
        pg_conf.get("user", ""),
        pg_conf.get("password", ""),
        pg_conf.get("sslmode", "prefer"),
    )


def _get_pg_pool(pg_conf: dict) -> psycopg2.pool.ThreadedConnectionPool:
    """按需创建并复用 PostgreSQL 连接池。"""
    global _PG_POOL, _PG_POOL_KEY
    current_key = _pg_pool_key(pg_conf)
    if _PG_POOL is not None and _PG_POOL_KEY == current_key:
        return _PG_POOL
    with _PG_POOL_LOCK:
        if _PG_POOL is not None and _PG_POOL_KEY == current_key:
            return _PG_POOL
        if _PG_POOL is not None:
            try:
                _PG_POOL.closeall()
            except Exception:
                pass
        _PG_POOL = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=int(os.getenv("PG_POOL_MAXCONN", "10")),
            host=pg_conf.get("host", "127.0.0.1"),
            port=int(pg_conf.get("port", 5432)),
            dbname=pg_conf.get("dbname"),
            user=pg_conf.get("user"),
            password=pg_conf.get("password"),
            sslmode=pg_conf.get("sslmode", "prefer"),
            connect_timeout=int(pg_conf.get("connect_timeout", "5")),
        )
        _PG_POOL_KEY = current_key
        logger.info("PostgreSQL 连接池已创建")
        return _PG_POOL


def _get_pg_conn(pg_conf: dict):
    """从连接池获取连接，失败时回退到单次连接。"""
    try:
        return _get_pg_pool(pg_conf).getconn()
    except Exception as e:
        logger.warning(f"连接池获取失败，回退到单次连接: {e}")
        return psycopg2.connect(
            host=pg_conf.get("host", "127.0.0.1"),
            port=int(pg_conf.get("port", 5432)),
            dbname=pg_conf.get("dbname"),
            user=pg_conf.get("user"),
            password=pg_conf.get("password"),
            sslmode=pg_conf.get("sslmode", "prefer"),
            connect_timeout=int(pg_conf.get("connect_timeout", "5")),
        )


def _put_pg_conn(pg_conf: dict, conn):
    """将连接归还连接池；若连接已坏则关闭。"""
    try:
        _get_pg_pool(pg_conf).putconn(conn)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


def _get_music_client():
    """复用同一个 MUSIC 客户端（内部复用 requests.Session）。"""
    global _MUSIC_CLIENT
    if _MUSIC_CLIENT is not None:
        return _MUSIC_CLIENT
    with _MUSIC_CLIENT_LOCK:
        if _MUSIC_CLIENT is not None:
            return _MUSIC_CLIENT
        from haihe_mcp_tools import MusicClient, MusicConfig as MusicApiConfig
        _MUSIC_CLIENT = MusicClient(MusicApiConfig())
        logger.info("MUSIC 客户端已创建")
        return _MUSIC_CLIENT


def _rainfall_cache_key(time_str: str, custom_timerange: str) -> tuple:
    return (time_str.strip(), custom_timerange.strip())


def _get_cached_rainfall(time_str: str, custom_timerange: str):
    """读取短时间缓存的降雨分析结果。"""
    key = _rainfall_cache_key(time_str, custom_timerange)
    with _RAINFALL_CACHE_LOCK:
        entry = _RAINFALL_CACHE.get(key)
        if entry is None:
            return None
        if time.time() - entry["ts"] > _RAINFALL_CACHE_TTL_SEC:
            _RAINFALL_CACHE.pop(key, None)
            return None
        return entry["data"]


def _set_cached_rainfall(time_str: str, custom_timerange: str, data: dict):
    """写入降雨分析结果缓存，超限时清理最旧条目。"""
    key = _rainfall_cache_key(time_str, custom_timerange)
    with _RAINFALL_CACHE_LOCK:
        _RAINFALL_CACHE[key] = {"ts": time.time(), "data": data}
        if len(_RAINFALL_CACHE) > _RAINFALL_CACHE_MAX_SIZE:
            oldest = min(_RAINFALL_CACHE.items(), key=lambda kv: kv[1]["ts"])[0]
            _RAINFALL_CACHE.pop(oldest, None)
_RIVER_LOCATE_CACHE_TTL_SEC = 300


def _invalidate_river_end_nodes_index():
    global _END_NODES_BY_RIVER, _END_NODES_INDEX_META
    with _GRAPH_LOCK:
        _END_NODES_BY_RIVER = None
        _END_NODES_INDEX_META = None


def _make_river_locate_cache_key(river_names: list[str], buffer_km: float) -> tuple:
    names = tuple(sorted({str(x).strip() for x in river_names if str(x).strip()}))
    return names, round(float(buffer_km), 3)


def _get_cached_river_locations(river_names: list[str], buffer_km: float) -> dict | None:
    key = _make_river_locate_cache_key(river_names, buffer_km)
    now = time.time()
    item = _RIVER_LOCATE_CACHE.get(key)
    if not item:
        return None
    if now - float(item.get("ts", 0.0)) > _RIVER_LOCATE_CACHE_TTL_SEC:
        _RIVER_LOCATE_CACHE.pop(key, None)
        return None
    return item.get("value")


def _set_cached_river_locations(river_names: list[str], buffer_km: float, value: dict) -> None:
    key = _make_river_locate_cache_key(river_names, buffer_km)
    _RIVER_LOCATE_CACHE[key] = {"ts": time.time(), "value": value}


def _get_end_nodes_by_river_map() -> dict[str, set]:
    """按 pickle 文件 mtime 失效；与 get_graph 数据源一致。"""
    global _END_NODES_BY_RIVER, _END_NODES_INDEX_META
    graph_path = config.get("paths", "graph")
    mtime = os.path.getmtime(graph_path)
    meta = (graph_path, mtime)
    with _GRAPH_LOCK:
        if _END_NODES_BY_RIVER is not None and _END_NODES_INDEX_META == meta:
            return _END_NODES_BY_RIVER
        G = get_graph(False)
        by_river: dict[str, set] = defaultdict(set)
        for _u, v, _key, attr in iter_graph_edges(G):
            rn = get_edge_river_name(attr)
            if rn:
                by_river[rn].add(v)
        _END_NODES_BY_RIVER = dict(by_river)
        _END_NODES_INDEX_META = meta
        logger.info("河网河名索引已构建: rivers=%s", len(_END_NODES_BY_RIVER))
        return _END_NODES_BY_RIVER

def get_graph(force_reload: bool = False):
    """
    懒加载并缓存河网图。
    - 首次调用时从 pickle 加载
    - 后续直接复用内存对象
    - 如果底层文件 mtime 变化，则自动重新加载
    - force_reload=True 时强制刷新
    """
    global _GRAPH_CACHE, _GRAPH_CACHE_PATH, _GRAPH_CACHE_MTIME

    graph_path = config.get("paths", "graph")
    # 优先使用 v6 修复版（如果存在）
    v6_path = os.path.join(os.path.dirname(graph_path), DIRECTED_GRAPH_FILENAME)
    if os.path.isfile(v6_path):
        graph_path = v6_path
    current_mtime = os.path.getmtime(graph_path)

    with _GRAPH_LOCK:
        need_reload = (
            force_reload
            or _GRAPH_CACHE is None
            or _GRAPH_CACHE_PATH != graph_path
            or _GRAPH_CACHE_MTIME != current_mtime
        )

        if need_reload:
            with open(graph_path, "rb") as f:
                graph = pickle.load(f)

            _GRAPH_CACHE = graph
            _GRAPH_CACHE_PATH = graph_path
            _GRAPH_CACHE_MTIME = current_mtime

            logger.info(f"河网图已加载到缓存: path={graph_path}")

        return _GRAPH_CACHE

def clear_graph_cache():
    """手动清空图缓存，便于调试或热更新。"""
    global _GRAPH_CACHE, _GRAPH_CACHE_PATH, _GRAPH_CACHE_MTIME
    global _END_NODES_BY_RIVER, _END_NODES_INDEX_META
    with _GRAPH_LOCK:
        _GRAPH_CACHE = None
        _GRAPH_CACHE_PATH = None
        _GRAPH_CACHE_MTIME = None
        _END_NODES_BY_RIVER = None
        _END_NODES_INDEX_META = None
        _RIVER_LOCATE_CACHE.clear()
        logger.info("河网图缓存已清空")

def iter_graph_edges(G) -> Iterator[tuple]:
    """
    统一遍历图的边：
    - DiGraph: yield (u, v, None, attr)
    - MultiDiGraph: yield (u, v, key, attr)
    """
    if G.is_multigraph():
        for u, v, key, attr in G.edges(keys=True, data=True):
            yield u, v, key, attr
    else:
        for u, v, attr in G.edges(data=True):
            yield u, v, None, attr


def iter_out_edges(G, node) -> Iterator[tuple]:
    """
    统一遍历某节点的出边：
    - DiGraph: yield (u, v, None, attr)
    - MultiDiGraph: yield (u, v, key, attr)
    """
    if G.is_multigraph():
        for u, v, key, attr in G.out_edges(node, keys=True, data=True):
            yield u, v, key, attr
    else:
        for u, v, attr in G.out_edges(node, data=True):
            yield u, v, None, attr


def make_edge_id(u, v, key=None):
    """统一边唯一标识。"""
    return (u, v, key) if key is not None else (u, v)


def get_edge_river_name(attr: dict) -> str:
    """兼容不同版本边属性字段，提取河流名称。"""
    if not isinstance(attr, dict):
        return ""
    for key in ("rivername", "river_name", "src_name", "name"):
        val = attr.get(key)
        if val is None:
            continue
        s = str(val).strip()
        if s:
            return s
    return ""


def get_edge_length_km(attr: dict, attr_name: str = "length_km") -> float:
    """兼容 length_km / len_km / length，返回公里值。"""
    if not isinstance(attr, dict):
        return 0.0
    seen: set[str] = set()
    for key in (attr_name, "length_km", "len_km", "length"):
        if not key or key in seen:
            continue
        seen.add(key)
        raw = attr.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        return 0.0 if value < 0 else value
    return 0.0


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Haversine 公式计算两点间距离（km）。"""
    R = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def _clip_polyline_to_length(coords: list[list[float]], max_length_km: float) -> list[list[float]]:
    """将折线按累计长度截断到 max_length_km（保留起点到截断点）。"""
    if len(coords) < 2:
        return coords
    total = 0.0
    for i in range(len(coords) - 1):
        seg_len = _haversine_km(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
        if total + seg_len >= max_length_km:
            remaining = max_length_km - total
            ratio = remaining / seg_len if seg_len > 0 else 0.0
            lon = coords[i][0] + ratio * (coords[i + 1][0] - coords[i][0])
            lat = coords[i][1] + ratio * (coords[i + 1][1] - coords[i][1])
            return coords[:i + 1] + [[lon, lat]]
        total += seg_len
    return coords


def _safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _edge_objectid_key(attr: dict) -> str:
    if not isinstance(attr, dict):
        return ""
    raw = attr.get("objectid")
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    # 统一 objectid 主键格式，兼容图里 1.0 / 库里 1 的情况
    try:
        fv = float(s)
        if fv.is_integer():
            return str(int(fv))
    except (TypeError, ValueError):
        pass
    return s


def _geom_to_paths(geom_obj: dict) -> list[list[list[float]]]:
    """
    将 GeoJSON 几何转为路径列表（每条路径是 [[x,y], ...]）。
    仅处理 LineString / MultiLineString。
    """
    if not isinstance(geom_obj, dict):
        return []
    gtype = str(geom_obj.get("type") or "")
    coords = geom_obj.get("coordinates")
    if gtype == "LineString" and isinstance(coords, list):
        out = []
        line = []
        for pt in coords:
            if not isinstance(pt, (list, tuple)) or len(pt) < 2:
                continue
            x = _safe_float(pt[0])
            y = _safe_float(pt[1])
            if x is None or y is None:
                continue
            line.append([x, y])
        if len(line) >= 2:
            out.append(line)
        return out
    if gtype == "MultiLineString" and isinstance(coords, list):
        out = []
        for line_coords in coords:
            if not isinstance(line_coords, list):
                continue
            line = []
            for pt in line_coords:
                if not isinstance(pt, (list, tuple)) or len(pt) < 2:
                    continue
                x = _safe_float(pt[0])
                y = _safe_float(pt[1])
                if x is None or y is None:
                    continue
                line.append([x, y])
            if len(line) >= 2:
                out.append(line)
        return out
    return []


def _get_clipped_buffer_segments(
    stations: list[dict], buffer_m: float = 30000
) -> tuple[dict[str, list[list[list[float]]]], dict[str, float]]:
    """
    查询数据库，返回暴雨站点缓冲区内被截断的河段。

    Returns:
        (objectid -> [paths], objectid -> clipped_length_km)
    """
    if not stations or "postgres" not in config:
        return {}, {}

    lons = [float(s["lon"]) for s in stations if s.get("lon") is not None]
    lats = [float(s["lat"]) for s in stations if s.get("lat") is not None]
    if not lons:
        return {}, {}

    pg_conf = config["postgres"]
    schema = pg_conf.get("schema", "public")
    river_table_full = (
        pg_conf.get("river_table_full", "haihe_river_directed_full_v2").strip()
        or "haihe_river_directed_full_v2"
    )
    clipped_paths: dict[str, list[list[list[float]]]] = {}
    clipped_lengths: dict[str, float] = {}

    try:
        with psycopg2.connect(
            host=pg_conf["host"],
            port=pg_conf["port"],
            dbname=pg_conf["dbname"],
            user=pg_conf["user"],
            password=pg_conf["password"],
            sslmode=pg_conf.get("sslmode", "prefer"),
            connect_timeout=int(pg_conf.get("connect_timeout", "5")),
        ) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(f"""
                    WITH pts AS (
                        SELECT row_number() OVER () AS idx,
                               ST_SetSRID(ST_MakePoint(lon, lat), 4326) AS geom
                        FROM unnest(%s::float[], %s::float[]) AS t(lon, lat)
                    ),
                    buf AS (
                        SELECT ST_Union(ST_Buffer(geom::geography, %s)::geometry) AS geom
                        FROM pts
                    )
                    SELECT r.objectid::text AS objectid,
                           ST_AsGeoJSON(ST_Intersection(r.geom, b.geom)) AS clipped_geojson,
                           ST_Length(ST_Intersection(r.geom, b.geom)::geography) / 1000.0 AS clipped_length_km,
                           ST_Length(r.geom::geography) / 1000.0 AS total_length_km
                    FROM {schema}.{river_table_full} r, buf b
                    WHERE ST_Intersects(r.geom, b.geom)
                      AND r.geom IS NOT NULL
                """, (lons, lats, buffer_m))
                for row in cur.fetchall():
                    oid = _edge_objectid_key({"objectid": row["objectid"]})
                    if not oid:
                        continue
                    geojson = row["clipped_geojson"]
                    try:
                        geom = json.loads(geojson) if isinstance(geojson, str) else geojson
                        paths = _geom_to_paths(geom)
                        if paths:
                            clipped_paths[oid] = paths
                            clipped_lengths[oid] = float(row.get("clipped_length_km") or 0.0)
                    except Exception:
                        continue
    except Exception as e:
        logger.warning(f"[缓冲区截断] 数据库查询失败: {e}")

    return clipped_paths, clipped_lengths


def _fetch_edge_paths_by_objectid(objectid_keys: set[str]) -> dict[str, list[dict]]:
    """
    从 river_table_full 读取河段几何，并按 objectid 返回路径列表。
    """
    if not objectid_keys or "postgres" not in config:
        return {}

    pg_conf = config["postgres"]
    schema = pg_conf.get("schema", "public")
    river_table_full = (
        pg_conf.get("river_table_full", "haihe_river_directed_full_v2").strip()
        or "haihe_river_directed_full_v2"
    )
    result: dict[str, list[dict]] = {}

    try:
        with psycopg2.connect(
            host=pg_conf["host"],
            port=pg_conf["port"],
            dbname=pg_conf["dbname"],
            user=pg_conf["user"],
            password=pg_conf["password"],
            sslmode=pg_conf.get("sslmode", "prefer"),
            connect_timeout=int(pg_conf.get("connect_timeout", "5")),
        ) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                sql = f"""
                    SELECT
                        objectid::text AS objectid_key,
                        from_x,
                        from_y,
                        to_x,
                        to_y,
                        ST_AsGeoJSON(geom) AS geom_json
                    FROM {schema}.{river_table_full}
                    WHERE objectid::text = ANY(%(ids)s)
                """
                cur.execute(sql, {"ids": list(objectid_keys)})
                for row in cur.fetchall():
                    key = str(row.get("objectid_key") or "").strip()
                    if not key:
                        continue
                    geom_json = row.get("geom_json")
                    if not geom_json:
                        continue
                    try:
                        geom_obj = json.loads(geom_json)
                    except Exception:
                        continue
                    paths = _geom_to_paths(geom_obj)
                    if paths:
                        fx = _safe_float(row.get("from_x"))
                        fy = _safe_float(row.get("from_y"))
                        tx = _safe_float(row.get("to_x"))
                        ty = _safe_float(row.get("to_y"))
                        result.setdefault(key, []).append({
                            "from_x": fx,
                            "from_y": fy,
                            "to_x": tx,
                            "to_y": ty,
                            "paths": paths,
                        })
    except Exception as e:
        logger.warning("批量读取河道几何失败，回退直线绘制: err=%s", e)
        return {}

    return result


def _path_matches_edge_endpoints(
    path: list[list[float]],
    from_x: float,
    from_y: float,
    to_x: float,
    to_y: float,
    tol: float = 1e-4,
) -> bool:
    """
    校验几何首尾点是否与边端点一致（允许反向），避免 objectid 错配导致串线。
    """
    if not isinstance(path, list) or len(path) < 2:
        return False
    try:
        sx, sy = float(path[0][0]), float(path[0][1])
        ex, ey = float(path[-1][0]), float(path[-1][1])
    except Exception:
        return False

    def _near(a: float, b: float) -> bool:
        return abs(a - b) <= tol

    forward_ok = _near(sx, from_x) and _near(sy, from_y) and _near(ex, to_x) and _near(ey, to_y)
    reverse_ok = _near(sx, to_x) and _near(sy, to_y) and _near(ex, from_x) and _near(ey, from_y)
    return forward_ok or reverse_ok


def _candidate_matches_edge_endpoints(
    candidate: dict,
    from_x: float,
    from_y: float,
    to_x: float,
    to_y: float,
    tol: float = 1e-4,
) -> bool:
    cfx = _safe_float(candidate.get("from_x"))
    cfy = _safe_float(candidate.get("from_y"))
    ctx = _safe_float(candidate.get("to_x"))
    cty = _safe_float(candidate.get("to_y"))
    if cfx is None or cfy is None or ctx is None or cty is None:
        return False

    def _near(a: float, b: float) -> bool:
        return abs(a - b) <= tol

    forward_ok = _near(cfx, from_x) and _near(cfy, from_y) and _near(ctx, to_x) and _near(cty, to_y)
    reverse_ok = _near(cfx, to_x) and _near(cfy, to_y) and _near(ctx, from_x) and _near(cty, from_y)
    return forward_ok or reverse_ok


_PARTITION_LAYER_LABELS: dict[str, str] = {
    "haihe_246_zone": "海河246分区",
    "haihe_zone_11": "海河11分区",
    "haihe_zone_32": "海河32分区",
    "haihe_zone_77": "海河77分区",
    "haihe_zone_9": "海河9分区",
}


def partitions_grouped_for_report(partitions: list | None) -> list[dict]:
    """
    将平面 partitions 列表转为按图层分组结构，便于模型按「第一版」样式输出
    （246 分区列代码；11/32/77/9 列分区名与 zone_code）。
    """
    from collections import defaultdict

    if not partitions:
        return []
    by_table: dict[str, list] = defaultdict(list)
    for p in partitions:
        if not isinstance(p, dict):
            continue
        tbl = (p.get("table") or "").strip() or "其它"
        by_table[tbl].append(p)

    preferred = [
        "haihe_246_zone",
        "haihe_zone_11",
        "haihe_zone_32",
        "haihe_zone_77",
        "haihe_zone_9",
    ]
    ordered = [k for k in preferred if k in by_table]
    ordered += sorted(k for k in by_table if k not in preferred)

    out: list[dict] = []
    for tbl in ordered:
        rows = by_table[tbl]
        title = _PARTITION_LAYER_LABELS.get(tbl, tbl)
        if tbl == "haihe_246_zone":
            codes: list[str] = []
            seen_c: set[str] = set()
            for r in rows:
                c = r.get("zone_code")
                if c is None:
                    continue
                s = str(c).strip()
                if s and s not in seen_c:
                    seen_c.add(s)
                    codes.append(s)
            out.append({
                "layer_key": tbl,
                "layer_title": title,
                "kind": "code_list",
                "zone_codes": codes,
            })
        else:
            entries: list[dict] = []
            seen_e: set[tuple] = set()
            for r in rows:
                zn = (r.get("zone_name") or "").strip()
                zc = r.get("zone_code")
                zs = str(zc).strip() if zc is not None else ""
                sig = (zn, zs)
                if sig in seen_e:
                    continue
                seen_e.add(sig)
                entries.append({"zone_name": zn or None, "zone_code": zs or None})
            out.append({
                "layer_key": tbl,
                "layer_title": title,
                "kind": "name_and_code",
                "entries": entries,
            })
    return out


def self_location_brief_report(self_location: dict) -> dict:
    names: list[str] = []
    for a in self_location.get("admin_regions") or []:
        if isinstance(a, dict) and a.get("name"):
            n = str(a["name"]).strip()
            if n:
                names.append(n)
    return {
        "river_name": self_location.get("river_name"),
        "admin_units": names,
        "partitions_by_layer": partitions_grouped_for_report(self_location.get("partitions")),
    }


def parse_node_xy(node, field_name: str = "node") -> tuple[float, float]:
    """
    解析坐标，兼容以下格式：
    - 'lon,lat' 字符串
    - (lon, lat) / [lon, lat] 元组或列表
    出错时抛 ValueError / TypeError，让上层决定记录还是跳过。
    """
    if isinstance(node, (tuple, list)):
        if len(node) != 2:
            raise ValueError(f"{field_name} 坐标格式不正确，应为长度为 2 的序列: {node!r}")
        try:
            x = float(node[0])
            y = float(node[1])
        except (TypeError, ValueError) as e:
            raise ValueError(f"{field_name} 序列坐标无法转成 float: {node!r}") from e
        return x, y

    if not isinstance(node, str):
        raise TypeError(f"{field_name} 既不是字符串也不是序列坐标: {node!r}")

    if "," not in node:
        raise ValueError(f"{field_name} 不包含逗号分隔坐标: {node!r}")

    parts = node.split(",")
    if len(parts) != 2:
        raise ValueError(f"{field_name} 坐标格式不正确，应为 'lon,lat': {node!r}")

    try:
        x = float(parts[0].strip())
        y = float(parts[1].strip())
    except ValueError as e:
        raise ValueError(f"{field_name} 坐标无法转成 float: {node!r}") from e

    return x, y


# =========================
# 河流影响时间（基于经验流速）
# =========================
RIVER_FLOW_SPEEDS_KMH = {
    "min": 4.67,  # 最小流速（更保守，耗时更长）
    "avg": 5.84,  # 平均流速（常用）
    "max": 7.01,  # 最大流速（更乐观，耗时更短）
}


def _format_duration_from_hours(hours: float) -> dict:
    if hours <= 0:
        return {"hours": 0.0, "days": 0.0, "human": "0 小时"}
    days = hours / 24.0
    if hours < 48:
        return {"hours": round(hours, 2), "days": round(days, 2), "human": f"{round(hours, 2)} 小时"}
    return {"hours": round(hours, 2), "days": round(days, 2), "human": f"{round(days, 2)} 天（约 {round(hours, 2)} 小时）"}


def _impact_time_descriptions(distance_km: float) -> dict:
    """
    给定沿程影响距离（km），输出三种流速下的时间与中文描述。
    """
    d = max(0.0, float(distance_km or 0.0))

    def calc(speed_kmh: float) -> dict:
        if speed_kmh <= 0:
            return {"speed_kmh": speed_kmh, "duration": _format_duration_from_hours(0.0)}
        return {"speed_kmh": speed_kmh, "duration": _format_duration_from_hours(d / speed_kmh)}

    min_case = calc(RIVER_FLOW_SPEEDS_KMH["min"])
    avg_case = calc(RIVER_FLOW_SPEEDS_KMH["avg"])
    max_case = calc(RIVER_FLOW_SPEEDS_KMH["max"])

    if d <= 1e-12:
        return {
            "distance_km": 0.0,
            "scenarios": {"min": min_case, "avg": avg_case, "max": max_case},
            "descriptions": {
                "min": f"距离为 0 km（直接汇入/分流），按最小流速 {RIVER_FLOW_SPEEDS_KMH['min']} km/h 影响时间为 0。",
                "avg": f"距离为 0 km（直接汇入/分流），按平均流速 {RIVER_FLOW_SPEEDS_KMH['avg']} km/h 影响时间为 0。",
                "max": f"距离为 0 km（直接汇入/分流），按最大流速 {RIVER_FLOW_SPEEDS_KMH['max']} km/h 影响时间为 0。",
            },
        }

    return {
        "distance_km": round(d, 3),
        "scenarios": {"min": min_case, "avg": avg_case, "max": max_case},
        "descriptions": {
            "min": (
                f"按最小流速 {RIVER_FLOW_SPEEDS_KMH['min']} km/h（更保守、耗时更长）估算，"
                f"沿程 {round(d, 3)} km 的影响时间约为 {min_case['duration']['human']}。"
            ),
            "avg": (
                f"按平均流速 {RIVER_FLOW_SPEEDS_KMH['avg']} km/h（常用估算）估算，"
                f"沿程 {round(d, 3)} km 的影响时间约为 {avg_case['duration']['human']}。"
            ),
            "max": (
                f"按最大流速 {RIVER_FLOW_SPEEDS_KMH['max']} km/h（更乐观、耗时更短）估算，"
                f"沿程 {round(d, 3)} km 的影响时间约为 {max_case['duration']['human']}。"
            ),
        },
    }


def _get_downstream_impacts_structured_core(
    river: str,
    attr_name: str = "length_km",
) -> list[dict]:
    """
    返回结构化的下游影响结果（源河到下游各河起点的最短沿程距离）。
    """
    import heapq

    G = get_graph()
    river_end_nodes = set(_get_end_nodes_by_river_map().get(river, ()))
    if not river_end_nodes:
        return []

    best_dist: dict = {node: 0.0 for node in river_end_nodes}
    heap: list[tuple[float, object]] = [(0.0, node) for node in river_end_nodes]
    heapq.heapify(heap)
    impact_distances: dict[str, float] = {}

    while heap:
        current_dist, curr_node = heapq.heappop(heap)
        if current_dist > best_dist.get(curr_node, float("inf")):
            continue

        for _u, next_node, _key, attr in iter_out_edges(G, curr_node):
            r_name = get_edge_river_name(attr)
            edge_len = get_edge_length_km(attr, attr_name=attr_name)

            if r_name and r_name != river:
                old = impact_distances.get(r_name, float("inf"))
                if current_dist < old:
                    impact_distances[r_name] = current_dist

            next_dist = current_dist if r_name == river else current_dist + edge_len
            if next_dist < best_dist.get(next_node, float("inf")):
                best_dist[next_node] = next_dist
                heapq.heappush(heap, (next_dist, next_node))

    result = []
    for to_river in sorted(impact_distances.keys()):
        result.append(
            {
                "river_name": to_river,
                "impact_distance_km": round(float(impact_distances[to_river]), 3),
            }
        )
    return result


def estimate_river_impact_time_core(
    river_name: str,
    target_downstream_river: str | None = None,
    max_rivers: int = 20,
    max_distance_km: float | None = None,
) -> dict:
    """
    估算“河流影响时间”（基于经验流速 4.67/5.84/7.01 km/h）。

    Args:
        max_distance_km: 若指定，只返回沿程距离不超过该值的下游河流。
    """
    if not river_name:
        raise BusinessException("river_name 不能为空")
    if max_rivers <= 0:
        raise BusinessException("max_rivers 必须大于 0")

    impacts = _get_downstream_impacts_structured_core(river_name)
    if not impacts:
        return {
            "source_river": river_name,
            "flow_speeds_kmh": dict(RIVER_FLOW_SPEEDS_KMH),
            "downstream_count": 0,
            "downstream": [],
            "note": f"暂未在河网中找到「{river_name}」的下游受影响河流信息。",
        }

    impacts_sorted = sorted(impacts, key=lambda x: float(x.get("impact_distance_km", 0.0) or 0.0))
    if max_distance_km is not None:
        max_distance_km = float(max_distance_km)
        impacts_sorted = [
            x for x in impacts_sorted
            if float(x.get("impact_distance_km", 0.0) or 0.0) <= max_distance_km
        ]
    if target_downstream_river:
        target = target_downstream_river.strip()
        match = next((x for x in impacts_sorted if str(x.get("river_name", "")).strip() == target), None)
        if not match:
            raise BusinessException(f"未在「{river_name}」的下游影响列表中找到「{target_downstream_river}」")
        items = [match]
    else:
        items = impacts_sorted[:max_rivers]

    downstream_results: list[dict] = []
    for item in items:
        to_river = item.get("river_name")
        dist_km = float(item.get("impact_distance_km", 0.0) or 0.0)
        time_pack = _impact_time_descriptions(dist_km)
        downstream_results.append(
            {
                "downstream_river": to_river,
                "impact_distance_km": round(max(0.0, dist_km), 3),
                "time_estimates": time_pack["scenarios"],
                "descriptions": time_pack["descriptions"],
            }
        )

    return {
        "source_river": river_name,
        "target_downstream_river": target_downstream_river,
        "flow_speeds_kmh": dict(RIVER_FLOW_SPEEDS_KMH),
        "downstream_count": len(downstream_results),
        "downstream": downstream_results,
    }


# 降雨等级定义（模块级别，避免在工具函数内重复定义）
RAIN_LEVELS = [
    ("特大暴雨", 250.0, float("inf")),
    ("大暴雨", 100.0, 250.0),
    ("暴雨", 50.0, 100.0),
    ("大雨", 25.0, 50.0),
    ("中雨", 10.0, 25.0),
    ("小雨", 0.1, 10.0),
]


def _rain_label(rain: float) -> str | None:
    """根据降雨量返回等级名称"""
    for name, lo, hi in RAIN_LEVELS:
        if lo <= rain < hi:
            return name
    return None


def _sanitize(val: Any) -> str:
    """清洗数据中的 HTML 标签和异常字符"""
    if val is None:
        return ""
    s = str(val)
    s = s.replace("<br>", "").replace("<br/>", "").replace("</br>", "")
    return s.strip()


def _aggregate_areal_rainfall_from_stations(
    time_range: str,
    zone_type: str,
    pg_conf: dict,
) -> list[dict] | None:
    """
    当天擎面雨量资料无数据时，用站点降雨数据 + PostGIS 分区表聚合出各子流域面雨量。
    通过 getSurfEleInBasinByTime 逐小时取 PRE_1h 并本地累加，规避 statSurfPreInBasin 参数兼容问题。
    返回与 query_basin_areal_rainfall 一致的 list[dict]，无数据时返回 None。
    """
    zone_tables = {
        "11": "haihe_zone_11",
        "77": "haihe_zone_77",
        "246": "haihe_246_zone",
        "32": "haihe_zone_32",
        "9": "haihe_zone_9",
    }
    zone_table = zone_tables.get(zone_type)
    if not zone_table:
        return None

    m = re.match(r'^\[(\d{14}),(\d{14})\]$', time_range)
    if not m:
        return None
    time_start, time_end = m.group(1), m.group(2)

    try:
        start_dt = datetime.strptime(time_start, "%Y%m%d%H%M%S")
        end_dt = datetime.strptime(time_end, "%Y%m%d%H%M%S")
    except Exception:
        return None

    if end_dt <= start_dt:
        return None

    # 生成整点时次列表
    times_list = []
    cur = start_dt.replace(minute=0, second=0)
    while cur <= end_dt:
        times_list.append(cur.strftime("%Y%m%d%H%M%S"))
        cur += timedelta(hours=1)

    if not times_list:
        return None

    try:
        client = _get_music_client()
        # 一次调用支持多个时次，逗号分隔
        times_str = ",".join(times_list)
        records = client.get_surf_ele_in_basin_by_time(
            basin_codes=DEFAULT_BASIN_CODES,
            times=times_str,
            elements="Station_Id_C,Lat,Lon,City,Station_Name,Cnty,Province,PRE_1h",
        )
        if not records:
            return None
    except Exception as e:
        logger.warning(f"站点降雨聚合面雨量失败（获取站点数据）: {e}")
        return None

    # 按站点累加 PRE_1h
    station_map: dict[str, dict] = {}
    for r in records:
        sid = str(r.get("Station_Id_C", "")).strip()
        if not sid:
            continue
        try:
            rain = float(r.get("PRE_1h", 0))
            if rain < 0 or rain > 9999:
                continue
        except Exception:
            continue
        if sid not in station_map:
            station_map[sid] = {
                "lon": float(r.get("Lon", 0)),
                "lat": float(r.get("Lat", 0)),
                "rainfall": 0.0,
            }
        station_map[sid]["rainfall"] += rain

    stations = [
        {"lon": s["lon"], "lat": s["lat"], "rainfall": s["rainfall"]}
        for s in station_map.values()
        if s["lon"] and s["lat"]
    ]

    if not stations:
        return None

    try:
        ct = pg_conf.get("connect_timeout", "5")
        timeout = int(ct) if str(ct).strip().isdigit() else 5
        with psycopg2.connect(
            host=pg_conf["host"], port=pg_conf["port"],
            dbname=pg_conf["dbname"], user=pg_conf["user"],
            password=pg_conf["password"],
            sslmode=pg_conf.get("sslmode", "prefer"),
            connect_timeout=timeout,
        ) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                lons = [s["lon"] for s in stations]
                lats = [s["lat"] for s in stations]
                cur.execute(f"""
                    WITH pts AS (
                        SELECT row_number() OVER () AS idx,
                               ST_SetSRID(ST_MakePoint(lon, lat), 4326) AS geom
                        FROM unnest(%s::float[], %s::float[]) AS t(lon, lat)
                    )
                    SELECT p.idx, z.zone_code, z.zone_name
                    FROM pts p
                    LEFT JOIN {zone_table} z
                      ON ST_Within(p.geom, z.geom)
                """, (lons, lats))
                zone_rows = {r["idx"]: r for r in cur.fetchall()}

        from collections import defaultdict
        area_groups = defaultdict(list)
        for i, s in enumerate(stations, start=1):
            row = zone_rows.get(i)
            if not row or not row.get("zone_code"):
                continue
            area_groups[row["zone_code"]].append(s["rainfall"])

        if not area_groups:
            return None

        result = []
        zone_name_map = {}
        for i, s in enumerate(stations, start=1):
            row = zone_rows.get(i)
            if row and row.get("zone_code") and row.get("zone_name"):
                zone_name_map[row["zone_code"]] = row["zone_name"]

        for zone_code, values in area_groups.items():
            if not values:
                continue
            zone_name = zone_name_map.get(zone_code) or f"分区{zone_code}"
            result.append({
                "zone_id": zone_code,
                "zone_name": zone_name,
                "avg_rainfall_mm": round(sum(values) / len(values), 2),
                "max_rainfall_mm": round(max(values), 2),
                "record_count": len(values),
            })

        result.sort(key=lambda x: x["avg_rainfall_mm"], reverse=True)
        return result
    except Exception as e:
        logger.warning(f"站点降雨聚合面雨量失败（空间聚合）: {e}")
        return None


def _analyze_rainfall_core(time_str: str, pg_conf: dict, custom_timerange: str = "") -> dict:
    """
    降雨分析核心逻辑（与 @mcp.tool 解耦，便于测试）

    Args:
        time_str: 查询时刻
        pg_conf: 数据库配置
        custom_timerange: 可选，自定义时间范围如 "[20260603000000,20260604140000]"
                          传入时使用此范围，忽略默认 -32h/-8h 窗口
    """
    # 统一时间格式
    raw = time_str.strip()
    if len(raw) == 10:
        raw = raw + "0000"
    elif len(raw) == 19:
        raw = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").strftime("%Y%m%d%H%M%S")

    timestr = raw
    dt = datetime.strptime(timestr, "%Y%m%d%H%M%S")

    if custom_timerange:
        timerange = custom_timerange
        # 从自定义范围中提取起止用于可读展示
        m = re.match(r"\[(\d{14}),(\d{14})\]", custom_timerange)
        if m:
            time_start = m[1]
            time_end = m[2]
        else:
            time_start = (dt - timedelta(hours=32)).strftime("%Y%m%d%H%M%S")
            time_end = (dt - timedelta(hours=8)).strftime("%Y%m%d%H%M%S")
    else:
        time_start = (dt - timedelta(hours=32)).strftime("%Y%m%d%H%M%S")
        time_end = (dt - timedelta(hours=8)).strftime("%Y%m%d%H%M%S")
        timerange = f"[{time_start},{time_end}]"

    # 先读缓存，避免重复请求 MUSIC/数据库
    cached = _get_cached_rainfall(timestr, custom_timerange)
    if cached is not None:
        logger.info(f"降雨分析命中缓存: time_str={timestr}, timerange={timerange}")
        return cached

    # 1. 拉取天擎站点降雨数据
    # statSurfPreInBasin 存在参数兼容问题，改为逐小时取 PRE_1h 并本地累加
    client = _get_music_client()
    m = re.match(r'^\[(\d{14}),(\d{14})\]$', timerange)
    if not m:
        raise BusinessException(f"无效的时间范围：{timerange}")
    time_start, time_end = m.group(1), m.group(2)
    start_dt = datetime.strptime(time_start, "%Y%m%d%H%M%S")
    end_dt = datetime.strptime(time_end, "%Y%m%d%H%M%S")

    times_list = []
    cur = start_dt.replace(minute=0, second=0)
    while cur <= end_dt:
        times_list.append(cur.strftime("%Y%m%d%H%M%S"))
        cur += timedelta(hours=1)

    if not times_list:
        raw_data = []
    else:
        times_str = ",".join(times_list)
        hourly_records = client.get_surf_ele_in_basin_by_time(
            basin_codes=DEFAULT_BASIN_CODES,
            times=times_str,
            elements="Station_Id_C,Lat,Lon,City,Station_Name,Cnty,Province,Town,PRE_1h",
        )
        station_map: dict[str, dict] = {}
        for r in hourly_records:
            sid = str(r.get("Station_Id_C", "")).strip()
            if not sid:
                continue
            try:
                rain = float(r.get("PRE_1h", 0))
                if rain < 0 or rain > 9999:
                    continue
            except Exception:
                continue
            if sid not in station_map:
                station_map[sid] = {
                    "Station_Id_C": sid,
                    "Lat": r.get("Lat"),
                    "Lon": r.get("Lon"),
                    "City": r.get("City"),
                    "Station_Name": r.get("Station_Name"),
                    "Cnty": r.get("Cnty"),
                    "Province": r.get("Province"),
                    "Town": r.get("Town"),
                    "SUM_PRE_1H": 0.0,
                }
            station_map[sid]["SUM_PRE_1H"] += rain
        raw_data = list(station_map.values())

    # 2. 按降雨等级分组
    df = pd.DataFrame(raw_data)
    level_groups = []
    if not df.empty:
        # 统一字段名大小写（接口可能返回 SUM_PRE_1H 或 SUM_PRE_1h）
        sum_col = "SUM_PRE_1H"
        if sum_col not in df.columns:
            for col in df.columns:
                if col.upper() == "SUM_PRE_1H":
                    sum_col = col
                    break

        df["Lat"] = df["Lat"].astype(float)
        df["Lon"] = df["Lon"].astype(float)
        df[sum_col] = df[sum_col].astype(float)
        df = df[df[sum_col] < 99999]

        if not df.empty:
            df = (
                df.groupby("Station_Id_C", as_index=False, sort=False)
                .agg({
                    "Lat": "first", "Lon": "first",
                    "City": "first", "Station_Name": "first",
                    "Cnty": "first", "Province": "first", "Town": "first",
                    sum_col: "sum",
                })
            )

            df["level"] = df[sum_col].apply(_rain_label)
            grouped = {}
            for _, row in df[df["level"].notna()].iterrows():
                lv = row["level"]
                if lv not in grouped:
                    grouped[lv] = []
                grouped[lv].append({
                    "station_id": _sanitize(row["Station_Id_C"]),
                    "name": _sanitize(row["Station_Name"]),
                    "province": _sanitize(row["Province"]),
                    "city": _sanitize(row["City"]),
                    "cnty": _sanitize(row["Cnty"]),
                    "lon": float(row["Lon"]),
                    "lat": float(row["Lat"]),
                    "rainfall": float(row[sum_col]),
                })

            for name, lo, hi in RAIN_LEVELS:
                stations = grouped.get(name, [])
                if stations:
                    stations.sort(key=lambda s: s["rainfall"], reverse=True)
                    level_groups.append({"level": name, "stations": stations})

    max_rain = 0.0
    if level_groups and level_groups[0].get("stations"):
        max_rain = level_groups[0]["stations"][0]["rainfall"]
    max_level = level_groups[0]["level"] if level_groups else None

    # 3. 空间分析（按等级分组批量查询行政区划、77分区、河流）
    conn_ctx = None
    if level_groups:
        try:
            conn_ctx = _get_pg_conn(pg_conf)
        except Exception as e:
            logger.warning(f"空间分析数据库连接失败，跳过空间分析: {e}")
            conn_ctx = None

        if conn_ctx is not None:
            try:
                with conn_ctx.cursor(cursor_factory=RealDictCursor) as cur:
                    for group in level_groups:
                        stations = group["stations"]
                        if not stations:
                            group["admin_divisions"] = []
                            group["zone_77_regions"] = []
                            group["affected_rivers"] = []
                            continue

                        lons = [float(s["lon"]) for s in stations]
                        lats = [float(s["lat"]) for s in stations]

                        try:
                            # 批量行政区划
                            cur.execute("""
                                WITH pts AS (
                                    SELECT row_number() OVER () AS idx,
                                           ST_SetSRID(ST_MakePoint(lon, lat), 4326) AS geom
                                    FROM unnest(%s::float[], %s::float[]) AS t(lon, lat)
                                )
                                SELECT p.idx, a.province_name, a.city_name, a.county_name
                                FROM pts p
                                LEFT JOIN haihe_admin_division a
                                  ON ST_Within(p.geom, a.geom)
                            """, (lons, lats))
                            admin_rows = {r["idx"]: r for r in cur.fetchall()}

                            # 批量77分区
                            cur.execute("""
                                WITH pts AS (
                                    SELECT row_number() OVER () AS idx,
                                           ST_SetSRID(ST_MakePoint(lon, lat), 4326) AS geom
                                    FROM unnest(%s::float[], %s::float[]) AS t(lon, lat)
                                )
                                SELECT p.idx, z.zone_name, z.zone_code
                                FROM pts p
                                LEFT JOIN haihe_zone_77 z
                                  ON ST_Within(p.geom, z.geom)
                            """, (lons, lats))
                            zone_rows = {r["idx"]: r for r in cur.fetchall()}

                            # 批量附近河流（站点 30 km 缓冲区）
                            river_table = pg_conf.get("river_table_full", RIVER_TABLE_FULL)
                            cur.execute(f"""
                                WITH pts AS (
                                    SELECT row_number() OVER () AS idx,
                                           ST_SetSRID(ST_MakePoint(lon, lat), 4326) AS geom
                                    FROM unnest(%s::float[], %s::float[]) AS t(lon, lat)
                                )
                                SELECT p.idx,
                                       array_agg(DISTINCT r.river_name)
                                       FILTER (WHERE r.river_name IS NOT NULL AND r.river_name != '')
                                       AS rivers
                                FROM pts p
                                LEFT JOIN {river_table} r
                                  ON ST_DWithin(r.geom::geography, p.geom::geography, 30000)
                                GROUP BY p.idx
                            """, (lons, lats))
                            river_rows = {
                                r["idx"]: [name for name in (r["rivers"] or []) if name]
                                for r in cur.fetchall()
                            }

                            admin_set: set[str] = set()
                            zone77_set: set[str] = set()
                            river_set: set[str] = set()

                            for i, s in enumerate(stations, start=1):
                                admin_row = admin_rows.get(i)
                                if admin_row:
                                    admin_set.add(
                                        f"{_sanitize(admin_row['province_name'])} "
                                        f"{_sanitize(admin_row['city_name'])} "
                                        f"{_sanitize(admin_row['county_name'])}"
                                    )
                                zone_row = zone_rows.get(i)
                                if zone_row:
                                    zone77_set.add(
                                        f"{_sanitize(zone_row['zone_name'])}（{zone_row['zone_code']}）"
                                    )
                                river_set.update(_sanitize(name) for name in river_rows.get(i, []))

                            group["admin_divisions"] = sorted(admin_set)
                            group["zone_77_regions"] = sorted(zone77_set)
                            group["affected_rivers"] = sorted(river_set)
                        except Exception as e:
                            logger.warning(f"空间分析批量查询失败: {e}")
                            group["admin_divisions"] = []
                            group["zone_77_regions"] = []
                            group["affected_rivers"] = []
            finally:
                if conn_ctx is not None:
                    _put_pg_conn(pg_conf, conn_ctx)

    # 4. 组装结果
    summary_parts = []
    if max_rain <= 0:
        summary_parts.append("当前时段海河流域无有效降雨数据。")
    elif not max_level:
        summary_parts.append(f"当前最大降雨量{max_rain:.1f}mm。")
    else:
        top = level_groups[0] if level_groups else {}
        summary_parts.append(
            f"当前最大降雨量{max_rain:.1f}mm，达到「{max_level}」级别，"
            f"{max_level}级站点共{len(top.get('stations', []))}个。"
            f"涉及行政区划{len(top.get('admin_divisions', []))}个，"
            f"涉及77分区子流域{len(top.get('zone_77_regions', []))}个，"
            f"影响河流{len(top.get('affected_rivers', []))}条。"
        )

    data_time = dt.strftime("%Y-%m-%d %H:%M:%S")
    # 可读的时间范围
    time_start_readable = datetime.strptime(time_start, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M")
    time_end_readable = datetime.strptime(time_end, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M")
    result = {
        "time": timestr,
        "data_time": data_time,
        "time_range_readable": f"{time_start_readable} ~ {time_end_readable}",
        "total_stations": sum(len(g["stations"]) for g in level_groups),
        "max_rainfall": round(max_rain, 1),
        "max_level": max_level,
        "max_station": level_groups[0]["stations"][0] if level_groups and level_groups[0].get("stations") else None,
        "level_analysis": [
            {
                "level": g["level"],
                "station_count": len(g["stations"]),
                "stations": g["stations"][:10],
                "admin_divisions": g.get("admin_divisions", []),
                "zone_77_regions": g.get("zone_77_regions", []),
                "affected_rivers": g.get("affected_rivers", []),
            }
            for g in level_groups
        ],
        "summary": "".join(summary_parts),
    }
    _set_cached_rainfall(timestr, custom_timerange, result)
    return result


def register_tools(mcp: FastMCP):
    """注册所有降雨分析工具到MCP服务器"""

    register_haihe_tools(mcp)

    @mcp.tool()
    def get_xialiu_rivername(river: str) -> str:
        """根据提供的河流名称查询下流河有哪些"""
        riverlist = analysisRiverByName(river)
        riverlist = list(riverlist)
        return ",".join(riverlist)

    @mcp.tool()
    def get_xialiu_river_length(river: str, attr_name: str = "length_km") -> float:
        """
        根据提供的河流名称，计算其所有下游河段的总长度

        Args:
            river: 河流名称（与图中 edge 属性 rivername 一致）
            attr_name: 存储长度的属性名，默认为 "length_km"（单位：公里）

        Returns:
            float: 下游河段总长度（单位取决于 attr_name 对应字段，例如公里或米）
        """
        G = get_graph()

        # 先找到该河流对应的所有起始边
        river_edges = [
            (start, end, attr)
            for start, end, attr in G.edges(data=True)
            if get_edge_river_name(attr) == river
        ]

        if not river_edges:
            raise BusinessException(f"未在河流图中找到名称为「{river}」的河流")

        total_length = 0.0
        visited_edges = set()

        for _, end, _ in river_edges:
            # 包含当前起点及所有下游边
            downstream_edges = get_downstream_edges(G, end)
            for u, v, attr in downstream_edges:
                edge_id = (u, v)
                if edge_id in visited_edges:
                    continue
                visited_edges.add(edge_id)

                value = attr.get(attr_name)
                if isinstance(value, (int, float)):
                    total_length += float(value)

        return total_length

    @mcp.tool()
    def get_xialiu_river_profile(
        river: str,
        attr_name: str = "length_km",
        stop_at_river: str | None = None,
    ) -> list[dict]:
        """
        获取指定河流及其下游的精细“河段长度剖面”。

        功能：
        - 按沿程顺序返回每一段河段的长度信息
        - 给出每一段的长度、累计长度、上下游节点坐标、河名等
        - 可选：在到达某个指定下游河流（stop_at_river）时停止累计

        Args:
            river: 起始河流名称（与图中 edge 属性 rivername 一致）
            attr_name: 存储长度的属性名，默认为 "length_km"（单位：公里）
            stop_at_river: 可选，下游终止河流名称；如果为 None，则一直到图中的所有下游终点

        Returns:
            list[dict]: 每一段河段的信息，按沿程顺序排列
        """
        G = get_graph()

        # 找到所有起始边
        river_edges = [
            (start, end, attr)
            for start, end, attr in G.edges(data=True)
            if get_edge_river_name(attr) == river
        ]

        if not river_edges:
            raise BusinessException(f"未在河流图中找到名称为「{river}」的河流")

        profile: list[dict] = []
        visited_edges: set[tuple] = set()

        # 这里简单按 graph 的拓扑顺序展开；如需更严格的主干/支流区分，可后续扩展
        for _, end, _ in river_edges:
            downstream_edges = get_downstream_edges(G, end)
            for u, v, attr in downstream_edges:
                edge_id = (u, v)
                if edge_id in visited_edges:
                    continue
                visited_edges.add(edge_id)

                seg_rivername = get_edge_river_name(attr)
                seg_len = attr.get(attr_name)

                if not isinstance(seg_len, (int, float)):
                    continue

                item = {
                    # 段序号，方便前端按顺序展示
                    "index": len(profile),
                    # 河流名称（段所属河名）
                    "rivername": seg_rivername,
                    # 本段长度（单位由 attr_name 决定，默认 km）
                    "length": float(seg_len),
                    "attr_name": attr_name,
                    # 方便需要米/公里两种单位的场景
                    "length_m": float(attr.get("length_m", 0.0)),
                    "length_km": get_edge_length_km(attr, attr_name="length_km"),
                    # 一些拓扑/水文相关属性
                    "strahler_order": attr.get("strahler_order"),
                    "direct_downstream_of_rivers": attr.get("direct_downstream_of_rivers", ""),
                }
                profile.append(item)

                if stop_at_river and seg_rivername == stop_at_river:
                    # 到达指定终止河流，立即返回当前剖面
                    return profile

        return profile

    @mcp.tool()
    def get_river_network_for_plot(max_edges: int = 5000, start_river: str = None, downstream_km: float = None) -> list[dict]:
        """
        获取河网的线段数据，用于前端地理可视化图表。
        如果提供了 start_river，则仅返回该河流及其下游受影响水系的河网数据。
        当 downstream_km 大于 0 时，下游跟踪限制在该距离（km）内；否则一直跟踪到河口/末端。
        兼容 DiGraph / MultiDiGraph。
        """
        G = get_graph()

        if max_edges <= 0:
            raise BusinessException("max_edges 必须大于 0")

        # 收集待输出的边，统一格式为 (u, v, key, attr)
        edges_to_iterate: list[tuple] = []

        if start_river:
            from collections import deque

            target_edge_ids: set[tuple] = set()
            river_end_nodes: set = set()
            edge_start_dist: dict[tuple, float] = {}
            allowed_rivers: set[str] = {start_river}
            allowed_rivers.update(analysisRiverByName(start_river))

            # 1. 找到起始河流所有边和其下游节点
            for u, v, key, attr in iter_graph_edges(G):
                if get_edge_river_name(attr) == start_river:
                    river_end_nodes.add(v)
                    edge_id = make_edge_id(u, v, key)
                    target_edge_ids.add(edge_id)
                    edge_start_dist[edge_id] = 0.0

            if not river_end_nodes:
                logger.info(f"get_river_network_for_plot: 未找到起始河流 start_river={start_river!r}")
                return []

            # 2. 顺流而下，收集下游边
            if downstream_km is not None and downstream_km > 0:
                # 距离受限 BFS：从起始河流末端开始，累计河长不超过 downstream_km
                # 超过限制的河段会被截断保留，而不是整条丢弃
                queue = deque([(node, 0.0) for node in river_end_nodes])
                node_dist: dict = {node: 0.0 for node in river_end_nodes}
                while queue:
                    curr_node, curr_dist = queue.popleft()
                    for _u, next_node, key, attr in iter_out_edges(G, curr_node):
                        edge_river = get_edge_river_name(attr)
                        if allowed_rivers and edge_river and edge_river not in allowed_rivers:
                            continue
                        # 如果当前节点已经超过限制，不再往下游走
                        if curr_dist >= downstream_km:
                            continue
                        edge_id = make_edge_id(_u, next_node, key)
                        edge_len = get_edge_length_km(attr, attr_name="length_km") or 0.0
                        # 记录该边起点距离（用于后续截断）
                        if edge_id not in edge_start_dist or curr_dist < edge_start_dist[edge_id]:
                            edge_start_dist[edge_id] = curr_dist
                        target_edge_ids.add(edge_id)
                        next_dist = curr_dist + edge_len
                        # 只有未超限时，才继续从该边末端往下游走
                        if next_dist <= downstream_km:
                            if next_node not in node_dist or next_dist < node_dist[next_node]:
                                node_dist[next_node] = next_dist
                                queue.append((next_node, next_dist))
            else:
                # 全图 BFS：跟踪到河口/末端
                queue = deque(river_end_nodes)
                visited_nodes = set(river_end_nodes)
                while queue:
                    curr_node = queue.popleft()
                    for _u, next_node, key, attr in iter_out_edges(G, curr_node):
                        edge_river = get_edge_river_name(attr)
                        if allowed_rivers and edge_river and edge_river not in allowed_rivers:
                            continue
                        edge_id = make_edge_id(_u, next_node, key)
                        target_edge_ids.add(edge_id)
                        edge_start_dist[edge_id] = 0.0
                        if next_node not in visited_nodes:
                            visited_nodes.add(next_node)
                            queue.append(next_node)

            # 3. 再从图中筛出这些边，保持统一结构
            for u, v, key, attr in iter_graph_edges(G):
                edge_id = make_edge_id(u, v, key)
                if edge_id in target_edge_ids:
                    edges_to_iterate.append((u, v, key, attr))
        else:
            # 全图模式
            edges_to_iterate = list(iter_graph_edges(G))

        segments: list[dict] = []
        skipped_count = 0
        render_edges = edges_to_iterate[:max_edges]
        objectid_keys = {
            _edge_objectid_key(attr)
            for _u, _v, _key, attr in render_edges
            if _edge_objectid_key(attr)
        }
        edge_paths_map = _fetch_edge_paths_by_objectid(objectid_keys)

        for u, v, key, attr in render_edges:
            try:
                from_x, from_y = parse_node_xy(u, "u")
                to_x, to_y = parse_node_xy(v, "v")
                path: list[list[float]] = []
                paths: list[list[list[float]]] = []
                objectid_key = _edge_objectid_key(attr)
                if objectid_key and objectid_key in edge_paths_map:
                    candidates = edge_paths_map[objectid_key]
                    matched_fromto = [
                        c for c in candidates
                        if _candidate_matches_edge_endpoints(c, from_x, from_y, to_x, to_y)
                    ]
                    if matched_fromto:
                        for c in matched_fromto:
                            for ln in c.get("paths", []):
                                if _path_matches_edge_endpoints(ln, from_x, from_y, to_x, to_y):
                                    paths.append(ln)
                    if not paths:
                        for c in candidates:
                            for ln in c.get("paths", []):
                                if _path_matches_edge_endpoints(ln, from_x, from_y, to_x, to_y):
                                    paths.append(ln)
                    if paths:
                        # 取点数最多的一条作为主路径，兼容只支持单折线的前端
                        path = max(paths, key=lambda ln: len(ln))

                if not path:
                    path = [[from_x, from_y], [to_x, to_y]]
                    paths = [path]

                length_km = get_edge_length_km(attr, attr_name="length_km")

                # 下游距离受限时，对超出限制的河段进行截断
                if downstream_km is not None and downstream_km > 0:
                    edge_id = make_edge_id(u, v, key)
                    start_dist = edge_start_dist.get(edge_id, 0.0)
                    if start_dist + length_km > downstream_km:
                        remaining = max(0.0, downstream_km - start_dist)
                        path = _clip_polyline_to_length(path, remaining)
                        paths = [path]
                        if path:
                            from_x, from_y = path[0][0], path[0][1]
                            to_x, to_y = path[-1][0], path[-1][1]
                        length_km = remaining

                segments.append({
                    "from_x": from_x,
                    "from_y": from_y,
                    "to_x": to_x,
                    "to_y": to_y,
                    "path": path,
                    "paths": paths,
                    "geometry": {
                        "type": "MultiLineString" if len(paths) > 1 else "LineString",
                        "coordinates": paths if len(paths) > 1 else path,
                    },
                    "rivername": get_edge_river_name(attr) or "未知",
                    "length_km": length_km,
                    "strahler_order": attr.get("strahler_order", 1),
                    "edge_key": key,  # MultiDiGraph 下可用于前端区分并行边；DiGraph 下为 None
                    "objectid": objectid_key or None,
                })

            except (TypeError, ValueError) as e:
                skipped_count += 1
                logger.warning(
                    f"跳过无法解析的河网边: edge_id={make_edge_id(u, v, key)}, err={e}"
                )
                continue
            except Exception as e:
                skipped_count += 1
                logger.exception(
                    f"处理河网边时发生未预期错误: edge_id={make_edge_id(u, v, key)}, err={e}"
                )
                continue

        if skipped_count:
            logger.info(
                f"get_river_network_for_plot 完成: 输出 {len(segments)} 条线段，跳过 {skipped_count} 条异常线段"
            )

        return segments

    @mcp.tool()
    def get_river_network_leader_view(
        start_river: str = "",
        max_edges: int = 3000,
        top_n_rivers: int = 10,
    ) -> dict:
        """
        返回“领导/外行可读”的河网可视化结构：
        - KPI 卡片（总河段数、河流数、总长度、最大河阶）
        - Top 河流表格（按累计长度降序）
        - 地图线图层（可直接喂给前端画线）
        """
        query_river = (start_river or "").strip()
        segments = get_river_network_for_plot(
            max_edges=max_edges,
            start_river=query_river or None,
        )

        if top_n_rivers <= 0:
            top_n_rivers = 10

        if not segments:
            title = f"河网概览（起始河流：{query_river}）" if query_river else "河网概览（全流域）"
            kpi_cards = [
                {"label": "河段数", "value": 0},
                {"label": "河流数", "value": 0},
                {"label": "总长度(km)", "value": 0.0},
                {"label": "最大河阶", "value": 0},
            ]
            top_table: list[dict] = []
            map_layers = [{"layer_id": "river_network", "layer_type": "polyline", "features": []}]
            suggestions = [
                "先检查河流名称是否准确（建议使用标准河名）。",
                "如需全流域全量河网，可增大 max_edges。",
                "可先调用 get_river_network_for_plot 验证底层河网数据。",
            ]
            return {
                "view_type": "leader_brief",
                "title": title,
                "kpi_cards": kpi_cards,
                "top_rivers_table": top_table,
                "map_layers": map_layers,
                "message": "未找到可绘制的河网数据，请检查河流名称或图数据。",
                "leader_output": {
                    "title": title,
                    "cards": kpi_cards,
                    "table": {
                        "title": "重点河流TOP（按累计长度）",
                        "columns": [
                            {"key": "river_name", "label": "河流名称"},
                            {"key": "length_km", "label": "累计长度(km)"},
                            {"key": "edge_count", "label": "河段数"},
                            {"key": "max_strahler_order", "label": "最大河阶"},
                        ],
                        "rows": top_table,
                    },
                    "map": {
                        "title": "河网影响图",
                        "layers": map_layers,
                    },
                    "suggestions": suggestions[:3],
                },
            }

        river_stats: dict[str, dict] = {}
        total_length = 0.0
        max_order = 0
        for seg in segments:
            rn = str(seg.get("rivername", "未知") or "未知")
            lk = float(seg.get("length_km", 0.0) or 0.0)
            so = int(seg.get("strahler_order", 0) or 0)
            total_length += lk
            max_order = max(max_order, so)
            if rn not in river_stats:
                river_stats[rn] = {"river_name": rn, "edge_count": 0, "length_km": 0.0, "max_strahler_order": 0}
            river_stats[rn]["edge_count"] += 1
            river_stats[rn]["length_km"] += lk
            river_stats[rn]["max_strahler_order"] = max(river_stats[rn]["max_strahler_order"], so)

        table_rows = sorted(
            river_stats.values(),
            key=lambda x: x["length_km"],
            reverse=True,
        )[:top_n_rivers]
        for row in table_rows:
            row["length_km"] = round(float(row["length_km"]), 2)

        kpi_cards = [
            {"label": "河段数", "value": len(segments)},
            {"label": "河流数", "value": len(river_stats)},
            {"label": "总长度(km)", "value": round(total_length, 2)},
            {"label": "最大河阶", "value": int(max_order)},
        ]

        title = f"河网概览（起始河流：{query_river}）" if query_river else "河网概览（全流域）"
        suggestions = [
            "优先盯防表格前3条河流沿线区域。",
            "在地图上对起始河流及下游交汇处加密巡查。",
            "如用于汇报，建议只展示 cards+table+map+3条建议。",
        ]
        return {
            "view_type": "leader_brief",
            "title": title,
            "kpi_cards": kpi_cards,
            "top_rivers_table": table_rows,
            "map_layers": [
                {
                    "layer_id": "river_network",
                    "layer_type": "polyline",
                    "features": segments,
                }
            ],
            "message": "建议前端按 map_layers 直接绘图，并将 kpi_cards 与 top_rivers_table 作为卡片和表格展示。",
            "leader_output": {
                "title": title,
                "cards": kpi_cards,
                "table": {
                    "title": "重点河流TOP（按累计长度）",
                    "columns": [
                        {"key": "river_name", "label": "河流名称"},
                        {"key": "length_km", "label": "累计长度(km)"},
                        {"key": "edge_count", "label": "河段数"},
                        {"key": "max_strahler_order", "label": "最大河阶"},
                    ],
                    "rows": table_rows,
                },
                "map": {
                    "title": "河网影响图",
                    "layers": [
                        {
                            "layer_id": "river_network",
                            "layer_type": "polyline",
                            "features": segments,
                        }
                    ],
                },
                "suggestions": suggestions[:3],
            },
        }

    def _parse_downstream_river_names(raw_value: str, self_river: str) -> set[str]:
        if raw_value is None:
            return set()
        s = str(raw_value).strip()
        if not s:
            return set()

        # 兼容数组样式与多种分隔符：["A","B"] / A,B / A、B / A;B
        s = s.strip("[]{}()")
        parts = re.split(r"[,，、;；|/\\\s]+", s)
        out: set[str] = set()
        for p in parts:
            name = str(p).strip().strip("'\"")
            if not name or name == self_river:
                continue
            out.add(name)
        return out

    def _get_direct_downstream_from_table(rivername: str) -> set[str]:
        if not rivername:
            return set()
        if "postgres" not in config:
            return set()

        pg_conf = config["postgres"]
        schema = pg_conf.get("schema", "public")
        river_table_full = (
            pg_conf.get("river_table_full", "haihe_river_directed_full_v2").strip()
            or "haihe_river_directed_full_v2"
        )
        query_name = rivername.strip()
        if not query_name:
            return set()

        downstream: set[str] = set()
        try:
            with _get_pg_conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = %(schema)s
                          AND table_name = %(table)s
                        """,
                        {"schema": schema, "table": river_table_full},
                    )
                    cols = {str(r.get("column_name") or "").strip() for r in cur.fetchall()}

                    # 兼容旧结构：若表中存在 direct_downstream_of_rivers 列，优先直读。
                    if "direct_downstream_of_rivers" in cols:
                        sql = f"""
                            SELECT DISTINCT NULLIF(TRIM(direct_downstream_of_rivers), '') AS downstream_names
                            FROM {schema}.{river_table_full}
                            WHERE (
                                COALESCE(NULLIF(TRIM(river_name), ''), NULLIF(TRIM(src_name), ''), NULLIF(TRIM(name), '')) = %(river)s
                                OR NULLIF(TRIM(river_name), '') = %(river)s
                                OR NULLIF(TRIM(src_name), '') = %(river)s
                                OR NULLIF(TRIM(name), '') = %(river)s
                            )
                              AND NULLIF(TRIM(direct_downstream_of_rivers), '') IS NOT NULL
                        """
                        cur.execute(sql, {"river": query_name})
                        for row in cur.fetchall():
                            downstream.update(
                                _parse_downstream_river_names(row.get("downstream_names"), query_name)
                            )
                    else:
                        # 新版 v2 表：通过端点拓扑关系推导“直接下游”河流。
                        # 规则：当前河流任意线段的终点(to_x,to_y)与其他线段起点(from_x,from_y)相连。
                        sql = f"""
                            WITH curr AS (
                                SELECT to_x, to_y
                                FROM {schema}.{river_table_full}
                                WHERE COALESCE(NULLIF(TRIM(river_name), ''), NULLIF(TRIM(src_name), '')) = %(river)s
                            )
                            SELECT DISTINCT
                                COALESCE(NULLIF(TRIM(t.river_name), ''), NULLIF(TRIM(t.src_name), '')) AS downstream_name
                            FROM {schema}.{river_table_full} t
                            JOIN curr c
                              ON t.from_x = c.to_x AND t.from_y = c.to_y
                            WHERE COALESCE(NULLIF(TRIM(t.river_name), ''), NULLIF(TRIM(t.src_name), '')) IS NOT NULL
                              AND COALESCE(NULLIF(TRIM(t.river_name), ''), NULLIF(TRIM(t.src_name), '')) <> %(river)s
                        """
                        cur.execute(sql, {"river": query_name})
                        for row in cur.fetchall():
                            dn = str(row.get("downstream_name") or "").strip()
                            if dn and dn != query_name:
                                downstream.add(dn)
        except Exception as e:
            logger.warning("从 river_table_full 获取下游河流失败，回退图算法: river=%r err=%s", rivername, e)
            return set()

        return downstream

    def analysisRiverByName(rivername: str) -> set[str]:
        # 优先读取数据库直接下游，但不能直接返回，否则会丢失“间接下游”。
        # 这里与图拓扑结果做并集，保证下游范围完整。
        table_downstream = _get_direct_downstream_from_table(rivername)

        # 图拓扑下游判定（包含间接下游）
        G = get_graph()
        river_edges = [
            end for start, end, attr in G.edges(data=True)
            if get_edge_river_name(attr) == rivername
        ]
        res = []
        for end in river_edges:
            res.extend(get_downstream_edges(G, end))

        river_names: set[str] = set()
        for _u, _v, attr in res:
            name = get_edge_river_name(attr)
            if not name or name == rivername:
                continue
            river_names.add(name)
        river_names.update(table_downstream)
        river_names.discard(rivername)
        return river_names

    @mcp.tool()
    def describe_downstream_impacts(
        river: str,
        attr_name: str = "length_km",
        unit_label: str = "公里",
    ) -> list[str]:
        """
        用自然语言描述某条河对其下游河流的“影响距离”
        （即：从该河流终点沿水系到下游某条河流起点的最短沿程距离）。
        """
        import heapq

        G = get_graph()

        # 1. 找到指定河流的所有终点节点，作为多源最短路起点
        river_end_nodes: set = set()
        for u, v, attr in G.edges(data=True):
            if get_edge_river_name(attr) == river:
                river_end_nodes.add(v)

        if not river_end_nodes:
            return [f"暂未在河网中找到「{river}」的信息。"]

        # 2. 多源 Dijkstra
        # best_dist[node] = 从 river 的任一终点到该 node 的最短沿程距离
        best_dist: dict = {node: 0.0 for node in river_end_nodes}
        heap: list[tuple[float, object]] = [(0.0, node) for node in river_end_nodes]
        heapq.heapify(heap)

        # 记录到“下游河流起点”的最短距离
        impact_distances: dict[str, float] = {}

        while heap:
            current_dist, curr_node = heapq.heappop(heap)

            # 跳过过期堆元素
            if current_dist > best_dist.get(curr_node, float("inf")):
                continue

            # 遍历所有下游边
            for _u, next_node, attr in G.out_edges(curr_node, data=True):
                r_name = get_edge_river_name(attr)
                edge_len = get_edge_length_km(attr, attr_name=attr_name)

                # 如果当前边已经进入“其他河流”，则 current_dist 就是到该河流起点的距离
                if r_name and r_name != river:
                    old = impact_distances.get(r_name, float("inf"))
                    if current_dist < old:
                        impact_distances[r_name] = current_dist

                # 继续向下游传播距离：
                # - 源河流本身的边不增加距离
                # - 其他河流的边增加 edge_len
                next_dist = current_dist if r_name == river else current_dist + edge_len

                # 松弛
                if next_dist < best_dist.get(next_node, float("inf")):
                    best_dist[next_node] = next_dist
                    heapq.heappush(heap, (next_dist, next_node))

        # 3. 组装自然语言结果
        if not impact_distances:
            return [f"暂未在河网中找到「{river}」的下游河流信息。"]

        sentences: list[str] = []
        for to_river in sorted(impact_distances.keys()):
            length_val = impact_distances[to_river]
            if abs(length_val) < 1e-12:
                sentences.append(
                    f"「{river}」直接汇入或分流至「{to_river}」，距离为 0 {unit_label}。"
                )
            else:
                sentences.append(
                    f"从「{river}」到「{to_river}」的沿程影响距离约为 {length_val:.2f} {unit_label}。"
                )

        return sentences

    @mcp.tool()
    def estimate_river_impact_time(
        river_name: str,
        target_downstream_river: str | None = None,
        max_rivers: int = 20,
        max_distance_km: float | None = None,
    ) -> dict:
        """
        估算“河流影响时间”（基于经验流速 4.67/5.84/7.01 km/h）。

        说明：
        - 先使用河网图计算从源河流末端到下游河流“起点”的沿程影响距离（km）
        - 再以三档流速分别换算成时间（小时/天），并输出对应中文描述

        Args:
            river_name: 源河流名称
            target_downstream_river: 可选，指定只看某一条下游河流；为空则返回若干条下游河流
            max_rivers: 未指定 target_downstream_river 时，最多返回的下游河流数量（按距离升序）
            max_distance_km: 可选，只返回沿程距离不超过该值的下游河流
        """
        return estimate_river_impact_time_core(
            river_name=river_name,
            target_downstream_river=target_downstream_river,
            max_rivers=max_rivers,
            max_distance_km=max_distance_km,
        )

    @mcp.tool()
    def analyze_rainfall_by_time(
        time_str: str,
        start_time: str = "",
        end_time: str = "",
    ) -> dict:
        """
        基于天擎自动站降雨数据，分析指定时刻各降雨等级（小雨→特大暴雨）涉及的行政区划、77分区河系、受影响河流。
        当用户询问"某时间点的降雨情况/雨情/雨量分析/最大降雨"时，必须调用此工具。
        如果要查询"今天0点到现在的累计降雨"，传入 start_time 和 end_time 即可覆盖默认时间窗口。

        Args:
            time_str: 查询时刻（支持过去任意日期），支持格式：
                - "YYYYMMDDHH"（如 2026051700 表示2026年5月17日0点）
                - "YYYYMMDDHHMMSS"（如 20260517000000）
                - "YYYY-MM-DD HH:MM:SS"（如 2026-05-17 00:00:00）
            start_time: 可选，自定义开始时间，格式 "YYYYMMDDHHmmss"，传此参数时覆盖默认窗口
            end_time: 可选，自定义结束时间，格式 "YYYYMMDDHHmmss"，与 start_time 配合使用
        """
        pg_conf = config["postgres"]
        custom_timerange = ""
        if start_time and end_time:
            custom_timerange = f"[{start_time},{end_time}]"
        return _analyze_rainfall_core(time_str, pg_conf, custom_timerange)

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
        """
        分析指定时段内暴雨及以上降雨影响的河系/河流，返回可用于前端高亮绘制的河网线段。

        处理流程：
        1. 复用 analyze_rainfall_by_time 提取暴雨及以上站点；
        2. 对暴雨站点做 30km 地理缓冲区，与河网求交并截断，只保留缓冲区内的河段部分；
        3. 从 30km 缓冲区边界向下游拓扑跟踪约 50km，超出 50km 的河段继续截断；
        4. 合并缓冲区河段与下游河段，标记 is_affected=True，可选叠加全流域背景河段。

        Args:
            time_str: 查询时刻，支持 "YYYYMMDDHH" / "YYYYMMDDHHMMSS" / "YYYY-MM-DD HH:MM:SS"
            start_time: 可选自定义开始时间，格式 "YYYYMMDDHHMMSS"
            end_time: 可选自定义结束时间，格式 "YYYYMMDDHHMMSS"
            rainfall_threshold_mm: 判定阈值，默认 50.0（暴雨）
            max_edges: 背景河网查询返回的最大河段数，默认 5000
            include_background: 是否同时返回全流域背景河段（未受影响的标记 is_affected=False）
            downstream_km: 从 30km 缓冲区边界向下游跟踪的距离（km），默认 50.0；设 0 或负数表示不限制
        Returns:
            dict: 含 time_range_readable、rainfall_threshold_mm、affected_rivers、
                  affected_zone_77_regions、affected_admin_divisions、stations（暴雨及以上站点）、
                  total_segments、affected_segments、segments（每条含 is_affected 标记）、summary
        """
        pg_conf = config["postgres"]
        custom_timerange = ""
        if start_time and end_time:
            custom_timerange = f"[{start_time},{end_time}]"

        rainfall_result = _analyze_rainfall_core(time_str, pg_conf, custom_timerange)

        # 降雨等级 -> 最小阈值
        level_to_threshold = {name: lo for name, lo, hi in RAIN_LEVELS}

        affected_rivers: set[str] = set()
        affected_zone_77_regions: set[str] = set()
        affected_admin_divisions: set[str] = set()
        stations: list[dict] = []

        for level_item in rainfall_result.get("level_analysis", []):
            level = level_item.get("level", "")
            threshold = level_to_threshold.get(level, float("inf"))
            if threshold < rainfall_threshold_mm:
                continue
            for r in level_item.get("affected_rivers", []):
                if r and str(r).strip() not in ("未知", "None", ""):
                    affected_rivers.add(str(r).strip())
            for z in level_item.get("zone_77_regions", []):
                if z:
                    affected_zone_77_regions.add(str(z).strip())
            for a in level_item.get("admin_divisions", []):
                if a:
                    affected_admin_divisions.add(str(a).strip())
            for s in level_item.get("stations", []):
                if isinstance(s, dict):
                    stations.append({
                        "station_id": s.get("station_id"),
                        "name": s.get("name"),
                        "lon": s.get("lon"),
                        "lat": s.get("lat"),
                        "rainfall": s.get("rainfall"),
                        "level": level,
                    })

        if not affected_rivers:
            return {
                "time_range_readable": rainfall_result.get("time_range_readable", ""),
                "rainfall_threshold_mm": rainfall_threshold_mm,
                "affected_rivers": [],
                "affected_zone_77_regions": sorted(affected_zone_77_regions),
                "affected_admin_divisions": sorted(affected_admin_divisions),
                "stations": sorted(stations, key=lambda x: x.get("rainfall", 0), reverse=True),
                "total_segments": 0,
                "affected_segments": 0,
                "segments": [],
                "summary": (
                    f"统计时段 {rainfall_result.get('time_range_readable', '')} 内，"
                    f"未达到 {rainfall_threshold_mm}mm 降雨阈值的河系数据。"
                ),
            }

        def _segment_key(seg: dict) -> tuple:
            return (
                seg.get("from_x"),
                seg.get("from_y"),
                seg.get("to_x"),
                seg.get("to_y"),
                str(seg.get("rivername", "")).strip(),
            )

        def _build_segment_from_graph_edge(
            u, v, key, attr, path: list[list[float]] | None = None, is_affected: bool = False
        ) -> dict | None:
            """根据图边和路径构建 segment 字典。"""
            try:
                from_x, from_y = parse_node_xy(u, "u")
                to_x, to_y = parse_node_xy(v, "v")
            except Exception:
                return None

            objectid_key = _edge_objectid_key(attr)
            if path is None or len(path) < 2:
                path = [[from_x, from_y], [to_x, to_y]]

            length_km = get_edge_length_km(attr, attr_name="length_km")
            # 如果提供了真实路径，用路径重新估算长度
            if len(path) >= 2:
                path_len = _haversine_km(path[0][0], path[0][1], path[1][0], path[1][1])
                for i in range(1, len(path) - 1):
                    path_len += _haversine_km(path[i][0], path[i][1], path[i + 1][0], path[i + 1][1])
                if path_len > 0:
                    length_km = path_len

            return {
                "from_x": path[0][0],
                "from_y": path[0][1],
                "to_x": path[-1][0],
                "to_y": path[-1][1],
                "path": path,
                "paths": [path],
                "geometry": {"type": "LineString", "coordinates": path},
                "rivername": get_edge_river_name(attr) or "未知",
                "length_km": length_km,
                "strahler_order": attr.get("strahler_order", 1),
                "edge_key": key,
                "objectid": objectid_key or None,
                "is_affected": is_affected,
            }

        segment_map: dict[tuple, dict] = {}

        # 1. 暴雨站点 30km 缓冲区截断：从数据库拿到被截断的河段几何
        clipped_paths, clipped_lengths = _get_clipped_buffer_segments(stations, buffer_m=30000)
        logger.info(f"[缓冲区截断] 站点数={len(stations)}, 受影响河段 objectid 数={len(clipped_paths)}")

        G = get_graph()
        affected_edge_ids: set[tuple] = set()
        # 记录每个节点距离 30km 缓冲区边界的下游距离（用于后续 50km 截断）
        node_dist: dict[Any, float] = {}

        if clipped_paths:
            for u, v, key, attr in iter_graph_edges(G):
                objectid_key = _edge_objectid_key(attr)
                if not objectid_key or objectid_key not in clipped_paths:
                    continue
                edge_id = make_edge_id(u, v, key)
                affected_edge_ids.add(edge_id)

                paths = clipped_paths[objectid_key]
                # 取最长的一段作为该边在缓冲区内的部分（通常只有一段）
                clipped_path = max(paths, key=lambda p: len(p))
                seg = _build_segment_from_graph_edge(u, v, key, attr, clipped_path, is_affected=True)
                if seg is None:
                    continue

                # 计算该边末端（v）超出 30km 缓冲区的距离，作为下游 50km 的起算距离
                total_len = get_edge_length_km(attr, attr_name="length_km")
                clipped_len = clipped_lengths.get(objectid_key, 0.0)
                # v 点到最近站点的距离，用于判断 v 是否在缓冲区内
                try:
                    vx, vy = parse_node_xy(v, "v")
                    v_dist_to_station = min(
                        _haversine_km(vx, vy, float(s["lon"]), float(s["lat"]))
                        for s in stations if s.get("lon") is not None and s.get("lat") is not None
                    )
                except Exception:
                    v_dist_to_station = float("inf")

                if v_dist_to_station <= 30.0:
                    # v 在缓冲区内，下游从 v 开始算，距离为 0
                    out_dist = 0.0
                else:
                    # v 在缓冲区外，下游从 30km 边界开始算，v 距离边界约 (总长 - 缓冲区内长)
                    out_dist = max(0.0, total_len - clipped_len)

                if v not in node_dist or out_dist < node_dist[v]:
                    node_dist[v] = out_dist

                seg_key = _segment_key(seg)
                segment_map[seg_key] = seg

        # 2. 从 30km 缓冲区边界/末端向下游跟踪 50km，并做截断
        if downstream_km is not None and downstream_km > 0 and node_dist:
            from collections import deque

            # BFS：节点 -> 到 30km 边界的最短下游距离
            queue = deque([(node, node_dist[node]) for node in node_dist])
            visited_edges: set[tuple] = set(affected_edge_ids)

            while queue:
                curr_node, curr_dist = queue.popleft()
                if curr_dist >= downstream_km:
                    continue

                for _u, next_node, key, attr in iter_out_edges(G, curr_node):
                    edge_id = make_edge_id(_u, next_node, key)
                    if edge_id in visited_edges:
                        continue
                    visited_edges.add(edge_id)

                    edge_len = get_edge_length_km(attr, attr_name="length_km") or 0.0
                    next_dist = curr_dist + edge_len

                    # 整条边都在 50km 内
                    if next_dist <= downstream_km:
                        seg = _build_segment_from_graph_edge(_u, next_node, key, attr, is_affected=True)
                        if seg:
                            seg_key = _segment_key(seg)
                            if seg_key not in segment_map:
                                segment_map[seg_key] = seg
                        if next_node not in node_dist or next_dist < node_dist[next_node]:
                            node_dist[next_node] = next_dist
                            queue.append((next_node, next_dist))
                    else:
                        # 部分在 50km 内，需要截断
                        remaining = max(0.0, downstream_km - curr_dist)
                        # 获取该边的真实几何
                        full_path = None
                        objectid_key = _edge_objectid_key(attr)
                        if objectid_key:
                            # 尝试从数据库取真实几何
                            try:
                                tmp_map = _fetch_edge_paths_by_objectid({objectid_key})
                                if tmp_map.get(objectid_key):
                                    candidates = tmp_map[objectid_key]
                                    from_x, from_y = parse_node_xy(_u, "u")
                                    to_x, to_y = parse_node_xy(next_node, "v")
                                    for c in candidates:
                                        for ln in c.get("paths", []):
                                            if _path_matches_edge_endpoints(ln, from_x, from_y, to_x, to_y):
                                                full_path = ln
                                                break
                                        if full_path:
                                            break
                            except Exception:
                                pass
                        if not full_path:
                            from_x, from_y = parse_node_xy(_u, "u")
                            to_x, to_y = parse_node_xy(next_node, "v")
                            full_path = [[from_x, from_y], [to_x, to_y]]

                        clipped_path = _clip_polyline_to_length(full_path, remaining)
                        if len(clipped_path) >= 2:
                            seg = _build_segment_from_graph_edge(_u, next_node, key, attr, clipped_path, is_affected=True)
                            if seg:
                                seg_key = _segment_key(seg)
                                if seg_key not in segment_map:
                                    segment_map[seg_key] = seg

        # 3. 可选：补充全流域背景河段
        if include_background:
            try:
                background_segs = get_river_network_for_plot(max_edges=max_edges, start_river=None)
                if isinstance(background_segs, list):
                    for seg in background_segs:
                        if not isinstance(seg, dict):
                            continue
                        key = _segment_key(seg)
                        if key in segment_map:
                            continue
                        seg_copy = dict(seg)
                        seg_copy["is_affected"] = False
                        segment_map[key] = seg_copy
            except Exception as e:
                logger.warning(f"[get_affected_river_network_by_rainfall] 获取背景河网失败: {e}")

        segments = list(segment_map.values())
        affected_segments = sum(1 for s in segments if s.get("is_affected"))

        return {
            "time_range_readable": rainfall_result.get("time_range_readable", ""),
            "rainfall_threshold_mm": rainfall_threshold_mm,
            "affected_rivers": sorted(affected_rivers),
            "affected_zone_77_regions": sorted(affected_zone_77_regions),
            "affected_admin_divisions": sorted(affected_admin_divisions),
            "stations": sorted(stations, key=lambda x: x.get("rainfall", 0), reverse=True),
            "total_segments": len(segments),
            "affected_segments": affected_segments,
            "segments": segments,
            "summary": (
                f"统计时段 {rainfall_result.get('time_range_readable', '')} 内，"
                f"降雨量≥{rainfall_threshold_mm}mm 的站点共影响 {len(affected_rivers)} 条河流，"
                f"涉及 {len(affected_zone_77_regions)} 个 77 分区子流域、"
                f"{len(affected_admin_divisions)} 个行政区划。"
            ),
        }

    def get_downstream_edges(G, node):
        sub_nodes = {node} | nx.descendants(G, node)
        subgraph = G.subgraph(sub_nodes)
        return list(subgraph.edges(data=True))

    @mcp.tool()
    def get_city_rainfall_time_range(
        city_name: str = "",
        start_time: str = "",
        forecast_hours: int = 24,
        end_time: str = "",
        city_list: list | None = None,
    ) -> RainfallCityData:
        """
        获取指定城市在特定时间范围内的降水（降雨）情况

        【使用场景】
        当用户询问以下问题时使用此工具：
        - "XX 市明天有雨吗？"、"天津市周末下雨吗？"（城市降水预报）
        - "XX 市降雨情况如何？"、"北京市降水情况怎样？"（城市降雨情况）
        - "明天 XX 市雨量多少？"、"天津市累计降雨量是多少？"（城市雨量查询）
        ⚠️ 注意：此工具依赖磁盘上的 EC 预报栅格文件，不是所有时次都有数据。
        如果报错"未找到降雨数据文件"，请改用 analyze_rainfall_by_time（天擎实况）或 query_basin_areal_rainfall（面雨量）。
        Args:
            city_name (str): 城市名称（如：天津市、北京市、石家庄市等）
            start_time (str): 开始时间，支持多种格式：
                - "2025-07-01 02:00:00"（凌晨 2 点，用于日期查询）
                - "2025-07-01 08:00:00"（上午 8 点，用于白天查询）
                - "2025-07-01 20:00:00"（晚上 8 点，用于夜间查询）
                - "2025070102"、"2025070108"、"2025070120"（紧凑格式）
                注意：小时数必须为 2、8、14、20 之一
            forecast_hours (int): 预报时长（小时），可选值：6、12、24
                - 6 小时：短时预报
                - 12 小时：半天预报（推荐用于白天/夜间查询）
                - 24 小时：全天预报（推荐用于日期查询）
            end_time (str): 结束时间（兼容 LLM 误传），若与 forecast_hours 同时存在以 forecast_hours 为准
            city_list (list): 城市列表（兼容 LLM 误传），只取第一个城市查询
        """
        # 兼容 LLM 可能误传的参数
        if city_list and isinstance(city_list, list) and len(city_list) > 0:
            city_name = str(city_list[0])
        if isinstance(city_name, list):
            city_name = str(city_name[0]) if city_name else ""
        city_name = str(city_name or "").strip()
        if not city_name:
            raise BusinessException("缺少城市名称参数 city_name")

        # 修正模型常犯的紧凑时间格式：2026-06-2602:00:00 -> 2026-06-26 02:00:00
        start_time = str(start_time or "").strip()
        start_time = re.sub(r"(\d{4}-\d{2}-\d{2})(\d{2}:\d{2}:\d{2})", r"\1 \2", start_time)
        start_time = re.sub(r"(\d{4}-\d{2}-\d{2})(\d{2}:\d{2})", r"\1 \2", start_time)
        start_time = re.sub(r"(\d{4}-\d{2}-\d{2})(\d{2})", r"\1 \2", start_time)

        # 若 LLM 传了 end_time 但没传 forecast_hours（或给了默认值 24），尝试推算
        end_time = str(end_time or "").strip()
        end_time = re.sub(r"(\d{4}-\d{2}-\d{2})(\d{2}:\d{2}:\d{2})", r"\1 \2", end_time)
        end_time = re.sub(r"(\d{4}-\d{2}-\d{2})(\d{2}:\d{2})", r"\1 \2", end_time)
        end_time = re.sub(r"(\d{4}-\d{2}-\d{2})(\d{2})", r"\1 \2", end_time)

        logger.info(
            f"📊 调用 get_city_rainfall_time_range - 城市：{city_name}, 开始时间：{start_time}, 预报时长：{forecast_hours}小时")

        # 1. 解析和验证时间字符串
        try:
            # 支持多种时间格式
            time_formats = [
                "%Y-%m-%d %H:%M:%S",  # 2025-07-01 23:00:00
                "%Y-%m-%d %H:%M",  # 2025-07-01 23:00
                "%Y-%m-%d %H",  # 2025-07-01 23
                "%Y%m%d%H",  # 2025070123
                "%Y/%m/%d %H:%M:%S",  # 2025/07/01 23:00:00
                "%Y/%m/%d %H:%M",  # 2025/07/01 23:00
                "%Y/%m/%d %H",  # 2025/07/01 23
                "%Y%m%d",  # 20250701
            ]

            parsed_time = None
            last_error = None

            for fmt in time_formats:
                try:
                    parsed_time = datetime.strptime(start_time, fmt)
                    break
                except ValueError as e:
                    last_error = e
                    continue

            if parsed_time is None:
                raise BusinessException(f"时间格式不正确: {start_time}。支持的格式包括: "
                                        "YYYY-MM-DD HH:MM:SS, YYYY-MM-DD HH:MM, YYYY-MM-DD HH, YYYYMMDDHH")

            # 2. 验证时间是否为整点
            if parsed_time.minute != 0 or parsed_time.second != 0:
                raise BusinessException(f"时间必须为整点: {start_time}")

            # 3. 验证小时数是否满足 hour%6==2 (即小时数为2,  8, 14,  20,)
            if parsed_time.hour % 6 != 2:
                valid_hours = [2, 8, 14, 20]
                raise BusinessException(f"小时数必须满足 hour%6==2，有效小时数为: {valid_hours}")

            # 4. 验证预报小时数
            if forecast_hours not in [6, 12, 24]:
                raise BusinessException(f"预报小时数只支持6或12或24小时，当前值: {forecast_hours}")

            # 5. 调用分析器
            return analyzer.get_city_rainfall_time_range(city_name, parsed_time, forecast_hours)

        except BusinessException:
            # 重新抛出自定义异常
            raise
        except Exception as e:
            # 包装其他异常
            raise BusinessException(f"处理时间参数时发生错误: {str(e)}")

    @mcp.tool()
    def get_server_time() -> str:
        """
        获取当前服务器时间

        Returns:
            str: 格式化的时间字符串
        """
        from datetime import timezone, timedelta

        # 获取当前 UTC 时间
        now_utc = datetime.now(timezone.utc)

        # 转换为中国标准时间（UTC+8）
        china_tz = timezone(timedelta(hours=8))
        now_china = now_utc.astimezone(china_tz)

        # 直接返回字符串，避免 JSON 序列化问题
        return f"当前服务器时间：{now_china.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)"

    def _get_pg_conn():
        """
        获取 PostgreSQL 连接。

        需要在 config.ini 中增加类似配置：
        [postgres]
        host = 127.0.0.1
        port = 5432
        dbname = your_db
        user = your_user
        password = your_password
        schema = public
        srid = 4490
        """
        if "postgres" not in config:
            raise BusinessException("config.ini 中缺少 [postgres] 配置段，无法连接 PostGIS 数据库。")

        pg_conf = config["postgres"]
        try:
            conn = psycopg2.connect(
                host=pg_conf.get("host", "127.0.0.1"),
                port=pg_conf.getint("port", 5432),
                dbname=pg_conf.get("dbname"),
                user=pg_conf.get("user"),
                password=pg_conf.get("password"),
            )
            return conn
        except Exception as e:
            raise BusinessException(f"连接 PostgreSQL 失败: {e}")

    def _parse_emergency_time(value: str) -> datetime:
        if not value:
            raise BusinessException("时间不能为空")
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d %H",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y/%m/%d %H",
            "%Y%m%d%H%M%S",
            "%Y%m%d%H",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        raise BusinessException(f"无法解析时间: {value}")

    def _get_pg_schema_and_srid() -> tuple[str, int]:
        if "postgres" not in config:
            raise BusinessException("config.ini 缺少 [postgres] 配置段。")
        pg_conf = config["postgres"]
        schema = pg_conf.get("schema", "public")
        try:
            srid = int(pg_conf.get("srid", "4326"))
        except Exception:
            srid = 4326
        return schema, srid

    def _ensure_emergency_tables(conn) -> None:
        schema, _ = _get_pg_schema_and_srid()
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema};")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {schema}.hh_emergency_forecast_cycle (
                    id BIGSERIAL PRIMARY KEY,
                    cycle_key TEXT NOT NULL UNIQUE,
                    forecast_time TIMESTAMP NOT NULL,
                    source_kind TEXT NOT NULL DEFAULT 'forecast',
                    trigger_id TEXT,
                    ext JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {schema}.hh_emergency_event (
                    id BIGSERIAL PRIMARY KEY,
                    event_code TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL DEFAULT 'rainstorm',
                    event_level TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    start_time TIMESTAMP NOT NULL,
                    end_time TIMESTAMP,
                    latest_cycle_id BIGINT REFERENCES {schema}.hh_emergency_forecast_cycle(id),
                    ext JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {schema}.hh_emergency_product (
                    id BIGSERIAL PRIMARY KEY,
                    event_id BIGINT NOT NULL REFERENCES {schema}.hh_emergency_event(id) ON DELETE CASCADE,
                    cycle_id BIGINT REFERENCES {schema}.hh_emergency_forecast_cycle(id),
                    product_type TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    product_name TEXT,
                    product_uri TEXT NOT NULL,
                    product_time TIMESTAMP,
                    ext JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute(f"""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_hh_emergency_product_main
                ON {schema}.hh_emergency_product (event_id, cycle_id, product_type, source_kind, product_uri);
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {schema}.hh_emergency_station_snapshot (
                    id BIGSERIAL PRIMARY KEY,
                    event_id BIGINT NOT NULL REFERENCES {schema}.hh_emergency_event(id) ON DELETE CASCADE,
                    cycle_id BIGINT REFERENCES {schema}.hh_emergency_forecast_cycle(id),
                    snapshot_kind TEXT NOT NULL,
                    station_count INT NOT NULL DEFAULT 0,
                    station_json_path TEXT,
                    station_data_json JSONB,
                    ext JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute(f"""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_hh_emergency_station_snapshot_main
                ON {schema}.hh_emergency_station_snapshot (event_id, cycle_id, snapshot_kind);
            """)

    def _upsert_forecast_cycle(
        conn,
        cycle_key: str,
        forecast_time: datetime,
        source_kind: str,
        trigger_id: str | None,
        ext: dict | None,
    ) -> dict:
        schema, _ = _get_pg_schema_and_srid()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                INSERT INTO {schema}.hh_emergency_forecast_cycle
                    (cycle_key, forecast_time, source_kind, trigger_id, ext)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (cycle_key)
                DO UPDATE SET
                    forecast_time = EXCLUDED.forecast_time,
                    source_kind = EXCLUDED.source_kind,
                    trigger_id = EXCLUDED.trigger_id,
                    ext = EXCLUDED.ext
                RETURNING id, cycle_key, forecast_time, source_kind, trigger_id, ext, created_at;
                """,
                (cycle_key, forecast_time, source_kind, trigger_id, json.dumps(ext or {}, ensure_ascii=False)),
            )
            return dict(cur.fetchone())

    def _event_level_rank(level: str) -> int:
        # 数值越小等级越高：I > II > III > IV
        mapping = {"I": 1, "II": 2, "III": 3, "IV": 4}
        return mapping.get((level or "").upper().strip(), 99)

    def _higher_level(level_a: str, level_b: str) -> str:
        return level_a if _event_level_rank(level_a) <= _event_level_rank(level_b) else level_b

    def _floor_to_window(dt: datetime, hours: int) -> datetime:
        if hours <= 0:
            return dt
        base_hour = (dt.hour // hours) * hours
        return dt.replace(hour=base_hour, minute=0, second=0, microsecond=0)

    def _find_or_create_event(
        conn,
        event_code: str | None,
        event_type: str,
        event_level: str,
        title: str,
        cycle_id: int,
        start_time: datetime,
        status: str,
        zone_code: str | None,
        city_name: str | None,
        response_window_hours: int,
        ext: dict | None,
    ) -> tuple[dict, bool]:
        schema, _ = _get_pg_schema_and_srid()
        ext_obj = dict(ext or {})
        ext_obj["zone_code"] = (zone_code or "").strip() or None
        ext_obj["city_name"] = (city_name or "").strip() or None
        ext_obj["current_level"] = event_level
        ext_obj["window_hours"] = response_window_hours
        window_start = _floor_to_window(start_time, response_window_hours)
        window_end = window_start + timedelta(hours=response_window_hours)
        ext_obj["window_start"] = window_start.strftime("%Y-%m-%d %H:%M:%S")

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if event_code:
                cur.execute(
                    f"""
                    SELECT id, event_code, event_type, event_level, title, status, start_time, end_time, latest_cycle_id, ext
                    FROM {schema}.hh_emergency_event
                    WHERE event_code=%s
                    LIMIT 1;
                    """,
                    (event_code,),
                )
                row = cur.fetchone()
                if row:
                    final_level = _higher_level(str(row.get("event_level") or ""), event_level)
                    cur.execute(
                        f"""
                        UPDATE {schema}.hh_emergency_event
                        SET latest_cycle_id=%s, updated_at=NOW(), status=%s, event_level=%s, ext=%s::jsonb
                        WHERE id=%s
                        RETURNING id, event_code, event_type, event_level, title, status, start_time, end_time, latest_cycle_id, ext;
                        """,
                        (cycle_id, status, final_level, json.dumps(ext_obj, ensure_ascii=False), row["id"]),
                    )
                    return dict(cur.fetchone()), False

            cur.execute(
                f"""
                SELECT id, event_code, event_type, event_level, title, status, start_time, end_time, latest_cycle_id, ext
                FROM {schema}.hh_emergency_event
                WHERE event_type=%s
                  AND status='active'
                  AND start_time >= %s
                  AND start_time < %s
                  AND COALESCE(ext->>'zone_code','') = %s
                  AND COALESCE(ext->>'city_name','') = %s
                ORDER BY start_time DESC
                LIMIT 1;
                """,
                (
                    event_type,
                    window_start,
                    window_end,
                    (zone_code or "").strip(),
                    (city_name or "").strip(),
                ),
            )
            same_active = cur.fetchone()
            if same_active:
                final_level = _higher_level(str(same_active.get("event_level") or ""), event_level)
                cur.execute(
                    f"""
                    UPDATE {schema}.hh_emergency_event
                    SET latest_cycle_id=%s, updated_at=NOW(), event_level=%s, ext=%s::jsonb
                    WHERE id=%s
                    RETURNING id, event_code, event_type, event_level, title, status, start_time, end_time, latest_cycle_id, ext;
                    """,
                    (cycle_id, final_level, json.dumps(ext_obj, ensure_ascii=False), same_active["id"]),
                )
                return dict(cur.fetchone()), False

            new_event_code = event_code or f"EVT-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
            cur.execute(
                f"""
                INSERT INTO {schema}.hh_emergency_event
                    (event_code, event_type, event_level, title, status, start_time, latest_cycle_id, ext)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING id, event_code, event_type, event_level, title, status, start_time, end_time, latest_cycle_id, ext;
                """,
                (new_event_code, event_type, event_level, title, status, start_time, cycle_id, json.dumps(ext_obj, ensure_ascii=False)),
            )
            return dict(cur.fetchone()), True

    @mcp.tool()
    def init_emergency_management_tables() -> dict:
        """
        初始化应急事件管理相关数据表（起报批次、事件、产品图、站点快照）。
        """
        schema, _ = _get_pg_schema_and_srid()
        with _get_pg_conn() as conn:
            _ensure_emergency_tables(conn)
            conn.commit()
        return {"ok": True, "schema": schema, "message": "应急事件管理数据表初始化完成。"}

    @mcp.tool()
    def upsert_emergency_event_management(
        forecast_time: str,
        event_level: str,
        event_type: str = "rainstorm",
        zone_code: str | None = None,
        city_name: str | None = None,
        response_window_hours: int = 3,
        event_title: str = "",
        event_status: str = "active",
        cycle_key: str | None = None,
        event_code: str | None = None,
        trigger_id: str | None = None,
        source_kind: str = "forecast",
        product_images_json: str | None = None,
        station_distribution_json: str | None = None,
        station_snapshot_kind: str = "forecast_station_distribution",
        station_json_dir: str = "data/emergency_station_snapshots",
    ) -> dict:
        """
        应急事件管理入库：
        1) 起报批次表（hh_emergency_forecast_cycle）
        2) 应急事件表（hh_emergency_event）：若已有对应 active 事件则关联，否则新建
        3) 产品图表（hh_emergency_product）：支持同时入 forecast / observation 图片
        4) 站点快照（hh_emergency_station_snapshot）：保存 JSON 文件路径与 JSONB
        """
        level = (event_level or "").strip().upper()
        if level not in {"I", "II", "III", "IV"}:
            raise BusinessException("event_level 必须是 I/II/III/IV")
        if response_window_hours <= 0:
            raise BusinessException("response_window_hours 必须大于 0")
        dt = _parse_emergency_time(forecast_time)
        cycle_key_val = cycle_key or f"{event_type}:{dt.strftime('%Y%m%d%H%M%S')}"
        title = event_title.strip() if event_title else f"{event_type}-{level}级应急事件"
        products = []
        if product_images_json:
            try:
                products = json.loads(product_images_json)
                if not isinstance(products, list):
                    raise BusinessException("product_images_json 必须是 JSON 数组字符串")
            except json.JSONDecodeError as e:
                raise BusinessException(f"product_images_json 不是合法 JSON: {e}")
        station_obj = None
        if station_distribution_json:
            try:
                station_obj = json.loads(station_distribution_json)
            except json.JSONDecodeError as e:
                raise BusinessException(f"station_distribution_json 不是合法 JSON: {e}")

        schema, _ = _get_pg_schema_and_srid()
        result: dict[str, Any] = {"schema": schema}
        with _get_pg_conn() as conn:
            _ensure_emergency_tables(conn)
            cycle_row = _upsert_forecast_cycle(
                conn=conn,
                cycle_key=cycle_key_val,
                forecast_time=dt,
                source_kind=source_kind,
                trigger_id=trigger_id,
                ext={"input_forecast_time": forecast_time},
            )
            event_row, created = _find_or_create_event(
                conn=conn,
                event_code=event_code,
                event_type=event_type,
                event_level=level,
                title=title,
                cycle_id=cycle_row["id"],
                start_time=dt,
                status=event_status,
                zone_code=zone_code,
                city_name=city_name,
                response_window_hours=response_window_hours,
                ext={
                    "source_kind": source_kind,
                    "trigger_id": trigger_id,
                    "zone_code": (zone_code or "").strip() or None,
                    "city_name": (city_name or "").strip() or None,
                },
            )

            inserted_products = []
            if products:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    for p in products:
                        if not isinstance(p, dict):
                            continue
                        p_uri = str(p.get("uri") or p.get("path") or "").strip()
                        if not p_uri:
                            continue
                        p_type = str(p.get("product_type") or "unknown")
                        p_source_kind = str(p.get("source_kind") or source_kind)
                        p_name = p.get("product_name")
                        p_time_text = p.get("product_time")
                        p_time = _parse_emergency_time(str(p_time_text)) if p_time_text else None
                        p_ext = p.get("ext") if isinstance(p.get("ext"), dict) else {}
                        cur.execute(
                            f"""
                            INSERT INTO {schema}.hh_emergency_product
                                (event_id, cycle_id, product_type, source_kind, product_name, product_uri, product_time, ext)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                            ON CONFLICT (event_id, cycle_id, product_type, source_kind, product_uri)
                            DO UPDATE SET product_name=EXCLUDED.product_name, product_time=EXCLUDED.product_time, ext=EXCLUDED.ext
                            RETURNING id, event_id, cycle_id, product_type, source_kind, product_name, product_uri, product_time, ext;
                            """,
                            (
                                event_row["id"],
                                cycle_row["id"],
                                p_type,
                                p_source_kind,
                                p_name,
                                p_uri,
                                p_time,
                                json.dumps(p_ext, ensure_ascii=False),
                            ),
                        )
                        inserted_products.append(dict(cur.fetchone()))

            station_snapshot = None
            if station_obj is not None:
                os.makedirs(station_json_dir, exist_ok=True)
                file_name = f"{cycle_key_val.replace(':', '_')}_{station_snapshot_kind}.json"
                file_path = os.path.join(station_json_dir, file_name)
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(station_obj, f, ensure_ascii=False, indent=2)
                station_count = 0
                if isinstance(station_obj, dict):
                    if isinstance(station_obj.get("stations"), list):
                        station_count = len(station_obj["stations"])
                    elif isinstance(station_obj.get("features"), list):
                        station_count = len(station_obj["features"])
                elif isinstance(station_obj, list):
                    station_count = len(station_obj)
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        f"""
                        INSERT INTO {schema}.hh_emergency_station_snapshot
                            (event_id, cycle_id, snapshot_kind, station_count, station_json_path, station_data_json, ext)
                        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                        ON CONFLICT (event_id, cycle_id, snapshot_kind)
                        DO UPDATE SET
                            station_count=EXCLUDED.station_count,
                            station_json_path=EXCLUDED.station_json_path,
                            station_data_json=EXCLUDED.station_data_json,
                            ext=EXCLUDED.ext
                        RETURNING id, event_id, cycle_id, snapshot_kind, station_count, station_json_path, created_at;
                        """,
                        (
                            event_row["id"],
                            cycle_row["id"],
                            station_snapshot_kind,
                            station_count,
                            file_path,
                            json.dumps(station_obj, ensure_ascii=False),
                            json.dumps({"source_kind": source_kind}, ensure_ascii=False),
                        ),
                    )
                    station_snapshot = dict(cur.fetchone())

            conn.commit()
            result.update(
                {
                    "ok": True,
                    "event_created": created,
                    "cycle": cycle_row,
                    "event": event_row,
                    "product_count": len(inserted_products),
                    "products": inserted_products,
                    "station_snapshot": station_snapshot,
                }
            )
        return result

    def _geojson_geom_to_plot_polygons(geom_obj: dict) -> list[dict]:
        """
        把 GeoJSON Polygon / MultiPolygon 转成前端绘图更容易消费的结构：
        [
            {
                "outer": [[x, y], ...],
                "holes": [
                    [[x, y], ...],
                    ...
                ]
            },
            ...
        ]
        """
        if not geom_obj:
            return []

        geom_type = geom_obj.get("type")
        coords = geom_obj.get("coordinates", [])
        polygons: list[dict] = []

        if geom_type == "Polygon":
            if coords:
                polygons.append({
                    "outer": coords[0],
                    "holes": coords[1:] if len(coords) > 1 else [],
                })

        elif geom_type == "MultiPolygon":
            for poly in coords:
                if not poly:
                    continue
                polygons.append({
                    "outer": poly[0],
                    "holes": poly[1:] if len(poly) > 1 else [],
                })

        return polygons


    @mcp.tool()
    def get_admin_division_for_plot(
        min_x: float | None = None,
        min_y: float | None = None,
        max_x: float | None = None,
        max_y: float | None = None,
        buffer_deg: float = 0.12,
        simplify_tolerance: float = 0.001,
        max_features: int = 200,
    ) -> list[dict]:
        """
        获取用于前端绘图的行政区划底图，按视域跨度自动选「比例尺」级别（与地图 zoom 一致）：

        - **小比例尺**（视野大、经纬跨度大，远看全流域）：按 **地级市 city_name** 聚合，简化较强、面数少。
        - **中比例尺**：按 **县/区 county_name** 聚合（市辖区、县界在此字段时一并体现）。
        - **大比例尺**（视野小、近看局部）：仍按 **县/区 county_name**，但减小简化容差、提高面数上限，边界更细。

        视域由 min/max_x/y 定义；未传 bbox 时按「全图粗览」处理，默认市级。
        """
        if "postgres" not in config:
            raise BusinessException("config.ini 中缺少 [postgres] 配置段，请先配置数据库连接。")

        if buffer_deg < 0:
            raise BusinessException("buffer_deg 不能为负数")
        if simplify_tolerance < 0:
            raise BusinessException("simplify_tolerance 不能为负数")
        if max_features <= 0:
            raise BusinessException("max_features 必须大于 0")

        pg_conf = config["postgres"]
        schema = pg_conf.get("schema", "public")
        try:
            srid = int(pg_conf.get("srid", "4326"))
        except (TypeError, ValueError):
            srid = 4326

        has_bbox = None not in (min_x, min_y, max_x, max_y)

        group_field = "city_name"
        eff_simplify = float(simplify_tolerance)
        eff_max = int(max_features)
        eff_buffer = float(buffer_deg)
        raw_span: float | None = None

        if has_bbox:
            if min_x >= max_x or min_y >= max_y:
                raise BusinessException("bbox 范围不合法：必须满足 min_x < max_x 且 min_y < max_y")

            raw_span = max(float(max_x) - float(min_x), float(max_y) - float(min_y))

            # 跨度大 ≈ 小比例尺（远看）→ 市；跨度小 ≈ 大比例尺（近看）→ 县/区细边界
            if raw_span >= 1.35:
                group_field = "city_name"
                eff_simplify = max(eff_simplify, 0.0022)
                eff_max = min(eff_max, 95)
                eff_buffer = max(eff_buffer, 0.14)
            elif raw_span >= 0.38:
                group_field = "county_name"
                eff_simplify = max(eff_simplify, 0.00085)
                eff_max = min(max(eff_max, 200), 280)
            else:
                group_field = "county_name"
                eff_simplify = min(eff_simplify, 0.00038)
                eff_max = max(min(eff_max, 450), 280)

            min_x = float(min_x) - eff_buffer
            min_y = float(min_y) - eff_buffer
            max_x = float(max_x) + eff_buffer
            max_y = float(max_y) + eff_buffer

            logger.info(
                "行政区划底图: span_deg=%.4f -> group=%s simplify=%s max_features=%s buffer=%.4f",
                raw_span,
                group_field,
                eff_simplify,
                eff_max,
                eff_buffer,
            )
        else:
            group_field = "city_name"
            eff_simplify = max(eff_simplify, 0.002)
            eff_max = min(eff_max, 85)
            logger.info(
                "行政区划底图: 无 bbox -> group=%s simplify=%s max_features=%s",
                group_field,
                eff_simplify,
                eff_max,
            )

        results: list[dict] = []

        with _get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                params = {
                    "max_features": eff_max,
                    "simplify": eff_simplify,
                }

                # 2. 动态构建 SQL，使用 ST_UnaryUnion(ST_Collect) 进行空间聚合
                if has_bbox:
                    params.update({
                        "min_x": min_x,
                        "min_y": min_y,
                        "max_x": max_x,
                        "max_y": max_y,
                    })

                    sql = f"""
                        WITH bbox AS (
                            SELECT ST_MakeEnvelope(
                                %(min_x)s, %(min_y)s, %(max_x)s, %(max_y)s, {srid}
                            ) AS geom
                        )
                        SELECT
                            {group_field} AS name,
                            ST_AsGeoJSON(
                                ST_SimplifyPreserveTopology(
                                    ST_UnaryUnion(ST_Collect(a.geom)), %(simplify)s
                                )
                            ) AS geom_json
                        FROM {schema}.haihe_admin_division a
                        CROSS JOIN bbox b
                        WHERE a.geom && b.geom
                          AND ST_Intersects(a.geom, b.geom)
                          AND {group_field} IS NOT NULL 
                          AND {group_field} != ''
                        GROUP BY {group_field}
                        LIMIT %(max_features)s
                    """
                    cur.execute(sql, params)
                else:
                    sql = f"""
                        SELECT
                            {group_field} AS name,
                            ST_AsGeoJSON(
                                ST_SimplifyPreserveTopology(
                                    ST_UnaryUnion(ST_Collect(a.geom)), %(simplify)s
                                )
                            ) AS geom_json
                        FROM {schema}.haihe_admin_division a
                        WHERE {group_field} IS NOT NULL AND {group_field} != ''
                        GROUP BY {group_field}
                        LIMIT %(max_features)s
                    """
                    cur.execute(sql, params)

                rows = cur.fetchall()

                # 3. 解析结果封装返回
                for row in rows:
                    geom_json = row.get("geom_json")
                    if not geom_json:
                        continue

                    try:
                        geom_obj = json.loads(geom_json)
                    except Exception:
                        continue

                    polygons = _geojson_geom_to_plot_polygons(geom_obj)
                    if not polygons:
                        continue

                    results.append({
                        "name": row.get("name"),
                        "polygons": polygons,
                    })

        return results

    @mcp.tool()
    def locate_river_regions(
        river_name: str,
        buffer_km: float = 0.0,
    ) -> dict:
        """
        根据河流名称，直接从 PostGIS 河流表中查询其所在的行政区划和各类分区。
        """
        return _locate_river_regions_core(river_name=river_name, buffer_km=buffer_km)

    @mcp.tool()
    def locate_region_rivers(
        region_name: str,
        max_rivers: int = 200,
    ) -> dict:
        """
        根据行政区划名称或 adcode 反查相关河流。
        """
        return _locate_region_rivers_core(region_name=region_name, max_rivers=max_rivers)

    def _build_admin_region_match_cte(schema: str) -> str:
        return f"""
            WITH input_regions AS (
                SELECT DISTINCT NULLIF(TRIM(unnest(%(region_names)s::text[])), '') AS query_region_name
            ),
            region_units AS (
                SELECT
                    ir.query_region_name,
                    a.id,
                    a.adcode,
                    a.name,
                    a.city_name,
                    a.county_name,
                    a.geom
                FROM input_regions ir
                JOIN {schema}.haihe_admin_division a
                  ON ir.query_region_name IS NOT NULL
                 AND (
                    COALESCE(a.adcode::text, '') = ir.query_region_name
                    OR COALESCE(a.name, '') ILIKE ir.query_region_name
                    OR COALESCE(a.city_name, '') ILIKE ir.query_region_name
                    OR COALESCE(a.county_name, '') ILIKE ir.query_region_name
                    OR COALESCE(a.name, '') ILIKE ('%%' || ir.query_region_name || '%%')
                    OR COALESCE(a.city_name, '') ILIKE ('%%' || ir.query_region_name || '%%')
                    OR COALESCE(a.county_name, '') ILIKE ('%%' || ir.query_region_name || '%%')
                 )
            ),
            region_match AS (
                SELECT
                    query_region_name,
                    ST_UnaryUnion(ST_Collect(geom)) AS geom
                FROM region_units
                GROUP BY query_region_name
            )
        """

    def _locate_region_rivers_batch_core(
        region_names: list[str],
        max_rivers: int = 200,
    ) -> dict[str, dict]:
        """
        批量根据行政区划反查河流。
        """
        if max_rivers <= 0:
            raise BusinessException("max_rivers 必须大于 0")
        if "postgres" not in config:
            raise BusinessException("config.ini 中缺少 [postgres] 配置段，请先配置数据库连接。")

        cleaned_names: list[str] = []
        seen_names: set[str] = set()
        for name in region_names:
            if not isinstance(name, str):
                continue
            normalized = name.strip()
            if not normalized or normalized in seen_names:
                continue
            seen_names.add(normalized)
            cleaned_names.append(normalized)

        if not cleaned_names:
            return {}

        pg_conf = config["postgres"]
        schema = pg_conf.get("schema", "public")
        river_table = pg_conf.get("river_table", "haihe_river_directed_simple_v2").strip() or "haihe_river_directed_simple_v2"

        result_map: dict[str, dict] = {
            name: {
                "region_name": name,
                "matched_admin_regions": [],
                "rivers": [],
                "max_rivers": max_rivers,
            }
            for name in cleaned_names
        }
        admin_seen: dict[str, set] = {name: set() for name in cleaned_names}
        params = {"region_names": cleaned_names}
        match_cte = _build_admin_region_match_cte(schema)

        with _get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    f"""
                    {match_cte}
                    SELECT query_region_name
                    FROM region_match
                    ORDER BY query_region_name
                    """,
                    params,
                )
                found_names = {row["query_region_name"] for row in cur.fetchall()}

                for name in cleaned_names:
                    if name not in found_names:
                        result_map[name]["error"] = f"Region not found: {name}"

                if found_names:
                    cur.execute(
                        f"""
                        {match_cte}
                        SELECT
                            query_region_name,
                            id,
                            adcode,
                            name,
                            city_name,
                            county_name
                        FROM region_units
                        ORDER BY query_region_name, id
                        """,
                        params,
                    )
                    for row in cur.fetchall():
                        region_name = row["query_region_name"]
                        if region_name not in result_map:
                            continue
                        admin_id = row["id"]
                        if admin_id in admin_seen[region_name]:
                            continue
                        admin_seen[region_name].add(admin_id)
                        result_map[region_name]["matched_admin_regions"].append(
                            {
                                "id": admin_id,
                                "adcode": row.get("adcode"),
                                "name": row.get("name"),
                                "city_name": row.get("city_name"),
                                "county_name": row.get("county_name"),
                            }
                        )

                    cur.execute(
                        f"""
                        {match_cte}
                        SELECT
                            region_name,
                            river_name,
                            src_name,
                            segment_count
                        FROM (
                            SELECT
                                rm.query_region_name AS region_name,
                                COALESCE(NULLIF(TRIM(r.river_name), ''), NULLIF(TRIM(r.src_name), '')) AS river_name,
                                MIN(NULLIF(TRIM(r.src_name), '')) AS src_name,
                                COUNT(*) AS segment_count,
                                ROW_NUMBER() OVER (
                                    PARTITION BY rm.query_region_name
                                    ORDER BY COALESCE(NULLIF(TRIM(r.river_name), ''), NULLIF(TRIM(r.src_name), ''))
                                ) AS rn
                            FROM region_match rm
                            JOIN {schema}.{river_table} r
                              ON ST_Intersects(r.geom, rm.geom)
                            GROUP BY
                                rm.query_region_name,
                                COALESCE(NULLIF(TRIM(r.river_name), ''), NULLIF(TRIM(r.src_name), ''))
                        ) t
                        WHERE river_name IS NOT NULL
                          AND rn <= %(max_rivers)s
                        ORDER BY region_name, river_name
                        """,
                        {**params, "max_rivers": int(max_rivers)},
                    )
                    for row in cur.fetchall():
                        region_name = row["region_name"]
                        if region_name not in result_map:
                            continue
                        river_item = {
                            "river_name": row.get("river_name"),
                            "segment_count": int(row.get("segment_count") or 0),
                        }
                        src_name = row.get("src_name")
                        if src_name and src_name != river_item["river_name"]:
                            river_item["src_name"] = src_name
                        result_map[region_name]["rivers"].append(river_item)

        for name, result in result_map.items():
            result["river_count"] = len(result["rivers"])
            result["matched_admin_region_count"] = len(result["matched_admin_regions"])

        return result_map

    def _locate_region_rivers_core(
        region_name: str,
        max_rivers: int = 200,
    ) -> dict:
        """
        根据单个行政区划反查河流。
        """
        if not region_name:
            raise BusinessException("region_name cannot be empty")

        result = _locate_region_rivers_batch_core([region_name], max_rivers=max_rivers).get(region_name.strip())
        if not result:
            raise BusinessException(f"Region not found: {region_name}")
        if result.get("error"):
            raise BusinessException(result["error"])
        return result

    def _build_river_match_cte(schema: str, river_table: str) -> str:
        return f"""
            WITH input_rivers AS (
                SELECT DISTINCT NULLIF(TRIM(unnest(%(river_names)s::text[])), '') AS query_river_name
            ),
            river_match AS (
                SELECT
                    ir.query_river_name,
                    ST_UnaryUnion(ST_Collect(r.geom)) AS geom
                FROM input_rivers ir
                JOIN {schema}.{river_table} r
                  ON ir.query_river_name IS NOT NULL
                 AND (
                    COALESCE(r.river_name, '') ILIKE ir.query_river_name
                    OR COALESCE(r.src_name, '') ILIKE ir.query_river_name
                    OR COALESCE(r.river_name, '') ILIKE ('%%' || ir.query_river_name || '%%')
                    OR COALESCE(r.src_name, '') ILIKE ('%%' || ir.query_river_name || '%%')
                 )
                GROUP BY ir.query_river_name
            )
        """

    def _river_extent_cte(buffer_km: float, data_srid: int) -> str:
        """
        buffer_km>0 时在 Web 墨卡托下按米做 ST_Buffer，再转回数据 SRID，与行政区/分区做 ST_Intersects。
        比 geography 的 ST_DWithin 更易走 GIST，避免对全表做逐点测距（常见 1 分钟+）。
        """
        if buffer_km <= 0:
            return """
            , river_extent AS (
                SELECT query_river_name, geom
                FROM river_match
            )
            """
        sid = int(data_srid)
        return f"""
            , river_extent AS (
                SELECT
                    query_river_name,
                    ST_Transform(
                        ST_Buffer(ST_Transform(geom, 3857), %(buf_m)s),
                        {sid}
                    ) AS geom
                FROM river_match
            )
        """

    def _locate_rivers_regions_batch_core(
        river_names: list[str],
        buffer_km: float = 0.0,
    ) -> dict[str, dict]:
        """
        批量查询多条河流所在的行政区划和各类分区。
        将每条河 7 次往返合并为约 3 次（存在性 + 行政区 + 分区 UNION），显著降低延迟。
        """
        if buffer_km < 0:
            raise BusinessException("buffer_km 不能为负数")
        if "postgres" not in config:
            raise BusinessException("config.ini 中缺少 [postgres] 配置段，请先配置数据库连接。")

        cleaned_names: list[str] = []
        seen_names: set[str] = set()
        for name in river_names:
            if not isinstance(name, str):
                continue
            normalized = name.strip()
            if not normalized or normalized in seen_names:
                continue
            seen_names.add(normalized)
            cleaned_names.append(normalized)

        if not cleaned_names:
            return {}

        if len(cleaned_names) <= 3:
            logger.info(
                "🌊 批量河流定位 n=%s buffer_km=%s rivers=%s",
                len(cleaned_names),
                buffer_km,
                ", ".join(cleaned_names),
            )
        else:
            logger.info(
                "🌊 批量河流定位 n=%s buffer_km=%s (示例: %s...)",
                len(cleaned_names),
                buffer_km,
                ", ".join(cleaned_names[:3]),
            )

        pg_conf = config["postgres"]
        schema = pg_conf.get("schema", "public")
        river_table = pg_conf.get("river_table", "haihe_river_directed_simple_v2").strip() or "haihe_river_directed_simple_v2"

        partition_layers = [
            {"table": "haihe_zone_11", "id_field": "gid", "name_field": "zone_name", "code_field": "zone_code"},
            {"table": "haihe_zone_32", "id_field": "gid", "name_field": "zone_name", "code_field": "zone_code"},
            {"table": "haihe_zone_77", "id_field": "gid", "name_field": "zone_name", "code_field": "zone_code"},
            {"table": "haihe_246_zone", "id_field": "gid", "name_field": "zone_name", "code_field": "zone_code"},
            {"table": "haihe_zone_9", "id_field": "gid", "name_field": "zone_name", "code_field": "zone_code"},
        ]

        result_map: dict[str, dict] = {
            name: {
                "river_name": name,
                "partitions": [],
                "admin_regions": [],
                "used_buffer_km": buffer_km if buffer_km > 0 else 0.0,
            }
            for name in cleaned_names
        }
        admin_seen: dict[str, set] = {name: set() for name in cleaned_names}
        partition_seen: dict[str, set] = {name: set() for name in cleaned_names}

        params = {"river_names": cleaned_names}
        if buffer_km > 0:
            params["buf_m"] = float(buffer_km) * 1000.0

        try:
            data_srid = int(pg_conf.get("srid", "4326"))
        except (TypeError, ValueError):
            data_srid = 4326

        match_cte = _build_river_match_cte(schema, river_table)
        spatial_cte = match_cte.rstrip() + _river_extent_cte(buffer_km, data_srid)
        admin_predicate = "ST_Intersects(a.geom, rm.geom)"
        zone_predicate = "ST_Intersects(z.geom, rm.geom)"

        union_sql_parts = []
        for layer in partition_layers:
            tbl = layer["table"]
            id_field = layer["id_field"]
            name_field = layer["name_field"]
            code_field = layer["code_field"]
            union_sql_parts.append(
                f"""
                SELECT
                    rm.query_river_name AS river_name,
                    '{tbl}' AS table_name,
                    z.{id_field} AS id,
                    z.{name_field} AS zone_name,
                    z.{code_field} AS zone_code
                FROM river_extent rm
                JOIN {schema}.{tbl} z
                  ON {zone_predicate}
                """
            )
        partition_union_sql = "\nUNION ALL\n".join(union_sql_parts)

        with _get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    f"""
                    {match_cte}
                    SELECT query_river_name
                    FROM river_match
                    ORDER BY query_river_name
                    """,
                    params,
                )
                found_names = {row["query_river_name"] for row in cur.fetchall()}

                for name in cleaned_names:
                    if name not in found_names:
                        result_map[name]["error"] = (
                            f"在表 {schema}.{river_table} 中未找到名称为「{name}」的河流。"
                        )

                if found_names:
                    cur.execute(
                        f"""
                        {spatial_cte}
                        SELECT
                            rm.query_river_name AS river_name,
                            a.id,
                            a.adcode,
                            a.name
                        FROM river_extent rm
                        JOIN {schema}.haihe_admin_division a
                          ON {admin_predicate}
                        ORDER BY rm.query_river_name, a.id
                        """,
                        params,
                    )
                    for row in cur.fetchall():
                        rname = row["river_name"]
                        if rname not in result_map:
                            continue
                        admin_id = row["id"]
                        if admin_id in admin_seen[rname]:
                            continue
                        admin_seen[rname].add(admin_id)
                        result_map[rname]["admin_regions"].append(
                            {
                                "id": admin_id,
                                "adcode": row.get("adcode"),
                                "name": row.get("name"),
                            }
                        )

                    cur.execute(
                        f"""
                        {spatial_cte}
                        {partition_union_sql}
                        ORDER BY river_name, table_name, id
                        """,
                        params,
                    )
                    for row in cur.fetchall():
                        rname = row["river_name"]
                        if rname not in result_map:
                            continue
                        partition_key = (row["table_name"], row["id"])
                        if partition_key in partition_seen[rname]:
                            continue
                        partition_seen[rname].add(partition_key)
                        result_map[rname]["partitions"].append(
                            {
                                "table": row["table_name"],
                                "id": row["id"],
                                "zone_name": row.get("zone_name"),
                                "zone_code": row.get("zone_code"),
                            }
                        )

        return result_map

    def _locate_river_regions_core(
        river_name: str,
        buffer_km: float = 0.0,
    ) -> dict:
        """
        根据单条河流名称查询其所在分区和行政区划（底层走批量 SQL，逻辑与原先一致）。
        """
        if not river_name:
            raise BusinessException("river_name 不能为空")

        result = _locate_rivers_regions_batch_core([river_name], buffer_km=buffer_km).get(river_name.strip())
        if not result:
            raise BusinessException(f"未找到名称为「{river_name}」的河流。")
        if result.get("error"):
            raise BusinessException(result["error"])
        return result

    def _get_downstream_impacts_structured(
        river: str,
        attr_name: str = "length_km",
    ) -> list[dict]:
        """
        返回结构化的下游影响结果，不是自然语言。
        """
        import heapq

        G = get_graph()

        river_end_nodes = set(_get_end_nodes_by_river_map().get(river, ()))

        if not river_end_nodes:
            return []

        best_dist: dict = {node: 0.0 for node in river_end_nodes}
        heap: list[tuple[float, object]] = [(0.0, node) for node in river_end_nodes]
        heapq.heapify(heap)

        impact_distances: dict[str, float] = {}

        while heap:
            current_dist, curr_node = heapq.heappop(heap)

            if current_dist > best_dist.get(curr_node, float("inf")):
                continue

            for _u, next_node, attr in G.out_edges(curr_node, data=True):
                r_name = get_edge_river_name(attr)
                edge_len = get_edge_length_km(attr, attr_name=attr_name)

                if r_name and r_name != river:
                    old = impact_distances.get(r_name, float("inf"))
                    if current_dist < old:
                        impact_distances[r_name] = current_dist

                next_dist = current_dist if r_name == river else current_dist + edge_len

                if next_dist < best_dist.get(next_node, float("inf")):
                    best_dist[next_node] = next_dist
                    heapq.heappush(heap, (next_dist, next_node))

        result = []
        for to_river in sorted(impact_distances.keys()):
            result.append({
                "river_name": to_river,
                "impact_distance_km": round(float(impact_distances[to_river]), 3),
            })
        return result

    def _get_location_map_with_cache(river_names: list[str], buffer_km: float) -> dict[str, dict]:
        cached_location_map = _get_cached_river_locations(river_names, buffer_km)
        if cached_location_map is not None:
            logger.info(
                "河流定位命中缓存: rivers=%s buffer_km=%s",
                len(river_names),
                buffer_km,
            )
            return cached_location_map

        location_map = _locate_rivers_regions_batch_core(river_names, buffer_km=buffer_km)
        _set_cached_river_locations(river_names, buffer_km, location_map)
        return location_map

    def _build_downstream_locations(
        selected_items: list[dict],
        location_map: dict[str, dict],
        buffer_km: float,
    ) -> list[dict]:
        downstream_locations = []
        for item in selected_items:
            downstream_name = item["river_name"]
            loc = location_map.get(
                downstream_name,
                {
                    "river_name": downstream_name,
                    "partitions": [],
                    "admin_regions": [],
                    "used_buffer_km": buffer_km,
                    "error": f"未找到名称为「{downstream_name}」的河流。",
                },
            )
            downstream_locations.append({
                "river_name": downstream_name,
                "impact_distance_km": item["impact_distance_km"],
                "admin_regions": loc.get("admin_regions", []),
                "partitions": loc.get("partitions", []),
                **({"error": loc["error"]} if loc.get("error") else {}),
            })
        return downstream_locations

    def _build_downstream_report(downstream_locations: list[dict]) -> list[dict]:
        downstream_report: list[dict] = []
        for dl in downstream_locations:
            admins = []
            for a in dl.get("admin_regions") or []:
                if isinstance(a, dict) and a.get("name"):
                    n = str(a["name"]).strip()
                    if n:
                        admins.append(n)
            downstream_report.append({
                "river_name": dl.get("river_name"),
                "impact_distance_km": dl.get("impact_distance_km"),
                "admin_units": admins,
                "partitions_by_layer": partitions_grouped_for_report(dl.get("partitions")),
            })
        return downstream_report

    def _build_rainstorm_leader_output(
        river_name: str,
        summary: dict,
        downstream_report: list[dict],
        downstream_impacts: list[dict],
        include_downstream: bool,
        max_downstream_rivers: int,
        buffer_km: float,
        leader_table_max_rows: int,
        leader_admin_display_max: int,
        leader_zone246_display_max: int,
    ) -> dict:
        table_cap = (
            len(downstream_report)
            if int(leader_table_max_rows) == 0
            else min(int(leader_table_max_rows), len(downstream_report))
        )
        admin_cap = int(leader_admin_display_max)
        zone_cap = int(leader_zone246_display_max)

        table_rows: list[dict] = []
        for d in downstream_report[:table_cap]:
            dist = float(d.get("impact_distance_km", 0.0) or 0.0)
            time_pack = _impact_time_descriptions(dist)
            avg_hours = (
                time_pack.get("scenarios", {})
                .get("avg", {})
                .get("duration", {})
                .get("hours", 0.0)
            )

            admins = d.get("admin_units") or []
            admin_slice = admins if admin_cap <= 0 else admins[:admin_cap]
            admin_units_sample = "、".join([str(x) for x in admin_slice]) if admins else "-"

            zone_codes_246: list[str] = []
            for layer in d.get("partitions_by_layer") or []:
                if str(layer.get("layer_key", "")).strip() == "haihe_246_zone":
                    zone_codes_246 = [str(x) for x in (layer.get("zone_codes") or []) if str(x).strip()]
                    break
            z_slice = zone_codes_246 if zone_cap <= 0 else zone_codes_246[:zone_cap]
            zone_246_text = "、".join(z_slice) if zone_codes_246 else "-"

            table_rows.append(
                {
                    "river_name": d.get("river_name"),
                    "impact_distance_km": round(dist, 2),
                    "eta_avg_hours": avg_hours,
                    "admin_units_count": len(admins),
                    "admin_units_sample": admin_units_sample,
                    "zone_codes_246": zone_246_text,
                }
            )

        admin_col_label = (
            "涉及行政区(全部)" if admin_cap <= 0 else f"涉及行政区(前{admin_cap}项)"
        )
        zone_col_label = (
            "246分区代码(全部)" if zone_cap <= 0 else f"246分区代码(前{zone_cap}项)"
        )
        meta = {
            "downstream_rivers_analyzed": len(downstream_report),
            "downstream_rivers_ordered_in_graph": len(downstream_impacts) if include_downstream else 0,
            "downstream_cap": max_downstream_rivers,
            "downstream_list_may_be_truncated_by_cap": bool(
                include_downstream and len(downstream_impacts) > max_downstream_rivers
            ),
            "leader_table_max_rows": leader_table_max_rows,
            "leader_table_rows_shown": len(table_rows),
            "leader_table_truncated": len(downstream_report) > len(table_rows),
            "admin_units_column_truncated": admin_cap > 0,
            "zone246_column_truncated": zone_cap > 0,
            "full_lists_in": ["downstream_report", "downstream_locations"],
            "professional_note": (
                "自然语言中的「等」多为概括，不作为枚举完整性依据；"
                "完整行政区与分区请以 downstream_report 中对应河流条目的 "
                "admin_units 与 partitions_by_layer 为准。"
            ),
        }

        return {
            "title": f"{river_name} 风险影响简报",
            "meta": meta,
            "cards": [
                {"label": "本河涉及行政区", "value": summary["self_admin_region_count"]},
                {"label": "本河分区数量", "value": summary["self_partition_count"]},
                {"label": "下游受影响河流", "value": summary["downstream_river_count"]},
                {"label": "查询缓冲(km)", "value": buffer_km},
            ],
            "table": {
                "title": "下游重点河流（TOP）",
                "columns": [
                    {"key": "river_name", "label": "河流"},
                    {"key": "impact_distance_km", "label": "影响距离(km)"},
                    {"key": "eta_avg_hours", "label": "预计到达(小时,均速)"},
                    {"key": "admin_units_count", "label": "涉及行政区数"},
                    {"key": "admin_units_sample", "label": admin_col_label},
                    {"key": "zone_codes_246", "label": zone_col_label},
                ],
                "rows": table_rows,
            },
            "suggestions": [
                "先盯防影响距离最短的前3条下游河流。",
                "优先巡查交汇节点及穿城段。",
                "汇报时建议只展示卡片+表格+河网图。",
            ],
        }

    def _analyze_rainstorm_impact_core(
        river_name: str,
        buffer_km: float = 1.0,
        max_downstream_rivers: int = 20,
        include_downstream: bool = False,
        leader_table_max_rows: int = 0,
        leader_admin_display_max: int = 0,
        leader_zone246_display_max: int = 0,
    ) -> dict:
        if not river_name:
            raise BusinessException("river_name 不能为空")
        if buffer_km < 0:
            raise BusinessException("buffer_km 不能为负数")
        if max_downstream_rivers <= 0:
            raise BusinessException("max_downstream_rivers 必须大于 0")
        if leader_table_max_rows < 0:
            raise BusinessException("leader_table_max_rows 不能为负数")

        river_name = river_name.strip()
        if include_downstream:
            downstream_impacts = _get_downstream_impacts_structured(river_name)
            selected_items = downstream_impacts[:max_downstream_rivers]
            all_river_names = [river_name] + [item["river_name"] for item in selected_items]
        else:
            downstream_impacts = []
            selected_items = []
            all_river_names = [river_name]

        location_map = _get_location_map_with_cache(all_river_names, buffer_km)

        self_location = location_map.get(river_name)
        if not self_location:
            raise BusinessException(f"未找到名称为「{river_name}」的河流。")
        if self_location.get("error"):
            raise BusinessException(self_location["error"])

        downstream_locations = _build_downstream_locations(selected_items, location_map, buffer_km)
        self_report = self_location_brief_report(self_location)
        downstream_report = _build_downstream_report(downstream_locations)
        summary = {
            "self_admin_region_count": len(self_location.get("admin_regions", [])),
            "self_partition_count": len(self_location.get("partitions", [])),
            "downstream_river_count": len(downstream_locations),
        }

        key_rivers = [x.get("river_name") for x in selected_items[:3]]
        compact_text = (
            f"{river_name} 下游涉及 {len(downstream_locations)} 条河流，"
            f"重点关注 {', '.join([r for r in key_rivers if r]) or '暂无'}。"
        )
        leader_output = _build_rainstorm_leader_output(
            river_name=river_name,
            summary=summary,
            downstream_report=downstream_report,
            downstream_impacts=downstream_impacts,
            include_downstream=include_downstream,
            max_downstream_rivers=max_downstream_rivers,
            buffer_km=buffer_km,
            leader_table_max_rows=leader_table_max_rows,
            leader_admin_display_max=leader_admin_display_max,
            leader_zone246_display_max=leader_zone246_display_max,
        )

        return {
            "river_name": river_name,
            "buffer_km": buffer_km,
            "include_downstream": include_downstream,
            "self_location": self_location,
            "self_report": self_report,
            "downstream_impacts": selected_items,
            "downstream_locations": downstream_locations,
            "downstream_report": downstream_report,
            "summary": summary,
            "compact_text": compact_text,
            "leader_output": leader_output,
        }

    @mcp.tool()
    def locate_downstream_rivers(
        river_name: str,
        buffer_km: float = 0.0,
        max_rivers: int = 20,
    ) -> dict:
        """
        查询某条河的下游受影响河流，并进一步定位这些河流所在的行政区划和分区。
        对下游河流批量查库，避免每条河单独多次 PostGIS 往返。
        """
        if not river_name:
            raise BusinessException("river_name 不能为空")
        if max_rivers <= 0:
            raise BusinessException("max_rivers 必须大于 0")

        downstream_items = _get_downstream_impacts_structured(river_name)
        selected_items = downstream_items[:max_rivers]
        if not selected_items:
            return {
                "source_river": river_name,
                "downstream_count": 0,
                "downstream_rivers": [],
            }

        downstream_names = [item["river_name"] for item in selected_items]
        location_map = _get_location_map_with_cache(downstream_names, buffer_km=buffer_km)

        results = [
            {
                "river_name": item["river_name"],
                "impact_distance_km": item["impact_distance_km"],
                "locations": location_map.get(
                    item["river_name"],
                    {
                        "river_name": item["river_name"],
                        "partitions": [],
                        "admin_regions": [],
                        "used_buffer_km": buffer_km,
                        "error": f"未找到名称为「{item['river_name']}」的河流。",
                    },
                ),
            }
            for item in selected_items
        ]

        return {
            "source_river": river_name,
            "downstream_count": len(results),
            "downstream_rivers": results,
        }

    @mcp.tool()
    def get_rainstorm_self_context(
        river_name: str,
        buffer_km: float = 1.0,
    ) -> dict:
        """
        暴雨影响拆分工具-1：只分析本河涉及行政区与分区，不做下游链路推演。
        """
        full = _analyze_rainstorm_impact_core(
            river_name=river_name,
            buffer_km=buffer_km,
            include_downstream=False,
        )
        return {
            "river_name": full["river_name"],
            "buffer_km": full["buffer_km"],
            "self_location": full["self_location"],
            "self_report": full["self_report"],
            "summary": full["summary"],
        }

    @mcp.tool()
    def get_rainstorm_downstream_context(
        river_name: str,
        buffer_km: float = 1.0,
        max_downstream_rivers: int = 20,
    ) -> dict:
        """
        暴雨影响拆分工具-2：只分析下游受影响河流及其行政区/分区。
        """
        full = _analyze_rainstorm_impact_core(
            river_name=river_name,
            buffer_km=buffer_km,
            max_downstream_rivers=max_downstream_rivers,
            include_downstream=True,
        )
        return {
            "river_name": full["river_name"],
            "buffer_km": full["buffer_km"],
            "downstream_impacts": full["downstream_impacts"],
            "downstream_locations": full["downstream_locations"],
            "downstream_report": full["downstream_report"],
            "summary": full["summary"],
            "compact_text": full["compact_text"],
        }

    @mcp.tool()
    def get_rainstorm_leader_view(
        river_name: str,
        buffer_km: float = 1.0,
        include_downstream: bool = True,
        max_downstream_rivers: int = 20,
        leader_table_max_rows: int = 0,
        leader_admin_display_max: int = 0,
        leader_zone246_display_max: int = 0,
    ) -> dict:
        """
        暴雨影响拆分工具-3：输出领导视图卡片+表格，可按需是否包含下游链路。
        """
        full = _analyze_rainstorm_impact_core(
            river_name=river_name,
            buffer_km=buffer_km,
            max_downstream_rivers=max_downstream_rivers,
            include_downstream=include_downstream,
            leader_table_max_rows=leader_table_max_rows,
            leader_admin_display_max=leader_admin_display_max,
            leader_zone246_display_max=leader_zone246_display_max,
        )
        return {
            "river_name": full["river_name"],
            "buffer_km": full["buffer_km"],
            "include_downstream": full["include_downstream"],
            "summary": full["summary"],
            "compact_text": full["compact_text"],
            "leader_output": full["leader_output"],
        }

    @mcp.tool()
    def analyze_rainstorm_impact(
        river_name: str,
        buffer_km: float = 1.0,
        max_downstream_rivers: int = 20,
        include_downstream: bool = False,
        leader_table_max_rows: int = 0,
        leader_admin_display_max: int = 0,
        leader_zone246_display_max: int = 0,
    ) -> dict:
        """
        综合分析某条河发生暴雨时的影响。

        默认 **只分析本河**（include_downstream=False）：涉及的行政区划与各类分区。
        仅当用户明确提到下游、汇入干流、波及其它河流、连锁影响等时，再将 include_downstream 设为 True。

        返回中的 self_report / downstream_report 已按图层（246、11、32、77、9）分组，
        回答时请保留该结构：先逐条列出 admin_units；再按 partitions_by_layer 各 layer_title 分小节
        （246 分区用 zone_codes 写成「区域代码：…」；其它图层用 entries 写「名称（代码）」）。

        leader_table_max_rows：领导视图表格最多行数；传 0 表示展示全部下游河流。
        leader_admin_display_max：表格「行政区」列最多展示几条；传 0 表示该列展示全部行政区（专业人士核对用）。
        leader_zone246_display_max：表格「246分区代码」列最多展示几个；传 0 表示该列展示该河全部 246 代码。
        """
        result = _analyze_rainstorm_impact_core(
            river_name=river_name,
            buffer_km=buffer_km,
            max_downstream_rivers=max_downstream_rivers,
            include_downstream=include_downstream,
            leader_table_max_rows=leader_table_max_rows,
            leader_admin_display_max=leader_admin_display_max,
            leader_zone246_display_max=leader_zone246_display_max,
        )
        result["deprecated_note"] = (
            "建议迁移到拆分工具链：get_rainstorm_self_context / "
            "get_rainstorm_downstream_context / get_rainstorm_leader_view。"
        )
        return result

    @mcp.tool()
    def fetch_emergency_http_scenario(
        route: str,
        query_params_json: str = "{}",
        timeout_sec: int = 120,
        base_url: str | None = None,
        map_render: str | None = None,
    ) -> dict:
        """
        调用 ``emergency_http_server`` 的场景接口（GET），供 GIS / Chainlit 拿 ``map_sql_id`` 或表格。

        默认附带 ``map_render=wms_sql``（可用环境变量 SCENARIO_MAP_RENDER；``map_render`` 传空字符串则拉整包 GeoJSON）。

        Args:
            route: 如 ``/scenario/river/downstream``、``/scenario/emergency/regions``
            query_params_json: JSON 对象字符串，如 ``{"river_name":"永定河","max_rivers":10}`` 或 ``{"times":"20250723080000"}``
            base_url: 默认 ``EMERGENCY_HTTP_BASE`` 或 ``http://127.0.0.1:8080``
        """
        root = (base_url or emergency_http_base_url()).strip().rstrip("/")
        path = str(route or "").strip()
        if not path.startswith("/"):
            path = "/" + path
        try:
            params = json.loads(query_params_json or "{}")
        except Exception as exc:
            raise BusinessException(f"query_params_json 不是合法 JSON: {exc}") from exc
        if not isinstance(params, dict):
            raise BusinessException("query_params_json 必须是 JSON 对象")
        return fetch_scenario_get(root, path, params, timeout_sec=int(timeout_sec), map_render=map_render)

    @mcp.tool()
    def reload_river_graph() -> str:
        get_graph(force_reload=True)
        _invalidate_river_end_nodes_index()
        return "河网图已重新加载"

    @mcp.tool()
    def query_basin_areal_rainfall(
        time_range: str = "",
        hours: int = 24,
        zone_type: str = "9",
    ) -> list[dict]:
        """
        查询各子流域的分区面雨量实况。
        当用户询问"各子流域面雨量对比"、"过去一周哪个河系降雨最多"、"各分区雨量排名"时使用。

        Args:
            time_range: 时间范围，如 "[20260517000000,20260518000000]"，
                        不传则自动取过去N小时（由hours参数决定）
            hours: 查询过去多少小时（默认24，time_range为空时生效）
            zone_type: 分区类型，默认"9"（海河9分区，即海河、北三河、永定河、大清河等9大分区）。
                       可传"11"/"77"/"246"/"32"/"9"等。不指定时默认9分区

        Returns:
            list[dict]: 各分区的面雨量数据，含 zone_name、rainfall 等字段
        """
        from datetime import datetime as _dt
        from utils.TQ_utils import getSevpEleByTimeRangeHistory, statSevpEleByTimeRangeHistory, getSevpEleByTime

        # 自动计算时间范围：结束时刻对齐到上一个整点，避免请求不完整小时数据
        if not time_range:
            now = _dt.now()
            end = now.replace(minute=0, second=0, microsecond=0)
            start = end - timedelta(hours=hours)
            time_range = f"[{start.strftime('%Y%m%d%H%M%S')},{end.strftime('%Y%m%d%H%M%S')}]"

        # 校验时间范围格式
        if not re.match(r'^\[\d{14},\d{14}\]$', time_range):
            return [{"error": f"时间范围格式错误: {time_range}，应为 [YYYYMMDDhhmmss,YYYYMMDDhhmmss]"}]

        raw = None
        rain_field = None

        # 辅助：判断时间范围是否为一整天（同日 00:00 ~ 23:59:59）
        def _is_full_day_range(tr: str) -> bool:
            m = re.match(r'^\[(\d{14}),(\d{14})\]$', tr)
            if not m:
                return False
            start_s, end_s = m.group(1), m.group(2)
            return start_s.endswith("000000") and end_s.endswith("235959") and start_s[:8] == end_s[:8]

        def _day_from_range(tr: str) -> str:
            return re.match(r'^\[(\d{14}),(\d{14})\]$', tr).group(1)[:8]

        # 单日查询优先尝试 08:00 / 20:00 单时次的 V_RAIN_24H
        if _is_full_day_range(time_range):
            day = _day_from_range(time_range)
            for hh in ["08", "20"]:
                if raw:
                    break
                try:
                    single_time = f"{day}{hh}0000"
                    raw = getSevpEleByTime(
                        times=single_time,
                        elements="Datetime,V_AREA_ID,V_RAIN_24H",
                    )
                    if raw:
                        rain_field = "V_RAIN_24H"
                        print(f"[query_basin_areal_rainfall] 单日单时次 {single_time} V_RAIN_24H 成功，返回 {len(raw)} 条")
                except Exception as e:
                    print(f"[query_basin_areal_rainfall] 单日单时次 {day}{hh}0000 V_RAIN_24H 失败：{e}")

        # 尝试 1：24 小时累计字段（按时间段）
        if not raw:
            try:
                raw = getSevpEleByTimeRangeHistory(
                    time_range=time_range,
                    elements="Datetime,V_AREA_ID,V_RAIN_24H",
                )
                if raw:
                    rain_field = "V_RAIN_24H"
                    print(f"[query_basin_areal_rainfall] V_RAIN_24H 成功，返回 {len(raw)} 条")
            except Exception as e:
                print(f"[query_basin_areal_rainfall] V_RAIN_24H 失败：{e}")

        # 尝试 2：逐小时字段本地累加
        if not raw:
            try:
                raw = getSevpEleByTimeRangeHistory(
                    time_range=time_range,
                    elements="Datetime,V_AREA_ID,V_RAIN_1H",
                )
                if raw:
                    rain_field = "V_RAIN_1H"
                    print(f"[query_basin_areal_rainfall] V_RAIN_1H 成功，返回 {len(raw)} 条")
            except Exception as e:
                print(f"[query_basin_areal_rainfall] V_RAIN_1H 失败：{e}")

        # 尝试 3：统计接口 SUM_V_RAIN_1H
        if not raw:
            try:
                raw = statSevpEleByTimeRangeHistory(
                    time_range=time_range,
                    elements="V_AREA_ID,Datetime",
                )
                if raw:
                    rain_field = "SUM_V_RAIN_1H"
                    print(f"[query_basin_areal_rainfall] SUM_V_RAIN_1H 成功，返回 {len(raw)} 条")
            except Exception as e:
                print(f"[query_basin_areal_rainfall] SUM_V_RAIN_1H 失败：{e}")

        # Fallback：天擎面雨量资料无数据时，用站点降雨数据 + 分区表聚合
        if not raw:
            try:
                pg_conf = config["postgres"]
                raw = _aggregate_areal_rainfall_from_stations(time_range, zone_type, pg_conf)
                if raw:
                    rain_field = "STATION_AGG"
                    print(f"[query_basin_areal_rainfall] 站点聚合面雨量成功，返回 {len(raw)} 条")
            except Exception as e:
                print(f"[query_basin_areal_rainfall] 站点聚合面雨量失败：{e}")

        if not raw or not isinstance(raw, list):
            return [{"error": "面雨量无数据"}]

        # 站点聚合结果已经是最终分区聚合，直接返回
        if rain_field == "STATION_AGG":
            return raw

        # —— 预加载分区名 ——
        zone_tables = {
            "11": "haihe_zone_11",
            "77": "haihe_zone_77",
            "246": "haihe_246_zone",
            "32": "haihe_zone_32",
            "9": "haihe_zone_9",
        }
        zone_cache_loaded = False
        zone_cache = {}
        try:
            pg_conf = config["postgres"]
            ct = pg_conf.get("connect_timeout", "5")
            timeout = int(ct) if ct.strip().isdigit() else 5
            with psycopg2.connect(
                host=pg_conf["host"], port=pg_conf["port"],
                dbname=pg_conf["dbname"], user=pg_conf["user"],
                password=pg_conf["password"],
                sslmode=pg_conf.get("sslmode", "prefer"),
                connect_timeout=timeout,
            ) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    for tn in zone_tables.values():
                        cur.execute(f"SELECT zone_code, zone_name FROM {tn} WHERE zone_name IS NOT NULL")
                        for row in cur.fetchall():
                            code = str(row["zone_code"]).strip()
                            name = row["zone_name"]
                            zone_cache[code] = name
                            # 纯数字别名（取最后一段）
                            num_part = code.split("_")[-1]
                            if num_part.isdigit():
                                zone_cache[num_part] = name
                            # 去零别名
                            if num_part.isdigit():
                                zone_cache[str(int(num_part))] = name
            zone_cache_loaded = True
        except Exception:
            pass

        # 按分区聚合 + 过滤无效值
        from collections import defaultdict
        area_groups = defaultdict(list)
        max_valid_rain = 9999
        for item in raw:
            area_id = str(item.get("V_AREA_ID", "")).strip()
            rain_val = item.get(rain_field)
            try:
                rain_val = float(rain_val) if rain_val not in (None, "", "None") else 0.0
                if rain_val > max_valid_rain or rain_val < 0:
                    continue  # 过滤无效值
            except (ValueError, TypeError):
                continue
            area_groups[area_id].append(rain_val)

        result = []
        for area_id, values in area_groups.items():
            if rain_field in ("V_RAIN_1H", "SUM_V_RAIN_1H"):
                # 逐小时数据：累加为时段总量，最大值为最大小时面雨量
                avg_rain = sum(values)
                max_rain = max(values) if values else 0
            else:
                # 24 小时累计数据：取平均与最大（兼容多时刻返回值）
                avg_rain = sum(values) / len(values) if values else 0
                max_rain = max(values) if values else 0
            # —— 分区名称匹配 ——
            zone_name = None
            if zone_cache_loaded:
                zone_name = zone_cache.get(area_id)
                # 短码补零匹配
                if not zone_name:
                    for pad in range(2, 6):
                        zn = zone_cache.get(area_id.zfill(pad))
                        if zn:
                            zone_name = zn
                            break
                # 纯整数（去前导零）
                if not zone_name and area_id.lstrip("0").isdigit():
                    zone_name = zone_cache.get(str(int(area_id)))
            # 兜底：回查数据库（按分区类型重的表）
            if not zone_name and zone_type in zone_tables:
                try:
                    tn = zone_tables[zone_type]
                    pg_conf = config["postgres"]
                    ct = pg_conf.get("connect_timeout", "5")
                    timeout = int(ct) if ct.strip().isdigit() else 5
                    with psycopg2.connect(
                        host=pg_conf["host"], port=pg_conf["port"],
                        dbname=pg_conf["dbname"], user=pg_conf["user"],
                        password=pg_conf["password"],
                        sslmode=pg_conf.get("sslmode", "prefer"),
                        connect_timeout=timeout,
                    ) as conn:
                        with conn.cursor() as cur:
                            # 取 zone_code 末尾纯数字段匹配（兼容 "11_447" → "447"）
                            cur.execute(
                                f"SELECT zone_name FROM {tn} "
                                f"WHERE zone_code::text = %s"
                                f"   OR SPLIT_PART(zone_code::text, '_', 2) = %s"
                                f" LIMIT 1",
                                (area_id, str(int(area_id))),
                            )
                            row = cur.fetchone()
                            if row:
                                zone_name = str(row[0])
                except Exception:
                    pass
            if not zone_name:
                zone_name = f"分区{area_id}"
            result.append({
                "zone_id": area_id,
                "zone_name": zone_name,
                "avg_rainfall_mm": round(avg_rain, 2),
                "max_rainfall_mm": round(max_rain, 2),
                "record_count": len(values),
            })

        result.sort(key=lambda x: x["avg_rainfall_mm"], reverse=True)
        return result

    @mcp.tool()
    def query_water_level(
        river_name: str = "",
        begin_time: str = "",
        end_time: str = "",
        data_type: str = "river",
    ) -> dict:
        """
        调用十四所接口查询水位数据（河道/水库/堰闸）。
        Args:
            river_name: 河流名称，如"子牙河"（可选，不传则查全部）
            begin_time: 开始时间 "YYYY-MM-DD HH:MM:SS"（不传默认当前-24h）
            end_time: 结束时间 "YYYY-MM-DD HH:MM:SS"（不传默认当前）
            data_type: 数据类型 river(河道) / reservoir(水库) / gate(堰闸)
        Returns:
            dict: 水位数据列表
        """
        import requests as _req
        from datetime import datetime as _dt
        base_url = os.getenv("WATER_LEVEL_API_URL", "http://10.226.107.35:8001")
        now = _dt.now()
        end = end_time or now.strftime("%Y-%m-%d %H:%M:%S")
        begin = begin_time or (now.replace(hour=0, minute=0, second=0, microsecond=0)).strftime("%Y-%m-%d %H:%M:%S")
        area_ids_str = os.getenv("WATER_LEVEL_AREA_IDS", "6,7,8,9,10,11,12,13,14")
        area_ids = [int(x.strip()) for x in area_ids_str.split(",") if x.strip().isdigit()]
        urls = {"river": f"{base_url}/openapi/water_level/river", "reservoir": f"{base_url}/openapi/water_level/reservoir", "gate": f"{base_url}/openapi/water_level/gate"}
        api_url = urls.get(data_type, urls["river"])
        payload = {"areaIds": area_ids, "beginTime": begin, "endTime": end, "sources": ["hwdb"]}
        logger.info(f"[水位查询] {data_type} {api_url}")
        try:
            resp = _req.post(api_url, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            # 统一字段名，让LLM能读懂
            normalized = []
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    n = {
                        "station_name": item.get("stationName") or item.get("station_name", ""),
                        "area_name": item.get("areaName") or item.get("area_name", ""),
                        "area_id": item.get("areaId") or item.get("area_id"),
                        "time": item.get("bjDatetime") or item.get("time", ""),
                        "longitude": item.get("lan") or item.get("longitude"),
                        "latitude": item.get("lat") or item.get("latitude"),
                        "data_source": item.get("source") or item.get("data_source", ""),
                        "data_type": data_type,
                        "raw": item,
                    }
                    # 河道特有
                    if data_type == "river":
                        n["water_level_m"] = item.get("waterLevel")
                        n["warning_level_m"] = item.get("waterWarn")
                        n["flow_rate_m3s"] = item.get("waterFlow")
                        n["水位(m)"] = item.get("waterLevel")
                        n["警戒水位(m)"] = item.get("waterWarn")
                        n["超警戒(m)"] = item.get("overLimit")
                        n["流量(m³/s)"] = item.get("waterFlow")
                        n["涨率"] = item.get("waterRate")
                        n["水势"] = item.get("levelStatus")
                    # 水库特有
                    elif data_type == "reservoir":
                        n["water_level_m"] = item.get("waterStageUp")
                        n["库上水位(m)"] = item.get("waterStageUp")
                        n["库下水位(m)"] = item.get("waterStageDown")
                        n["入库流量(m³/s)"] = item.get("stationFlowIn")
                        n["出库流量(m³/s)"] = item.get("stationFlowOut")
                        n["蓄水量(百万m³)"] = item.get("waterStorage")
                        n["汛限水位(m)"] = item.get("floodLimit")
                        n["水势"] = item.get("stationWaterFlow")
                    # 堰闸特有
                    elif data_type == "gate":
                        n["闸上水位(m)"] = item.get("waterGateUp")
                        n["闸下水位(m)"] = item.get("waterGateDown")
                        n["总过闸流量(m³/s)"] = item.get("waterGateOver")
                        n["闸上水势"] = item.get("gateFlowUp")
                        n["闸下水势"] = item.get("gateFlowDown")
                        n["警戒水位(m)"] = item.get("warnWaterLevel")
                    normalized.append(n)

            if river_name and normalized:
                kw = river_name.strip()
                filtered = [n for n in normalized if kw in n.get("station_name", "") or kw in n.get("area_name", "")]
                return {"data_type": data_type, "river_name": river_name, "count": len(filtered), "records": filtered, "source": "十四所水位接口"}
            return {"data_type": data_type, "count": len(normalized), "records": normalized, "source": "十四所水位接口"}
        except _req.exceptions.Timeout:
            return {"error": "水位服务请求超时", "data_type": data_type}
        except _req.exceptions.ConnectionError:
            return {"error": "无法连接水位服务", "data_type": data_type}
        except Exception as e:
            return {"error": f"水位查询失败: {e}", "data_type": data_type}
    # @mcp.tool()
    # def get_station_history(station_id: str, hours_back: int = 24) -> List[RainfallData]:
    #     """
    #     获取指定气象站点的历史降雨数据
    #
    #     Args:
    #         station_id: 气象站点ID
    #         hours_back: 查询历史小时数，默认24小时
    #     """
    #     return analyzer.get_station_data(station_id, hours_back)
    #
    # @mcp.tool()
    # def query_time_range(start_time: str, end_time: str, station_ids: Optional[List[str]] = None) -> List[RainfallData]:
    #     """
    #     按时间范围查询降雨数据
    #
    #     Args:
    #         start_time: 开始时间 (ISO格式: 2024-01-01T00:00:00)
    #         end_time: 结束时间 (ISO格式: 2024-01-01T23:59:59)
    #         station_ids: 站点ID列表，为空则查询所有站点
    #     """
    #     query = TimeRangeQuery(
    #         start_time=datetime.fromisoformat(start_time.replace('Z', '+00:00')),
    #         end_time=datetime.fromisoformat(end_time.replace('Z', '+00:00')),
    #         station_ids=station_ids
    #     )
    #     return analyzer.query_by_time_range(query)
    #
    # @mcp.tool()
    # def query_nearby_stations(latitude: float, longitude: float, radius_km: float = 10.0) -> List[RainfallData]:
    #     """
    #     查询指定位置附近的气象站点降雨数据
    #
    #     Args:
    #         latitude: 纬度
    #         longitude: 经度
    #         radius_km: 搜索半径(公里)，默认10公里
    #     """
    #     query = LocationQuery(
    #         latitude=latitude,
    #         longitude=longitude,
    #         radius_km=radius_km
    #     )
    #     return analyzer.query_by_location(query)
    #
    # @mcp.tool()
    # def calculate_rainfall_statistics(data: List[RainfallData]) -> StatisticalResult:
    #     """
    #     计算降雨数据的统计信息
    #
    #     Args:
    #         data: 降雨数据列表
    #     """
    #     return analyzer.calculate_statistics(data)
    #
    # @mcp.tool()
    # def analyze_region_rainfall(region_name: str, station_ids: List[str]) -> RegionalAnalysis:
    #     """
    #     对指定区域进行降雨分析
    #
    #     Args:
    #         region_name: 区域名称
    #         station_ids: 该区域包含的站点ID列表
    #     """
    #     return analyzer.analyze_region(region_name, station_ids)
    #
    # @mcp.tool()
    # def get_rainfall_forecast(hours_ahead: int = 24) -> List[ForecastData]:
    #     """
    #     获取未来降雨预报
    #
    #     Args:
    #         hours_ahead: 预报时长(小时)，默认24小时
    #     """
    #     return analyzer.generate_forecast(hours_ahead)
    #
    # @mcp.tool()
    # def check_rainfall_alerts() -> List[AlertInfo]:
    #     """
    #     检查当前降雨预警信息
    #     """
    #     return analyzer.check_alerts()
    #
    # @mcp.tool()
    # def get_available_stations() -> List[Dict[str, Any]]:
    #     """
    #     获取所有可用的气象站点信息
    #     """
    #     return analyzer.stations
