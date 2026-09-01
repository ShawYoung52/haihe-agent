"""河流/河系降雨预报问题的名称与时间窗口解析。"""
from __future__ import annotations

import re
import math
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from constants import RIVER_TABLE_FULL
import river_system_forecast as rsf
import time_source

TIANJIN_TIMEZONE = ZoneInfo("Asia/Shanghai")

KNOWN_RIVER_SYSTEMS: frozenset[str] = frozenset({
    "大清河",
    "子牙河",
    "永定河",
    "北三河",
    "漳卫南运河",
    "徒骇马颊河",
    "黑龙港",
    "滦河",
    "海河",
    "海河流域",
})

_RIVER_CORRIDOR_RE = re.compile(r"([\u4e00-\u9fff]{1,8}?)(?:的)?河道")
_RIVER_NAME_RE = re.compile(r"([\u4e00-\u9fff]{1,8}河)")
_RAIN_OR_WEATHER_PREDICATE_RE = re.compile(r"有雨|下雨|降雨|降水|雨量|天气")
_FUTURE_DAYS_RE = re.compile(r"未来\s*([^\s，。！？?、]{1,8}?)\s*天")
_SUPPORTED_PERIOD_RE = re.compile(
    r"今天晚上|今晚|今天|今日|明天|明日|(?<!大)后天|未来\s*"
    r"(?:[1-9]\d?|[一二两三四五六七八九]|[一二两三四五六七八九]?十[一二两三四五六七八九]?)\s*天"
)
_OTHER_PERIOD_RE = re.compile(
    r"未来|今夜|周(?!边)|星期|礼拜|年|月|小时|钟头|分钟|"
    r"清晨|早上|上午|中午|下午|傍晚|晚上|夜间|夜里|夜晚|凌晨|白天|"
    r"昨天|昨日|前天|近期|最近|接下来|之后|以后|后续|"
    r"\d\s*(?:日|号|点|时|[:：/-])|[0-9一二两三四五六七八九十几]+\s*天"
)
_LEADING_POLITENESS_RE = re.compile(
    r"^(?:(?:请|劳烦|烦请|麻烦)(?:问|教|帮忙)?|(?:想|想要|想请)(?:问|了解|咨询)|(?:咨询|请教))(?:一下|下)?"
)
_LEADING_DATE_RE = re.compile(
    r"^(?:今天晚上|今天|今日|明天|明日|后天|今晚|未来\s*(?:[0-9一二两三四五六七八九十]+)?\s*天?)"
)
_LEADING_TIME_OF_DAY_RE = re.compile(r"^(?:清晨|早上|上午|中午|下午|傍晚|晚上|夜间|夜里|夜晚|凌晨)")
_CHINESE_DIGITS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


@dataclass(frozen=True)
class ForecastPeriod:
    label: str
    target_start: datetime
    target_end: datetime


class RiverNotFoundError(Exception):
    """请求的河流在河网数据中不存在有效几何。"""


class RiverDatabaseError(Exception):
    """加载河流走廊时发生数据库或几何解析错误。"""


@dataclass(frozen=True)
class RiverCorridor:
    """用于降雨预报空间统计的河道缓冲区。"""

    river_name: str
    matched_name: str
    srid: int
    geometry: Any
    buffer_km: float


