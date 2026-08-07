"""POI 周边灾害隐患点查询 MCP 工具。

按经纬度半径（默认 5km）在 3 张静态隐患点表上做 haversine 过滤，
返回周边地质灾害/山洪/中小河流隐患点清单，供点位决策天气回答生成注意事项。

隐患点表为甲方导入内网的静态基础表，无 geom 列，仅含 lon/lat 经纬度，
因此使用 Python haversine 计算距离，不依赖 PostGIS。
数据静态，按表懒加载缓存（TTL 默认 1 小时），缺表不致命、降级为空清单。
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from typing import Any

import psycopg2
from fastmcp import FastMCP
from psycopg2.extras import RealDictCursor

from tools import config

logger = logging.getLogger(__name__)

# 三张隐患点基础表（顺序即返回优先级：地灾 → 山洪 → 中小河流）
_HAZARD_TABLES: list[dict[str, str]] = [
    {
        "key": "dzzh",
        "env": "HAZARD_TABLE_DZZH",
        "default": "t_msis_be_fxyj_dzzh_info",
        "label": "地质灾害",
        "kind": "geologic",
    },
    {
        "key": "sh",
        "env": "HAZARD_TABLE_SH",
        "default": "t_msis_be_fxyj_sh_info",
        "label": "山洪",
        "kind": "mountain",
    },
    {
        "key": "zxhl",
        "env": "HAZARD_TABLE_ZXHL",
        "default": "t_msis_be_fxyj_zxhl_info",
        "label": "中小河流",
        "kind": "river",
    },
]

# 隐患点 schema：为空时使用 config["postgres"].schema（默认 public），可用环境变量覆盖
HAZARD_SCHEMA = os.getenv("HAZARD_SCHEMA", "").strip()
# 隐患点表数据静态，缓存 TTL（秒）
HAZARD_CACHE_TTL = int(os.getenv("HAZARD_CACHE_TTL", "3600"))
# 每类隐患点最多返回条数
MAX_HAZARD_RECORDS = int(os.getenv("MAX_HAZARD_RECORDS", "12"))
# 默认查询半径（公里）
DEFAULT_HAZARD_RADIUS_KM = 5.0

# 懒加载缓存：qualified table name -> (load_ts, rows)；None 表示加载失败
_hazard_rows_cache: dict[str, tuple[float, list[dict]] | None] = {}
_hazard_cache_lock = threading.Lock()


def _safe_float(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        number = float(value)
    except Exception:
        return None
    if not math.isfinite(number):
        return None
    return number


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """计算两点间大地线距离（千米）。"""
    radius_km = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(a))


def _get_postgres_conf() -> dict | None:
    if "postgres" not in config:
        return None
    return config["postgres"]


def _resolve_schema(pg_conf: dict) -> str:
    if HAZARD_SCHEMA:
        return HAZARD_SCHEMA
    return str(pg_conf.get("schema") or "public").strip() or "public"


def _qualified_table(schema: str, table: str) -> str:
    if schema:
        return f"{schema}.{table}".replace("..", ".")
    return table


def _load_hazard_rows(pg_conf: dict, schema: str) -> dict[str, list[dict]]:
    """加载 3 张隐患点表；单表缺失/失败不致命，继续加载其余表。

    每张表的加载结果（成功为行列表，失败为 None）连同时间戳写入缓存，
    失败也在 TTL 内被缓存，避免对缺失表反复重连。返回按 _HAZARD_TABLES
    的 key 分组的规范化行（仅成功加载的表）。
    """
    rows_by_key: dict[str, list[dict]] = {}
    timeout = int(pg_conf.get("connect_timeout", "5")) if str(pg_conf.get("connect_timeout", "5")).isdigit() else 5
    try:
        conn = psycopg2.connect(
            host=pg_conf["host"],
            port=pg_conf["port"],
            dbname=pg_conf["dbname"],
            user=pg_conf["user"],
            password=pg_conf["password"],
            sslmode=pg_conf.get("sslmode", "prefer"),
            connect_timeout=timeout,
        )
    except Exception as exc:
        logger.warning("[poi_hazard_reminder] connect failed: %s", exc)
        raise
    now = time.time()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            for cfg in _HAZARD_TABLES:
                table = os.getenv(cfg["env"], cfg["default"]) or cfg["default"]
                qualified = _qualified_table(schema, table)
                try:
                    cur.execute(
                        f"SELECT id, name, lon, lat, county_name, city_name, status "
                        f"FROM {qualified} WHERE status = 0 OR status IS NULL"
                    )
                    rows = cur.fetchall()
                except Exception as exc:
                    # 单表失败后事务被终止，必须回滚否则后续表查询全部失败
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    logger.warning("[poi_hazard_reminder] table %s load failed: %s", qualified, exc)
                    with _hazard_cache_lock:
                        _hazard_rows_cache[qualified] = (now, None)
                    continue
                normalized: list[dict] = []
                for row in rows or []:
                    lon = _safe_float(row.get("lon"))
                    lat = _safe_float(row.get("lat"))
                    if lon is None or lat is None:
                        continue
                    normalized.append({
                        "id": row.get("id"),
                        "name": str(row.get("name") or "").strip(),
                        "lon": lon,
                        "lat": lat,
                        "county_name": str(row.get("county_name") or "").strip(),
                        "city_name": str(row.get("city_name") or "").strip(),
                        "status": row.get("status"),
                    })
                with _hazard_cache_lock:
                    _hazard_rows_cache[qualified] = (now, normalized)
                rows_by_key[cfg["key"]] = normalized
    finally:
        conn.close()
    return rows_by_key


def _get_cached_hazard_rows(pg_conf: dict, schema: str) -> tuple[dict[str, list[dict]], list[str]]:
    """按表懒加载缓存；返回 (rows_by_key, errors)。

    只要任一表缓存未命中或已过期，就用一条连接整体重载三张表；
    加载结果（含失败）连同时间戳写入缓存，TTL 内不再重连。
    """
    now = time.time()
    need_reload = False
    for cfg in _HAZARD_TABLES:
        table = os.getenv(cfg["env"], cfg["default"]) or cfg["default"]
        qualified = _qualified_table(schema, table)
        with _hazard_cache_lock:
            cached = _hazard_rows_cache.get(qualified)
        if cached is None or (now - cached[0]) >= HAZARD_CACHE_TTL:
            need_reload = True
            break

    errors: list[str] = []
    if need_reload:
        try:
            _load_hazard_rows(pg_conf, schema)
        except Exception as exc:
            errors.append(f"数据库连接失败: {str(exc)[:120]}")

    rows_by_key: dict[str, list[dict]] = {}
    for cfg in _HAZARD_TABLES:
        table = os.getenv(cfg["env"], cfg["default"]) or cfg["default"]
        qualified = _qualified_table(schema, table)
        with _hazard_cache_lock:
            cached = _hazard_rows_cache.get(qualified)
        if cached is not None and cached[1] is not None:
            rows_by_key[cfg["key"]] = cached[1]
        else:
            errors.append(f"{cfg['label']}: 表加载失败或无数据。")
    return rows_by_key, errors


def _error_payload(message: str, reason: str = "", **extra: Any) -> dict:
    payload: dict[str, Any] = {
        "status": "error",
        "query_type": "poi_hazard_reminders",
        "lon": extra.pop("lon", None),
        "lat": extra.pop("lat", None),
        "radius_km": extra.pop("radius_km", None),
        "total_found": 0,
        "categories": [],
        "message": message,
        "debug_reason": reason[:500] if reason else "",
    }
    payload.update(extra)
    return payload


def _no_data_payload(lon: float, lat: float, radius_km: float, reason: str = "") -> dict:
    return {
        "status": "no_data",
        "query_type": "poi_hazard_reminders",
        "lon": lon,
        "lat": lat,
        "radius_km": radius_km,
        "total_found": 0,
        "categories": [],
        "message": f"周边 {radius_km:g} 公里内暂无已知灾害隐患点。",
        "debug_reason": reason[:500] if reason else "",
    }


def _query_poi_hazard_reminders_core(lon: float, lat: float, radius_km: float = DEFAULT_HAZARD_RADIUS_KM) -> dict:
    """查询指定经纬度周边指定半径内的地质灾害/山洪/中小河流隐患点。"""
    if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
        return _error_payload("经纬度必须为数值。", lon=lon, lat=lat, radius_km=radius_km)
    lon_f = float(lon)
    lat_f = float(lat)
    if not (-180 <= lon_f <= 180 and -90 <= lat_f <= 90):
        return _error_payload("经纬度超出有效范围。", lon=lon_f, lat=lat_f, radius_km=radius_km)
    try:
        radius_f = float(radius_km)
    except Exception:
        return _error_payload("查询半径必须为数值。", lon=lon_f, lat=lat_f, radius_km=radius_km)
    if not (0 < radius_f <= 50):
        return _error_payload("查询半径需在 (0, 50] 公里范围内。", lon=lon_f, lat=lat_f, radius_km=radius_f)

    pg_conf = _get_postgres_conf()
    if not pg_conf:
        return _error_payload("PostgreSQL 未配置，无法查询隐患点。", lon=lon_f, lat=lat_f, radius_km=radius_f)

    try:
        schema = _resolve_schema(pg_conf)
        rows_by_key, errors = _get_cached_hazard_rows(pg_conf, schema)
    except Exception as exc:
        logger.warning("[poi_hazard_reminder] load failed: %s", exc)
        return _error_payload("隐患点数据加载失败。", str(exc), lon=lon_f, lat=lat_f, radius_km=radius_f)

    if not rows_by_key:
        reason = "; ".join(errors) if errors else "三张隐患点表均无数据。"
        return _error_payload("隐患点数据不可用。", reason, lon=lon_f, lat=lat_f, radius_km=radius_f)

    categories: list[dict[str, Any]] = []
    total_found = 0
    for cfg in _HAZARD_TABLES:
        rows = rows_by_key.get(cfg["key"]) or []
        hits: list[dict[str, Any]] = []
        for row in rows:
            distance_km = _haversine_km(lon_f, lat_f, row["lon"], row["lat"])
            if distance_km > radius_f:
                continue
            hits.append({
                "name": row["name"] or "未命名隐患点",
                "county": row["county_name"],
                "city": row["city_name"],
                "distance_km": round(distance_km, 1),
            })
        hits.sort(key=lambda x: float(x["distance_km"]))
        if not hits:
            continue
        capped = hits[:MAX_HAZARD_RECORDS]
        categories.append({
            "key": cfg["key"],
            "label": cfg["label"],
            "kind": cfg["kind"],
            "count": len(hits),
            "records": capped,
        })
        total_found += len(hits)

    if total_found <= 0:
        reason = "; ".join(errors) if errors else ""
        return _no_data_payload(lon_f, lat_f, radius_f, reason=reason)

    return {
        "status": "ok",
        "query_type": "poi_hazard_reminders",
        "lon": lon_f,
        "lat": lat_f,
        "radius_km": radius_f,
        "total_found": total_found,
        "categories": categories,
        "message": f"查询到周边 {total_found} 个灾害隐患点。",
        "debug_reason": "; ".join(errors)[:500] if errors else "",
    }


def register_poi_hazard_reminder_tool(mcp: FastMCP) -> None:
    @mcp.tool()
    def query_poi_hazard_reminders(lon: float, lat: float, radius_km: float = DEFAULT_HAZARD_RADIUS_KM) -> dict:
        """查询指定经纬度周边指定半径内的地质灾害/山洪/中小河流隐患点。

        用于点位天气回答时给出周边灾害隐患点提醒。数据来自甲方导入的
        静态隐患点基础表，按 haversine 距离过滤。

        Args:
            lon: 查询点经度。
            lat: 查询点纬度。
            radius_km: 查询半径（公里），默认 5，最大 50。

        Returns:
            dict: status=ok 时 categories 含各类隐患点及距离；no_data 表示半径内无；error 表示查询失败。
        """
        return _query_poi_hazard_reminders_core(lon, lat, radius_km)
