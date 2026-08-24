"""滚动预报回复的确定性数据区块渲染与组装。"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .request_intent_policy import is_rolling_activity_query

try:
    # 包式导入：python -m / pytest 从项目根目录运行。
    from ..utils.markdown import normalize_markdown_ranges
except ImportError:
    # Chainlit 生产启动目录为 chainlitexam，模块以 tools.* 导入。
    from utils.markdown import normalize_markdown_ranges


_CODE_OWNED_HEADERS = {
    "【未来7天预报表】",
    "【明日预报】",
    "【今日预报】",
    "【本周末天气预报】",
    "【逐日天气预报】",
    "【未来一周气温预报】",
    "【明日气温预报】",
    "【逐日能见度】",
    "【逐日能见度与空气质量】",
    "【未来小时预报】",
    "【周末详细预报】",
    "【逐日活动预报】",
    "【过程详情】",
    "【关键节点】",
    "【天气实况】",
    "【天气预报】",
}
_CODE_OWNED_FORECAST_HEADER_KEYWORDS = ("天气预报", "预报详情")
_CODE_OWNED_ACTIVITY_HEADER_KEYWORDS = ("活动预报", "游玩建议", "活动建议", "出行建议")


def is_current_rolling_weather_query(user_text: str) -> bool:
    """识别需改走天擎聚合实况工具的固定问法。"""
    text = str(user_text or "")
    return (
        "滚动" in text
        and any(word in text for word in ("当前时刻", "当前", "现在", "实时"))
        and any(word in text for word in ("气象信息", "天气", "气象"))
        and "实况" in text
    )


def build_current_rolling_weather_query_plan(
    user_text: str,
) -> list[dict[str, Any]]:
    """旧版滚动预报双窗口计划，仅保留兼容；当前实况问答已不再调用。"""
    common = {
        "user_query": user_text,
        "regions": "",
    }
    return [
        {
            **common,
            "query_window": "current_hour",
        },
        {
            **common,
            "query_window": "next_12_hours",
        },
    ]


def _cell(value: Any, default: str = "—") -> str:
    if value is None or str(value).strip() == "":
        return default
    text = normalize_markdown_ranges(value).strip()
    text = text.replace("\r", " ").replace("\n", " ").replace("|", "｜")
    return re.sub(r"\s+", " ", text)


def _markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(_cell(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def _valid_number(value: Any) -> float | None:
    if value in (None, "", "--", "9999", "9999.0", 9999, 9999.0):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _display_number(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def _temperature_display_text(value: Any) -> str | None:
    """回答展示层的温度统一四舍五入为整数。"""
    if value is None or str(value).strip() in {"", "—"}:
        return None
    try:
        rounded = Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return str(value).strip()
    return format(rounded, "f")


def _temperature_range_display(item: dict) -> str | None:
    """将日预报气温范围的上下限分别四舍五入，兼容新旧字段。"""
    tmin = item.get("tmin_display")
    if tmin is None:
        tmin = item.get("tmin_c")
    tmax = item.get("tmax_display")
    if tmax is None:
        tmax = item.get("tmax_c")
    rounded_min = _temperature_display_text(tmin)
    rounded_max = _temperature_display_text(tmax)
    if rounded_min is not None and rounded_max is not None:
        return f"{rounded_min}~{rounded_max}"
    raw = str(item.get("temperature_range_c") or "").strip()
    rounded_raw = _round_temperature_values_in_text(raw)
    if rounded_raw != raw:
        return rounded_raw.replace("～", "~")
    if "~" in raw or "～" in raw:
        separator = "~" if "~" in raw else "～"
        parts = [part.strip() for part in raw.split(separator, 1)]
        rounded = [_temperature_display_text(part) for part in parts]
        if all(part is not None for part in rounded):
            return f"{rounded[0]}~{rounded[1]}"
    return _temperature_display_text(raw) or None


def _round_temperature_values_in_text(text: str) -> str:
    """兜底修正核心结论中的温度小数，不改变其它数值。"""
    range_pattern = re.compile(
        r"(-?\d+(?:\.\d+)?)\s*([~～至到])\s*(-?\d+(?:\.\d+)?)\s*(摄氏度|℃|°C|°|度)"
    )
    scalar_pattern = re.compile(r"(-?\d+(?:\.\d+)?)\s*(摄氏度|℃|°C|°|度)")

    def round_match(match: re.Match) -> str:
        left = _temperature_display_text(match.group(1)) or match.group(1)
        right = _temperature_display_text(match.group(3)) or match.group(3)
        return f"{left}{match.group(2)}{right}{match.group(4)}"

    text = range_pattern.sub(round_match, text)
    return scalar_pattern.sub(
        lambda match: f"{_temperature_display_text(match.group(1)) or match.group(1)}{match.group(2)}",
        text,
    )


def sanitize_forecast_core_summary(text: str, user_text: str = "") -> str:
    """收紧预报核心结论：不机械罗列无雨/风况，并统一温度整数展示。"""
    cleaned = _round_temperature_values_in_text(str(text or "").strip())
    query = str(user_text or "")
    rain_requested = any(word in query for word in ("下雨", "有雨", "降雨", "降水", "雨量", "暴雨", "强降雨"))
    wind_requested = any(word in query for word in ("风力", "风速", "风向", "大风"))

    if rain_requested:
        cleaned = re.sub(
            r"(?:预计|将)?\s*(?:无|没有)(?:明显)?(?:降水|降雨|雨)",
            "预计不会下雨",
            cleaned,
        )
    else:
        cleaned = re.sub(
            r"(?:(?:预计|将|未来[^，。；;]*?)\s*)?(?:无|没有)(?:明显)?(?:降水|降雨|雨)(?:情况)?\s*",
            "",
            cleaned,
        )

    if wind_requested:
        cleaned = re.sub(
            r"风力\s*(?:为|是)?\s*([0-9]+(?:\s*[～~至-]\s*[0-9]+)?\s*级?)",
            lambda match: f"{match.group(1).replace(' ', '')}风",
            cleaned,
        )
    else:
        cleaned = re.sub(
            r"(?:(?:预计|将|未来[^，。；;]*?)\s*)?风力\s*(?:为|是)?\s*[^，。；;!?！？]*",
            "",
            cleaned,
        )

    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"([，、；;])\s*[，、；;]+", r"\1", cleaned)
    cleaned = re.sub(r"^[，、；;]\s*|[，、；;]\s*(?=[。！？!?]|$)", "", cleaned).strip()
    if not re.sub(r"[，、；;。！？!?\s]", "", cleaned):
        return "暂无需要特别说明的天气现象。"
    return cleaned or "暂无需要特别说明的天气现象。"


def _rolling_snapshot_stats(periods: list[dict]) -> dict[str, Any]:
    valid_periods = [item for item in periods if isinstance(item, dict)]
    rain_rows: list[tuple[str, float]] = []
    weather_values: list[str] = []
    temperatures: list[float] = []
    for item in valid_periods:
        rain = _valid_number(item.get("TP1H"))
        if rain is not None:
            rain_rows.append((str(item.get("region_display") or item.get("region") or "未知地区"), rain))
        weather = str(item.get("WEA") or "").strip()
        if weather and weather != "--":
            weather_values.append(weather)
        for key in ("TMIN", "TMAX"):
            if (temperature := _valid_number(item.get(key))) is not None:
                temperatures.append(temperature)

    average = sum(value for _, value in rain_rows) / len(rain_rows) if rain_rows else None
    maximum = max((value for _, value in rain_rows), default=None)
    max_regions = list(dict.fromkeys(
        region for region, value in rain_rows if maximum is not None and value == maximum
    ))
    common_weather = [name for name, _ in Counter(weather_values).most_common(3)]
    return {
        "valid_region_count": len(rain_rows),
        "average_rain_mm": average,
        "max_rain_mm": maximum,
        "max_regions": max_regions,
        "common_weather": common_weather,
        "temperature_min_c": min(temperatures) if temperatures else None,
        "temperature_max_c": max(temperatures) if temperatures else None,
    }


def _compact_rolling_snapshot_period(item: dict) -> dict[str, Any]:
    return {
        "地区": item.get("region_display") or item.get("region"),
        "开始时间": item.get("start_time"),
        "结束时间": item.get("end_time"),
        "天气现象": item.get("WEA"),
        "最低气温℃": _temperature_display_text(item.get("TMIN")),
        "最高气温℃": _temperature_display_text(item.get("TMAX")),
        "TP1H毫米": _valid_number(item.get("TP1H")),
        "风况": item.get("EDA"),
    }


def _rolling_snapshot_facts(periods: list[dict], period_name: str) -> dict[str, Any]:
    stats = _rolling_snapshot_stats(periods)
    return {
        "时段名称": period_name,
        "代码统计": {
            "有效地区数": stats["valid_region_count"],
            "TP1H平均降水量毫米": stats["average_rain_mm"],
            "TP1H最大降水量毫米": stats["max_rain_mm"],
            "TP1H最大降水地区": stats["max_regions"],
            "主要天气现象": stats["common_weather"],
            "最低气温℃": _temperature_display_text(stats["temperature_min_c"]),
            "最高气温℃": _temperature_display_text(stats["temperature_max_c"]),
        },
        "接口返回内容": [
            _compact_rolling_snapshot_period(item)
            for item in periods
            if isinstance(item, dict)
        ],
    }


def build_current_rolling_weather_facts(payloads: list[Any]) -> dict[str, Any]:
    """将两次接口返回与代码统计结果组装为模型可用事实。"""
    normalized = [payload if isinstance(payload, dict) else {} for payload in payloads]
    current_periods = normalized[0].get("periods") or [] if normalized else []
    forecast_periods = normalized[1].get("periods") or [] if len(normalized) > 1 else []
    return {
        "weather_observation": _rolling_snapshot_facts(current_periods, "当前1小时"),
        "weather_forecast": _rolling_snapshot_facts(forecast_periods, "未来12小时"),
    }


def build_current_rolling_weather_summary_prompt(user_text: str, payloads: list[Any]) -> str:
    """生成仅要求模型撰写两段结论的提示词。"""
    facts = build_current_rolling_weather_facts(payloads)
    return (
        "你是天津气象业务助手。请根据下方“接口返回内容”和“代码统计”，"
        "分别为当前1小时实况与未来12小时预报撰写一段简洁结论。\n"
        "严格要求：\n"
        "1. 平均降水量、最大降水量及对应地区必须逐字使用“代码统计”，不得重新计算或改变数值。\n"
        "2. 平均降水量为0时，在说明平均值和最大值后，重点总结主要天气现象和气温范围。\n"
        "3. 有降水时，重点总结平均降水、最大降水及地区，可结合接口内容概括天气现象。\n"
        "4. 每段严格且只能有1句，只回答对应时段最重要的天气事实，不得扩展背景、建议或风险。\n"
        "5. “晴”“多云”“阴”不得互换；无降雨不等于晴，只有接口明确返回“晴”时才可写晴或转晴。\n"
        "6. 核心结论不要机械补充“无降水/无降雨”或“风力为X级”等泛化描述；只有用户明确询问降水或风力时才回答对应要素，并使用自然表述。\n"
        "7. 不得生成标题、Markdown表格、逐地区清单、数据来源或技术参数。\n"
        "8. 温度数据统一四舍五入为整数，不得输出小数。\n"
        "9. 只输出一个 JSON 对象，格式为："
        '{"weather_observation_summary":"...","weather_forecast_summary":"..."}\n\n'
        f"用户问题：{user_text}\n\n"
        f"业务事实：{json.dumps(facts, ensure_ascii=False, default=str)}"
    )


def _rolling_snapshot_table(periods: list[dict], title: str) -> str:
    rows = []
    for item in periods:
        if not isinstance(item, dict):
            continue
        tmin = _valid_number(item.get("TMIN"))
        tmax = _valid_number(item.get("TMAX"))
        if tmin is not None and tmax is not None:
            temperature = f"{_temperature_display_text(tmin)}~{_temperature_display_text(tmax)}"
        else:
            temperature = _temperature_display_text(tmax if tmax is not None else tmin) or "—"
        rain = _valid_number(item.get("TP1H"))
        rows.append([
            item.get("region_display") or item.get("region"),
            item.get("period_label") or f"{_format_period_time(item.get('start_time'))}-{_format_period_time(item.get('end_time'))}",
            item.get("WEA"),
            temperature,
            _display_number(rain),
            item.get("EDA"),
        ])
    return f"{title}\n{_markdown_table(['地区', '时段', '天气现象', '气温(℃)', 'TP1H(毫米)', '风况'], rows)}"


def _clean_rolling_weather_summary(value: Any, fallback: str, user_text: str = "") -> str:
    lines = []
    for line in str(value or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped in {"【天气实况】", "【天气预报】"}:
            continue
        if stripped.startswith("|") or stripped.startswith("数据来源"):
            continue
        lines.append(stripped)
    summary = _first_sentence(" ".join(lines).strip())
    return sanitize_forecast_core_summary(summary, user_text) if summary else fallback


def build_current_rolling_weather_answer(
    payloads: list[Any],
    summaries: dict[str, Any] | None = None,
    user_text: str = "",
) -> str:
    """模型结论与代码生成的两张权威表格进行确定性组装。"""
    normalized = [payload if isinstance(payload, dict) else {} for payload in payloads]
    current_periods = normalized[0].get("periods") or [] if normalized else []
    forecast_periods = normalized[1].get("periods") or [] if len(normalized) > 1 else []
    summaries = summaries if isinstance(summaries, dict) else {}
    current_summary = _clean_rolling_weather_summary(
        summaries.get("weather_observation_summary"),
        "当前实况总结暂未生成，请以下表代码统计结果为准。",
        user_text=user_text,
    )
    forecast_summary = _clean_rolling_weather_summary(
        summaries.get("weather_forecast_summary"),
        "未来12小时预报总结暂未生成，请以下表代码统计结果为准。",
        user_text=user_text,
    )
    source = next(
        (str(payload.get("data_source")) for payload in normalized if payload.get("data_source")),
        "天津市气象台滚动预报",
    )
    sections = [
        "\n".join([
            "【天气实况】",
            current_summary,
            _rolling_snapshot_table(current_periods, "").strip(),
        ]),
        "\n".join([
            "【天气预报】",
            forecast_summary,
            _rolling_snapshot_table(forecast_periods, "").strip(),
        ]),
        f"数据来源：{source}。",
    ]
    return "\n\n".join(section for section in sections if section).strip()


def _with_unit(value: Any, unit: str) -> str:
    text = _cell(value)
    return text if text == "—" or text.endswith(unit) else f"{text}{unit}"


def _format_period_time(value: Any) -> str:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
            minute = f"{parsed.minute}分" if parsed.minute else ""
            return f"{parsed.month}月{parsed.day}日{parsed.hour}时{minute}"
        except ValueError:
            continue
    return _cell(value)


def _query_category(user_text: str) -> str:
    text = str(user_text or "")
    if any(word in text for word in ("大暴雨", "强降雨", "暴雨")):
        return "rainstorm"
    if any(word in text for word in ("雾霾", "能见度", "大雾", "轻雾", "霾")):
        return "visibility"
    if any(word in text for word in ("高温", "气温", "温度", "升温", "降温", "多少度", "冷不冷", "热不热")):
        return "temperature"
    if is_rolling_activity_query(text):
        return "activity"
    return "weather"


def _weather_table(daily: list[dict], user_text: str) -> str:
    text = str(user_text or "")
    if "周末" in text:
        title = "【本周末天气预报】"
    elif len(daily) == 1:
        # 单日标题按所问日历日区分（2026-08-24 修复：今天查询曾误标【明日预报】）：
        # 今天→【今日预报】、明天→【明日预报】、后天/明确日期等→中性【天气预报】
        # （行内 date_label 已带具体日期）。
        if "今天" in text or "今日" in text:
            title = "【今日预报】"
        elif "明天" in text or "明日" in text:
            title = "【明日预报】"
        else:
            title = "【天气预报】"
    elif len(daily) == 7:
        title = "【未来7天预报表】"
    else:
        title = "【逐日天气预报】"
    # 降水量列（2026-08-24 甲方反馈矛盾修复）：天气现象可能为"阴转多云"但当
    # 日仍有降水（WEA 是天气现象、TP1H 是时段累计降水量，两者可并存），必须同表
    # 列出降水量，避免核心结论说阴、注意事项/风险表又提降雨的自相矛盾。
    show_visibility = any(_visibility_value_km(item) is not None for item in daily)
    rows = []
    for item in daily:
        row = [
            item.get("date_label"),
            item.get("weather"),
            _temperature_range_display(item),
            item.get("EDA"),
            _cell(item.get("rainfall_max_24h_display") or item.get("rainfall_max_24h_mm")),
        ]
        if show_visibility:
            row.append(_visibility_display(item))
        rows.append(row)
    headers = ['日期', '天气现象', '气温(℃)', '风力风向', '降水量(毫米)']
    if show_visibility:
        headers.append('最低能见度(千米)')
    return f"{title}\n{_markdown_table(headers, rows)}"


def _daily_rain_only(daily: list[dict]) -> bool:
    """逐日汇总仅有降水、无天气/气温/风（外埠城市滚动预报只回降水格点 TP1H）。

    滚动预报的天气现象/气温/风况文字要素只对天津 11 代表站生成；海河网格内的外埠
    城市（唐山/承德/北京等，2026-08-24 起按城市坐标采样）只能拿到降水。此时应渲染
    逐日降水表，而不是全 "—" 的天气/气温/风表——否则用户误以为"没数据"。与决策天气
    _decision_periods_rain_only 同口径。
    """
    if not daily:
        return False
    has_rain = any(item.get("rainfall_max_24h_mm") is not None for item in daily)
    has_text = any(
        (str(item.get("weather") or "").strip() not in ("", "--"))
        or (item.get("tmax_c") is not None)
        or (item.get("tmin_c") is not None)
        or (str(item.get("EDA") or "").strip() not in ("", "--"))
        for item in daily
    )
    return has_rain and not has_text


def _rain_only_daily_table(daily: list[dict], user_text: str) -> str:
    """外埠城市降水-only 预报：逐日降水表 + 文字要素覆盖范围说明（确定性生成，零编造）。"""
    if len(daily) == 1:
        title = "【明日降水预报】"
    else:
        title = "【逐日降水预报】"
    rows = [
        [
            item.get("date_label"),
            _cell(item.get("rainfall_max_24h_display") or item.get("rainfall_max_24h_mm")),
        ]
        for item in daily
    ]
    note = (
        "注：该地位于天津代表站覆盖范围外，滚动预报暂只提供降水格点数据，"
        "天气现象、气温、风力等文字要素暂不覆盖。"
    )
    return f"{title}\n{_markdown_table(['日期', '降水量(毫米)'], rows)}\n{note}"


def _temperature_sections(daily: list[dict], analysis: dict) -> str:
    title = "【明日气温预报】" if len(daily) == 1 else "【未来一周气温预报】"
    rows = [
        [
            item.get("date_label"),
            _temperature_display_text(item.get("tmax_display") or item.get("tmax_c")),
            _temperature_display_text(item.get("tmin_display") or item.get("tmin_c")),
        ]
        for item in daily
    ]
    sections = [f"{title}\n{_markdown_table(['日期', '最高气温(℃)', '最低气温(℃)'], rows)}"]

    highest = analysis.get("highest") if isinstance(analysis, dict) else None
    lowest = analysis.get("lowest") if isinstance(analysis, dict) else None
    largest = analysis.get("largest_diurnal_range") if isinstance(analysis, dict) else None
    if highest or lowest or largest:
        key_lines = ["【关键节点】"]
        if highest:
            key_lines.append(
                f"气温最高点：{_cell(highest.get('date_label'))}，最高"
                f"{_with_unit(_temperature_display_text(highest.get('temperature_display')), '℃')}"
            )
        if lowest:
            key_lines.append(
                f"气温最低点：{_cell(lowest.get('date_label'))}，最低"
                f"{_with_unit(_temperature_display_text(lowest.get('temperature_display')), '℃')}"
            )
        if largest:
            key_lines.append(
                f"昼夜温差最大：{_cell(largest.get('date_label'))}，温差"
                f"{_with_unit(_temperature_display_text(largest.get('temperature_difference_display') or largest.get('temperature_difference_c')), '℃')}"
            )
        sections.append("\n".join(key_lines))
    return "\n\n".join(sections)


def _visibility_value_km(item: dict) -> Any:
    """读取有效千米制能见度；0/负数/非有限值均视为缺测占位。"""
    value = item.get("visibility_min_km")
    if value is None and item.get("visibility_min_m") is not None:
        try:
            value = float(item.get("visibility_min_m")) / 1000.0
        except (TypeError, ValueError):
            value = None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _visibility_display(item: dict) -> str:
    value = _visibility_value_km(item)
    return f"{value:g}" if value is not None else "—"


def _visibility_table(daily: list[dict]) -> str:
    rows = [
        [
            item.get("date_label"),
            _with_unit(_visibility_display(item), "千米"),
        ]
        for item in daily
    ]
    return "【逐日能见度】\n" + _markdown_table(["日期", "最低能见度"], rows)


def _hourly_weather_table(hourly: list[dict]) -> str:
    show_visibility = any(_visibility_value_km(item) is not None for item in hourly)
    rows = []
    for item in hourly:
        if not isinstance(item, dict):
            continue
        row = [
            item.get("period_label") or (
                f"{_format_period_time(item.get('start_time'))}-"
                f"{_format_period_time(item.get('end_time'))}"
            ),
            item.get("weather"),
            (
                f"{_temperature_display_text(item.get('tmin')) or '—'}~{_temperature_display_text(item.get('tmax')) or '—'}"
                if item.get("tmin") is not None and item.get("tmax") is not None
                else _temperature_display_text(item.get("tmax") if item.get("tmax") is not None else item.get("tmin")) or "—"
            ),
            item.get("EDA") if item.get("EDA") is not None else item.get("wind"),
            # 降水量列（2026-08-24 矛盾修复，同 _weather_table）：时段天气现象与时段
            # 累计降水量可并存，必须列出避免"阴转多云 vs 有降雨"自相矛盾。
            _cell(item.get("rainfall_mm")),
        ]
        if show_visibility:
            row.append(_visibility_display(item))
        rows.append(row)
    headers = ["时段", "天气现象", "气温(℃)", "风力风向", "降水量(毫米)"]
    if show_visibility:
        headers.append("最低能见度(千米)")
    return "【未来小时预报】\n" + _markdown_table(
        headers, rows,
    )


# 显著天气词（触发模型输出【温馨提示】）：降水/强对流/大风/剧烈降温/沙尘等。
# 不含"雾/霾"等几乎天天出现的词，避免平稳天气也硬塞建议。
_NOTABLE_WEATHER_WORDS = ("雨", "雪", "雹", "雷", "暴雨", "大风", "沙尘", "寒潮", "降温")


def _has_notable_weather(daily: list[dict], hourly: list[dict], tod_summary: list[dict]) -> bool:
    """是否存在值得提示的显著天气（降雨/强对流/大风≥5级等）。纯代码判定，供
    rolling_forecast_llm_instruction 决定是否让模型输出【温馨提示】（甲方口径：
    有显著天气才给丰富建议，平稳天气不硬塞）。"""

    def rain_hit(value: Any) -> bool:
        try:
            return float(value or 0) > 0.1
        except (TypeError, ValueError):
            return False

    def notable(item: dict, rain_key: str) -> bool:
        if rain_hit(item.get(rain_key)):
            return True
        if any(w in str(item.get("weather") or "") for w in _NOTABLE_WEATHER_WORDS):
            return True
        return _max_wind_level(item.get("wind_force") or item.get("EDA")) >= 5

    return (
        any(notable(item, "rainfall_max_24h_mm") for item in daily)
        or any(notable(item, "rainfall_mm") for item in hourly)
        or any(notable(item, "rainfall_mm") for item in tod_summary)
    )


def _tod_temp_range(item: dict) -> str:
    """时段气温区间：tmin~tmax；只一边给单边；都无 → —。"""
    tmin = item.get("tmin")
    tmax = item.get("tmax")
    if tmin is not None and tmax is not None:
        return f"{tmin}~{tmax}"
    if tmax is not None:
        return str(tmax)
    if tmin is not None:
        return str(tmin)
    return "—"


def _time_of_day_period_table(rows: list[dict], label: str) -> str:
    """时段化查询（"今天下午/晚上"）的单条时段汇总表（甲方 2026-08-24 口径：
    不要逐小时，时段写"今天下午"，列=时段/天气现象/气温/风力风向/降水量）。

    rows 由 MCP `_time_of_day_summary_rows` 聚合（每区域一条），本函数只负责排版。
    标题含"天气预报"关键词，属代码所有表头（防 LLM 重复生成）。
    """
    show_visibility = any(_visibility_value_km(item) is not None for item in rows)
    table_rows = []
    for item in rows:
        row = [
            label,
            item.get("weather"),
            _tod_temp_range(item),
            item.get("EDA"),
            _cell(item.get("rainfall_mm")),
        ]
        if show_visibility:
            row.append(_visibility_display(item))
        table_rows.append(row)
    headers = ["时段", "天气现象", "气温(℃)", "风力风向", "降水量(毫米)"]
    if show_visibility:
        headers.append("最低能见度(千米)")
    return f"【{label}天气预报】\n" + _markdown_table(
        headers, table_rows
    )


def _max_wind_level(value: Any) -> int:
    levels = [int(item) for item in re.findall(r"\d+", str(value or ""))]
    return max(levels, default=0)


def _activity_advice(item: dict) -> str:
    rain = float(item.get("rainfall_max_24h_mm") or 0)
    visibility_value = _visibility_value_km(item)
    visibility_km = float(visibility_value) if visibility_value is not None else float("inf")
    wind = _max_wind_level(item.get("wind_force"))
    weather = str(item.get("weather") or "")
    if rain >= 50 or any(word in weather for word in ("暴雨", "雷暴")) or wind >= 7:
        return "不适宜"
    if rain >= 10 or visibility_km < 1 or wind >= 5 or any(word in weather for word in ("雨", "雪", "雾")):
        return "需谨慎安排"
    return "较适宜"


def _activity_table(daily: list[dict], user_text: str) -> str:
    title = "【周末详细预报】" if "周末" in str(user_text or "") else "【逐日活动预报】"
    show_visibility = any(_visibility_value_km(item) is not None for item in daily)
    rows = []
    for item in daily:
        row = [
            item.get("date_label"),
            item.get("weather"),
            _temperature_range_display(item),
            item.get("EDA"),
        ]
        if show_visibility:
            row.append(_visibility_display(item))
        row.append(_activity_advice(item))
        rows.append(row)
    headers = ['日期/时段', '天气', '气温(℃)', '风力风向']
    if show_visibility:
        headers.append('最低能见度(千米)')
    headers.append('活动建议')
    return f"{title}\n{_markdown_table(headers, rows)}"


# 山区活动查询关键词（区域级，非 POI）：蓟州/蓟县/盘山/黄崖关等山地。
_MOUNTAIN_ACTIVITY_WORDS = ("蓟州", "蓟县", "盘山", "黄崖关", "山区", "山野")


def _is_mountain_activity_query(user_text: str) -> bool:
    return any(word in str(user_text or "") for word in _MOUNTAIN_ACTIVITY_WORDS)


def _activity_mountain_reminder(daily: list[dict]) -> str:
    """区域活动预报的山区注意事项（文案与 decision_weather_core._decision_mountain_reminder_lines
    保持一致，改动需同步；本模块不能 import decision_weather_core——它会反向 import 本模块）。"""
    if not daily:
        return ""
    has_rain = any(
        float(item.get("rainfall_max_24h_mm") or 0) > 0.1
        or "雨" in str(item.get("weather") or "")
        for item in daily
    )
    lines = ["【注意事项】"]
    if has_rain:
        lines.extend(
            [
                "1. 受降雨影响，户外游玩适宜性一般，适宜短途室内休闲、农家院休整；"
                "不建议登山、溯溪、野外徒步等山野户外活动。",
                "2. 山区降雨易造成步道湿滑，沟谷存在山洪、落石隐患，请勿前往未开发野景点、河道低洼处；"
                "备好雨衣、防滑鞋，自驾山区路段减速慢行，及时关注短时气象预警，遇强降雨尽快到安全区域避险。",
            ]
        )
    else:
        lines.append(
            "1. 山区地形复杂、昼夜温差较大，登山徒步请量力而行、备好饮水与防晒，"
            "勿前往未开发野景点与沟谷河道，及时关注短时气象预警。"
        )
    return "\n".join(lines)


# 区域灾害风险研判矩阵：隐患类型 key → (风险研判, 防范建议)。纯规则、零编造，
# 只基于隐患点类型给通用研判（数据来自 MCP 端按区域坐标查的三张静态隐患表）。
# 口径与 decision_weather_core 的 POI 风险研判同族，但本模块不能 import
# decision_weather_core——它会反向 import 本模块（循环依赖）。
_REGION_HAZARD_RISK = {
    "dzzh": ("滑坡、崩塌、泥石流等地质灾害易发", "山区道路、切坡建房、沟谷低洼处注意防范，降雨期间减少进山活动"),
    "sh": ("山洪灾害危险区", "沟谷、河道低洼区遇强降雨易发山洪，避免在河道、沟谷露营停留"),
    "zxhl": ("中小河流洪水风险区", "沿河低洼地段关注水位上涨与行洪安全，遇预警及时转移避险"),
}
_REGION_HAZARD_LABELS = {
    "dzzh": "地质灾害",
    "sh": "山洪",
    "zxhl": "中小河流",
}

# 风险等级展示顺序（一级最重 → 四级最轻），与 risk_warning_tool._normalize_risk_level 同口径。
_LEVEL_SEVERITY_ORDER = ("一级", "二级", "三级", "四级")

# 区域风险等级"该起报时次无资料"哨兵（与 MCP 侧 risk_warning_tool.RISK_LEVELS_NO_DATA
# 同值，跨进程经 JSON 透传）。渲染层明确显示“暂无对应时次风险资料”，区别于
# None（接口失败 →“接口暂不可用”）和缺键（接口成功且该灾种无风险 →“本次无风险”）。
_RISK_LEVELS_NO_DATA = "no_data"


def _format_risk_level_counts(levels: dict) -> str:
    """把 {一级: n, ...} 渲染成"一级 2 处、三级 5 处"（按严重度排序）；空 → "本次无风险"。"""
    if not isinstance(levels, dict) or not levels:
        return "本次无风险"
    parts: list[str] = []
    invalid_count = False
    for lv in _LEVEL_SEVERITY_ORDER:
        if lv not in levels:
            continue
        n = _safe_nonnegative_int(levels.get(lv))
        if n is None:
            invalid_count = True
        elif n > 0:
            parts.append(f"{lv} {n} 处")
    for lv, raw_count in levels.items():  # 兜底：非一~四级键也如实列出
        if lv in _LEVEL_SEVERITY_ORDER:
            continue
        n = _safe_nonnegative_int(raw_count)
        if n is None:
            invalid_count = True
        elif n > 0:
            parts.append(f"{lv} {n} 处")
    if parts:
        return "、".join(parts)
    return "接口数据异常" if invalid_count else "本次无风险"


def _safe_nonnegative_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _region_hazard_table(region_hazards: list[dict]) -> str:
    """把 payload 的 region_hazards 渲染成【区域】灾害风险表（类型×数量×研判×建议）。

    每区域一张表；区域无数据/全部类别 count<=0 时跳过。纯代码确定性生成，
    不依赖 LLM，也不编造具体点位（区域级只报种类与数量）。
    区域天气#8：entry 带 risk_levels_available 字段（新 MCP 必有，键存在即
    判定接入）时，追加"本次风险等级"列——接口可达有数据按严重度列出各级数量、
    可达但无风险显示"本次无风险"；接口不可达（risk_levels=None）显示"接口暂不可用"
    （不再静默隐藏列，回答自身即可分辨"接口没调好"还是"旧代码未部署"）。
    旧 payload 无该字段 → 不加列（兼容升级前 MCP）。
    """
    if not region_hazards:
        return ""
    blocks: list[str] = []
    for entry in region_hazards:
        if not isinstance(entry, dict):
            continue
        risk_levels = entry.get("risk_levels")
        show_levels = "risk_levels_available" in entry
        categories = {
            item.get("key"): item
            for item in (entry.get("categories") or [])
            if isinstance(item, dict) and item.get("key")
        }
        row_keys = list(categories)
        if show_levels:
            for key in (*_REGION_HAZARD_LABELS, *((risk_levels or {}).keys() if isinstance(risk_levels, dict) else ())):
                if key not in row_keys:
                    row_keys.append(key)
        rows: list[list[str]] = []
        hazards_available = entry.get("hazards_available", True) is True
        for key in row_keys:
            category = categories.get(key) or {}
            count = _safe_nonnegative_int(category.get("count"))
            if not show_levels and (count is None or count <= 0):
                continue
            label = category.get("label") or (
                (key or "未知类型") if category else (_REGION_HAZARD_LABELS.get(key) or key or "未知类型")
            )
            risk, advice = _REGION_HAZARD_RISK.get(key, ("存在风险隐患", "注意防范相关灾害风险"))
            if count is not None:
                count_text = f"{count} 处"
            else:
                count_text = "0 处" if hazards_available else "暂无数据"
            row = [label, count_text]
            if show_levels:
                if isinstance(risk_levels, dict):
                    level_info = risk_levels.get(key)
                    if level_info is None and key in risk_levels:
                        # 该灾种接口单独失败（MCP 侧 None 打标）——区别于"可达但无风险"
                        row.append("接口暂不可用")
                    elif level_info == _RISK_LEVELS_NO_DATA:
                        # 对应起报时次无资料不等于无风险，必须明确区分。
                        row.append("暂无对应时次风险资料")
                    elif level_info is None:
                        row.append("本次无风险")
                    elif not isinstance(level_info, dict):
                        row.append("接口数据异常")
                    else:
                        row.append(_format_risk_level_counts(level_info.get("levels") or {}))
                else:
                    row.append("接口暂不可用")
            row.extend([risk, advice])
            rows.append(row)
        if not rows:
            continue
        display = entry.get("region_display") or entry.get("region") or "该区域"
        headers = ["灾害类型", "隐患点数量"]
        if show_levels:
            headers.append("本次风险等级")
        headers.extend(["风险研判", "防范建议"])
        blocks.append(f"【{display}灾害风险】\n" + _markdown_table(headers, rows))
    return "\n\n".join(blocks)


def _rainstorm_sections(analysis: dict) -> str:
    if not isinstance(analysis, dict):
        return ""
    processes = analysis.get("severe_processes") or []
    if not processes:
        return ""
    sections = ["【过程详情】"]
    for index, process in enumerate(processes, 1):
        if len(processes) > 1:
            sections.append(f"过程{index}：")
        sections.extend([
            f"影响时段：{_format_period_time(process.get('start_time'))} — {_format_period_time(process.get('end_time'))}",
            f"累计雨量：{_cell(process.get('cumulative_rain_min_mm'))}~{_cell(process.get('cumulative_rain_max_mm'))}毫米",
            f"局部最大：{_cell(process.get('local_max_24h_mm'))}毫米（{_cell(process.get('local_max_level'))}）",
            f"主要影响区域：{_cell('、'.join(process.get('affected_regions') or []))}",
        ])
        if index < len(processes):
            sections.append("")
    return "\n".join(sections)


def build_rolling_forecast_bundle(user_text: str, payload: Any) -> dict | None:
    if not isinstance(payload, dict):
        return None
    daily = [item for item in (payload.get("daily_summary") or []) if isinstance(item, dict)]
    hourly = [item for item in (payload.get("hourly_summary") or []) if isinstance(item, dict)]
    tod_summary = [item for item in (payload.get("time_of_day_summary") or []) if isinstance(item, dict)]
    category = _query_category(user_text)
    # 外埠城市降水-only（滚动预报文字要素只覆盖天津代表站）：除暴雨过程分析（只需
    # 降水量，rain-only 数据同样成立）外，一律渲染逐日降水表，不出全"—"天气/气温/风表。
    rain_only = bool(daily) and not hourly and not tod_summary and _daily_rain_only(daily)
    forced_core_conclusion = ""
    if tod_summary:
        # 时段化查询（"今天下午/晚上"）：单条时段汇总表，不铺逐小时。
        code_section = _time_of_day_period_table(tod_summary, payload.get("time_of_day_label") or "该时段")
    elif hourly:
        code_section = _hourly_weather_table(hourly)
    elif category == "rainstorm":
        analysis = payload.get("rainstorm_analysis") or {}
        if (
            "大暴雨" in str(user_text or "")
            and analysis.get("has_valid_rainfall_data")
            and not analysis.get("has_severe_rainstorm")
        ):
            forced_core_conclusion = "【核心结论】\n当前没有大暴雨过程发生。"
        code_section = _rainstorm_sections(payload.get("rainstorm_analysis") or {})
    elif rain_only:
        code_section = _rain_only_daily_table(daily, user_text)
    elif category == "visibility":
        code_section = _visibility_table(daily) if daily else ""
    elif category == "temperature":
        code_section = _temperature_sections(daily, payload.get("temperature_analysis") or {}) if daily else ""
    elif category == "activity":
        code_section = _activity_table(daily, user_text) if daily else ""
    else:
        code_section = _weather_table(daily, user_text) if daily else ""
    # 山区查询（蓟州/盘山等，活动或一般天气类）：附山地注意事项（活动建议 + 山洪/落石风险提示）。
    if category in ("activity", "weather") and _is_mountain_activity_query(user_text):
        reminder = _activity_mountain_reminder(daily)
        if reminder:
            code_section = f"{code_section}\n\n{reminder}" if code_section else reminder
    # 区域灾害风险表（区域查询：蓟州/宝坻等，除天气外附带该区域灾害风险种类与数量）。
    # 数据来自 payload.region_hazards（MCP 端区域模式按代表坐标查 3 张静态隐患表）。
    region_hazards = payload.get("region_hazards")
    if isinstance(region_hazards, list):
        risk_section = _region_hazard_table(region_hazards)
        if risk_section:
            code_section = f"{code_section}\n\n{risk_section}" if code_section else risk_section
    return {
        "category": category,
        "code_section": code_section,
        "data_source": _cell(payload.get("data_source"), "天津市气象台滚动预报"),
        "forced_core_conclusion": forced_core_conclusion,
        "rain_only": rain_only,
        "notable_weather": _has_notable_weather(daily, hourly, tod_summary),
        "user_text": user_text,
    }


def compact_rolling_forecast_facts(payload: Any) -> Any:
    """仅向大模型提供已汇总的权威事实，避免其重新扫描 periods 得到不同结论。"""
    if not isinstance(payload, dict):
        return payload
    keys = (
        "data_source",
        "forecast_type",
        "query_mode",
        "fcst_time",
        "query_regions",
        "forecast_start_date",
        "forecast_days",
        "forecast_start_time",
        "forecast_end_time",
        "api_code",
        "api_message",
        "daily_summary",
        "temperature_analysis",
        "visibility_analysis",
        "rainstorm_analysis",
        "weather_focus",
        "hourly_summary",
        "time_of_day_label",
        "time_of_day_summary",
    )
    return {key: payload.get(key) for key in keys if key in payload}


def rolling_forecast_llm_instruction(bundle: dict | None) -> str:
    if not bundle:
        return ""
    category = bundle.get("category")
    extra = ""
    if category == "temperature":
        extra = (
            "核心结论中的最高气温必须使用 temperature_analysis.highest.temperature_display，"
            "其日期必须使用 temperature_analysis.highest.date_label；温度已按四舍五入处理，禁止恢复小数。"
        )
    if bundle.get("rain_only"):
        # 外埠城市降水-only：文字要素（天气现象/气温/风力）服务端未提供，
        # 核心结论只能陈述降水事实，禁止编造任何天气现象/气温/风力描述。
        extra += (
            "该点位仅有降水格点预报：核心结论只能依据降水量陈述降水情况，"
            "不得描述或推测天气现象、气温、风力、能见度等未提供的要素。"
        )
    # 显著天气时让模型补一段丰富专业的【温馨提示】（甲方 2026-08-24 口径：建议/注意事项
    # 让模型发挥、写得更丰富专业）；平稳天气不硬塞。内容由模型基于权威事实组织，不编造数值。
    notable = bool(bundle.get("notable_weather"))
    if notable:
        extra += (
            "本次权威事实含显著天气（降雨/雷阵雨/强对流/大风/明显降温等）。你必须在【核心结论】之后"
            "另起一段输出【温馨提示】，用丰富、专业、贴近公众出行与生产生活的语言，给出与本次天气直接"
            "相关的防范和出行建议（如降雨量级对应的携带雨具、道路湿滑减速、山区沟谷/河道避险、强对流"
            "防雷防风、降温添衣等，按实际天气取舍）。【温馨提示】只能基于已给出的权威事实（天气现象、"
            "降水量、气温、风力风向、风险等级）展开，不得编造任何数值、时段、地点或未提供的天气现象；"
            "代码已生成的山区注意事项、灾害风险防范建议由代码负责，你不要重复其字面内容，可从出行、"
            "健康、农业、城市运行等角度补充。"
        )
    return (
        "\n\n系统约束：数据表格、关键节点和过程详情将由代码根据本工具结果生成并插入。"
        "你必须生成【核心结论】，其正文严格且只能有一句，句号、问号或感叹号均视为一句结束；"
        "该句只回答用户最关心的问题，不得追加背景、原因、建议、风险或表格内容。"
        "除非数据存在与用户问题直接相关且核心结论未覆盖的显著风险，否则不要生成【重点关注】。"
        "核心结论应以时段内最显著、最强的天气现象为主，不得把雷阵雨、暴雨、大风、强对流等"
        "天气淡化表述为“以多云/阴为主”；含雷阵雨、暴雨时核心结论必须点明。"
        "核心结论不要机械补充“无降水/无降雨”或“风力为X级”等泛化描述；只有用户明确询问降水或风力时才回答对应要素。"
        "但当权威事实中降水量大于0或天气现象含雨（小雨/中雨/大雨/暴雨/阵雨/雷阵雨等）时，"
        "核心结论必须提及降雨（如“有中雨”），即使天气现象写的是阴转多云——天气现象与时段"
        "累计降水量可并存，只写“阴转多云”而不提降雨会与表格、注意事项、风险表自相矛盾。"
        "核心结论中的所有温度数值必须四舍五入为整数，不得保留小数。"
        "你不得生成任何表格、表头、"
        "逐日数据行、【关键节点】、【过程详情】或数据来源。"
        f"{extra}"
    )


def _first_sentence(value: str) -> str:
    """保留核心结论的第一句，作为提示词之外的确定性兜底。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    match = re.match(r"^.*?[。！？!?](?:[”’」』])?", text)
    return match.group(0).strip() if match else text


