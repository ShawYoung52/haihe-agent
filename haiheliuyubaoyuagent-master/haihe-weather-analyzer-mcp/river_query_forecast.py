"""河流/河系降雨预报问题的名称与时间窗口解析。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

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

_RIVER_CORRIDOR_RE = re.compile(r"([\u4e00-\u9fff]{1,8}?)河道")
_RIVER_NAME_RE = re.compile(r"([\u4e00-\u9fff]{1,8}河)")
_RAIN_OR_WEATHER_PREDICATE_RE = re.compile(r"有雨|下雨|降雨|降水|雨量|天气")
_FUTURE_DAYS_RE = re.compile(r"未来\s*([^\s，。！？?、]{1,8}?)\s*天")
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

    if "今天晚上" in query or "今晚" in query:
        day_start = datetime.combine(current.date(), time.min, tzinfo=TIANJIN_TIMEZONE)
        evening_start = day_start.replace(hour=18)
        current_hour = current.replace(minute=0, second=0, microsecond=0)
        start = max(evening_start, current_hour)
        return [ForecastPeriod("今天晚上", start, day_start + timedelta(days=1))]

    future_match = _FUTURE_DAYS_RE.search(query)
    if future_match:
        count = _parse_day_count(future_match.group(1))
        return [
            _day_period(current.date() + timedelta(days=offset), f"未来第{offset}天")
            for offset in range(1, count + 1)
        ]

    if "后天" in query:
        return [_day_period(current.date() + timedelta(days=2), "后天")]
    if "明天" in query or "明日" in query:
        return [_day_period(current.date() + timedelta(days=1), "明天")]
    return [_day_period(current.date(), "今天")]


def _day_period(day: date, label: str) -> ForecastPeriod:
    start = datetime.combine(day, time.min, tzinfo=TIANJIN_TIMEZONE)
    return ForecastPeriod(label, start, start + timedelta(days=1))


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
