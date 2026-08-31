"""河系降雨预报核心逻辑。

为流域/子流域未来天气问题提供按河系（九分区）聚合的降雨预报数据。
不依赖 FastMCP，便于独立测试。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

from analyzers.RainfallAnalyzer import (
    compute_rainfall_stats_for_geometry as _ra_compute_geometry_stats,
    resolve_forecast_raster_path as _ra_resolve_forecast_raster_path,
)

logger = logging.getLogger(__name__)

ZONE_TABLES = {
    "9": "haihe_zone_9",
    "11": "haihe_zone_11",
    "77": "haihe_zone_77",
    "246": "haihe_246_zone",
    "32": "haihe_zone_32",
}


class RiverSystemForecastError(Exception):
    """河系预报内部错误，不暴露给用户。"""


class _BoundaryLoadError(RiverSystemForecastError):
    """分区边界加载失败。"""


class _ForecastSourceError(RiverSystemForecastError):
    """预报数据源不可用。"""


@dataclass(frozen=True)
class _BoundaryRow:
    """可安全跨请求缓存的不可变边界行；不保存可变 OGR Geometry。"""

    zone_name: str
    zone_code: str
    srid: int
    geom_wkb: bytes


_ZONE_BOUNDARY_CACHE: OrderedDict[tuple, tuple[float, tuple[_BoundaryRow, ...]]] = OrderedDict()
_ZONE_BOUNDARY_CACHE_LOCK = threading.Lock()


def _boundary_cache_ttl_seconds() -> float:
    try:
        value = float(os.environ.get("RIVER_SYSTEM_BOUNDARY_CACHE_TTL", "3600"))
    except (TypeError, ValueError):
        return 3600.0
    return max(0.0, value)


def _boundary_cache_max_size() -> int:
    try:
        value = int(os.environ.get("RIVER_SYSTEM_BOUNDARY_CACHE_MAX_SIZE", "32"))
    except (TypeError, ValueError):
        return 32
    return min(32, max(1, value))


def _clear_zone_boundary_cache() -> None:
    """清空边界缓存，供测试和显式配置重载使用。"""
    with _ZONE_BOUNDARY_CACHE_LOCK:
        _ZONE_BOUNDARY_CACHE.clear()


def _boundary_cache_key(zone_type: str, zone_name: str | None, pg_conf: dict) -> tuple:
    table = ZONE_TABLES.get(str(zone_type), "haihe_zone_9")
    return (
        str(pg_conf.get("host") or ""),
        str(pg_conf.get("port") or ""),
        str(pg_conf.get("dbname") or ""),
        str(pg_conf.get("user") or ""),
        str(pg_conf.get("schema") or ""),
        table,
        str(zone_name or ""),
    )


def _get_cached_boundary_rows(key: tuple, ttl_seconds: float) -> tuple[_BoundaryRow, ...] | None:
    if ttl_seconds <= 0:
        return None
    now = time.monotonic()
    with _ZONE_BOUNDARY_CACHE_LOCK:
        entry = _ZONE_BOUNDARY_CACHE.get(key)
        if entry is None:
            return None
        created_at, rows = entry
        if now - created_at >= ttl_seconds:
            _ZONE_BOUNDARY_CACHE.pop(key, None)
            return None
        _ZONE_BOUNDARY_CACHE.move_to_end(key)
        return rows


def _store_boundary_rows(key: tuple, rows: tuple[_BoundaryRow, ...]) -> None:
    with _ZONE_BOUNDARY_CACHE_LOCK:
        _ZONE_BOUNDARY_CACHE[key] = (time.monotonic(), rows)
        _ZONE_BOUNDARY_CACHE.move_to_end(key)
        while len(_ZONE_BOUNDARY_CACHE) > _boundary_cache_max_size():
            _ZONE_BOUNDARY_CACHE.popitem(last=False)


def _query_zone_boundary_rows(
    zone_type: str,
    zone_name: str | None,
    pg_conf: dict,
) -> tuple[_BoundaryRow, ...]:
    """查询并归一化 PostgreSQL 边界行，不创建 OGR 对象。"""
    table = ZONE_TABLES.get(str(zone_type), "haihe_zone_9")
    connect_timeout = int(pg_conf.get("connect_timeout", "5") or "5")
    try:
        with psycopg2.connect(
            host=pg_conf.get("host"),
            port=pg_conf.get("port"),
            dbname=pg_conf.get("dbname"),
            user=pg_conf.get("user"),
            password=pg_conf.get("password"),
            sslmode=pg_conf.get("sslmode", "prefer"),
            connect_timeout=connect_timeout,
        ) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                sql = f"""
                    SELECT zone_code, zone_name, ST_SRID(geom) AS srid, ST_AsBinary(geom) AS geom_wkb
                    FROM {table}
                    WHERE zone_name IS NOT NULL
                """
                params = []
                if zone_name:
                    sql += " AND zone_name = %s"
                    params.append(zone_name)
                sql += " ORDER BY zone_code"
                cur.execute(sql, params)
                rows = cur.fetchall()
    except Exception as exc:
        logger.exception("加载 %s 分区边界失败", table)
        raise _BoundaryLoadError(f"加载分区边界失败: {exc}") from exc

    if not rows:
        raise _BoundaryLoadError(f"未在 {table} 中找到分区边界数据")

    normalized: list[_BoundaryRow] = []
    for row in rows:
        wkb = row.get("geom_wkb")
        if not wkb:
            continue
        try:
            normalized.append(_BoundaryRow(
                zone_name=str(row.get("zone_name") or "").strip(),
                zone_code=str(row.get("zone_code") or "").strip(),
                srid=int(row.get("srid") or 4326),
                geom_wkb=bytes(wkb),
            ))
        except Exception:
            logger.warning("归一化分区 %s 边界失败", row.get("zone_name"))
            continue

    if not normalized:
        raise _BoundaryLoadError("所有分区边界数据无效")
    return tuple(normalized)


def _materialize_zone_boundaries(rows: tuple[_BoundaryRow, ...]) -> list[dict]:
    """每次请求从 WKB 新建 OGR Geometry，禁止共享可变几何对象。"""
    from osgeo import ogr

    zones = []
    for row in rows:
        try:
            geom = ogr.CreateGeometryFromWkb(row.geom_wkb)
            if geom is None or geom.IsEmpty():
                continue
            zones.append({
                "zone_name": row.zone_name,
                "zone_code": row.zone_code,
                "srid": row.srid,
                "geometry": geom,
            })
        except Exception:
            logger.warning("解析分区 %s 几何失败", row.zone_name)
    if not zones:
        raise _BoundaryLoadError("所有分区边界几何解析失败")
    return zones


def _load_zone_boundaries_from_db(
    zone_type: str,
    zone_name: str | None,
    config: dict,
) -> list[dict]:
    """读取分区边界；缓存不可变 WKB，返回本次请求独立的 OGR Geometry。"""
    pg_conf = config.get("postgres", {})
    if not pg_conf:
        raise _BoundaryLoadError("缺少 PostgreSQL 配置")

    ttl_seconds = _boundary_cache_ttl_seconds()
    key = _boundary_cache_key(zone_type, zone_name, pg_conf)
    rows = _get_cached_boundary_rows(key, ttl_seconds)
    if rows is not None:
        return _materialize_zone_boundaries(rows)

    rows = _query_zone_boundary_rows(zone_type, zone_name, pg_conf)
    # 只有查询成功且至少一个几何可解析时才写缓存；失败/空结果绝不污染缓存。
    zones = _materialize_zone_boundaries(rows)
    if ttl_seconds > 0:
        _store_boundary_rows(key, rows)
    return zones


def _compute_rainfall_stats_for_geometry(
    geometry: Any,
    raster_path: str,
    data_source_label: str | None = None,
    source_srid: int = 4326,
) -> dict:
    """计算指定矢量几何在栅格内的降雨统计量。

    `data_source_label` 保留以保持历史测试接口，实际计算不依赖该值。
    """
    try:
        return _ra_compute_geometry_stats(
            geometry, raster_path, source_srid=source_srid
        )
    except Exception as exc:
        raise _ForecastSourceError(str(exc)) from exc


def _parse_start_time(start_time: str) -> datetime:
    """解析预报起始时间字符串。"""
    formats = ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y%m%d%H%M%S")
    for fmt in formats:
        try:
            return datetime.strptime(start_time.strip(), fmt)
        except Exception:
            continue
    raise ValueError(f"无法解析起始时间: {start_time}")


def _normalize_forecast_hours(value: Any) -> int:
    """归一化预报时长为有效小时数。"""
    try:
        hours = int(value)
    except Exception:
        hours = 24
    return max(1, min(hours, 240))


def _resolve_forecast_file(
    forecast_hours: int,
    start_time: datetime,
    ec_output_path: str,
    *,
    require_full_window: bool = False,
) -> tuple[str | None, str]:
    """根据数据可用性选择滚动预报或 EC AIFS 栅格文件。"""
    options = {"require_full_window": True} if require_full_window else {}
    return _ra_resolve_forecast_raster_path(forecast_hours, start_time, ec_output_path, **options)


# 支流/子河名 → 所属九分区（2026-08-26 领导问题清单标黄："明天泃河有雨吗"）。
# 九分区是面雨量预报的最细聚合粒度；支流问法归并到所属分区统计，并在结果中
# 以 scope_note 注明口径。与 rolling_forecast_service._BASIN_RIVER_NAMES 的河名
# 识别配套（那边负责把支流问法挡在天津区域滚动预报之外）。
_TRIBUTARY_TO_ZONE: dict[str, str] = {
    # 北三河 = 北运河 + 潮白河 + 蓟运河（泃河/州河/还乡河为蓟运河支流）
    "潮白河": "北三河",
    "蓟运河": "北三河",
    "北运河": "北三河",
    "泃河": "北三河",
    "州河": "北三河",
    "还乡河": "北三河",
    # 子牙河水系
    "滹沱河": "子牙河",
    "滏阳河": "子牙河",
    # 漳卫南运河水系
    "漳河": "漳卫南运河",
    "卫河": "漳卫南运河",
}


def _norm_zone_name(name: str) -> str:
    """归一化河系/分区名：去空白并剥"流域/河系/河"后缀，供匹配与 scope_note 同口径比较。"""
    return str(name or "").strip().rstrip("流域").rstrip("河系").rstrip("河")


def _tributary_zone_lookup(name: str) -> str | None:
    """支流名 → 所属九分区；容忍"流域/河系"后缀（"泃河流域"→北三河）。查不到返回 None。"""
    n = str(name or "").strip()
    if not n:
        return None
    if n in _TRIBUTARY_TO_ZONE:
        return _TRIBUTARY_TO_ZONE[n]
    for suf in ("流域", "河系"):
        if n.endswith(suf):
            base = n[: -len(suf)]
            if base in _TRIBUTARY_TO_ZONE:
                return _TRIBUTARY_TO_ZONE[base]
    return None


def tributary_zone_for(river_name: str) -> str | None:
    """支流/子河名 → 所属九分区名；非支流（含九分区本身）返回 None。"""
    return _tributary_zone_lookup(river_name)


def _match_zone_name(river_system: str, zones: list[dict]) -> list[dict]:
    """若用户指定了河系名称，过滤到对应分区；否则返回全部。

    支流/子河名（泃河、潮白河、蓟运河等）先经 _tributary_zone_lookup 归并到所属
    九分区（容忍"流域/河系"后缀）再匹配，避免返回"未找到指定的河系分区数据"。
    """
    raw = str(river_system or "").strip()
    name = _norm_zone_name(_tributary_zone_lookup(raw) or raw)
    if not name or name in ("全", "海河流域", "海河", "海"):
        return zones

    aliases = {
        "大清河": ["大清河"],
        "子牙河": ["子牙河"],
        "永定河": ["永定河"],
        "北三河": ["北三河"],
        "漳卫南运河": ["漳卫南运河", "漳卫南"],
        "徒骇马颊河": ["徒骇马颊河", "徒骇马颊"],
        "黑龙港": ["黑龙港"],
        "滦河": ["滦河"],
        "海河": ["海河"],
    }

    matched = []
    for z in zones:
        zn = _norm_zone_name(z["zone_name"])
        if name == zn:
            matched.append(z)
            continue
        zone_aliases = aliases.get(zn, [zn])
        if any(alias in name or name in alias for alias in zone_aliases):
            matched.append(z)

    return matched if matched else []


def get_river_system_rainfall_forecast(
    river_system: str = "",
    start_time: str = "",
    forecast_hours: int = 24,
    zone_type: str = "9",
    config: dict | None = None,
    ec_output_path: str = "",
    *,
    require_full_window: bool = False,
) -> dict:
    """获取指定河系/流域的未来降雨预报。

    Args:
        river_system: 河系名称，如“大清河”“海河”“全流域”。为空则返回全部分区。
        start_time: 预报起始时间，格式 `YYYY-MM-DD HH:MM:SS`。
        forecast_hours: 预报时长，默认 24，最大 240。
        zone_type: 分区类型，默认 "9"（九分区）。
        config: PostgreSQL 配置字典；为空时尝试读取当前目录 config.ini。
        ec_output_path: EC AIFS 输出根目录，为空时使用 config.ini 中的 paths/ecOutput。

    Returns:
        dict: 含 data_source、fcst_time、forecast_hours、zones；出错时含 error 字段。
    """
    import configparser

    try:
        hours = _normalize_forecast_hours(forecast_hours)
        start_dt = _parse_start_time(start_time)
    except Exception as exc:
        logger.warning("参数解析失败: %s", exc)
        return {"error": "查询参数有误，请确认时间格式和预报时长。"}

    cfg = config or {}
    if not cfg:
        try:
            cp = configparser.ConfigParser()
            cp.read("config.ini", encoding="utf-8-sig")
            cfg = dict(cp)
        except Exception as exc:
            logger.warning("读取 config.ini 失败: %s", exc)

    ec_path = ec_output_path or ""
    if not ec_path and isinstance(cfg, dict):
        paths = cfg.get("paths", {})
        if isinstance(paths, dict):
            ec_path = paths.get("ecOutput", "")

    try:
        zones = _load_zone_boundaries_from_db(zone_type, None, cfg)
        zones = _match_zone_name(river_system, zones)
        if not zones:
            return {"error": "未找到指定的河系分区数据。"}

        # 支流归并所属分区时注明口径，供回答如实说明（如"泃河按所属北三河分区统计"）。
        # zones 已经 _match_zone_name 按同一口径（rstrip 归一化 + 别名包含）过滤到所属分区，
        # 故"请求为支流 且 有命中分区"即成立——直接复用匹配结果，不再二次等值判断
        # （2026-08-26 code-review：裸等值/归一化等值都无法覆盖别名包含命中的场景，
        # 如 zone_name 带"区"后缀，会导致 scope_note 静默缺失）。
        scope_note = ""
        requested = str(river_system or "").strip()
        parent_zone = tributary_zone_for(requested)
        if parent_zone and zones:
            scope_note = f"{requested}按所属{parent_zone}分区统计"

        options = {"require_full_window": True} if require_full_window else {}
        raster_path, data_source_label = _resolve_forecast_file(hours, start_dt, ec_path, **options)
        if not raster_path:
            return {
                "data_source": data_source_label,
                "fcst_time": start_dt.strftime("%Y%m%d%H%M%S"),
                "forecast_hours": hours,
                "zones": [],
                **({"scope_note": scope_note} if scope_note else {}),
            }

        zone_results = []
        for zone in zones:
            try:
                stats = _compute_rainfall_stats_for_geometry(
                    zone["geometry"],
                    raster_path,
                    data_source_label,
                    source_srid=zone.get("srid", 4326),
                )
                zone_results.append({
                    "zone_name": zone["zone_name"],
                    "zone_code": zone.get("zone_code", ""),
                    **stats,
                })
            except Exception as exc:
                logger.warning("计算 %s 分区降雨统计失败: %s", zone.get("zone_name"), exc)
                zone_results.append({
                    "zone_name": zone["zone_name"],
                    "zone_code": zone.get("zone_code", ""),
                    "average_rainfall_mm": None,
                    "max_rainfall_mm": None,
                    "min_rainfall_mm": None,
                })

        return {
            "data_source": data_source_label,
            "fcst_time": start_dt.strftime("%Y%m%d%H%M%S"),
            "forecast_hours": hours,
            "zones": zone_results,
            **({"scope_note": scope_note} if scope_note else {}),
        }
    except Exception as exc:
        logger.exception("河系降雨预报处理失败: %s", exc)
        return {"error": "暂时无法获取河系预报数据，请稍后重试。"}