def _enforce_single_sentence_core(text: str, user_text: str = "") -> str:
    pattern = re.compile(r"(【核心结论】)\s*(.*?)(?=\n\s*【[^】]+】|\Z)", re.DOTALL)

    def replace(match: re.Match) -> str:
        sentence = sanitize_forecast_core_summary(_first_sentence(match.group(2)), user_text)
        return f"{match.group(1)}\n{sentence}".rstrip()

    return pattern.sub(replace, str(text or ""), count=1)


def _is_code_owned_header(header: str, user_text: str) -> bool:
    if header in _CODE_OWNED_HEADERS:
        return True
    if any(keyword in header for keyword in _CODE_OWNED_FORECAST_HEADER_KEYWORDS):
        return True
    if _query_category(user_text) == "activity" and any(
        keyword in header for keyword in _CODE_OWNED_ACTIVITY_HEADER_KEYWORDS
    ):
        return True
    return _is_mountain_activity_query(user_text) and "注意事项" in header


def _normalized_bracket_header(line: str) -> str | None:
    candidate = re.sub(r"^#{1,6}\s*", "", str(line or "").strip()).strip()
    candidate = candidate.strip("*_ ").strip()
    return candidate if re.fullmatch(r"【[^】]+】", candidate) else None


def _strip_llm_code_owned_content(llm_text: str, user_text: str = "") -> str:
    kept: list[str] = []
    skipping_owned_section = False
    for line in str(llm_text or "").splitlines():
        stripped = line.strip()
        header = _normalized_bracket_header(stripped)
        if header:
            if _is_code_owned_header(header, user_text):
                skipping_owned_section = True
                continue
            skipping_owned_section = False
        if skipping_owned_section:
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            continue
        if stripped.startswith("数据来源：") or stripped.startswith("数据来源:"):
            continue
        kept.append(line)
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return _enforce_single_sentence_core(cleaned, user_text)


