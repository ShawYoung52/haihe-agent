"""天津滚动预报接口服务。

本模块负责区域解析、日历日时间换算、接口调用、返回值标准化和大暴雨/趋势分析。
MCP 工具定义仍保留在 haihe_mcp_tools.py，只调用本模块的核心函数。
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from zoneinfo import ZoneInfo

import requests

import time_source


TIANJIN_TIMEZONE = ZoneInfo("Asia/Shanghai")

ROLLING_FORECAST_API_URL = os.getenv(
    "ROLLING_FORECAST_API_URL",
    "http://10.226.120.112:8088/tjgrid/gdyb/getGdybDataByParam",
)
ROLLING_FORECAST_TIMEOUT = int(os.getenv("ROLLING_FORECAST_TIMEOUT", "120"))
# 滚动预报数据查询缓存 TTL（秒，默认 10 分钟）。同一起报时次 + 坐标 + 时段
# 的查询在 TTL 内直接返回缓存，避免每次请求都重新打内网接口（该接口实测
# 可达几十秒，是"回答响应慢"的主要来源之一）。天气预报短时间稳定，TTL 内
# 缓存结果可信。
ROLLING_FORECAST_CACHE_TTL = int(os.getenv("ROLLING_FORECAST_CACHE_TTL", "600"))
_rolling_forecast_cache: dict[str, tuple[float, Any]] = {}
ROLLING_FORECAST_ELEMENTS = (
    "WEA",
    "TMAX",
    "TMIN",
    "EDA",
    "RHMAX",
    "RHMIN",
    "TCCMAX",
    "TCCMIN",
    "VISMIN",
    "TP1H",
)
ROLLING_FORECAST_ELEMENT_NAMES = {
    "WEA": "天气现象",
    "TMAX": "最高气温",
    "TMIN": "最低气温",
    "EDA": "风况",
    "RHMAX": "最大相对湿度",
    "RHMIN": "最小相对湿度",
    "TCCMAX": "最大总云量",
    "TCCMIN": "最小总云量",
    "VISMIN": "最小能见度（千米）",
    "TP1H": "时段累计降水量",
}
ROLLING_FORECAST_COORDS = {
    "天津市区": "117.14_39.24",
    "蓟州": "117.45_40.05",
    "宝坻": "117.28_39.73",
    "武清": "117.06_39.43",
    "宁河": "117.85_39.38",
    "静海": "116.92_38.93",
    "北辰": "117.21_39.07",
    "西青": "117.05_39.08",
    "津南": "117.42_38.95",
    "东丽": "117.34_39.08",
    "滨海新区": "117.79_39.16",
}
ROLLING_FORECAST_REGION_ALIASES = {
    "市区": "天津市区",
    "中心城区": "天津市区",
    "蓟州区": "蓟州",
    "宝坻区": "宝坻",
    "武清区": "武清",
    "宁河区": "宁河",
    "静海区": "静海",
    "北辰区": "北辰",
    "西青区": "西青",
    "津南区": "津南",
    "东丽区": "东丽",
    "滨海": "滨海新区",
}
REGION_DISPLAY_NAMES = {
    "天津市区": "天津市区",
    "蓟州": "蓟州区",
    "宝坻": "宝坻区",
    "武清": "武清区",
    "宁河": "宁河区",
    "静海": "静海区",
    "北辰": "北辰区",
    "西青": "西青区",
    "津南": "津南区",
    "东丽": "东丽区",
    "滨海新区": "滨海新区",
}

# 海河流域主要地级市代表坐标（市政府驻地，lon_lat，WGS84）。
# 口径（用户 2026-08-24）："除了天津用滚动预报，其它不都是用数据湖海河流域那个数据吗"——
# 滚动预报网格（数据湖 GRID_TJQX_LYPUB，111-120°E/34-43°N）覆盖整个海河流域；问句点名
# 流域内城市时按该市坐标采样网格，绝不静默退回天津市区代表点（"明天唐山的天气怎么样"
# 曾错回【天津市区灾害风险】表）。注意：文字要素（天气现象/气温/风况）服务端只对天津
# 11 代表站生成，外埠城市只能拿到降水格点 TP1H（2026-08-19 密云探针实锤），前端按
# 降水-only 渲染逐日降水表。所有坐标必须落在网格范围内（有测试锁定）。
BASIN_CITY_COORDS = {
    "北京": "116.41_39.90",
    "唐山": "118.18_39.63",
    "秦皇岛": "119.60_39.94",
    "承德": "117.96_40.95",
    "张家口": "114.89_40.82",
    "保定": "115.46_38.87",
    "沧州": "116.84_38.30",
    "廊坊": "116.68_39.54",
    "衡水": "115.67_37.73",
    "石家庄": "114.51_38.04",
    "邢台": "114.50_37.07",
    "邯郸": "114.54_36.61",
    "太原": "112.55_37.87",
    "大同": "113.30_40.08",
    "朔州": "112.43_39.33",
    "忻州": "112.73_38.42",
    "阳泉": "113.58_37.86",
    "长治": "113.12_36.19",
    "晋城": "112.85_35.49",
    "临汾": "111.52_36.09",
    "运城": "111.01_35.03",
    "吕梁": "111.14_37.52",
    "安阳": "114.39_36.10",
    "鹤壁": "114.30_35.75",
    "新乡": "113.93_35.30",
    "焦作": "113.24_35.22",
    "濮阳": "115.03_35.76",
    "德州": "116.36_37.45",
    "滨州": "117.97_37.38",
    "东营": "118.67_37.43",
}

REGION_DISPLAY_NAMES.update({name: f"{name}市" for name in BASIN_CITY_COORDS})

RAINSTORM_24H_MM = 50.0
SEVERE_RAINSTORM_24H_MM = 100.0
EXTRAORDINARY_RAINSTORM_24H_MM = 250.0
MAX_FORECAST_PERIOD_HOURS = 240
DEFAULT_ROLLING_FORECAST_REGION = "天津市区"

DEFAULT_SEVEN_DAY_QUERY_KEYWORDS = (
    "未来一周",
    "未来七天",
    "未来7天",
    "最近",
    "近期",
    "未来几天",
)
PAST_QUERY_KEYWORDS = (
    "过去",
    "历史",
    "发生过",
    "已发生",
    "已经出现",
    "实况",
)

def _display_region(region: str) -> str:
    return REGION_DISPLAY_NAMES.get(region, region)


# 区域灾害风险查询半径（公里，≤50）。区域天气回答（蓟州/宝坻等）附带【区域】
# 灾害风险表，按区域代表坐标查周边隐患点（地质灾害/山洪/中小河流），数据复用
# custom_tools/poi_hazard_reminder_tool 的三张静态表。风险表是增强——查询失败
# 静默降级（不阻断天气回答），生产默认 25km 覆盖区县级范围。
REGION_HAZARD_RADIUS_KM = float(os.getenv("REGION_HAZARD_RADIUS_KM", "25"))
if not (0 < REGION_HAZARD_RADIUS_KM <= 50):
    REGION_HAZARD_RADIUS_KM = 25.0

# 懒加载的 _query_poi_hazard_reminders_core（避免模块顶层触发 tools.py 的重依赖链）。
_region_hazard_queryer = None


def _load_region_hazard_queryer() -> Any:
    """按文件路径懒加载 poi_hazard_reminder_tool 的查询核心。

    绕开 custom_tools/__init__.py 的重依赖链（networkx/rasterio 等）：生产运行
    时 server.py 已先 import tools.py，模块内 `from tools import config` 命中缓存
    模块；测试环境则需 mock 掉本函数，避免触发 tools.py 顶层 RainfallAnalyzer。
    """
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "poi_hazard_reminder_tool",
        Path(__file__).resolve().parent / "custom_tools" / "poi_hazard_reminder_tool.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._query_poi_hazard_reminders_core


# 懒加载的 risk_warning_tool.query_region_risk_levels（区域天气#8 风险等级）。
_region_risk_level_queryer = None


def _load_region_risk_level_queryer() -> Any:
    """懒加载 risk_warning_tool.query_region_risk_levels。

    生产 server.py 已先 import custom_tools（命中 sys.modules 缓存，不重跑重依赖
    __init__）；测试环境 mock 本函数或 _region_risk_level_queryer 即可，避免触发
    custom_tools/__init__ 的 networkx/rasterio 重依赖链。
    """
    from custom_tools.risk_warning_tool import query_region_risk_levels

    return query_region_risk_levels


def _query_region_risk_levels(
    lon: float, lat: float, fcst_times: list[str] | None = None, *, include_coverage: bool = False
) -> dict | None:
    """查询区域代表点半径内各灾种风险等级分布（风险接口 level，一~四级）。

    返回 {hazard_key: {...}} / {}（可达无风险）/ None（接口失败）。任何异常静默降级
    返回 None——风险等级是增强，绝不阻断天气回答。fcst_times：跨日窗口（如"未来三天"）
    传各日 08:00 起报时次列表，逐日调用并把等级统计合并；None=单次最近起报时次。
    """
    global _region_risk_level_queryer
    try:
        if _region_risk_level_queryer is None:
            _region_risk_level_queryer = _load_region_risk_level_queryer()
        options = {"include_coverage": True} if include_coverage else {}
        return _region_risk_level_queryer(
            float(lon), float(lat), REGION_HAZARD_RADIUS_KM, fcst_times, **options
        )
    except Exception as exc:
        print(f"[region_risk_levels] query failed: {exc}", flush=True)
        return None


# 风险接口每个起报时次覆盖 24h（08→次日 08、20→次日 20）。多日天气查询
# 必须按目标日逐日起报查询并合并，不能因窗口超过 24h 就跳过接口、伪造 no_data。
RISK_FCST_MAX_DAYS = int(os.getenv("RISK_FCST_MAX_DAYS", "3"))

# 区域综合风险固定输出这三类。即使静态隐患点或风险接口缺失，调用方也能
# 稳定渲染三行并准确区分无风险和资料不可用。
REGION_RISK_CATEGORIES = (
    ("dzzh", "地质灾害"),
    ("sh", "山洪"),
    ("zxhl", "中小河流"),
)


def _risk_fcst_times_from_window(
    calendar_window: dict | None,
    now: datetime | None = None,
) -> list[str] | None:
    """把未来日历窗口换算为逐日 08 时风险起报列表。

    当天单日窗口返回 ``None``，继续使用“最近起报无资料时回退前一周期”的稳健路径；
    明天及多日窗口返回显式时次列表。
    """
    if not calendar_window:
        return None
    try:
        start = _parse_date(calendar_window.get("forecast_start_date") or "")
        days = max(1, min(int(calendar_window.get("forecast_days") or 1), RISK_FCST_MAX_DAYS))
    except Exception:
        return None
    current = now or time_source.now(TIANJIN_TIMEZONE)
    if days == 1 and start == current.date():
        return None
    return [
        (start + timedelta(days=offset)).strftime("%Y%m%d") + "080000"
        for offset in range(days)
    ]


def _query_region_hazards(
    lon: float,
    lat: float,
    risk_fcst_times: list[str] | None = None,
    *, include_risk_coverage: bool = False,
) -> dict:
    """查询区域代表点周边的灾害隐患，归一化为 {total_found, radius_km, categories}。

    只保留有数据的静态隐患类型（status=="ok" 且 count>0）；静态查询
    失败时仍返回结构化降级状态，不阻断独立的实时风险等级。categories 不带
    records 明细（区域级只报种类与数量，避免 payload 膨胀）。
    ``risk_fcst_times`` 非空时按目标日逐日查询并合并风险等级；为空时使用最近
    起报时次并在无资料时回退前一周期。
    """
    global _region_hazard_queryer
    payload: dict = {}
    hazards_available = False
    try:
        if _region_hazard_queryer is None:
            _region_hazard_queryer = _load_region_hazard_queryer()
        raw_payload = _region_hazard_queryer(float(lon), float(lat), REGION_HAZARD_RADIUS_KM)
        if isinstance(raw_payload, dict):
            payload = raw_payload
            hazards_available = payload.get("status") in {"ok", "no_data"}
    except Exception as exc:
        print(f"[region_hazards] query failed: {exc}", flush=True)
    categories = (payload.get("categories") or []) if payload.get("status") == "ok" else []
    try:
        total_found = max(0, int(payload.get("total_found") or 0))
    except (TypeError, ValueError, OverflowError):
        total_found = 0
    merged = []
    for category in categories:
        if not isinstance(category, dict):
            continue
        try:
            count = int(category.get("count") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if count <= 0:
            continue
        merged.append({
            "key": category.get("key"),
            "label": category.get("label"),
            "kind": category.get("kind"),
            "count": count,
        })
    # 区域天气#8：叠加目标窗口的风险等级；接口失败只影响增强列，不阻断天气回答。
    risk_levels = _query_region_risk_levels(
        lon, lat, risk_fcst_times, include_coverage=include_risk_coverage
    )
    return {
        "total_found": total_found,
        "radius_km": REGION_HAZARD_RADIUS_KM,
        "categories": merged,
        "hazards_available": hazards_available,
        "risk_levels": risk_levels,
        "risk_levels_available": (
            risk_levels is not None
            and not (isinstance(risk_levels, dict) and risk_levels.get("_all_unreachable") is True)
        ),
    }


def _risk_window_payload(
    calendar_window: dict | None,
    risk_fcst_times: list[str] | None,
) -> dict:
    """返回风险接口实际覆盖的时间范围，不把被截断的请求说成完整覆盖。"""
    payload = {
        "forecast_start_time": None,
        "forecast_end_time": None,
        "forecast_days": None,
        "fcst_times": risk_fcst_times,
        "time_mode": "latest_available_cycle_with_same_day_fallback",
    }
    if risk_fcst_times:
        try:
            start = datetime.strptime(risk_fcst_times[0], "%Y%m%d%H%M%S")
            end = datetime.strptime(risk_fcst_times[-1], "%Y%m%d%H%M%S") + timedelta(days=1)
        except (TypeError, ValueError):
            return payload
        payload.update({
            "forecast_start_time": start.strftime("%Y-%m-%d %H:%M"),
            "forecast_end_time": end.strftime("%Y-%m-%d %H:%M"),
            "forecast_days": len(risk_fcst_times),
            "time_mode": "explicit_daily_cycles",
        })
        return payload
    # None 代表既有 helper 自行选择最近可用起报并可能回退上一个周期；它不返回
    # 实际命中的周期，因而不能把用户的日历日窗口当成已经确认的风险覆盖范围。
    return payload


def _coerce_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and (not value.is_integer()):
        return None
    if isinstance(value, Decimal) and value != value.to_integral_value():
        return None
    if isinstance(value, str) and not re.fullmatch(r"\d+", value.strip()):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _region_static_counts(hazards: dict) -> dict[str, int | None]:
    """标准化静态隐患点数量；查询失败/格式错误不能伪造成零。"""
    if hazards.get("hazards_available") is not True:
        return {key: None for key, _label in REGION_RISK_CATEGORIES}
    categories = hazards.get("categories")
    if not isinstance(categories, list):
        return {key: None for key, _label in REGION_RISK_CATEGORIES}
    counts = {key: 0 for key, _label in REGION_RISK_CATEGORIES}
    for category in categories:
        if not isinstance(category, dict) or category.get("key") not in counts:
            continue
        count = _coerce_nonnegative_int(category.get("count"))
        if count is None:
            counts[category["key"]] = None
        elif counts[category["key"]] is not None:
            counts[category["key"]] += count
    return counts


def _normalize_region_risk(
    key: str,
    label: str,
    hidden_point_count: int | None,
    risk_levels: Any,
    risk_levels_available: Any,
    coverage: dict | None = None,
) -> dict:
    """把风险接口的三态及异常载荷归一化为单一灾种输出。"""
    result = {
        "key": key,
        "label": label,
        "hidden_point_count": hidden_point_count,
        "risk_status": "unavailable",
        "levels": {},
        "risk_point_count": None,
        "advice": [],
    }
    if coverage:
        result["coverage"] = coverage
    if risk_levels_available is not True:
        result["unavailable_reason"] = "risk_service_unavailable"
        return result
    if not isinstance(risk_levels, dict):
        result["unavailable_reason"] = "malformed_risk_payload"
        return result
    coverage_complete = not coverage or coverage.get("complete") is True
    if key not in risk_levels:
        if not coverage_complete:
            result["unavailable_reason"] = "risk_window_incomplete"
            return result
        result.update({"risk_status": "no_risk", "risk_point_count": 0})
        return result
    raw = risk_levels.get(key)
    # risk_warning_tool 的 no_data 表示这个起报时次缺少预报资料，绝非无风险。
    if raw == "no_data":
        result["unavailable_reason"] = "risk_forecast_no_data"
        return result
    if raw is None:
        result["unavailable_reason"] = "risk_kind_unavailable"
        return result
    if not isinstance(raw, dict) or not isinstance(raw.get("levels"), dict):
        result["unavailable_reason"] = "malformed_risk_payload"
        return result
    levels = raw["levels"]
    normalized_levels: dict[str, int] = {}
    for level, count in levels.items():
        number = _coerce_nonnegative_int(count)
        if number is None:
            result["unavailable_reason"] = "malformed_risk_payload"
            return result
        if number:
            normalized_levels[str(level)] = number
    advice = raw.get("level_advice") or []
    if not isinstance(advice, list):
        result["unavailable_reason"] = "malformed_risk_payload"
        return result
    if not normalized_levels:
        result.update({"risk_status": "no_risk", "risk_point_count": 0})
        return result
    total = _coerce_nonnegative_int(raw.get("total"))
    result.update({
        "risk_status": "risk",
        "levels": normalized_levels,
        "risk_point_count": total if total is not None else sum(normalized_levels.values()),
        "advice": advice,
    })
    if not coverage_complete:
        result["coverage_status"] = "partial"
    return result


def _unsupported_region_scopes(user_query: str, regions: str) -> list[str]:
    """Conservatively expose explicit unsupported administrative scopes; this is not a geocoder."""
    text = f"{user_query or ''} {regions or ''}"
    supported = set(ROLLING_FORECAST_COORDS) | set(ROLLING_FORECAST_REGION_ALIASES) | set(BASIN_CITY_COORDS)
    for name in sorted(supported | {"天津市", "天津", "全市", "我市", "本市", "市区", "中心城区"}, key=len, reverse=True):
        text = text.replace(name, " ")
    found = re.findall(r"[\u4e00-\u9fff]{2,8}?(?:新区|自治县|自治州|区|县|市)", text)
    cleaned = []
    for name in found:
        name = re.sub(r"^(?:今天|今日|明天|未来|和|与|及)+", "", name)
        if name and name not in cleaned:
            cleaned.append(name)
    return cleaned


def _resolve_region_risk_regions(user_query: str, regions: str) -> tuple[list[str], bool, list[str]]:
    """为独立风险入口校验范围，避免旧解析器把明确未知地点默认为天津市区。"""
    text = f"{user_query or ''} {regions or ''}".strip()
    unsupported_names = _unsupported_region_scopes(user_query, regions)
    matched_regions: list[str] = []
    if has_matched_rolling_region(text):
        matched_regions.extend(parse_rolling_forecast_regions(text))
    for city in match_basin_cities(text):
        if city not in matched_regions:
            matched_regions.append(city)
    # "天津/全市/我市"是明确支持的全市范围，且保持旧天气入口的默认语义。
    if not matched_regions and any(scope in text for scope in ("天津", "全市", "我市", "本市", "市区", "中心城区")):
        matched_regions.append(DEFAULT_ROLLING_FORECAST_REGION)
    if matched_regions and not unsupported_names:
        return matched_regions, False, []
    # 此专用核心只接受已验证的区域、别名或明确泛天津范围。与通用天气入口不同，
    # 不能因没有行政后缀而把“雄安未来风险”等未知地点默认为天津市区。
    return [], True, unsupported_names


def query_region_weather_risks_core(
    user_query: str,
    regions: str = "",
    now: datetime | None = None,
) -> dict:
    """查询区域代表点的地质灾害、山洪和中小河流综合风险。

    每个已验证区域只调用一次 _query_region_hazards，保留其中静态隐患点和
    三类风险预报各自的可用性，供上层按业务状态而非文案猜测结果。
    """
    current = now or time_source.now(TIANJIN_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TIANJIN_TIMEZONE)
    region_names, unsupported, unsupported_names = _resolve_region_risk_regions(user_query, regions)
    if unsupported:
        return {
            "status": "unsupported_region",
            "message": "暂不支持该区域的综合风险查询。",
            "query_time": current.strftime("%Y-%m-%d %H:%M:%S"),
            "regions": [],
            "unsupported_regions": unsupported_names,
            "risk_window": _risk_window_payload(None, None),
        }
    calendar_window = resolve_requested_calendar_window(user_query, now=current)
    risk_fcst_times = _risk_fcst_times_from_window(calendar_window, current)
    risk_window = _risk_window_payload(calendar_window, risk_fcst_times)
    entries: list[dict] = []
    all_risk_statuses: list[str] = []
    for name in region_names:
        try:
            lon_text, lat_text = _region_or_city_coord(name).split("_", 1)
            options = {"include_risk_coverage": True} if risk_fcst_times else {}
            hazards = _query_region_hazards(
                float(lon_text), float(lat_text), risk_fcst_times, **options
            )
        except Exception:
            hazards = None
            lon_text, lat_text = "", ""
        if not isinstance(hazards, dict):
            hazards = {}
        counts = _region_static_counts(hazards)
        coverage_by_kind = hazards.get("risk_levels", {}).get("_coverage", {}) if isinstance(hazards.get("risk_levels"), dict) else {}
        risks = [
            _normalize_region_risk(
                key, label, counts[key], hazards.get("risk_levels"), hazards.get("risk_levels_available"),
                coverage_by_kind.get(key) if isinstance(coverage_by_kind, dict) else None,
            )
            for key, label in REGION_RISK_CATEGORIES
        ]
        all_risk_statuses.extend(item["risk_status"] for item in risks)
        radius = _to_float(hazards.get("radius_km"))
        entries.append({
            "region": name,
            "region_display": _display_region(name),
            "longitude": _to_float(lon_text),
            "latitude": _to_float(lat_text),
            "radius_km": radius if radius is not None and radius > 0 else REGION_HAZARD_RADIUS_KM,
            "hazards_available": hazards.get("hazards_available") is True,
            "risks": risks,
        })
    if all_risk_statuses and all(status == "unavailable" for status in all_risk_statuses):
        status = "risk_service_unavailable"
    elif any(status == "unavailable" for status in all_risk_statuses):
        status = "partial"
    else:
        status = "ok"
    if risk_fcst_times:
        all_coverages = [
            risk.get("coverage")
            for entry in entries for risk in entry["risks"]
            if isinstance(risk.get("coverage"), dict)
        ]
        if all_coverages:
            complete = all(item.get("complete") is True for item in all_coverages)
            any_success = any(item.get("successful_times") for item in all_coverages)
            coverage_status = "complete" if complete else ("partial" if any_success else "unavailable")
            risk_window["coverage_status"] = coverage_status
            if not complete:
                risk_window.update({
                    "forecast_start_time": None,
                    "forecast_end_time": None,
                    "forecast_days": None,
                    "time_mode": "explicit_daily_cycles_incomplete",
                })
                if any_success:
                    status = "partial"
    return {
        "status": status,
        "query_time": current.strftime("%Y-%m-%d %H:%M:%S"),
        "risk_window": risk_window,
        "regions": entries,
    }


def _parse_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError("forecast_start_date 必须是 YYYY-MM-DD 格式")


def _latest_available_fcst_time(now: datetime) -> datetime:
    """按接口文档选择已生成的最新起报时次。

    08 时后使用当日 08 时；08 时前使用前一日 20 时。
    """
    if now.hour >= 8:
        return now.replace(hour=8, minute=0, second=0, microsecond=0)
    previous_day = now - timedelta(days=1)
    return previous_day.replace(hour=20, minute=0, second=0, microsecond=0)


def _select_fcst_for_target(target_start: datetime, now: datetime) -> datetime:
    latest = _latest_available_fcst_time(now)
    if latest <= target_start:
        return latest
    previous_day = target_start - timedelta(days=1)
    return previous_day.replace(hour=20, minute=0, second=0, microsecond=0)


def resolve_calendar_query_window(
    forecast_start_date: str | date | datetime,
    forecast_days: int,
    now: datetime | None = None,
) -> dict:
    """将业务日历日参数换算为滚动预报底层时效参数。"""
    now = now or time_source.now(TIANJIN_TIMEZONE)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TIANJIN_TIMEZONE)
    start_date = _parse_date(forecast_start_date)
    try:
        days = int(forecast_days)
    except (TypeError, ValueError) as exc:
        raise ValueError("forecast_days 必须是 1 至 10 的整数") from exc
    if not 1 <= days <= 10:
        raise ValueError("forecast_days 必须是 1 至 10 的整数")

    target_start = datetime.combine(start_date, time.min, tzinfo=TIANJIN_TIMEZONE)
    target_end = target_start + timedelta(days=days)
    selected_fcst = _select_fcst_for_target(target_start, now)
    start_period = int((target_start - selected_fcst).total_seconds() // 3600)
    end_period = int((target_end - selected_fcst).total_seconds() // 3600)
    if start_period < 0:
        raise ValueError("日历日查询的起始时效不能为负数")
    if end_period > MAX_FORECAST_PERIOD_HOURS:
        raise ValueError("查询范围超出滚动预报未来 240 小时时效")
    return {
        "forecast_start_date": start_date.isoformat(),
        "forecast_days": days,
        "target_start": target_start,
        "target_end": target_end,
        "fcst_time": selected_fcst.strftime("%Y%m%d%H%M%S"),
        "start_period": start_period,
        "end_period": end_period,
        "interval": 24,
    }


def resolve_weekend_query_window(
    user_query: str,
    now: datetime | None = None,
) -> dict | None:
    """将未来的“本周末/周末/下周末”换算为确定的日历日窗口。

    本周末在周一至周五表示当周周六、周日；周六包含当天和周日；
    周日只包含当天。下周末表示下一个自然周的周六、周日。
    历史的“上周末”不在此函数中处理。
    """
    query = re.sub(r"\s+", "", str(user_query or ""))
    if "周末" not in query or any(word in query for word in ("上周末", "上个周末")):
        return None

    now = now or time_source.now(TIANJIN_TIMEZONE)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TIANJIN_TIMEZONE)
    today = now.date()
    monday = today - timedelta(days=today.weekday())

    if any(word in query for word in ("下周末", "下个周末")):
        start_date = monday + timedelta(days=12)
        days = 2
    else:
        saturday = monday + timedelta(days=5)
        if today.weekday() == 6:
            start_date = today
            days = 1
        else:
            start_date = max(today, saturday)
            days = 2

    return resolve_calendar_query_window(start_date, days, now=now)


# 星期问法（下周一/本周五/周三）→ 单日日历窗口。与决策天气层
# decision_weather_core._decision_target_dates 的星期语义必须同口径：
# 下周X=下一自然周星期X；周X（无"下"）=本自然周星期X、已过去取下一周。
# 2026-08-24 生产缺陷：星期问法不识别 → 落默认 240h/12h 全量窗口，
# "下周一"（8/31）根本不在取数范围内，答案错锚到 8/25 并铺开整周表格。
_CN_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def resolve_weekday_query_window(user_query: str, now: datetime | None = None) -> dict | None:
    """把"下周一/下星期X/本周X/周X/星期X"换算为单日（1 天）日历窗口；无星期问法返回 None。

    - "下周X/下星期X" → 下一自然周的星期X；
    - "本周X/这周X/周X/星期X"（无"下"）→ 本自然周的星期X，已过去则取下一周。
    目标日超出 240h 时效时由 resolve_calendar_query_window 抛 ValueError
    （core 转 out_of_range 结构化提示），不静默回退。
    "下周末/周末"已由 resolve_weekend_query_window 先行处理，本函数不会命中。
    """
    query = re.sub(r"\s+", "", str(user_query or ""))
    if not query:
        return None
    now = now or time_source.now(TIANJIN_TIMEZONE)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TIANJIN_TIMEZONE)
    today = now.date()
    monday = today - timedelta(days=today.weekday())
    match = re.search(r"下(?:周|星期)([一二三四五六日天])", query)
    if match:
        target = monday + timedelta(days=7 + _CN_WEEKDAY[match.group(1)])
        return resolve_calendar_query_window(target, 1, now=now)
    match = re.search(r"(?:本周|这周|周|星期)([一二三四五六日天])", query)
    if match:
        target = monday + timedelta(days=_CN_WEEKDAY[match.group(1)])
        if target < today:
            target += timedelta(days=7)
        return resolve_calendar_query_window(target, 1, now=now)
    return None

_CN_SMALL_NUMBERS = {
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


def _parse_small_number(value: str) -> int | None:
    raw = str(value or "").strip()
    if raw.isdigit():
        return int(raw)
    if raw in _CN_SMALL_NUMBERS:
        return _CN_SMALL_NUMBERS[raw]
    if raw.startswith("十") and len(raw) == 2 and raw[1] in _CN_SMALL_NUMBERS:
        return 10 + _CN_SMALL_NUMBERS[raw[1]]
    if raw.endswith("十") and len(raw) == 2 and raw[0] in _CN_SMALL_NUMBERS:
        return _CN_SMALL_NUMBERS[raw[0]] * 10
    return None


# 无年份日期一律按当前日历解释（今年/当月已发生 = 历史实况，未发生 = 预报）。
# 不做"最近的未来同月同日"改写：推明年/下月对 240h 时效的滚动预报无法回答。
def _extract_explicit_query_dates(user_query: str, current: datetime) -> list[date]:
    """从用户原问中提取明确公历日期。

    写全年份的日期严格按该年解析（过去即历史）；无年份日期一律按当前日历解释：
    今年/当月未发生为未来（预报），今年/当月已发生为今年历史实况。
    天气问答场景下"推明年/下月"对滚动预报（240h 时效）永远无法回答，故不做
    "最近的未来同月同日"改写（原 15 天规则把 7月11日 这类同一年已过去日期
    推去明年，导致"暂无具体天气预报信息"）。支持"8月10日 / 8月10号 / 10号 /
    2026年8月10日 / 2026-8-10"。
    """
    text = str(user_query or "")
    matches: list[tuple[int, date]] = []
    occupied_spans: list[tuple[int, int]] = []
    full_pattern = re.compile(r"(\d{4})\s*(?:年|[-/])\s*(\d{1,2})\s*(?:月|[-/])\s*(\d{1,2})\s*日?")
    for match in full_pattern.finditer(text):
        try:
            matches.append((match.start(), date(int(match.group(1)), int(match.group(2)), int(match.group(3)))))
            occupied_spans.append(match.span())
        except ValueError:
            continue

    short_pattern = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*(?:日|号)")
    for match in short_pattern.finditer(text):
        if any(start <= match.start() < end for start, end in occupied_spans):
            continue
        try:
            candidate = date(current.year, int(match.group(1)), int(match.group(2)))
            matches.append((match.start(), candidate))
            occupied_spans.append(match.span())
        except ValueError:
            continue

    # 裸“N号/日”后紧跟地点/建筑名词首字（教学楼/病房/2号院/车间等）时视为门牌/编号而非日期。
    # 第/数字前置守卫防台风编号（第10号台风），此处后缀守卫防地址/建筑编号（3号教学楼）。
    _BARE_DAY_PLACE_SUFFIX = "楼馆室院栋房门间单元层床车台站区坊店堂厅所厂库班队组线教学医病办宿餐厨卫洗储机房"
    bare_pattern = re.compile(r"(?<![0-9第])(\d{1,2})\s*(?:日|号)(?![%s])" % _BARE_DAY_PLACE_SUFFIX)
    for match in bare_pattern.finditer(text):
        if any(start <= match.start() < end for start, end in occupied_spans):
            continue
        try:
            candidate = date(current.year, current.month, int(match.group(1)))
            matches.append((match.start(), candidate))
            occupied_spans.append(match.span())
        except ValueError:
            continue
    return [item for _, item in sorted(matches, key=lambda pair: pair[0])]


def resolve_requested_calendar_window(
    user_query: str,
    forecast_start_date: str | date | datetime = "",
    forecast_days: int = 0,
    now: datetime | None = None,
) -> dict | None:
    """解析实际查询窗口；明确的周末语义优先于模型传入的日期参数。"""
    weekend_window = resolve_weekend_query_window(user_query, now=now)
    if weekend_window is not None:
        return weekend_window
    query = re.sub(r"\s+", "", str(user_query or ""))
    if (
        any(keyword in query for keyword in DEFAULT_SEVEN_DAY_QUERY_KEYWORDS)
        and not any(keyword in query for keyword in PAST_QUERY_KEYWORDS)
    ):
        current = now or time_source.now(TIANJIN_TIMEZONE)
        if current.tzinfo is None:
            current = current.replace(tzinfo=TIANJIN_TIMEZONE)
        return resolve_calendar_query_window(current.date() + timedelta(days=1), 7, now=current)
    current = now or time_source.now(TIANJIN_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TIANJIN_TIMEZONE)
    future_days_match = re.search(r"未来\s*([0-9一二两三四五六七八九十]+)\s*(?:个)?天", query)
    if future_days_match:
        days = _parse_small_number(future_days_match.group(1))
        if days is not None and 1 <= days <= 10:
            return resolve_calendar_query_window(current.date() + timedelta(days=1), days, now=current)
    if "后天" in query:
        return resolve_calendar_query_window(current.date() + timedelta(days=2), 1, now=current)
    if "明天" in query or "明日" in query:
        return resolve_calendar_query_window(current.date() + timedelta(days=1), 1, now=current)
    if "前天" in query:
        return resolve_calendar_query_window(current.date() - timedelta(days=2), 1, now=current)
    if "昨天" in query or "昨日" in query:
        return resolve_calendar_query_window(current.date() - timedelta(days=1), 1, now=current)
    if "今天" in query or "今日" in query:
        return resolve_calendar_query_window(current.date(), 1, now=current)
    explicit_dates = _extract_explicit_query_dates(user_query, current)
    if explicit_dates:
        start_date = explicit_dates[0]
        days = 1
        if len(explicit_dates) >= 2 and explicit_dates[1] >= start_date:
            days = min((explicit_dates[1] - start_date).days + 1, 10)
        return resolve_calendar_query_window(start_date, days, now=current)
    # 星期问法（下周一/本周五/周三）：明确日期（"8月25日周一"）优先，星期兜底。
    weekday_window = resolve_weekday_query_window(query, now=current)
    if weekday_window is not None:
        return weekday_window
    if forecast_start_date or forecast_days:
        if not forecast_start_date or not forecast_days:
            raise ValueError("日历日查询必须同时提供 forecast_start_date 和 forecast_days")
        return resolve_calendar_query_window(forecast_start_date, forecast_days, now=now)
    return None


def _parse_future_hours(user_query: str) -> int | None:
    """识别“未来/接下来 N 小时”，返回 1 至 24 小时的查询时长。"""
    match = re.search(
        r"(?:未来|接下来|随后|后面|之后)\s*([0-9一二两三四五六七八九十]+)\s*(?:个)?小时",
        str(user_query or ""),
    )
    if not match:
        return None
    raw = match.group(1)
    value = _parse_small_number(raw)
    return value if value is not None and 1 <= value <= 24 else None


def resolve_future_hour_query_window(
    user_query: str,
    now: datetime | None = None,
) -> dict | None:
    """将未来 N 小时查询换算为从下一整点开始、1 小时步长的底层时效参数。"""
    hours = _parse_future_hours(user_query)
    if hours is None:
        return None
    current = now or time_source.now(TIANJIN_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TIANJIN_TIMEZONE)
    target_start = current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    target_end = target_start + timedelta(hours=hours)
    selected_fcst = _select_fcst_for_target(target_start, current)
    start_period = int((target_start - selected_fcst).total_seconds() // 3600)
    end_period = int((target_end - selected_fcst).total_seconds() // 3600)
    if start_period < 0 or end_period > MAX_FORECAST_PERIOD_HOURS:
        raise ValueError("未来小时查询范围超出滚动预报有效时效")
    return {
        "hours": hours,
        "target_start": target_start,
        "target_end": target_end,
        "fcst_time": selected_fcst.strftime("%Y%m%d%H%M%S"),
        "start_period": start_period,
        "end_period": end_period,
        "interval": 1,
    }


def resolve_current_hour_query_window(
    user_query: str,
    now: datetime | None = None,
) -> dict | None:
    """将“现在/当前”天气查询换算为当前整点至下一整点的1小时预报窗口。"""
    query = str(user_query or "")
    # 旧版滚动预报双窗口兼容：当前实况问答现已改用天擎聚合工具，
    # 这里继续避免把该固定问法误判为普通“当前天气”查询。
    if "滚动" in query and "实况" in query:
        return None
    if not any(keyword in query for keyword in ("现在", "当前", "目前", "此刻", "这会", "正在")):
        return None
    if not any(keyword in query for keyword in ("天气", "下雨", "有雨", "降雨", "降水", "气温", "温度", "风", "能见度", "雾", "霾")):
        return None
    current = now or time_source.now(TIANJIN_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TIANJIN_TIMEZONE)
    target_start = current.replace(minute=0, second=0, microsecond=0)
    target_end = target_start + timedelta(hours=1)
    selected_fcst = _select_fcst_for_target(target_start, current)
    start_period = int((target_start - selected_fcst).total_seconds() // 3600)
    return {
        "mode": "current_hour",
        "hours": 1,
        "target_start": target_start,
        "target_end": target_end,
        "fcst_time": selected_fcst.strftime("%Y%m%d%H%M%S"),
        "start_period": start_period,
        "end_period": start_period + 1,
        "interval": 1,
    }


def resolve_named_hour_query_window(
    query_window: str,
    now: datetime | None = None,
) -> dict | None:
    """将内部语义窗口转换为底层时效参数，调用方不得自行计算起报时间。"""
    role = str(query_window or "").strip()
    if role not in {"current_hour", "next_12_hours"}:
        return None
    current = now or time_source.now(TIANJIN_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TIANJIN_TIMEZONE)
    current_hour = current.replace(minute=0, second=0, microsecond=0)
    if role == "current_hour":
        target_start = current_hour
        target_end = target_start + timedelta(hours=1)
        interval = 1
    else:
        target_start = current_hour + timedelta(hours=1)
        target_end = target_start + timedelta(hours=12)
        interval = 12
    selected_fcst = _select_fcst_for_target(target_start, current)
    start_period = int((target_start - selected_fcst).total_seconds() // 3600)
    end_period = int((target_end - selected_fcst).total_seconds() // 3600)
    return {
        "mode": role,
        "hours": int((target_end - target_start).total_seconds() // 3600),
        "target_start": target_start,
        "target_end": target_end,
        "fcst_time": selected_fcst.strftime("%Y%m%d%H%M%S"),
        "start_period": start_period,
        "end_period": end_period,
        "interval": interval,
    }

def select_rolling_forecast_time(now: datetime | None = None) -> str:
    now = now or time_source.now(TIANJIN_TIMEZONE)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TIANJIN_TIMEZONE)
    return _latest_available_fcst_time(now).strftime("%Y%m%d%H%M%S")


# 时段词 → [start_hour, end_hour)（北京时间）。end_hour=24 表示当日 24:00（次日 00:00）；
# >24 表示跨到次日（如"夜里" 20:00→次日06:00=30）。与"今天/明天/…"日历日组合使用。
_TOD_RANGES = {
    "凌晨": (0, 6),
    "早晨": (6, 8),
    "早上": (6, 8),
    "清晨": (6, 8),
    "上午": (8, 12),
    "中午": (11, 14),
    "下午": (12, 18),
    "傍晚": (17, 20),
    "晚上": (18, 24),
    "夜里": (20, 30),
    "夜间": (20, 30),
    "深夜": (20, 30),
}


def _detect_time_of_day_range(user_query: str) -> tuple[int, int] | None:
    """识别"上午/下午/晚上/中午/夜里/凌晨/傍晚"等时段词，返回合并的 [start_hour, end_hour)。

    多个时段词取并集（min start / max end），如"今天下午和晚上"→(12, 24)。
    无时段词返回 None（只收窄时段化问法，整日问法不受影响）。
    """
    text = re.sub(r"\s+", "", str(user_query or ""))
    spans = [rng for word, rng in _TOD_RANGES.items() if word in text]
    if not spans:
        return None
    return min(s for s, _ in spans), max(e for _, e in spans)


def _narrow_calendar_window_to_time_of_day(
    calendar_window: dict,
    user_query: str,
    now: datetime | None = None,
) -> dict | None:
    """把单日日历窗口收窄为"时段逐小时"窗口（"今天下午/晚上"等）；不适配返回 None。

    日历窗口已解析出目标日（今天/明天/后天/周X/明确日期），时段词把当日 00:00-24:00
    收窄到所问时段，interval=1 逐小时取数。时段超出滚动预报 240h 时效或多日窗口时
    返回 None（调用方回退整日窗口）。
    """
    if not isinstance(calendar_window, dict):
        return None
    rng = _detect_time_of_day_range(user_query)
    if rng is None:
        return None
    try:
        days = int(calendar_window.get("forecast_days") or 1)
    except (TypeError, ValueError):
        return None
    if days != 1:
        return None  # 只收窄单日窗口，多日窗口保持逐日
    base = calendar_window.get("target_start")
    if base is None:
        return None
    now = now or time_source.now(TIANJIN_TIMEZONE)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TIANJIN_TIMEZONE)
    start_h, end_h = rng
    target_start = base + timedelta(hours=start_h)
    target_end = base + timedelta(hours=end_h)
    selected_fcst = _select_fcst_for_target(target_start, now)
    start_period = int((target_start - selected_fcst).total_seconds() // 3600)
    end_period = int((target_end - selected_fcst).total_seconds() // 3600)
    if start_period < 0 or end_period > MAX_FORECAST_PERIOD_HOURS:
        return None  # 时段超滚动预报时效 → 回退整日窗口
    return {
        "mode": "time_of_day",
        "hours": end_period - start_period,
        "target_start": target_start,
        "target_end": target_end,
        "fcst_time": selected_fcst.strftime("%Y%m%d%H%M%S"),
        "start_period": start_period,
        "end_period": end_period,
        "interval": 1,
    }


# 时段标签规范序（同时段词别名归并：早上/清晨→早晨，夜间/深夜→夜里）。
_TOD_LABEL_NORMALIZE = {"清晨": "早晨", "早上": "早晨", "夜间": "夜里", "深夜": "夜里"}
_TOD_CANONICAL_ORDER = ("凌晨", "早晨", "上午", "中午", "下午", "傍晚", "晚上", "夜里")


def _time_of_day_label(user_query: str, target_start: datetime, now: datetime) -> str:
    """生成时段标签（"今天下午""今天下午到晚上""明天上午"），用于时段汇总表的"时段"列。

    时段词取用户问法中出现的规范词（按日内先后排序）；多个不相邻取首尾用"到"连接
    （"下午和晚上"→"下午到晚上"）；日期前缀按目标日与 now 的差（今天/明天/后天/具体日期）。
    """
    text = re.sub(r"\s+", "", str(user_query or ""))
    present = {
        _TOD_LABEL_NORMALIZE.get(word, word)
        for word in _TOD_RANGES
        if word in text
    }
    words = [w for w in _TOD_CANONICAL_ORDER if w in present]
    if len(words) == 1:
        phrase = words[0]
    elif len(words) > 1:
        phrase = f"{words[0]}到{words[-1]}"
    else:
        phrase = ""
    if now.tzinfo is None:
        now = now.replace(tzinfo=TIANJIN_TIMEZONE)
    day = target_start.date()
    delta = (day - now.date()).days
    day_word = {0: "今天", 1: "明天", 2: "后天"}.get(delta) or f"{day.month}月{day.day}日"
    return f"{day_word}{phrase}" if phrase else day_word


_TOD_SIMPLE_WIND_RE = re.compile(
    r"^(?P<direction>[东南西北偏]+风)\s*"
    r"(?P<lo>\d+)\s*(?:[-~～]\s*(?P<hi>\d+)\s*)?级$"
)

DYNAMIC_EVENT_DATE_EXPLICIT_KEYWORDS = ("高考", "中考", "考试期间", "节假日", "假期")
DYNAMIC_EVENT_DATE_HOLIDAY_NAME_PATTERN = re.compile(r"元旦|春节|清明|劳动节|五一|端午|中秋|国庆")
DYNAMIC_EVENT_DATE_WEATHER_WORDS = ("天气", "气象", "预报", "出游", "出行", "适合")
DYNAMIC_EVENT_DATE_EXPLICIT_SUFFIX_PATTERN = re.compile(r"^(?:节|假期|期间|放假)")
DYNAMIC_EVENT_DATE_COMPETING_TIME_PATTERN = re.compile(
    r"今天|今日|明天|后天|大后天|本周|下周|周[一二三四五六日天]|"
    r"未来\s*(?:\d+|[一二三四五六七八九十]+)\s*(?:天|日|小时)"
)


def is_dynamic_event_date_query(user_query: str) -> bool:
    """仅这些逐年变化的活动允许模型提供自然日日期窗口。"""
    text = str(user_query or "")
    if any(keyword in text for keyword in DYNAMIC_EVENT_DATE_EXPLICIT_KEYWORDS):
        return True
    holiday = DYNAMIC_EVENT_DATE_HOLIDAY_NAME_PATTERN.search(text)
    if holiday is None:
        return False
    tail = text[holiday.end():]
    if DYNAMIC_EVENT_DATE_EXPLICIT_SUFFIX_PATTERN.match(tail):
        return True
    if not any(word in tail for word in DYNAMIC_EVENT_DATE_WEATHER_WORDS):
        return False
    return DYNAMIC_EVENT_DATE_COMPETING_TIME_PATTERN.search(tail) is None


_TOD_WIND_DIRECTION_ANGLE = {
    "北风": 0, "东北风": 45, "东风": 90, "东南风": 135,
    "南风": 180, "西南风": 225, "西风": 270, "西北风": 315,
}
_TOD_THUNDER_QUALIFIER_RE = re.compile(r"^(?P<base>.+?)(?:并)?伴(?:随|有)?雷电$")
_TOD_COMPOUND_WEATHER_RE = re.compile(r"[转到、,，/]")


def _summarize_tod_wind(eda_values: list) -> str | None:
    """时段汇总风力风向：按连续风向阶段合并风力区间，风向变化用"转"连接。

    修复甲方 2026-08-24 反馈的可读性问题——把逐小时 EDA 原文简单去重拼接会出
    "西北风0-1级；东南风0-1级；东风0-1级；东风1-2级"这种既长又自相矛盾的列表。
    连续同风向的多条合并风力区间（东风0-1级 + 东风1-2级 → 东风0~2级）；带
    “阵风”等附加语义的复合风况及无风向词的原文（如“静风”）原样保留。
    0~2级弱风只在相邻方位持续单向演变时表达具体阶段；大角度跳变或来回
    摆动时保留出现次数最多的实际风向；无唯一主导风向时保留首尾转向，
    首尾相同则保留连续去重后的完整阶段路径。
    较强风和复合原文不进入该弱风压缩分支。
    纯代码确定性、零编造：只重组工具返回的 EDA 文本，不引入任何新数值。
    """
    entries: list[dict[str, object]] = []  # 可解析风况与兜底原文共用同一时间顺序
    observed_forces: list[tuple[int, int]] = []  # 每条逐小时原始风力，供压缩资格判断
    observed_directions: list[str] = []  # 保留逐小时方向频次，不能用阶段去重结果判断主导风向
    for raw in eda_values:
        e = str(raw or "").strip()
        if not e or e == "--":
            continue
        match = _TOD_SIMPLE_WIND_RE.fullmatch(e)
        if not match:
            if not entries or entries[-1].get("raw") != e:
                entries.append({"raw": e})
            continue
        direction = match.group("direction")
        lo = int(match.group("lo"))
        hi = int(match.group("hi") or lo)
        observed_forces.append((lo, hi))
        observed_directions.append(direction)
        if entries and entries[-1].get("direction") == direction:
            phase = entries[-1]
            phase["lo"] = min(int(phase["lo"]), lo)
            phase["hi"] = max(int(phase["hi"]), hi)
            samples = phase["force_samples"]
            if (lo, hi) not in samples:
                samples.append((lo, hi))
        else:
            entries.append({
                "direction": direction,
                "lo": lo,
                "hi": hi,
                "force_samples": [(lo, hi)],
            })

    phases = [entry for entry in entries if "direction" in entry]

    # 0~2 级弱风下，逐小时风向容易摆动。只有每次按同一方向连续跨越相邻方位
    # （每步 45°）才表述完整转向；其余情况使用原始逐小时频次概括主导风向，
    # 频次并列时保留实际首尾转向；首尾相同则保留连续去重后的阶段路径。
    # 这样避免机械罗列，且不再输出笼统的“风向多变”。
    if len(phases) > 1 and len(phases) == len(entries) and observed_forces:
        directions = [str(phase["direction"]) for phase in phases]
        angles = [_TOD_WIND_DIRECTION_ANGLE.get(direction) for direction in directions]
        if all(angle is not None for angle in angles) and max(force[1] for force in observed_forces) <= 2:
            deltas = [
                (int(current) - int(previous) + 180) % 360 - 180
                for previous, current in zip(angles, angles[1:])
            ]
            gradual = (
                len(set(directions)) == len(directions)
                and all(abs(delta) == 45 for delta in deltas)
                and (all(delta > 0 for delta in deltas) or all(delta < 0 for delta in deltas))
            )
            if not gradual:
                lo = min(force[0] for force in observed_forces)
                hi = max(force[1] for force in observed_forces)
                force = f"{lo}级" if lo == hi else f"{lo}~{hi}级"
                counts = Counter(observed_directions)
                highest = max(counts.values())
                leaders = [direction for direction, count in counts.items() if count == highest]
                if len(leaders) == 1:
                    direction_summary = f"以{leaders[0]}为主"
                elif observed_directions[0] == observed_directions[-1]:
                    phase_path: list[str] = []
                    for direction in observed_directions:
                        if not phase_path or phase_path[-1] != direction:
                            phase_path.append(direction)
                    direction_summary = "转".join(phase_path)
                else:
                    direction_summary = f"{observed_directions[0]}转{observed_directions[-1]}"
                return f"{direction_summary}，风力{force}"

    def format_phase(entry: dict[str, object]) -> str:
        direction = str(entry["direction"])
        lo, hi = int(entry["lo"]), int(entry["hi"])
        return f"{direction}{lo}级" if lo == hi else f"{direction}{lo}~{hi}级"

    def is_gradual_adjacent_turn(group: list[dict[str, object]]) -> bool:
        angles = [_TOD_WIND_DIRECTION_ANGLE.get(str(entry["direction"])) for entry in group]
        if any(angle is None for angle in angles):
            return False
        deltas = [
            (int(current) - int(previous) + 180) % 360 - 180
            for previous, current in zip(angles, angles[1:])
        ]
        return bool(deltas) and all(abs(delta) == 45 for delta in deltas) and (
            all(delta > 0 for delta in deltas) or all(delta < 0 for delta in deltas)
        )

    parts: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        if "raw" in entry:
            parts.append(str(entry["raw"]))
            index += 1
            continue

        samples = entry["force_samples"]
        stable_force = samples[0] if len(samples) == 1 else None
        group = [entry]
        next_index = index + 1
        while stable_force is not None and next_index < len(entries):
            candidate = entries[next_index]
            if "raw" in candidate:
                break
            candidate_samples = candidate["force_samples"]
            if len(candidate_samples) != 1 or candidate_samples[0] != stable_force:
                break
            group.append(candidate)
            next_index += 1

        if stable_force is not None and stable_force[1] <= 2 and is_gradual_adjacent_turn(group):
            start = str(group[0]["direction"]).removesuffix("风")
            end = str(group[-1]["direction"])
            lo, hi = stable_force
            force = f"{lo}级" if lo == hi else f"{lo}~{hi}级"
            parts.append(f"{start}到{end}{force}")
        else:
            parts.extend(format_phase(phase) for phase in group)
        index = next_index
    return "转".join(parts) if parts else None


def _summarize_tod_weather(weather_values: list) -> str | None:
    """把逐小时天气标签归并为专业、简洁的时段天气演变。

    接口可能把同一雷雨过程逐小时标成“雨伴随雷电、雨、雨伴随雷电”，
    直接拼接会形成无意义的往返描述。这里仅合并这种语义包含关系；雨势变化
    （如“小雨、中雨伴随雷电、小雨”）及阴晴转变仍按原顺序完整保留。
    """
    raw_values = [
        str(value or "").strip()
        for value in weather_values
        if str(value or "").strip() not in ("", "--")
    ]
    if not raw_values:
        return None

    groups: list[dict[str, object]] = []
    for raw in raw_values:
        if _TOD_COMPOUND_WEATHER_RE.search(raw):
            part = {"base": raw, "display": raw, "thunder": False, "simple": False}
        elif raw == "雷阵雨":
            part = {"base": "雨", "display": raw, "thunder": True, "simple": True}
        else:
            match = _TOD_THUNDER_QUALIFIER_RE.fullmatch(raw)
            base = match.group("base").strip(" ，、") if match else raw
            has_thunder = bool(match)
            display = "雷阵雨" if has_thunder and base == "雨" else raw
            part = {
                "base": base,
                "display": display,
                "thunder": has_thunder,
                "simple": True,
            }

        previous = groups[-1] if groups else None
        if (
            previous
            and previous["simple"]
            and part["simple"]
            and previous["base"] == part["base"]
        ):
            # 同一基底（雨/雨伴随雷电、小雨/小雨伴随雷电）属于连续天气过程；
            # 保留带雷电的更具体标签，不受 A-B-A 次数和方向限制。
            if part["thunder"] and not previous["thunder"]:
                previous["display"] = part["display"]
                previous["thunder"] = True
            continue
        groups.append(part)

    # 已确认的源标签组合：“雷雨过程”紧接“阴有轻雾伴随雷电”。雷电已由前一阶段
    # 表达，局部去掉后一矛盾修饰；若中间隔有其他天气，则完整保留后段雷电信息。
    for previous, current in zip(groups, groups[1:]):
        if (
            previous["thunder"]
            and "雨" in str(previous["base"])
            and current["base"] == "阴有轻雾"
            and current["thunder"]
        ):
            current["display"] = current["base"]

    return "转".join(str(group["display"]) for group in groups)


def _time_of_day_summary_rows(periods: list[dict]) -> list[dict]:
    """时段化查询：把逐小时 periods 按区域聚合为单条时段汇总（甲方 2026-08-24 口径：
    "今天下午有雨吗"不要逐小时，给该时段整体天气——时段/天气现象/气温/风力风向/降水量）。

    聚合口径（纯代码确定性，零编造）：天气现象按出现顺序归并，同一雷雨过程的
    泛化标签抖动会被压缩，真实天气变化用"转"连接；
    气温取时段内 tmin 最小~tmax 最大；风力风向按风向分组合并区间、风向变化用"转"连接
    （`_summarize_tod_wind`，避免逐小时 EDA 拼接出自相矛盾的长列表）；降水量为各小时求和。
    """
    by_region: dict[str, list[dict]] = {}
    order: list[str] = []
    for item in periods:
        if not isinstance(item, dict):
            continue
        region = item.get("region_display") or item.get("region") or "该区域"
        if region not in by_region:
            by_region[region] = []
            order.append(region)
        by_region[region].append(item)
    rows: list[dict] = []
    for region in order:
        items = by_region[region]
        weather = _summarize_tod_weather([it.get("WEA") for it in items])
        tmax_vals = [v for v in (_to_float(it.get("TMAX")) for it in items) if v is not None]
        tmin_vals = [v for v in (_to_float(it.get("TMIN")) for it in items) if v is not None]
        rain_vals = [v for v in (_to_float(it.get("TP1H")) for it in items) if v is not None]
        visibility_vals = [
            v for v in (_to_positive_float(it.get("VISMIN")) for it in items) if v is not None
        ]
        rows.append({
            "region": region,
            "weather": weather,
            "tmax": _temperature_display_text(max(tmax_vals)) if tmax_vals else None,
            "tmin": _temperature_display_text(min(tmin_vals)) if tmin_vals else None,
            "EDA": _summarize_tod_wind([it.get("EDA") for it in items]),
            "rainfall_mm": round(sum(rain_vals), 1) if rain_vals else None,
            "visibility_min_km": round(min(visibility_vals), 1) if visibility_vals else None,
        })
    return rows


_BASIN_STRONG_KEYWORDS = ("海河流域", "流域", "河系")
# 裸河名只在无 POI 语境时视为流域问题；关键词清单不求穷尽，
# 运行时守卫只是 prompt 规则之外的兜底。
_BASIN_RIVER_NAMES = (
    "大清河", "子牙河", "永定河", "北三河",
    "漳卫南运河", "漳卫河", "徒骇马颊河", "黑龙港",
    "滦河", "潮白河", "蓟运河", "泃河", "海河干流",
)
_POI_CONTEXT_MARKERS = (
    "公园", "湿地", "附近", "沿线", "景区", "机场",
    "大学", "医院", "广场", "车站", "火车站",
)


def is_basin_weather_query(user_query: str) -> bool:
    """判断问题对象是否为海河流域/河系，而非天津及区级区域。

    "流域/河系"为强信号直接命中；裸河名（如"大清河明天有雨吗"）在无 POI
    语境（公园/湿地/附近/沿线等）时才算流域问题。裸"海河"不算，
    避免误伤"海河夜景"类点位问题。
    """
    text = str(user_query or "")
    if any(keyword in text for keyword in _BASIN_STRONG_KEYWORDS):
        return True
    if any(marker in text for marker in _POI_CONTEXT_MARKERS):
        return False
    return any(name in text for name in _BASIN_RIVER_NAMES)


# 具体点位指示词：出现这些词、但区域表（天津11区县）匹配不到时，说明问的是"具体点位"，
# 区域工具不应静默退回天津市区代表点，应转决策天气 POI 路径（先 search_poi 定位经纬度）。
POI_PLACE_KEYWORDS = (
    "水库", "拦河坝", "学校", "大学", "中学", "小学", "幼儿园", "学院",
    "医院", "机场", "火车站", "高铁站", "汽车站", "客运站", "车站",
    "港口", "港区", "码头", "公园", "湿地", "景区", "景点", "旅游区",
    "广场", "大厦", "体育馆", "体育场", "博物馆", "展览馆",
    "开发区", "工业园", "园区", "度假区", "古镇",
    # 裸"天津港"（无"港口/港区"后缀）：领导问题清单（2026-08-26 行业服务）
    # "天津港明日风力多大"曾静默落到天津市区代表点。与 chainlitexam 决策天气
    # 词表同口径（同步由 TestPoiGuardDecisionWeatherKeywordSync 静态锁定）。
    "天津港",
)


def has_matched_rolling_region(text: str) -> bool:
    """文本是否命中任一已知滚动预报区域（天津 11 区县及其别名）。"""
    text = str(text or "")
    if any(alias in text for alias in ROLLING_FORECAST_REGION_ALIASES):
        return True
    return any(region in text for region in ROLLING_FORECAST_COORDS)


def match_basin_cities(text: str) -> list[str]:
    """匹配问句点名的海河流域外埠地级市（BASIN_CITY_COORDS），按表序去重返回。

    只认城市名子串（"唐山市"含"唐山"）；不匹配返回 []。天津 11 区县不在此表
    （走 ROLLING_FORECAST_COORDS）。供 query_rolling_forecast_core 把外埠城市
    路由到数据湖海河网格按城市坐标采样，替代静默退回天津市区代表点。
    """
    text = str(text or "")
    if not text:
        return []
    return [name for name in BASIN_CITY_COORDS if name in text]


def _region_or_city_coord(name: str) -> str:
    """区域/城市名 → "lon_lat" 坐标：先天津 11 区县表，再外埠城市表。"""
    return ROLLING_FORECAST_COORDS.get(name) or BASIN_CITY_COORDS[name]


def is_unresolved_poi_forecast_query(user_query: str, regions: str = "") -> bool:
    """问句含具体点位指示词、但区域表匹配不到 → 点位未解析（区域工具不应静默默认天津市区）。

    仅作区域工具的兜底守卫：命中时引导 planner 改用 query_decision_weather_for_poi。
    无点位词（"今天天气"/"我市"/"未来三天"）返回 False，保持默认天津市区行为不变。
    已知区域命中（即使同句含点位词，如"滨海新区大学城"）返回 False，区域路径优先。
    """
    text = f"{user_query or ''} {regions or ''}"
    if has_matched_rolling_region(text):
        return False
    return any(keyword in text for keyword in POI_PLACE_KEYWORDS)


def parse_rolling_forecast_regions(region_text: str | None) -> list[str]:
    text = (region_text or "").strip()
    matched: list[str] = []
    for alias, region in ROLLING_FORECAST_REGION_ALIASES.items():
        if alias in text and region not in matched:
            matched.append(region)
    for region in ROLLING_FORECAST_COORDS:
        if region in text and region not in matched:
            matched.append(region)
    if matched:
        return matched
    # “我市”“全市”“天津”或未说明地区时统一使用天津市区代表点，
    # 避免把11个区域的空间差异聚合成一个容易误解的天气演变结论。
    return [DEFAULT_ROLLING_FORECAST_REGION]


def _rolling_forecast_series(values: Any) -> list:
    if isinstance(values, list) and values and isinstance(values[0], list):
        return values[0]
    if isinstance(values, list):
        return values
    return []


def _clean_value(value: Any) -> Any:
    if value in (None, "--", "9999.0", "9999", 9999, 9999.0):
        return None
    return value


def _to_float(value: Any) -> float | None:
    value = _clean_value(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_positive_float(value: Any) -> float | None:
    """只保留正数数值；滚动预报 VISMIN=0 按接口缺测占位处理。"""
    number = _to_float(value)
    return number if number is not None and number > 0 else None


def format_rolling_forecast_coord(value: float | str) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value).strip()


def _format_period_dt_label(dt: datetime) -> str:
    """逐日时段边界落在整点 00 时不显示"00时"（08月20日），非零时（逐小时）保留小时。"""
    base = dt.strftime("%m月%d日")
    if dt.hour == 0 and dt.minute == 0:
        return base
    return f"{base}{dt.strftime('%H时')}"


def build_rolling_forecast_periods(
    result_data: dict,
    regions: list[str],
    fcst_time: str,
    start_period: int,
    interval: int,
    locations: list[dict] | None = None,
) -> list[dict]:
    fcst_dt = datetime.strptime(fcst_time, "%Y%m%d%H%M%S")
    periods: list[dict] = []
    if locations is None:
        locations = [
            {
                "name": region,
                "region": region,
                "lon": ROLLING_FORECAST_COORDS[region].split("_")[0],
                "lat": ROLLING_FORECAST_COORDS[region].split("_")[1],
                "coord": ROLLING_FORECAST_COORDS[region],
            }
            for region in regions
        ]

    for location in locations:
        region = str(location.get("name") or location.get("region") or location.get("coord") or "指定点位")
        coord = str(location.get("coord") or "")
        if not coord:
            coord = (
                f"{format_rolling_forecast_coord(location.get('lon'))}_"
                f"{format_rolling_forecast_coord(location.get('lat'))}"
            )
        data = result_data.get(coord) or {}
        series_by_element = {
            element: _rolling_forecast_series(data.get(element))
            for element in ROLLING_FORECAST_ELEMENTS
        }
        point_count = max((len(series) for series in series_by_element.values()), default=0)
        for index in range(point_count):
            start_dt = fcst_dt + timedelta(hours=start_period + index * interval)
            end_dt = start_dt + timedelta(hours=interval)
            row = {
                "region": region,
                "region_display": _display_region(str(location.get("region") or region)),
                "lon": location.get("lon"),
                "lat": location.get("lat"),
                "start_time": start_dt.strftime("%Y-%m-%d %H:%M"),
                "end_time": end_dt.strftime("%Y-%m-%d %H:%M"),
                "period_label": f"{_format_period_dt_label(start_dt)}-{_format_period_dt_label(end_dt)}",
            }
            for element, series in series_by_element.items():
                row[element] = _clean_value(series[index] if index < len(series) else None)
            periods.append(row)
    return periods


def _parse_period_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _weather_tokens(value: Any) -> list[str]:
    return [part.strip() for part in re.split(r"[、,，/]|转|到", str(value or "")) if part.strip()]


def _top_items(values: list[str], limit: int = 4) -> list[str]:
    ranked = sorted(Counter(item for item in values if item).items(), key=lambda item: (-item[1], item[0]))
    return [item for item, _ in ranked[:limit]]


def _wind_parts(value: Any) -> tuple[list[str], list[int]]:
    text = str(value or "")
    directions = list(dict.fromkeys(re.findall(r"([东北西南中]{1,3}风)", text)))
    levels: list[int] = []
    for low, high in re.findall(r"(\d+)\s*[-~～到]\s*(\d+)\s*级", text):
        levels.extend((int(low), int(high)))
    levels.extend(int(item) for item in re.findall(r"(?<![-~～到])(\d+)\s*级", text))
    return directions, levels


def _rain_level(value: float | None) -> str:
    if value is None:
        return "无有效数据"
    if value >= EXTRAORDINARY_RAINSTORM_24H_MM:
        return "特大暴雨"
    if value >= SEVERE_RAINSTORM_24H_MM:
        return "大暴雨"
    if value >= RAINSTORM_24H_MM:
        return "暴雨"
    if value >= 25:
        return "大雨"
    if value >= 10:
        return "中雨"
    if value >= 0.1:
        return "小雨"
    return "无有效降水"


def _source_value_text(value: Any) -> str | None:
    """保留接口值的展示形式，不在服务端补零、取整或四舍五入。"""
    value = _clean_value(value)
    if value is None:
        return None
    return str(value).strip()


def _temperature_display_text(value: Any) -> str | None:
    """温度展示统一按整数四舍五入，原始数值仍保留给业务计算使用。"""
    value = _clean_value(value)
    if value is None:
        return None
    try:
        rounded = Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return str(value).strip()
    return format(rounded, "f")


def _difference_text(high: Any, low: Any) -> str | None:
    """计算派生温差并按整数四舍五入，避免小数进入展示层。"""
    try:
        value = Decimal(str(high)) - Decimal(str(low))
    except (InvalidOperation, TypeError, ValueError):
        return None
    rounded = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return format(rounded, "f")


def build_daily_summary(periods: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = {}
    for item in periods:
        if not isinstance(item, dict):
            continue
        start = str(item.get("start_time") or "")
        end = str(item.get("end_time") or "")
        if start and end:
            groups.setdefault((start, end), []).append(item)

    rows: list[dict] = []
    for (start, end), items in sorted(groups.items(), key=lambda pair: _parse_period_time(pair[0][0]) or datetime.min):
        weather: list[str] = []
        raw_weather: list[str] = []
        tmax_values: list[tuple[float, Any]] = []
        tmin_values: list[tuple[float, Any]] = []
        visibility_values: list[tuple[str, float, Any]] = []
        rain_values: list[tuple[str, float, Any]] = []
        raw_eda_values: list[str] = []
        wind_directions: list[str] = []
        wind_levels: list[int] = []
        for item in items:
            region = str(item.get("region_display") or item.get("region") or "")
            weather_text = str(item.get("WEA") or "").strip()
            if weather_text and weather_text != "--":
                raw_weather.append(weather_text)
                weather.extend(_weather_tokens(weather_text))
            if (value := _to_float(item.get("TMAX"))) is not None:
                tmax_values.append((value, item.get("TMAX")))
            if (value := _to_float(item.get("TMIN"))) is not None:
                tmin_values.append((value, item.get("TMIN")))
            if (value := _to_positive_float(item.get("VISMIN"))) is not None:
                visibility_values.append((region, value, item.get("VISMIN")))
            if (value := _to_float(item.get("TP1H"))) is not None:
                rain_values.append((region, value, item.get("TP1H")))
            eda = _clean_value(item.get("EDA"))
            if eda is not None and str(eda).strip():
                raw_eda_values.append(str(eda).strip())
            directions, levels = _wind_parts(eda)
            wind_directions.extend(directions)
            wind_levels.extend(levels)

        max_rain_item = max(rain_values, key=lambda item: item[1]) if rain_values else None
        max_rain = max_rain_item[1] if max_rain_item else None
        max_rain_display = _source_value_text(max_rain_item[2]) if max_rain_item else None
        max_rain_regions = [
            region for region, value, _ in rain_values
            if max_rain is not None and value == max_rain
        ]
        min_visibility_item = min(visibility_values, key=lambda item: item[1]) if visibility_values else None
        min_visibility = min_visibility_item[1] if min_visibility_item else None
        min_visibility_display = _source_value_text(min_visibility_item[2]) if min_visibility_item else None
        min_visibility_regions = [
            region for region, value, _ in visibility_values
            if min_visibility is not None and value == min_visibility
        ]
        start_dt = _parse_period_time(start)
        tmax_item = max(tmax_values, key=lambda item: item[0]) if tmax_values else None
        tmin_item = min(tmin_values, key=lambda item: item[0]) if tmin_values else None
        tmax = tmax_item[0] if tmax_item else None
        tmin = tmin_item[0] if tmin_item else None
        tmax_display = _temperature_display_text(tmax_item[1]) if tmax_item else None
        tmin_display = _temperature_display_text(tmin_item[1]) if tmin_item else None
        rows.append({
            "date": start_dt.strftime("%Y-%m-%d") if start_dt else start[:10],
            "date_label": f"{start_dt.month}月{start_dt.day}日" if start_dt else start[:10],
            "start_time": start,
            "end_time": end,
            # 单地区查询保留接口原始天气演变（如“小雨转多云”）；
            # 只有多地区查询才拆词聚合，避免把“转”的先后关系丢掉。
            "weather": (
                raw_weather[0]
                if len(raw_weather) == 1
                else "、".join(_top_items(weather)) if weather else None
            ),
            "tmax_c": tmax,
            "tmin_c": tmin,
            "tmax_display": tmax_display,
            "tmin_display": tmin_display,
            "temperature_range_c": (
                f"{tmin_display}~{tmax_display}"
                if tmin_display is not None and tmax_display is not None else None
            ),
            "diurnal_range_c": tmax - tmin if tmax is not None and tmin is not None else None,
            "diurnal_range_display": (
                _difference_text(tmax_item[1], tmin_item[1]) if tmax_item and tmin_item else None
            ),
            # 展示层直接使用接口 EDA 原文；多地区查询只去重拼接，不拆分或改写内容。
            "EDA": "；".join(dict.fromkeys(raw_eda_values)) if raw_eda_values else None,
            "wind_force": f"{min(wind_levels)}-{max(wind_levels)}级" if wind_levels else None,
            "wind_direction": "、".join(_top_items(wind_directions)) if wind_directions else None,
            "visibility_min_km": round(min_visibility, 1) if min_visibility is not None else None,
            "visibility_min_display": min_visibility_display,
            "visibility_unit": "千米",
            "visibility_min_regions": list(dict.fromkeys(min_visibility_regions)),
            "rainfall_max_24h_mm": round(max_rain, 1) if max_rain is not None else None,
            "rainfall_max_24h_display": max_rain_display,
            "rainfall_max_regions": list(dict.fromkeys(max_rain_regions)),
            "rainfall_level": _rain_level(max_rain),
        })
    return rows


def _temperature_analysis(daily: list[dict]) -> dict:
    valid = [row for row in daily if row.get("tmax_c") is not None and row.get("tmin_c") is not None]
    if not valid:
        return {"trend": "无有效气温数据", "highest": None, "lowest": None, "largest_diurnal_range": None}
    highest = max(valid, key=lambda row: float(row["tmax_c"]))
    lowest = min(valid, key=lambda row: float(row["tmin_c"]))
    largest_range = max(valid, key=lambda row: float(row.get("diurnal_range_c") or 0))
    means = [(float(row["tmax_c"]) + float(row["tmin_c"])) / 2 for row in valid]
    if len(means) < 3:
        trend = "气温变化不明显"
    else:
        peak_index = max(range(len(means)), key=means.__getitem__)
        if 0 < peak_index < len(means) - 1 and means[peak_index] - means[0] >= 1 and means[peak_index] - means[-1] >= 1:
            trend = "先升后降"
        elif means[-1] - means[0] >= 1:
            trend = "逐步回升"
        elif means[0] - means[-1] >= 1:
            trend = "总体下降"
        else:
            trend = "气温起伏不大"
    return {
        "trend": trend,
        "highest": {
            "date": highest["date"],
            "date_label": highest["date_label"],
            "temperature_c": highest["tmax_c"],
            "temperature_display": highest.get("tmax_display"),
        },
        "lowest": {
            "date": lowest["date"],
            "date_label": lowest["date_label"],
            "temperature_c": lowest["tmin_c"],
            "temperature_display": lowest.get("tmin_display"),
        },
        "largest_diurnal_range": {
            "date": largest_range["date"],
            "date_label": largest_range["date_label"],
            "temperature_difference_c": largest_range["diurnal_range_c"],
            "temperature_difference_display": largest_range.get("diurnal_range_display"),
        },
    }


def _visibility_analysis(daily: list[dict]) -> dict:
    """按照接口原始单位千米分析最低能见度，低于 1 表示不足 1 千米。"""
    valid = [row for row in daily if row.get("visibility_min_km") is not None]
    low = [row for row in valid if float(row["visibility_min_km"]) < 1]
    minimum = min(valid, key=lambda row: float(row["visibility_min_km"])) if valid else None
    return {
        "minimum": (
            {
                "date": minimum["date"],
                "date_label": minimum["date_label"],
                "visibility_km": minimum["visibility_min_km"],
                "regions": minimum["visibility_min_regions"],
            }
            if minimum else None
        ),
        "below_1km_dates": [
            {
                "date": row["date"],
                "date_label": row["date_label"],
                "visibility_km": row["visibility_min_km"],
            }
            for row in low
        ],
        "has_persistent_low_visibility": len(low) >= 3,
        "unit": "千米",
        "air_quality_available": False,
        "note": "滚动预报未返回 AQI/PM2.5，不得仅依据能见度判定空气质量。",
    }


def _rainfall_by_period(periods: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = {}
    for item in periods:
        start = str(item.get("start_time") or "")
        end = str(item.get("end_time") or "")
        if start and end:
            groups.setdefault((start, end), []).append(item)
    result: list[dict] = []
    for (start, end), items in sorted(groups.items(), key=lambda pair: _parse_period_time(pair[0][0]) or datetime.min):
        values: dict[str, float] = {}
        for item in items:
            value = _to_float(item.get("TP1H"))
            if value is None:
                continue
            region = str(item.get("region_display") or item.get("region") or "")
            values[region] = value
        if values:
            max_region, max_value = max(values.items(), key=lambda pair: pair[1])
        else:
            max_region, max_value = "", None
        result.append({
            "start_time": start,
            "end_time": end,
            "rainfall_by_region": values,
            "local_max_24h_mm": max_value,
            "local_max_region": max_region or None,
            "local_max_level": _rain_level(max_value),
            "has_rainstorm": max_value is not None and max_value >= RAINSTORM_24H_MM,
        })
    return result


def _build_rainstorm_process(period_group: list[dict]) -> dict:
    affected_regions = sorted({
        region
        for period in period_group
        for region, value in period["rainfall_by_region"].items()
        if value >= RAINSTORM_24H_MM
    })
    cumulative = {
        region: round(sum(period["rainfall_by_region"].get(region, 0.0) for period in period_group), 1)
        for region in affected_regions
    }
    local_period = max(
        period_group,
        key=lambda period: float(period.get("local_max_24h_mm") or -1),
    )
    cumulative_values = list(cumulative.values())
    local_max = local_period.get("local_max_24h_mm")
    return {
        "start_time": period_group[0]["start_time"],
        "end_time": period_group[-1]["end_time"],
        "cumulative_rain_min_mm": min(cumulative_values) if cumulative_values else None,
        "cumulative_rain_max_mm": max(cumulative_values) if cumulative_values else None,
        "cumulative_rainfall_by_region": cumulative,
        "local_max_24h_mm": local_max,
        "local_max_region": local_period.get("local_max_region"),
        "local_max_start_time": local_period.get("start_time"),
        "local_max_end_time": local_period.get("end_time"),
        "local_max_level": _rain_level(_to_float(local_max)),
        "affected_regions": affected_regions,
        "has_severe_rainstorm": local_max is not None and float(local_max) >= SEVERE_RAINSTORM_24H_MM,
    }


def _rainstorm_analysis(periods: list[dict]) -> dict:
    rainfall_periods = _rainfall_by_period(periods)
    groups: list[list[dict]] = []
    current: list[dict] = []
    for period in rainfall_periods:
        if not period["has_rainstorm"]:
            if current:
                groups.append(current)
                current = []
            continue
        if current:
            previous_end = _parse_period_time(current[-1]["end_time"])
            current_start = _parse_period_time(period["start_time"])
            if previous_end != current_start:
                groups.append(current)
                current = []
        current.append(period)
    if current:
        groups.append(current)
    processes = [_build_rainstorm_process(group) for group in groups]
    severe_processes = [process for process in processes if process["has_severe_rainstorm"]]
    all_periods = [period for period in rainfall_periods if period.get("local_max_24h_mm") is not None]
    local = max(all_periods, key=lambda period: float(period["local_max_24h_mm"])) if all_periods else None
    return {
        "has_valid_rainfall_data": bool(all_periods),
        "valid_rainfall_period_count": len(all_periods),
        "has_severe_rainstorm": bool(severe_processes),
        "has_extraordinary_rainstorm": bool(
            local and float(local["local_max_24h_mm"]) >= EXTRAORDINARY_RAINSTORM_24H_MM
        ),
        "local_max_24h_mm": local.get("local_max_24h_mm") if local else None,
        "local_max_region": local.get("local_max_region") if local else None,
        "local_max_start_time": local.get("start_time") if local else None,
        "local_max_end_time": local.get("end_time") if local else None,
        "local_max_level": local.get("local_max_level") if local else "无有效数据",
        "processes": processes,
        "severe_processes": severe_processes,
        "affected_region_definition": "任一自然日 24 小时降水量达到 50 毫米及以上的区域",
    }


def _weather_focus_analysis(daily: list[dict]) -> dict:
    rainy = [row for row in daily if float(row.get("rainfall_max_24h_mm") or 0) >= 0.1]
    rain_periods: list[dict] = []
    current: list[dict] = []
    for row in daily:
        if row not in rainy:
            if current:
                rain_periods.append({"start_date": current[0]["date"], "end_date": current[-1]["date"]})
                current = []
            continue
        current.append(row)
    if current:
        rain_periods.append({"start_date": current[0]["date"], "end_date": current[-1]["date"]})
    changes = []
    for previous, current_row in zip(daily, daily[1:]):
        if previous.get("tmax_c") is None or current_row.get("tmax_c") is None:
            continue
        changes.append({
            "from_date": previous["date"],
            "to_date": current_row["date"],
            "tmax_change_c": float(current_row["tmax_c"]) - float(previous["tmax_c"]),
        })
    largest_cooling = min(changes, key=lambda item: item["tmax_change_c"]) if changes else None
    return {"rain_periods": rain_periods, "largest_cooling": largest_cooling}


def analyze_rolling_forecast_periods(periods: list[dict]) -> dict:
    daily = build_daily_summary(periods)
    return {
        "daily_summary": daily,
        "temperature_analysis": _temperature_analysis(daily),
        "visibility_analysis": _visibility_analysis(daily),
        "rainstorm_analysis": _rainstorm_analysis(periods),
        "weather_focus": _weather_focus_analysis(daily),
    }


def _build_point_mode_query_point(
    point_mode: bool,
    lon: float | None,
    lat: float | None,
    point_name: str,
    matched_region: str,
) -> dict | None:
    """点位模式下构造结构化 query_point 字段（区域模式返回 None）。"""
    if not point_mode:
        return None
    return {
        "point_name": point_name or None,
        "matched_region": matched_region or None,
        "lon": format_rolling_forecast_coord(lon) if lon is not None else None,
        "lat": format_rolling_forecast_coord(lat) if lat is not None else None,
    }


def _build_past_date_payload(
    window: dict,
    point_mode: bool,
    region_names: list[str],
    lon: float | None,
    lat: float | None,
    point_name: str,
    matched_region: str,
    now: datetime,
) -> dict:
    """将过去日历日转换为结构化历史日期标记，由调用方转历史实况查询。"""
    target_start = window["target_start"]
    target_end = window["target_end"]
    query_point = _build_point_mode_query_point(point_mode, lon, lat, point_name, matched_region)
    return {
        "status": "past_date",
        "query_mode": "historical_obs_request",
        "data_source": "历史日期",
        "historical_window": {
            "target_start": target_start.strftime("%Y-%m-%d %H:%M:%S"),
            "target_end": target_end.strftime("%Y-%m-%d %H:%M:%S"),
            "forecast_start_date": window.get("forecast_start_date"),
            "forecast_days": window.get("forecast_days"),
        },
        "query_regions": region_names,
        "query_point": query_point,
        "query_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "message": (
            f"您查询的目标日期（{target_start.strftime('%Y-%m-%d')}）已属于历史日期，"
            "滚动预报仅覆盖未来时效。请调用 query_poi_historical_weather "
            "查询该点位/区域该日期的历史实况；若该日无历史实况数据，请如实告知用户暂不支持该日期的历史查询。"
        ),
    }


def _build_calendar_error_payload(
    message: str,
    point_mode: bool,
    region_names: list[str],
    lon: float | None,
    lat: float | None,
    point_name: str,
    matched_region: str,
    now: datetime,
) -> dict:
    """将日历日窗口的 ValueError（超时效等）转换为清晰的结构化提示。"""
    query_point = _build_point_mode_query_point(point_mode, lon, lat, point_name, matched_region)
    return {
        "status": "out_of_range",
        "query_mode": "calendar_out_of_range",
        "data_source": "滚动预报",
        "message": f"{message}，请调整查询日期（滚动预报仅覆盖未来 10 天）。",
        "query_regions": region_names,
        "query_point": query_point,
        "query_time": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _resolve_ec_target_date(user_query: str, forecast_start_date: str, now: datetime) -> date | None:
    """EC 回退需要的目标日历日：优先 forecast_start_date，否则从 user_query 显式日期解析。"""
    if forecast_start_date:
        try:
            return _parse_date(forecast_start_date)
        except Exception:
            pass
    dates = _extract_explicit_query_dates(user_query, now)
    return dates[0] if dates else None


def _build_ec_rain_fallback_payload(
    sampled: dict, target: date, point_name: str, matched_region: str,
    lon: float | None, lat: float | None, now: datetime,
) -> dict:
    """EC 降水回退的结构化载荷：只含降雨，气温/风/能见度超时效不提供（零编造）。"""
    label = (point_name or matched_region or "指定点位").strip()
    return {
        "status": "ec_rain_fallback",
        "query_mode": "calendar_ec_rain_point",
        "data_source": "ECMWF AIFS",
        "target_date": target.isoformat(),
        "rain_mm": sampled["rain_mm"],
        "window_start": sampled["window_start"].strftime("%Y-%m-%d %H:%M"),
        "window_hours": sampled["window_hours"],
        "point_name": label,
        "query_point": _build_point_mode_query_point(True, lon, lat, point_name, matched_region),
        "query_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "message": "该日期超出滚动预报未来 10 天时效，已改用 ECMWF AIFS 累计降水产品，仅提供降雨参考。",
    }


def _try_ec_rain_fallback(
    user_query: str, forecast_start_date: str,
    lon: float | None, lat: float | None,
    point_name: str, matched_region: str, now: datetime,
) -> dict | None:
    """out_of_range + 点位模式时尝试 EC 降水回退；目标日无法解析或 EC 无数据返回 None。"""
    target = _resolve_ec_target_date(user_query, forecast_start_date, now)
    if target is None:
        return None
    try:
        from haihe_mcp_tools import sample_ec_point_daily_rain  # 惰性 import 防循环
    except Exception:
        return None
    try:
        sampled = sample_ec_point_daily_rain(lon, lat, target)
    except Exception:
        return None
    if sampled is None:
        return None
    return _build_ec_rain_fallback_payload(
        sampled, target, point_name, matched_region, lon, lat, now
    )


def _cached_rolling_forecast_request(params: dict) -> dict:
    """带 TTL 缓存的滚动预报接口查询。

    同一起报时次 + 区域/点位 + 时段的查询在 TTL 内直接返回缓存，避免重复
    打内网接口。缓存的是原始 JSON payload（不缓存格式化后的结果），这样
    每次调用仍会重新格式化，query_time 保持最新。
    """
    import time as _time

    key = json.dumps(params, sort_keys=True, ensure_ascii=False)
    now_ts = _time.time()
    hit = _rolling_forecast_cache.get(key)
    if hit is not None and (now_ts - hit[0]) < ROLLING_FORECAST_CACHE_TTL:
        print(f"[FC-CACHE] HIT key_len={len(key)} age={now_ts-hit[0]:.0f}s", flush=True)
        return hit[1]
    print(f"[FC-CACHE] MISS key_len={len(key)}", flush=True)

    response = requests.get(
        ROLLING_FORECAST_API_URL, params=params, timeout=ROLLING_FORECAST_TIMEOUT
    )
    response.raise_for_status()
    payload = response.json()
    _rolling_forecast_cache[key] = (now_ts, payload)
    return payload


def query_rolling_forecast_core(
    user_query: str,
    regions: str = "",
    lon: float | None = None,
    lat: float | None = None,
    point_name: str = "",
    matched_region: str = "",
    fcst_time: str | None = None,
    start_period: int = 0,
    end_period: int = 240,
    interval: int = 12,
    forecast_start_date: str = "",
    forecast_days: int = 0,
    query_window: str = "",
    now: datetime | None = None,
) -> dict:
    """执行滚动预报查询，日历日入参存在时覆盖底层时效参数。"""
    now = now or time_source.now(TIANJIN_TIMEZONE)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TIANJIN_TIMEZONE)
    point_mode = lon is not None and lat is not None
    if point_mode:
        lon_text = format_rolling_forecast_coord(lon)
        lat_text = format_rolling_forecast_coord(lat)
        coord = f"{lon_text}_{lat_text}"
        label = (point_name or matched_region or "指定点位").strip()
        region_names = [label]
        locations = [{
            "name": label,
            "region": matched_region or label,
            "lon": lon_text,
            "lat": lat_text,
            "coord": coord,
        }]
        lons, lats = [lon_text], [lat_text]
    else:
        text = regions or user_query
        city_names = match_basin_cities(text)
        if city_names and has_matched_rolling_region(text):
            # 天津区域 + 外埠城市混合（"蓟州和唐山明天天气"）：各自坐标都采样。
            region_names = parse_rolling_forecast_regions(text)
            region_names += [name for name in city_names if name not in region_names]
        elif city_names:
            # 纯外埠城市（"明天唐山的天气怎么样"）：按城市坐标采数据湖海河网格，
            # 不再静默退回天津市区代表点（2026-08-24 用户口径：除天津外都用海河网格数据）。
            region_names = city_names
        else:
            region_names = parse_rolling_forecast_regions(text)
        coords = [_region_or_city_coord(name) for name in region_names]
        locations = [
            {
                "name": name,
                "region": name,
                "lon": coord.split("_")[0],
                "lat": coord.split("_")[1],
                "coord": coord,
            }
            for name, coord in zip(region_names, coords)
        ]
        lons = [coord.split("_")[0] for coord in coords]
        lats = [coord.split("_")[1] for coord in coords]

    hourly_window = (
        resolve_named_hour_query_window(query_window=query_window, now=now)
        or resolve_future_hour_query_window(user_query=user_query, now=now)
        or resolve_current_hour_query_window(user_query=user_query, now=now)
    )
    calendar_window = None
    historical_window = None
    calendar_error = None
    if hourly_window is None:
        try:
            calendar_window = resolve_requested_calendar_window(
                user_query=user_query,
                forecast_start_date=forecast_start_date,
                forecast_days=forecast_days,
                now=now,
            )
        except ValueError as exc:
            message = str(exc)
            if "240" in message or "时效" in message or "负数" in message:
                calendar_error = message
            else:
                raise
    if calendar_window is not None:
        target_day = calendar_window.get("target_start")
        if target_day is not None and getattr(target_day, "date", None) and target_day.date() < now.date():
            # 目标日早于今天：属历史日期，返回结构化标记，由调用方转历史实况查询。
            historical_window = calendar_window
            calendar_window = None
    if historical_window is not None:
        return _build_past_date_payload(
            historical_window, point_mode, region_names, lon, lat, point_name, matched_region, now
        )
    if calendar_error is not None:
        if point_mode:
            ec = _try_ec_rain_fallback(
                user_query, forecast_start_date, lon, lat, point_name, matched_region, now
            )
            if ec is not None:
                return ec
        return _build_calendar_error_payload(
            calendar_error, point_mode, region_names, lon, lat, point_name, matched_region, now
        )
    # 时段化收窄（2026-08-24 甲方口径）："今天下午和今天晚上"等带时段词的单日日历窗口
    # 收窄为逐小时时段窗口（只覆盖所问时段），不再铺开整日。历史/越界窗口已提前 return，
    # 这里 calendar_window 必是未来/今天窗口。
    if hourly_window is None and calendar_window is not None:
        tod_window = _narrow_calendar_window_to_time_of_day(calendar_window, user_query, now)
        if tod_window is not None:
            hourly_window = tod_window
            calendar_window = None  # 时段化后走 hourly 分支，不再走整日日历窗口
    if hourly_window:
        selected_fcst_time = hourly_window["fcst_time"]
        start_period = hourly_window["start_period"]
        end_period = hourly_window["end_period"]
        interval = int(hourly_window["interval"])
        mode_prefix = hourly_window.get("mode") or "future_hour"
        query_mode = f"{mode_prefix}_{'point' if point_mode else 'region'}"
    elif calendar_window:
        selected_fcst_time = calendar_window["fcst_time"]
        start_period = calendar_window["start_period"]
        end_period = calendar_window["end_period"]
        interval = 24
        query_mode = "calendar_daily_point" if point_mode else "calendar_daily"
    else:
        selected_fcst_time = fcst_time or select_rolling_forecast_time(now=now)
        query_mode = "point" if point_mode else "region"

    params = {
        "fcstTime": selected_fcst_time,
        "element": ",".join(ROLLING_FORECAST_ELEMENTS),
        "lon": ",".join(lons),
        "lat": ",".join(lats),
        "mode": "GDMODE",
        "startPeriod": str(start_period),
        "endPeriod": str(end_period),
        "interval": str(interval),
        "count": "0",
        "stationType": "3",
    }
    response = _cached_rolling_forecast_request(params)
    payload = response
    result_data = payload.get("resultData") or {}
    periods = build_rolling_forecast_periods(
        result_data=result_data,
        regions=region_names,
        fcst_time=selected_fcst_time,
        start_period=start_period,
        interval=interval,
        locations=locations,
    )
    result = {
        "data_source": "天津市气象台滚动预报",
        "forecast_type": "rolling_forecast",
        "query_mode": query_mode,
        "query_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "fcst_time": selected_fcst_time,
        "query_regions": region_names,
        "query_point": {
            "point_name": point_name or None,
            "matched_region": matched_region or None,
            "lon": lons[0] if point_mode else None,
            "lat": lats[0] if point_mode else None,
        } if point_mode else None,
        "elements": ROLLING_FORECAST_ELEMENT_NAMES,
        "start_period": start_period,
        "end_period": end_period,
        "interval_hours": interval,
        "forecast_start_date": calendar_window["forecast_start_date"] if calendar_window else None,
        "forecast_start_time": (
            (hourly_window or calendar_window)["target_start"].strftime("%Y-%m-%d %H:%M")
            if hourly_window or calendar_window else None
        ),
        "forecast_end_time": (
            (hourly_window or calendar_window)["target_end"].strftime("%Y-%m-%d %H:%M")
            if hourly_window or calendar_window else None
        ),
        "api_code": payload.get("code"),
        "api_message": payload.get("message"),
        "periods": periods,
    }
    if hourly_window:
        if hourly_window.get("mode") == "time_of_day":
            # 时段化查询（"今天下午/晚上"）：聚合为该时段单条汇总，不产逐小时行。
            result["time_of_day_label"] = _time_of_day_label(user_query, hourly_window["target_start"], now)
            result["time_of_day_summary"] = _time_of_day_summary_rows(periods)
        else:
            result["hourly_summary"] = [
                {
                    "region": item.get("region_display") or item.get("region"),
                    "start_time": item.get("start_time"),
                    "end_time": item.get("end_time"),
                    "period_label": item.get("period_label"),
                    "weather": item.get("WEA"),
                    "tmax": _temperature_display_text(item.get("TMAX")),
                    "tmin": _temperature_display_text(item.get("TMIN")),
                    "EDA": item.get("EDA"),
                    "wind": item.get("EDA"),
                    "rainfall_mm": item.get("TP1H"),
                    "visibility_min_km": _to_positive_float(item.get("VISMIN")),
                    "visibility_unit": "千米",
                }
                for item in periods
                if isinstance(item, dict)
            ]
    if calendar_window:
        result.update(analyze_rolling_forecast_periods(periods))
    # 区域模式附带静态隐患点 + 风险等级；点位模式只附风险等级，供点位天气/游玩回答
    # 单独渲染【本次风险等级】，不与 5km POI 静态隐患提醒混为一谈。
    # 风险接口每起报时次只出 24h：多日窗口按目标日 08 时逐日查询并合并；
    # 无日历窗口（普通当前/今天下午）沿用最近起报时次 + 前一周期回退逻辑。
    risk_fcst_times = _risk_fcst_times_from_window(calendar_window, now)
    if point_mode:
        point_levels = _query_region_risk_levels(lons[0], lats[0], risk_fcst_times)
        result["point_risk_levels"] = point_levels
        result["point_risk_levels_available"] = point_levels is not None
    elif region_names:
        region_hazards = []
        for name, lon_t, lat_t in zip(region_names, lons, lats):
            hazards = _query_region_hazards(lon_t, lat_t, risk_fcst_times)
            if hazards:
                region_hazards.append(
                    {
                        "region": name,
                        "region_display": _display_region(name),
                        **hazards,
                    }
                )
        if region_hazards:
            result["region_hazards"] = region_hazards
    if os.getenv("DEBUG_ROLLING_FORECAST", "").strip().lower() in {"1", "true", "yes", "on"}:
        print("[query_rolling_forecast] full result:\n" + json.dumps(result, ensure_ascii=False, default=str, indent=2),
              flush=True)
    return result
