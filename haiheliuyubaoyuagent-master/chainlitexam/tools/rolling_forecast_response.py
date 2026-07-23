"""滚动预报回复的确定性数据区块渲染与组装。"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any


_CODE_OWNED_HEADERS = {
    "【未来7天预报表】",
    "【明日预报】",
    "【本周末天气预报】",
    "【逐日天气预报】",
    "【未来一周气温预报】",
    "【明日气温预报】",
    "【逐日能见度与空气质量】",
    "【周末详细预报】",
    "【逐日活动预报】",
    "【过程详情】",
    "【关键节点】",
    "【天气实况】",
    "【天气预报】",
}


def is_current_rolling_weather_query(user_text: str) -> bool:
    """识别“当前时刻的滚动气象信息实况”类专用双时段查询。"""
    text = str(user_text or "")
    return (
        "滚动" in text
        and any(word in text for word in ("当前时刻", "当前", "现在", "实时"))
        and any(word in text for word in ("气象信息", "天气", "气象"))
        and "实况" in text
    )


def build_current_rolling_weather_query_plan(
    user_text: str,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """按滚动预报接口规则生成当前1小时和未来12小时两次调用参数。"""
    now = now or datetime.now()
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    if now.hour >= 8:
        fcst_dt = now.replace(hour=8, minute=0, second=0, microsecond=0)
    else:
        fcst_dt = (now - timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)

    current_start = int((current_hour - fcst_dt).total_seconds() // 3600)
    future_start = current_start + 1
    common = {
        "user_query": user_text,
        "regions": "",
        "fcst_time": fcst_dt.strftime("%Y%m%d%H%M%S"),
    }
    return [
        {
            **common,
            "start_period": current_start,
            "end_period": current_start + 1,
            "interval": 1,
        },
        {
            **common,
            "start_period": future_start,
            "end_period": future_start + 12,
            "interval": 12,
        },
    ]


def _cell(value: Any, default: str = "—") -> str:
    if value is None or str(value).strip() == "":
        return default
    text = str(value).strip()
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
        "最低气温℃": _valid_number(item.get("TMIN")),
        "最高气温℃": _valid_number(item.get("TMAX")),
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
            "最低气温℃": stats["temperature_min_c"],
            "最高气温℃": stats["temperature_max_c"],
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
        "4. 每段1-2句，不得生成标题、Markdown表格、逐地区清单、数据来源或技术参数。\n"
        "5. 只输出一个 JSON 对象，格式为："
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
            temperature = f"{_display_number(tmin)}~{_display_number(tmax)}"
        else:
            temperature = _display_number(tmax if tmax is not None else tmin)
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


def _clean_rolling_weather_summary(value: Any, fallback: str) -> str:
    lines = []
    for line in str(value or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped in {"【天气实况】", "【天气预报】"}:
            continue
        if stripped.startswith("|") or stripped.startswith("数据来源"):
            continue
        lines.append(stripped)
    return " ".join(lines).strip() or fallback


def build_current_rolling_weather_answer(
    payloads: list[Any],
    summaries: dict[str, Any] | None = None,
) -> str:
    """模型结论与代码生成的两张权威表格进行确定性组装。"""
    normalized = [payload if isinstance(payload, dict) else {} for payload in payloads]
    current_periods = normalized[0].get("periods") or [] if normalized else []
    forecast_periods = normalized[1].get("periods") or [] if len(normalized) > 1 else []
    summaries = summaries if isinstance(summaries, dict) else {}
    current_summary = _clean_rolling_weather_summary(
        summaries.get("weather_observation_summary"),
        "当前实况总结暂未生成，请以下表代码统计结果为准。",
    )
    forecast_summary = _clean_rolling_weather_summary(
        summaries.get("weather_forecast_summary"),
        "未来12小时预报总结暂未生成，请以下表代码统计结果为准。",
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
    if any(word in text for word in ("户外", "活动", "作业", "适合", "出行")):
        return "activity"
    return "weather"


def _weather_table(daily: list[dict], user_text: str) -> str:
    if "周末" in str(user_text or ""):
        title = "【本周末天气预报】"
    elif len(daily) == 1:
        title = "【明日预报】"
    elif len(daily) == 7:
        title = "【未来7天预报表】"
    else:
        title = "【逐日天气预报】"
    rows = [
        [
            item.get("date_label"),
            item.get("weather"),
            item.get("temperature_range_c"),
            item.get("wind_force"),
            item.get("wind_direction"),
        ]
        for item in daily
    ]
    return f"{title}\n{_markdown_table(['日期', '天气现象', '气温(℃)', '风力', '风向'], rows)}"


def _temperature_sections(daily: list[dict], analysis: dict) -> str:
    title = "【明日气温预报】" if len(daily) == 1 else "【未来一周气温预报】"
    rows = [
        [item.get("date_label"), item.get("tmax_display"), item.get("tmin_display")]
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
                f"{_with_unit(highest.get('temperature_display'), '℃')}"
            )
        if lowest:
            key_lines.append(
                f"气温最低点：{_cell(lowest.get('date_label'))}，最低"
                f"{_with_unit(lowest.get('temperature_display'), '℃')}"
            )
        if largest:
            key_lines.append(
                f"昼夜温差最大：{_cell(largest.get('date_label'))}，温差"
                f"{_with_unit(largest.get('temperature_difference_display') or largest.get('temperature_difference_c'), '℃')}"
            )
        sections.append("\n".join(key_lines))
    return "\n\n".join(sections)


def _visibility_table(daily: list[dict]) -> str:
    rows = [
        [
            item.get("date_label"),
            _with_unit(item.get("visibility_min_display") or item.get("visibility_min_m"), "米"),
        ]
        for item in daily
    ]
    return "【逐日能见度与空气质量】\n" + _markdown_table(["日期", "能见度"], rows)


def _max_wind_level(value: Any) -> int:
    levels = [int(item) for item in re.findall(r"\d+", str(value or ""))]
    return max(levels, default=0)


def _activity_advice(item: dict) -> str:
    rain = float(item.get("rainfall_max_24h_mm") or 0)
    visibility = float(item.get("visibility_min_m") or 999999)
    wind = _max_wind_level(item.get("wind_force"))
    weather = str(item.get("weather") or "")
    if rain >= 50 or any(word in weather for word in ("暴雨", "雷暴")) or wind >= 7:
        return "不适宜"
    if rain >= 10 or visibility < 1000 or wind >= 5 or any(word in weather for word in ("雨", "雪", "雾")):
        return "需谨慎安排"
    return "较适宜"


def _activity_table(daily: list[dict], user_text: str) -> str:
    title = "【周末详细预报】" if "周末" in str(user_text or "") else "【逐日活动预报】"
    rows = [
        [
            item.get("date_label"),
            item.get("weather"),
            item.get("temperature_range_c"),
            item.get("wind_force"),
            _activity_advice(item),
        ]
        for item in daily
    ]
    return f"{title}\n{_markdown_table(['日期/时段', '天气', '气温(℃)', '风力', '活动建议'], rows)}"


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
    category = _query_category(user_text)
    if category == "rainstorm":
        code_section = _rainstorm_sections(payload.get("rainstorm_analysis") or {})
    elif category == "visibility":
        code_section = _visibility_table(daily) if daily else ""
    elif category == "temperature":
        code_section = _temperature_sections(daily, payload.get("temperature_analysis") or {}) if daily else ""
    elif category == "activity":
        code_section = _activity_table(daily, user_text) if daily else ""
    else:
        code_section = _weather_table(daily, user_text) if daily else ""
    return {
        "category": category,
        "code_section": code_section,
        "data_source": _cell(payload.get("data_source"), "天津市气象台滚动预报"),
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
    )
    return {key: payload.get(key) for key in keys if key in payload}


def rolling_forecast_llm_instruction(bundle: dict | None) -> str:
    if not bundle:
        return ""
    category = bundle.get("category")
    extra = ""
    if category == "temperature":
        extra = (
            "核心结论中的最高气温必须逐字使用 temperature_analysis.highest.temperature_display，"
            "其日期必须使用 temperature_analysis.highest.date_label。"
        )
    return (
        "\n\n系统约束：数据表格、关键节点和过程详情将由代码根据本工具结果生成并插入。"
        "你只生成【核心结论】以及必要的【重点关注】等纯文字内容；不得生成任何表格、表头、"
        "逐日数据行、【关键节点】、【过程详情】或数据来源。"
        f"{extra}"
    )


def _strip_llm_code_owned_content(llm_text: str) -> str:
    kept: list[str] = []
    skipping_owned_section = False
    for line in str(llm_text or "").splitlines():
        stripped = line.strip()
        header_match = re.fullmatch(r"【[^】]+】", stripped)
        if header_match:
            if stripped in _CODE_OWNED_HEADERS:
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
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


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
    cleaned = _strip_llm_code_owned_content(llm_text)
    assembled = _insert_after_core(cleaned, str(bundle.get("code_section") or ""))
    source = f"数据来源：{bundle.get('data_source') or '天津市气象台滚动预报'}。"
    return f"{assembled.rstrip()}\n\n{source}".strip()