def _insert_after_core(text: str, code_section: str) -> str:
    if not code_section:
        return text
    core_header = "【核心结论】"
    if core_header not in text:
        return f"{text}\n\n{code_section}".strip()
    core_start = text.index(core_header) + len(core_header)
    next_header = re.search(r"\n\s*【[^】]+】", text[core_start:])
    if not next_header:
        return f"{text}\n\n{code_section}".strip()
    split_at = core_start + next_header.start()
    return f"{text[:split_at].rstrip()}\n\n{code_section}\n\n{text[split_at:].lstrip()}".strip()


def assemble_rolling_forecast_answer(llm_text: str, bundles: list[dict]) -> str:
    valid = [bundle for bundle in bundles if isinstance(bundle, dict)]
    if not valid:
        return str(llm_text or "")
    bundle = valid[-1]
    forced_core = str(bundle.get("forced_core_conclusion") or "").strip()
    user_text = str(bundle.get("user_text") or "")
    cleaned = (
        sanitize_forecast_core_summary(forced_core, user_text)
        if forced_core
        else _strip_llm_code_owned_content(llm_text, user_text)
    )
    assembled = _insert_after_core(cleaned, str(bundle.get("code_section") or ""))
    source = f"数据来源：{bundle.get('data_source') or '天津市气象台滚动预报'}。"
    return f"{assembled.rstrip()}\n\n{source}".strip()