def load_river_corridor(
    river_name: str,
    pg_conf: dict,
    buffer_km: float = 5.0,
) -> RiverCorridor:
    """从 full_v6 河网加载 WGS84 geography 缓冲后的河道走廊。"""
    requested_name = str(river_name or "").strip()
    schema = str(pg_conf.get("schema", "public") or "public").strip()
    table = str(pg_conf.get("river_table_full", RIVER_TABLE_FULL) or RIVER_TABLE_FULL).strip()
    source_srid = int(
        pg_conf.get("source_srid", pg_conf.get("srid", 4326)) or 4326
    )
    buffer_km = float(buffer_km)
    buffer_m = buffer_km * 1000.0
    statement = sql.SQL(
        """
        WITH candidates AS (
            SELECT river_name, src_name, geom,
                   CASE
                     WHEN river_name = %(river_name)s OR src_name = %(river_name)s THEN 0
                     ELSE 1
                   END AS match_rank
            FROM {}.{}
            WHERE river_name = %(river_name)s
               OR src_name = %(river_name)s
               OR river_name ILIKE %(contains)s
               OR src_name ILIKE %(contains)s
        ), best AS (
            SELECT * FROM candidates
            WHERE match_rank = (SELECT MIN(match_rank) FROM candidates)
        ), merged AS (
            SELECT COALESCE(MIN(NULLIF(river_name, '')), MIN(src_name)) AS matched_name,
                   ST_UnaryUnion(ST_Collect(ST_MakeValid(geom))) AS geom
            FROM best
        )
        SELECT matched_name, 4326 AS srid,
               ST_AsBinary(
                 ST_Buffer(
                   ST_Transform(
                     CASE
                       WHEN ST_SRID(geom) = 0 THEN ST_SetSRID(geom, %(source_srid)s)
                       ELSE geom
                     END,
                     4326
                   )::geography,
                   %(buffer_m)s
                 )::geometry
               ) AS geom_wkb
        FROM merged
        WHERE geom IS NOT NULL;
        """
    ).format(sql.Identifier(schema), sql.Identifier(table))
    connection_kwargs = {
        "host": pg_conf.get("host"),
        "port": pg_conf.get("port"),
        "dbname": pg_conf.get("dbname"),
        "user": pg_conf.get("user"),
        "password": pg_conf.get("password"),
        "sslmode": pg_conf.get("sslmode", "prefer"),
        "connect_timeout": int(pg_conf.get("connect_timeout", "5") or "5"),
    }
    params = {
        "river_name": requested_name,
        "contains": f"%{requested_name}%",
        "source_srid": source_srid,
        "buffer_m": buffer_m,
    }

    try:
        with closing(psycopg2.connect(**connection_kwargs)) as conn:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(statement, params)
                    row = cur.fetchone()
        if not row or not row.get("geom_wkb"):
            raise RiverNotFoundError(f"未找到河流 {requested_name} 的有效河道几何")
        return RiverCorridor(
            river_name=requested_name,
            matched_name=str(row.get("matched_name") or requested_name),
            srid=int(row.get("srid") or 4326),
            geometry=_geometry_from_wkb(bytes(row["geom_wkb"])),
            buffer_km=buffer_km,
        )
    except RiverNotFoundError:
        raise
    except Exception as exc:
        raise RiverDatabaseError(f"加载河道缓冲区失败: {exc}") from exc


def _geometry_from_wkb(value: bytes) -> Any:
    """从 WKB 物化独立的 OGR 几何对象。"""
    from osgeo import ogr

    geometry = ogr.CreateGeometryFromWkb(value)
    if geometry is None or geometry.IsEmpty():
        raise ValueError("河道缓冲区 WKB 无效")
    return geometry


def extract_river_target(user_query: str) -> str:
    """提取已知河系或最接近降雨/天气谓词的具体河名。"""
    query = str(user_query or "").strip()
    for river_system in sorted(KNOWN_RIVER_SYSTEMS, key=len, reverse=True):
        if river_system in query:
            return river_system

    query = _strip_leading_query_modifiers(query)
    corridor_match = _RIVER_CORRIDOR_RE.search(query)
    if corridor_match:
        candidate = _clean_river_candidate(corridor_match.group(1) + "河")
        if candidate:
            return candidate

    for predicate in _RAIN_OR_WEATHER_PREDICATE_RE.finditer(query):
        candidate = _extract_nearest_river(query[:predicate.start()])
        if candidate:
            return candidate

    candidate = _extract_nearest_river(query)
    if candidate:
        return candidate
    raise ValueError("未识别到河流或河系名称")


