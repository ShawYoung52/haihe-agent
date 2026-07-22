"""河系降雨预报核心逻辑。

为流域/子流域未来天气问题提供按河系（九分区）聚合的降雨预报数据。
不依赖 FastMCP，便于独立测试。
"""
from __future__ import annotations

import logging
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


def _load_zone_boundaries_from_db(
    zone_type: str,
    zone_name: str | None,
    config: dict,
) -> list[dict]:
    """从 PostgreSQL 读取分区边界。

    Args:
        zone_type: 分区类型，默认 "9"（九分区）。
        zone_name: 若指定，仅返回匹配的分区；否则返回该类型全部分区。
        config: 包含 postgres 配置的字典。

    Returns:
        list[dict]: 每个元素含 zone_name、zone_code、geometry（ogr.Geometry）。
    """
    from osgeo import ogr

    table = ZONE_TABLES.get(str(zone_type), "haihe_zone_9")
    pg_conf = config.get("postgres", {})
    if not pg_conf:
        raise _BoundaryLoadError("缺少 PostgreSQL 配置")

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

    zones = []
    for row in rows:
        wkb = row.get("geom_wkb")
        if not wkb:
            continue
        try:
            geom = ogr.CreateGeometryFromWkb(wkb)
            if geom is None or geom.IsEmpty():
                continue
            zones.append({
                "zone_name": str(row.get("zone_name") or "").strip(),
                "zone_code": str(row.get("zone_code") or "").strip(),
                "srid": int(row.get("srid") or 4326),
                "geometry": geom,
            })
        except Exception:
            logger.warning("解析分区 %s 几何失败", row.get("zone_name"))
            continue

    if not zones:
        raise _BoundaryLoadError("所有分区边界几何解析失败")

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
) -> tuple[str | None, str]:
    """根据数据可用性选择滚动预报或 EC AIFS 栅格文件。"""
    return _ra_resolve_forecast_raster_path(forecast_hours, start_time, ec_output_path)


def _match_zone_name(river_system: str, zones: list[dict]) -> list[dict]:
    """若用户指定了河系名称，过滤到对应分区；否则返回全部。"""
    name = str(river_system or "").strip().rstrip("流域").rstrip("河系").rstrip("河")
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
        zn = z["zone_name"].rstrip("流域").rstrip("河系").rstrip("河")
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

        raster_path, data_source_label = _resolve_forecast_file(hours, start_dt, ec_path)
        if not raster_path:
            return {
                "data_source": data_source_label,
                "fcst_time": start_dt.strftime("%Y%m%d%H%M%S"),
                "forecast_hours": hours,
                "zones": [],
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
        }
    except Exception as exc:
        logger.exception("河系降雨预报处理失败: %s", exc)
        return {"error": "暂时无法获取河系预报数据，请稍后重试。"}
