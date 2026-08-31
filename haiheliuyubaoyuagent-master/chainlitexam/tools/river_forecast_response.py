"""统一河流/河系降雨预报（query_river_rainfall_forecast）的确定性答案组装。

2026-08-31 用户口径：河流预报回答"太简单、很抽象"（只有一句话、无数据支撑）。
本模块把工具返回的逐时段降雨统计确定性组装为完整回答（核心结论 + 逐时段降雨表
+ 数据来源），零编造——只引用工具返回的降雨字段与 data_source，不经 answer LLM，
与 query_decision_weather_for_poi 的 forced_final_text 确定性收口同模式。
"""
from __future__ import annotations

from typing import Any

_NO_RAIN = "无明显降雨"


def _fmt_rain(value: Any) -> str:
    """降雨量保留 1 位小数；None/非法显示"—"。"""
    if isinstance(value, bool) or value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number != number:  # NaN
        return "—"
    return f"{number:.1f}"


def _period_rain_judgment(period: dict) -> str:
    """单时段降雨判断文案：has_rain True/False/None 三态。"""
    has_rain = period.get("has_rain")
    if has_rain is True:
        return "有降雨"
    if has_rain is False:
        return _NO_RAIN
    return "资料不足"


def _window_text(periods: list[dict]) -> str:
    """结论里的时间窗口表述：单时段用其 label，多时段并列。"""
    labels = [str(p.get("label") or "").strip() for p in periods]
    labels = [label for label in labels if label]
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    return "、".join(labels)


def _build_core_conclusion(result: dict, periods: list[dict]) -> str:
    river_name = str(result.get("river_name") or "该河流")
    scope = str(result.get("scope_description") or "").strip()
    window = _window_text(periods)
    # scope_description 通常已含河名（如"泃河河道两侧约5公里沿线范围"），避免重复叠加。
    if scope:
        target = scope if scope.startswith(river_name) else f"{river_name}{scope}"
    else:
        target = river_name
    lead = f"预计{window}{target}" if window else f"预计{target}"

    judged = [p for p in periods if p.get("has_rain") is not None]
    rain_periods = [p for p in judged if p.get("has_rain") is True]
    if not judged:
        return f"{lead}暂缺有效降雨预报资料。"
    if not rain_periods:
        return f"{lead}{_NO_RAIN}。"
    # 有降雨：报各降雨时段最大雨量（不编造，只引用 max_rainfall_mm）。
    maxima = [p.get("max_rainfall_mm") for p in rain_periods]
    maxima = [m for m in maxima if isinstance(m, (int, float)) and not isinstance(m, bool)]
    if len(periods) > 1 and len(rain_periods) < len(periods):
        rain_labels = "、".join(str(p.get("label") or "") for p in rain_periods)
        if maxima:
            return f"{lead}{rain_labels}有降雨（时段最大雨量约 {max(maxima):.1f} 毫米），其余时段{_NO_RAIN}。"
        return f"{lead}{rain_labels}有降雨，其余时段{_NO_RAIN}。"
    if maxima:
        return f"{lead}有降雨，时段最大雨量约 {max(maxima):.1f} 毫米。"
    return f"{lead}有降雨。"


def _build_period_table(periods: list[dict]) -> str:
    lines = [
        "【逐时段降雨预报】",
        "| 时段 | 平均雨量(毫米) | 最大雨量(毫米) | 降雨 |",
        "| --- | --- | --- | --- |",
    ]
    for period in periods:
        label = str(period.get("label") or "—")
        lines.append(
            f"| {label} | {_fmt_rain(period.get('average_rainfall_mm'))} | "
            f"{_fmt_rain(period.get('max_rainfall_mm'))} | {_period_rain_judgment(period)} |"
        )
    return "\n".join(lines)


def build_river_forecast_answer(user_text: str, result: Any) -> str | None:
    """把统一河流降雨预报工具结果组装为确定性完整回答；非 ok 返回 None 交回原路径。

    结构：【核心结论】+【逐时段降雨预报】表 + 数据来源。只引用工具返回字段，零编造。
    """
    if not isinstance(result, dict) or result.get("status") != "ok":
        return None
    periods = [p for p in (result.get("periods") or []) if isinstance(p, dict)]
    if not periods:
        return None
    data_source = next(
        (str(p.get("data_source")).strip() for p in periods if str(p.get("data_source") or "").strip()),
        "",
    )
    sections = [f"【核心结论】\n{_build_core_conclusion(result, periods)}"]
    sections.append(_build_period_table(periods))
    if data_source:
        sections.append(f"数据来源：{data_source}。")
    return "\n\n".join(sections).strip()