def resolve_river_forecast_periods(
    user_query: str, now: datetime | None = None
) -> list[ForecastPeriod]:
    """将相对时间解析为北京时间的连续预报窗口。"""
    current = _as_tianjin_time(now) if now is not None else time_source.now(TIANJIN_TIMEZONE)
    query = str(user_query or "")
    future_match = _FUTURE_DAYS_RE.search(query)
    future_days = _parse_day_count(future_match.group(1)) if future_match else None
    # 只接受已实现的单一窗口。先移除完整窗口词，避免“今天晚上”被当成额外“晚上”；
    # 其余明确日期、周/小时、日内时段与混合窗口不得缩成今天或一个自然日。
    if len(_SUPPORTED_PERIOD_RE.findall(query)) != 1 or _OTHER_PERIOD_RE.search(
        _SUPPORTED_PERIOD_RE.sub("", query)
    ):
        raise ValueError("统一河流预报暂不支持该时间表达，请由原有预报工具处理明确时段")

    if "今天晚上" in query or "今晚" in query:
        day_start = datetime.combine(current.date(), time.min, tzinfo=TIANJIN_TIMEZONE)
        evening_start = day_start.replace(hour=18)
        current_hour = current.replace(minute=0, second=0, microsecond=0)
        start = max(evening_start, current_hour)
        return [ForecastPeriod("今天晚上", start, day_start + timedelta(days=1))]

    if future_days is not None:
        today = current.date()
        return [
            _day_period(day, _relative_day_label(day, today))
            for day in (today + timedelta(days=offset) for offset in range(1, future_days + 1))
        ]

    if "后天" in query:
        return [_day_period(current.date() + timedelta(days=2), "后天")]
    if "明天" in query or "明日" in query:
        return [_day_period(current.date() + timedelta(days=1), "明天")]
    return [_day_period(current.date(), "今天")]


def _day_period(day: date, label: str) -> ForecastPeriod:
    start = datetime.combine(day, time.min, tzinfo=TIANJIN_TIMEZONE)
    return ForecastPeriod(label, start, start + timedelta(days=1))


def _relative_day_label(day: date, today: date) -> str:
    """未来第 N 天的业务化时段标签：明天（M月D日）/后天（M月D日）/具体日期（M月D日）。

    明天/后天也带具体日期（2026-09-01 用户口径："带个具体日期也是比较好的"）——
    多天窗口里相对词与绝对日期（M月D日）混排时，带上日期可避免歧义。不用"未来第N天"。
    """
    delta = (day - today).days
    date_str = f"{day.month}月{day.day}日"
    if delta == 1:
        return f"明天（{date_str}）"
    if delta == 2:
        return f"后天（{date_str}）"
    return date_str


def _as_tianjin_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=TIANJIN_TIMEZONE)
    return value.astimezone(TIANJIN_TIMEZONE)


def _parse_day_count(value: str) -> int:
    text = str(value or "").strip()
    if text.isdecimal():
        count = int(text)
    elif text in _CHINESE_DIGITS:
        count = _CHINESE_DIGITS[text]
    elif text.count("十") == 1:
        tens_text, ones_text = text.split("十")
        if tens_text and tens_text not in _CHINESE_DIGITS:
            raise ValueError(f"无法解析未来天数: {text}")
        if ones_text and ones_text not in _CHINESE_DIGITS:
            raise ValueError(f"无法解析未来天数: {text}")
        tens = _CHINESE_DIGITS[tens_text] if tens_text else 1
        ones = _CHINESE_DIGITS[ones_text] if ones_text else 0
        count = tens * 10 + ones
    else:
        raise ValueError(f"无法解析未来天数: {text}")
    if not 1 <= count <= 99:
        raise ValueError(f"无法解析未来天数: {text}")
    return count


def _clean_river_candidate(candidate: str) -> str:
    return re.sub(r"河(?:河|道)+$", "河", candidate)


def _strip_leading_query_modifiers(query: str) -> str:
    """循环去除句首礼貌短语、日期词和日内时段词。"""
    while query:
        for pattern in (
            _LEADING_POLITENESS_RE,
            _LEADING_DATE_RE,
            _LEADING_TIME_OF_DAY_RE,
        ):
            stripped = pattern.sub("", query, count=1)
            if stripped != query:
                query = stripped.lstrip()
                break
        else:
            return query
    return query


def _extract_nearest_river(text: str) -> str | None:
    for match in reversed(list(_RIVER_NAME_RE.finditer(text))):
        candidate = _clean_river_candidate(match.group(1))
        if candidate:
            return candidate
    return None


