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
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import requests


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
    now = now or datetime.now(TIANJIN_TIMEZONE)
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

    now = now or datetime.now(TIANJIN_TIMEZONE)
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


def _extract_explicit_query_dates(user_query: str, current: datetime) -> list[date]:
    """从用户原问中提取明确公历日期，未写年份时取最近的未来同月同日。"""
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

    short_pattern = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日")
    for match in short_pattern.finditer(text):
        if any(start <= match.start() < end for start, end in occupied_spans):
            continue
        try:
            candidate = date(current.year, int(match.group(1)), int(match.group(2)))
            if candidate < current.date():
                candidate = date(current.year + 1, candidate.month, candidate.day)
            matches.append((match.start(), candidate))
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
        current = now or datetime.now(TIANJIN_TIMEZONE)
        if current.tzinfo is None:
            current = current.replace(tzinfo=TIANJIN_TIMEZONE)
        return resolve_calendar_query_window(current.date() + timedelta(days=1), 7, now=current)
    current = now or datetime.now(TIANJIN_TIMEZONE)
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
    if "今天" in query or "今日" in query:
        return resolve_calendar_query_window(current.date(), 1, now=current)
    explicit_dates = _extract_explicit_query_dates(user_query, current)
    if explicit_dates:
        start_date = explicit_dates[0]
        days = 1
        if len(explicit_dates) >= 2 and explicit_dates[1] >= start_date:
            days = min((explicit_dates[1] - start_date).days + 1, 10)
        return resolve_calendar_query_window(start_date, days, now=current)
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
    current = now or datetime.now(TIANJIN_TIMEZONE)
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
    current = now or datetime.now(TIANJIN_TIMEZONE)
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
    current = now or datetime.now(TIANJIN_TIMEZONE)
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
    now = now or datetime.now(TIANJIN_TIMEZONE)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TIANJIN_TIMEZONE)
    return _latest_available_fcst_time(now).strftime("%Y%m%d%H%M%S")


_BASIN_STRONG_KEYWORDS = ("海河流域", "流域", "河系")
# 裸河名只在无 POI 语境时视为流域问题；关键词清单不求穷尽，
# 运行时守卫只是 prompt 规则之外的兜底。
_BASIN_RIVER_NAMES = (
    "大清河", "子牙河", "永定河", "北三河",
    "漳卫南运河", "漳卫河", "徒骇马颊河", "黑龙港",
    "滦河", "潮白河", "蓟运河", "海河干流",
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


def format_rolling_forecast_coord(value: float | str) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value).strip()


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
                "period_label": f"{start_dt.strftime('%m月%d日%H时')}-{end_dt.strftime('%m月%d日%H时')}",
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


def _difference_text(high: Any, low: Any) -> str | None:
    """计算派生差值，避免二进制浮点数尾差进入代码生成的表格或关键节点。"""
    try:
        value = Decimal(str(high)) - Decimal(str(low))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return format(value.normalize(), "f")


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
            if (value := _to_float(item.get("VISMIN"))) is not None:
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
        tmax_display = _source_value_text(tmax_item[1]) if tmax_item else None
        tmin_display = _source_value_text(tmin_item[1]) if tmin_item else None
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
    now = now or datetime.now(TIANJIN_TIMEZONE)
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
        region_names = parse_rolling_forecast_regions(regions or user_query)
        coords = [ROLLING_FORECAST_COORDS[name] for name in region_names]
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
    calendar_window = resolve_requested_calendar_window(
        user_query=user_query,
        forecast_start_date=forecast_start_date,
        forecast_days=forecast_days,
        now=now,
    ) if hourly_window is None else None
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
        result["hourly_summary"] = [
            {
                "region": item.get("region_display") or item.get("region"),
                "start_time": item.get("start_time"),
                "end_time": item.get("end_time"),
                "period_label": item.get("period_label"),
                "weather": item.get("WEA"),
                "tmax": item.get("TMAX"),
                "tmin": item.get("TMIN"),
                "EDA": item.get("EDA"),
                "wind": item.get("EDA"),
                "rainfall_mm": item.get("TP1H"),
                "visibility_min_km": item.get("VISMIN"),
                "visibility_unit": "千米",
            }
            for item in periods
            if isinstance(item, dict)
        ]
    if calendar_window:
        result.update(analyze_rolling_forecast_periods(periods))
    if os.getenv("DEBUG_ROLLING_FORECAST", "").strip().lower() in {"1", "true", "yes", "on"}:
        print("[query_rolling_forecast] full result:\n" + json.dumps(result, ensure_ascii=False, default=str, indent=2),
              flush=True)
    return result