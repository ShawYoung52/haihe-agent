"""天擎当前气象实况的模型提示与确定性回答装配。"""
from __future__ import annotations

import json
import re
from typing import Any


REGION_LABELS = {
    "tianjin": "天津市",
    "tianjin_central": "中心城区",
    "jizhou": "蓟州",
    "beijing": "北京市",
    "hebei": "河北省",
    "haihe_basin": "海河流域",
}


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mm(value: Any) -> str:
    number = _number(value)
    return "—" if number is None else f"{number:.1f}"


def _station_display(value: Any) -> str:
    if not isinstance(value, dict):
        return "未知站点"
    return str(value.get("display") or value.get("station_name") or "未知站点").strip()


def _has_rain(stats: Any) -> bool:
    if not isinstance(stats, dict):
        return False
    judgement = stats.get("rainfall_judgement")
    return bool(isinstance(judgement, dict) and judgement.get("has_rain"))


def _has_data(stats: Any) -> bool:
    if not isinstance(stats, dict):
        return False
    judgement = stats.get("rainfall_judgement")
    return bool(isinstance(judgement, dict) and judgement.get("has_data"))


def _fallback_region_summary(key: str, stats: dict[str, Any]) -> str:
    judgement = stats.get("rainfall_judgement") or {}
    if not judgement.get("has_data"):
        return "暂无有效降水数据"
    if not judgement.get("has_rain"):
        return "没有降水"

    level = str(judgement.get("level") or "降雨")
    average = _mm(stats.get("average_pre_mm"))
    maximum = _mm(stats.get("max_pre_mm"))
    max_station = _station_display(stats.get("max_pre_station"))
    if key in {"tianjin_central", "jizhou", "haihe_basin"}:
        return (
            f"出现降雨，局地{level}，平均降雨量{average}毫米，"
            f"最大{maximum}毫米（{max_station}）"
        )
    hourly = _mm(stats.get("max_pre_1h_mm"))
    hourly_station = _station_display(stats.get("max_pre_1h_station"))
    return (
        f"出现降雨，局地{level}，平均降雨量{average}毫米，"
        f"最大{maximum}毫米（{max_station}），"
        f"最大小时降雨量{hourly}毫米（{hourly_station}）"
    )


def build_current_weather_observation_summary_prompt(
    user_text: str,
    payload: dict[str, Any],
) -> str:
    """让模型只组织有降水地区的业务语言，不重新计算代码统计。"""
    regions = payload.get("regions") if isinstance(payload, dict) else {}
    compact = {
        key: {
            "地区": REGION_LABELS[key],
            "平均PRE毫米": stats.get("average_pre_mm"),
            "最大PRE毫米": stats.get("max_pre_mm"),
            "最大PRE站点": stats.get("max_pre_station"),
            "最大PRE_1h毫米": stats.get("max_pre_1h_mm"),
            "最大PRE_1h站点": stats.get("max_pre_1h_station"),
            "代码降水判断": stats.get("rainfall_judgement"),
        }
        for key, stats in (regions or {}).items()
        if key in REGION_LABELS and isinstance(stats, dict)
    }
    return (
        "你是天津气象业务助手。请根据代码统计结果，为各地区组织简洁的降水实况描述，"
        "并仅在代码统计包含明确降水风险时生成一段关注与建议。\n"
        "严格要求：\n"
        "1. 只输出一个JSON对象，字段固定为 tianjin、tianjin_central、jizhou、beijing、"
        "hebei、haihe_basin、advice；不要输出标题、表格或数据来源。\n"
        "2. 各地区字段只写冒号后的正文，不重复地区名称。\n"
        "3. 平均值、最大值、最大小时值、降水等级和站点必须逐字使用代码统计，不得重新计算、"
        "四舍五入、替换站点或编造数字。\n"
        "4. 天津、北京、河北有降水时，应包含平均PRE、最大PRE及站点、最大PRE_1h及站点；"
        "中心城区、蓟州和海河流域包含平均PRE、最大PRE及站点。\n"
        "5. 降水等级只能使用“代码降水判断”；仅有PRE和PRE_1h数据，禁止写阵雨、雷阵雨、"
        "雷电、大风等接口未提供的天气现象。\n"
        "6. 海河流域最大值只写站点名称，禁止补充无法确定的河流名称。\n"
        "7. 代码判断无降水时对应字段只能写“没有降水”；无有效数据时只能写“暂无有效降水数据”。\n"
        "8. advice限1-2句。只有代码降水判断或输入内容明确体现风险时，才围绕道路积水、"
        "早晚高峰或节假日出行、天津海上作业、山区旅游等与实际降水等级相符的事项生成建议；"
        "不得编造预警等级、停工停航命令或具体受灾情况。无明确风险或全部地区无降水时，"
        "advice 留空。\n\n"
        f"用户问题：{user_text}\n"
        f"北京时间：{payload.get('observation_time_beijing') if isinstance(payload, dict) else ''}\n"
        f"代码统计：{json.dumps(compact, ensure_ascii=False, default=str)}"
    )