def query_river_rainfall_forecast_core(
    user_query: str,
    config: dict,
    ec_output_path: str = "",
    now: datetime | None = None,
) -> dict:
    """按河道走廊或九分区河系聚合用户请求的逐时段降雨预报。"""
    try:
        target = extract_river_target(user_query)
        periods = resolve_river_forecast_periods(user_query, now=now)
    except ValueError as exc:
        return {"status": "invalid_request", "message": str(exc), "periods": []}

    if _is_explicit_river_system_request(user_query, target):
        return _query_river_system_periods(target, periods, config, ec_output_path, now=now)

    pg_conf = config.get("postgres", {}) if isinstance(config, dict) else {}
    try:
        corridor = load_river_corridor(target, pg_conf)
    except RiverNotFoundError as exc:
        # 九分区本身直接按分区统计。
        if target in KNOWN_RIVER_SYSTEMS:
            return _query_river_system_periods(target, periods, config, ec_output_path, now=now)
        # 支流/子河（泃河、潮白河、蓟运河等）不在 KNOWN_RIVER_SYSTEMS，但属于某个
        # 九分区水系；走廊未命中时回退所属分区，保留用户所问河名并注明统计口径。
        zone = rsf.tributary_zone_for(target)
        if zone:
            return _query_river_system_periods(
                zone, periods, config, ec_output_path, display_name=target, now=now
            )
        return _query_error("river_not_found", target, str(exc))
    except RiverDatabaseError as exc:
        return _query_error("database_error", target, str(exc))

    return _query_corridor_periods(corridor, periods, ec_output_path, now=now)


def _load_rolling_forecast_service():
    """惰性加载 rolling_forecast_service（避免模块顶层触发重依赖链/循环），供测试 monkeypatch。"""
    import rolling_forecast_service as rfs

    return rfs


def _corridor_representative_point(corridor) -> tuple[float, float] | None:
    """走廊几何质心（4326）作为灾害隐患/风险查询的代表点；几何异常返回 None。

    走廊是河道 + 缓冲的多边形，质心落在多边形内部，可代表该河段的区域位置。
    测试环境无 osgeo 时（dummy 几何）异常被吞掉返回 None，风险附着静默跳过。
    """
    try:
        centroid = corridor.geometry.Centroid()
        if centroid is None or centroid.IsEmpty():
            return None
        return float(centroid.GetX()), float(centroid.GetY())
    except Exception:
        return None


def _river_risk_calendar_window(periods) -> dict:
    """由河流预报时段构造风险起报换算所需的最小日历窗口（复用滚动预报逐日逻辑）。"""
    return {
        "forecast_start_date": periods[0].target_start.date().isoformat(),
        "forecast_days": len(periods),
    }


def _attach_corridor_region_hazards(result: dict, corridor, periods, now=None) -> None:
    """按走廊代表点查灾害隐患+风险等级，附 result["region_hazards"]；失败静默降级。

    2026-09-01 用户口径：河流预报回答要像"天气怎么样"那样带灾害风险。复用滚动预报
    `_query_region_hazards`（含逐日风险起报合并），与区域天气同渲染口径。隐患/风险
    是增强——任何失败都不得阻断降雨回答。九分区路径由 `_attach_system_region_hazards`
    负责（分区边界质心代表点）。
    """
    if not periods:
        return
    point = _corridor_representative_point(corridor)
    if point is None:
        return
    lon, lat = point
    try:
        rfs = _load_rolling_forecast_service()
        risk_fcst_times = rfs._risk_fcst_times_from_window(
            _river_risk_calendar_window(periods), now
        )
        hazards = rfs._query_region_hazards(lon, lat, risk_fcst_times)
    except Exception:
        return
    if not isinstance(hazards, dict) or not hazards:
        return
    result["region_hazards"] = [
        {
            "region": corridor.river_name,
            "region_display": f"{corridor.matched_name}沿线",
            **hazards,
        }
    ]


def _zone_representative_point(target: str, config: dict) -> tuple[float, float] | None:
    """九分区边界质心（4326）作为灾害隐患/风险查询的代表点；不可用返回 None。

    边界经 rsf._load_zone_boundaries_from_db 加载（TTL/LRU 缓存 WKB、每次新建
    OGR Geometry，与九分区降雨统计同一几何来源）。数据库/几何任何异常都返回
    None，风险附着静默跳过——测试环境无 osgeo 时 dummy 几何走同一降级路径。
    """
    try:
        zones = rsf._load_zone_boundaries_from_db("9", target, config)
    except Exception:
        return None
    for zone in zones or []:
        geom = zone.get("geometry") if isinstance(zone, dict) else None
        if geom is None:
            continue
        try:
            centroid = geom.Centroid()
            if centroid is None or centroid.IsEmpty():
                continue
            return float(centroid.GetX()), float(centroid.GetY())
        except Exception:
            continue
    return None


def _attach_system_region_hazards(
    result: dict,
    target: str,
    config: dict,
    periods,
    now=None,
) -> None:
    """九分区路径灾害风险附着：按分区边界质心查隐患+风险等级；失败静默降级。

    2026-09-01 用户口径："九分区这个也得做"——与走廊路径同口径复用滚动预报
    `_query_region_hazards`（含逐日风险起报合并），display 标注"九分区河系"
    让用户知道统计口径。隐患/风险是增强——任何失败都不得阻断降雨回答。
    """
    if not periods:
        return
    point = _zone_representative_point(target, config)
    if point is None:
        return
    lon, lat = point
    try:
        rfs = _load_rolling_forecast_service()
        risk_fcst_times = rfs._risk_fcst_times_from_window(
            _river_risk_calendar_window(periods), now
        )
        hazards = rfs._query_region_hazards(lon, lat, risk_fcst_times)
    except Exception:
        return
    if not isinstance(hazards, dict) or not hazards:
        return
    result["region_hazards"] = [
        {
            "region": target,
            "region_display": f"{target}九分区河系",
            **hazards,
        }
    ]


def _is_explicit_river_system_request(user_query: str, target: str) -> bool:
    """判断请求是否明确要求河系/流域，而非只出现一个河名。"""
    query = str(user_query or "")
    return target in KNOWN_RIVER_SYSTEMS and (
        target.endswith(("流域", "河系"))
        or f"{target}流域" in query
        or f"{target}河系" in query
    )


def _query_corridor_periods(
    corridor: RiverCorridor,
    periods: list[ForecastPeriod],
    ec_output_path: str,
    now: datetime | None = None,
) -> dict:
    period_results = []
    for period in periods:
        hours = _forecast_hours(period)
        try:
            raster_path, data_source = rsf._resolve_forecast_file(
                hours, period.target_start.astimezone(TIANJIN_TIMEZONE).replace(tzinfo=None),
                ec_output_path, require_full_window=True,
            )
        except Exception as exc:
            return _query_error("forecast_unavailable", corridor.river_name, str(exc))
        if not raster_path:
            return _query_error("forecast_unavailable", corridor.river_name, data_source)

        try:
            stats = rsf._compute_rainfall_stats_for_geometry(
                corridor.geometry,
                raster_path,
                data_source,
                source_srid=corridor.srid,
            )
        except Exception as exc:
            return _query_error("calculation_error", corridor.river_name, str(exc))
        period_results.append(_build_corridor_period(period, data_source, stats))

    result = {
        "status": "ok",
        "river_name": corridor.river_name,
        "scope_type": "river_corridor",
        "scope_description": f"{corridor.matched_name}河道两侧约{_format_buffer_km(corridor.buffer_km)}公里沿线范围",
        "buffer_km": corridor.buffer_km,
        "periods": period_results,
    }
    _attach_corridor_region_hazards(result, corridor, periods, now=now)
    return result


def _query_river_system_periods(
    target: str,
    periods: list[ForecastPeriod],
    config: dict,
    ec_output_path: str,
    display_name: str | None = None,
    now: datetime | None = None,
) -> dict:
    """按九分区河系统计各时段降雨。display_name：支流回退时保留用户所问河名。"""
    period_results = []
    for period in periods:
        hours = _forecast_hours(period)
        try:
            forecast = rsf.get_river_system_rainfall_forecast(
                river_system=target,
                start_time=period.target_start.strftime("%Y-%m-%d %H:%M:%S"),
                forecast_hours=hours,
                zone_type="9",
                config=config,
                ec_output_path=ec_output_path,
                require_full_window=True,
            )
        except Exception as exc:
            return _query_error("calculation_error", target, str(exc))

        if not isinstance(forecast, dict):
            return _query_error("system_unavailable", target, "河系预报返回格式无效")
        if forecast.get("error"):
            return _query_error("system_unavailable", target, str(forecast["error"]))

        data_source = str(forecast.get("data_source") or "")
        zones = forecast.get("zones")
        if not isinstance(zones, list):
            return _query_error("system_unavailable", target, "河系预报缺少分区结果")
        if not zones and "无可用预报文件" in data_source:
            return _query_error("forecast_unavailable", target, data_source)
        if not zones:
            return _query_error("system_unavailable", target, "河系预报未返回分区结果")
        period_results.append(_build_system_period(period, data_source, zones))

    if display_name and display_name != target:
        scope_description = f"{display_name}所属{target}九分区河系范围"
    else:
        scope_description = f"{target}九分区河系范围"
    result = {
        "status": "ok",
        "river_name": display_name or target,
        "scope_type": "river_system",
        "scope_description": scope_description,
        "buffer_km": None,
        "periods": period_results,
    }
    _attach_system_region_hazards(result, target, config, periods, now=now)
    return result