def _clean_model_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"^(?:天津市|中心城区|蓟州|北京市|河北省|海河流域)\s*[：:]\s*", "", text)
    return text.rstrip("。；; ")


def _summary_matches_stats(key: str, summary: str, stats: dict[str, Any]) -> bool:
    """模型文字缺少任一代码事实或包含无依据现象时，改用代码兜底。"""
    if not summary:
        return False
    if any(word in summary for word in ("阵雨", "雷阵雨", "雷电", "大风")):
        return False
    judgement = stats.get("rainfall_judgement") or {}
    required = [
        str(judgement.get("level") or ""),
        _mm(stats.get("average_pre_mm")),
        _mm(stats.get("max_pre_mm")),
        _station_display(stats.get("max_pre_station")),
    ]
    if key in {"tianjin", "beijing", "hebei"}:
        required.extend([
            _mm(stats.get("max_pre_1h_mm")),
            _station_display(stats.get("max_pre_1h_station")),
        ])
    return all(value and value != "—" and value in summary for value in required)


def _fallback_advice(regions: dict[str, Any]) -> str:
    parts = ["请关注降水对早晚高峰和节假日出行的影响，并结合最新实况防范道路积水"]
    if _has_rain(regions.get("tianjin")):
        parts.append("天津沿海及海上作业应注意降水带来的能见度和作业安全影响")
    if _has_rain(regions.get("jizhou")):
        parts.append("蓟州山区旅游应关注道路湿滑和低洼路段积水")
    return "；".join(parts)


def build_current_weather_observation_answer(
    payload: dict[str, Any],
    summaries: dict[str, Any] | None = None,
) -> str:
    """用代码控制时间、无雨结论、地区顺序和最终模块结构。"""
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        message = (
            payload.get("message")
            if isinstance(payload, dict)
            else ""
        ) or "当前暂未取得京津冀及海河流域同一时次的天擎实况数据。"
        return f"【天气实况】\n{message}"

    regions = payload.get("regions") if isinstance(payload.get("regions"), dict) else {}
    summaries = summaries if isinstance(summaries, dict) else {}

    rendered: dict[str, str] = {}
    for key in REGION_LABELS:
        stats = regions.get(key) if isinstance(regions.get(key), dict) else {}
        if not _has_data(stats):
            rendered[key] = "暂无有效降水数据"
        elif not _has_rain(stats):
            rendered[key] = "没有降水"
        else:
            model_summary = _clean_model_text(summaries.get(key))
            rendered[key] = (
                model_summary
                if _summary_matches_stats(key, model_summary, stats)
                else _fallback_region_summary(key, stats)
            )

    tianjin_text = f"天津市：{rendered['tianjin']}。"
    if rendered["tianjin"] != "没有降水":
        tianjin_text += f"中心城区：{rendered['tianjin_central']}。"

    lines = [
        str(payload.get("observation_time_label") or "截至本次查询") + "。",
        tianjin_text,
        f"蓟州：{rendered['jizhou']}。",
        f"北京市：{rendered['beijing']}。",
        f"河北省：{rendered['hebei']}。",
        f"海河流域：{rendered['haihe_basin']}。",
    ]
    sections = ["【天气实况】\n" + "\n".join(lines)]

    any_rain = any(
        _has_rain(regions.get(key))
        for key in REGION_LABELS
        if key != "tianjin_central"
    )
    advice = _clean_model_text(summaries.get("advice"))
    if any_rain:
        sections.append("【关注与建议】\n" + (advice or _fallback_advice(regions)) + "。")
    sections.append(f"数据来源：{payload.get('data_source') or '天擎自动站'}。")
    return "\n\n".join(sections)