def _build_corridor_period(
    period: ForecastPeriod,
    data_source: str,
    stats: dict,
) -> dict:
    valid_count = _as_nonnegative_int(stats.get("valid_count"))
    values = {
        "average_rainfall_mm": stats.get("average_rainfall_mm"),
        "max_rainfall_mm": stats.get("max_rainfall_mm"),
        "min_rainfall_mm": stats.get("min_rainfall_mm"),
    }
    if valid_count == 0:
        has_rain, status = None, "no_coverage"
    elif valid_count is None or not _is_number(values["max_rainfall_mm"]):
        has_rain, status = None, "unknown_coverage"
    else:
        has_rain, status = values["max_rainfall_mm"] > 0, "ok"
    return {
        **_period_metadata(period, data_source),
        "status": status,
        "has_rain": has_rain,
        **values,
        "valid_count": valid_count,
    }


def _build_system_period(
    period: ForecastPeriod,
    data_source: str,
    zones: list[dict],
) -> dict:
    covered_stats = []
    incomplete_zone_count = 0
    confirmed_zero_coverage_count = 0
    for zone in zones:
        raw_valid_count = zone.get("valid_count")
        valid_count = _as_positive_int(raw_valid_count)
        average = zone.get("average_rainfall_mm")
        maximum = zone.get("max_rainfall_mm")
        minimum = zone.get("min_rainfall_mm")
        if valid_count is None:
            if _as_nonnegative_int(raw_valid_count) == 0:
                confirmed_zero_coverage_count += 1
            else:
                incomplete_zone_count += 1
            continue
        if (
            not _is_number(average)
            or not _is_number(maximum)
            or not _is_number(minimum)
        ):
            incomplete_zone_count += 1
            continue
        covered_stats.append((valid_count, average, maximum, minimum))

    valid_count = sum(item[0] for item in covered_stats)
    if not covered_stats:
        has_rain = None
        status = (
            "no_coverage"
            if zones and confirmed_zero_coverage_count == len(zones)
            else "unknown_coverage"
        )
    elif incomplete_zone_count or confirmed_zero_coverage_count:
        has_rain = True if any(item[2] > 0 for item in covered_stats) else None
        status = "partial"
    else:
        has_rain = any(item[2] > 0 for item in covered_stats)
        status = "ok"

    if incomplete_zone_count or confirmed_zero_coverage_count:
        average = maximum = minimum = None
    elif covered_stats:
        average = sum(item[0] * item[1] for item in covered_stats) / valid_count
        maximum = max(item[2] for item in covered_stats)
        minimum = min(item[3] for item in covered_stats)
    else:
        average = maximum = minimum = None
    return {
        **_period_metadata(period, data_source),
        "status": status,
        "has_rain": has_rain,
        "average_rainfall_mm": average,
        "max_rainfall_mm": maximum,
        "min_rainfall_mm": minimum,
        "valid_count": valid_count,
        "zones": zones,
    }


def _period_metadata(period: ForecastPeriod, data_source: str) -> dict:
    return {
        "label": period.label,
        "start_time": period.target_start.isoformat(),
        "end_time": period.target_end.isoformat(),
        "data_source": data_source,
    }


def _query_error(status: str, target: str, message: str) -> dict:
    return {
        "status": status,
        "river_name": target,
        "message": message,
        "error": message,
        "periods": [],
    }


def _forecast_hours(period: ForecastPeriod) -> int:
    return int((period.target_end - period.target_start).total_seconds() // 3600)


def _as_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return result if result >= 0 else None


def _as_positive_int(value: Any) -> int | None:
    result = _as_nonnegative_int(value)
    if result is None:
        return None
    return result if result > 0 else None


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _format_buffer_km(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)
