"""预警查询、装配与回答工作流。

无论查询由快捷路径的专用路由器触发，还是由 Planner 触发，最终均在本模块
完成：代码生成预警清单和原始正文；模型从生效预警正文中提取防范建议，
代码校验提取内容后去重并组装，同时将模型核心结论收口为严格一句。
"""
from __future__ import annotations

import asyncio
import json
import re
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import chainlit as cl
from langchain_core.messages import AIMessage, HumanMessage

from prompts import WARNING_ROUTE_PROMPT, WARNING_SUMMARY_PROMPT
from tools.decision_weather_core import _extract_first_json_object
from utils.tool_result import _unwrap_tool_result


WARNING_TOOL_NAMES = frozenset({
    "get_effective_warning_info",
    "get_history_warning_info",
    "get_today_warning_summary",
    "get_national_warning_info",
})

# 市级/全市范围问法关键词：命中时折叠各区县明细为市级层面，不展开影响区域列。
_BROAD_SCOPE_TERMS = frozenset({"天津", "天津市", "我市", "全市", "本市"})


@dataclass(frozen=True)
class WarningRuntime:
    """由编排器注入的通用能力，避免本模块反向依赖 message_orchestrator。"""

    find_tool: Callable[..., Any]
    invoke_fast_tool: Callable[..., Any]
    handle_fast_path_error: Callable[..., Any]
    sanitize_display_text: Callable[[str], str]
    prepend_thinking_summary: Callable[..., str]


def is_warning_tool(tool_name: str) -> bool:
    return tool_name in WARNING_TOOL_NAMES


def _clean_table_cell(text: Any) -> str:
    if text is None:
        return ""
    value = str(text)
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("|", "｜").replace("\n", " ").replace("\r", " ")
    return re.sub(r"\s+", " ", value).strip()


def _compact_warning_record_for_table(item: Any) -> dict[str, str]:
    if not isinstance(item, dict):
        return {
            "content": str(item), "eventType": "", "department": "", "msgType": "",
            "time": "", "severity": "", "locationName": "",
            "province": "", "city": "", "county": "",
        }
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    province = str(item.get("province") or raw.get("province") or "").strip()
    city = str(item.get("city") or raw.get("city") or "").strip()
    county = str(item.get("county") or raw.get("county") or "").strip()
    return {
        "content": str(item.get("content") or raw.get("content") or ""),
        "eventType": str(item.get("eventType") or item.get("event_type") or raw.get("eventType") or ""),
        "department": str(item.get("department") or raw.get("department") or item.get("source") or raw.get("source") or ""),
        "msgType": str(item.get("msgType") or item.get("msg_type") or raw.get("msgType") or ""),
        "time": str(item.get("time") or item.get("publish_time") or raw.get("time") or ""),
        "severity": str(item.get("severity") or raw.get("severity") or ""),
        "locationName": str(item.get("locationName") or item.get("location_name") or raw.get("locationName") or "".join([province, city, county])),
        "province": province,
        "city": city,
        "county": county,
    }


def _warning_records_from_payload(tool_name: str, payload: Any) -> list[dict[str, str]]:
    data = _unwrap_tool_result(payload)
    if not isinstance(data, dict):
        return []
    if tool_name == "get_today_warning_summary":
        items = data.get("today_published_warnings") or data.get("today_new_or_update_warnings") or []
    else:
        items = data.get("warnings") or data.get("effective_warnings") or data.get("today_published_warnings") or []
    if not isinstance(items, list):
        return []
    if tool_name == "get_national_warning_info":
        return [
            _compact_warning_record_for_table({**item, "department": item.get("department") or "中央气象台"})
            if isinstance(item, dict) else _compact_warning_record_for_table(item)
            for item in items
        ]
    return [_compact_warning_record_for_table(item) for item in items]


def _warning_table_title(tool_name: str, multi_tool: bool = False) -> str:
    if multi_tool:
        return "【相关预警清单】"
    return {
        "get_national_warning_info": "【国家预警清单】",
        "get_today_warning_summary": "【今日发布预警清单】",
        "get_history_warning_info": "【历史预警清单】",
    }.get(tool_name, "【生效预警清单】")


def _warning_department_area(department: str) -> str:
    area = re.sub(r"(气象台|气象局|预警发布中心|发布中心|台)$", "", (department or "").strip()).strip()
    if area == "天津市":
        return "天津市"
    return "天津海域" if "海洋中心" in area else area


def _extract_warning_area(record: dict[str, str]) -> str:
    return str(record.get("locationName") or "").strip() or _warning_department_area(str(record.get("department") or "")) or "暂未明确"


def _warning_query_event_keywords(user_text: str) -> list[str]:
    text = user_text or ""
    keywords: list[str] = []
    if any(k in text for k in ["暴雨", "大暴雨", "强降雨", "短时强降水"]): keywords.append("暴雨")
    if "海上大风" in text: keywords.append("海上大风")
    elif any(k in text for k in ["雷雨大风", "雷暴大风", "大风"]): keywords.append("雷雨大风")
    if any(k in text for k in ["冰雹", "雹"]): keywords.append("冰雹")
    if "高温" in text: keywords.append("高温")
    if any(k in text for k in ["雷电", "雷雨"]): keywords.append("雷")
    if "寒潮" in text: keywords.append("寒潮")
    if any(k in text for k in ["大雾", "低能见度"]): keywords.append("大雾")
    if any(k in text for k in ["道路结冰", "结冰"]): keywords.append("道路结冰")
    if "霾" in text: keywords.append("霾")
    if any(k in text for k in ["地质灾害", "山洪"]): keywords.extend(["地质灾害", "山洪"])
    return list(dict.fromkeys(keywords))


def _is_national_and_tianjin_warning_query(user_text: str) -> bool:
    text = str(user_text or "")
    asks_national = any(
        word in text
        for word in ("国家局", "中央气象台", "国家中央气象台", "国家气象中心", "中央台")
    )
    asks_tianjin = any(word in text for word in ("天津", "天津市", "我市", "本市"))
    return asks_national and asks_tianjin


def _is_tianjin_national_warning(record: dict[str, str]) -> bool:
    """判断中央气象台记录是否明确指向天津地区。"""
    structured_area = "".join(
        str(record.get(key) or "")
        for key in ("province", "city", "county", "locationName")
    ).strip()
    if structured_area:
        return "天津" in structured_area
    return "天津" in str(record.get("content") or "")


def _filter_warning_records_for_user(records: list[dict[str, str]], user_text: str) -> list[dict[str, str]]:
    filtered = list(records)
    if _is_national_and_tianjin_warning_query(user_text):
        filtered = [
            record
            for record in filtered
            if (
                record.get("_source_tool") != "get_national_warning_info"
                or _is_tianjin_national_warning(record)
            )
        ]
    events = _warning_query_event_keywords(user_text)
    if events:
        filtered = [r for r in filtered if any(key in str(r.get("eventType") or "") for key in events)]
    severities = [level for level in ["红色", "橙色", "黄色", "蓝色"] if level in (user_text or "")]
    if severities:
        filtered = [r for r in filtered if any(level in str(r.get("severity") or "") for level in severities)]
    areas = list(dict.fromkeys(
        area for area in (_extract_warning_area(r) for r in filtered)
        if area not in _BROAD_SCOPE_TERMS and area in (user_text or "")
    ))
    if areas:
        filtered = [r for r in filtered if any(a in _extract_warning_area(r) or a in str(r.get("department") or "") for a in areas)]
    text = user_text or ""
    asks_released_list = any(k in text for k in ["已解除预警", "解除预警有哪些", "解除的预警"])
    asks_release_judgement = any(k in text for k in ["解除了吗", "是否解除", "何时解除", "什么时候解除", "到什么时候"])
    if asks_released_list and not asks_release_judgement:
        filtered = [r for r in filtered if "解除" in str(r.get("msgType") or "")]
    return filtered


def _trim_warning_regions_for_scope(records: list[dict], user_text: str) -> list[dict]:
    """按用户问法作用域裁剪记录的影响区域。

    市级问法（市台/全市/本市/我市）不展开各区县明细，标记为市级层面；
    具体区县问法仅保留该区县相关记录；未指定区县且非市级时保留全部记录。
    """
    text = user_text or ""
    asks_broad = any(t in text for t in _BROAD_SCOPE_TERMS)
    # 问法是否明确指定了某个区县：遍历所有记录的影响区域，判断是否命中文本。
    all_districts = {
        a
        for rec in records
        for a in (_extract_warning_area(rec) or "").split("、")
        if a
    }
    names_district = any(a in text for a in all_districts)
    trimmed = []
    for rec in records:
        area = str(rec.get("locationName") or _extract_warning_area(rec) or "")
        if names_district:
            # 具体区县问法：仅保留包含该区县关键词的记录
            matching = [a for a in (_extract_warning_area(rec) or "").split("、") if a and a in text]
            if matching:
                rec["locationName"] = "、".join(matching)
            elif area and any(a in area for a in ["全市", "各区县"]):
                rec["locationName"] = "全市"
            else:
                continue  # 与问法无关的区县，丢弃
        elif asks_broad:
            # 市级问法：把区县明细折叠为市级层面
            rec["locationName"] = "全市"
        # 未指定区县且非市级：保留原记录，不做裁剪
        trimmed.append(rec)
    return trimmed


def _is_broad_scoped_warning_query(user_text: str) -> bool:
    """市级问法（市台/全市/本市/我市）不展开各区县影响区域列。

    若问法指定了具体区县（如"天津市蓟州区"、"滨海新区"），不算市级问法，
    保留影响区域列。
    """
    text = user_text or ""
    if re.search(r"[一-龥]{1,4}区(?:县)?", text):
        return False
    return any(t in text for t in _BROAD_SCOPE_TERMS)


_WARNING_DISPLAY_LABELS = (
    "短时强降水",
    "雷雨大风",
    "雷暴大风",
    "海上大风",
    "大暴雨",
    "强降雨",
    "道路结冰",
    "地质灾害",
    "低能见度",
    "暴雨",
    "高温",
    "冰雹",
    "雷电",
    "寒潮",
    "大雾",
    "山洪",
    "霾",
    "大风",
)


def _warning_query_display_label(user_text: str) -> str:
    """提取用户明确询问的预警类型，用于无生效预警的固定回执。"""
    text = str(user_text or "")
    known = next((label for label in _WARNING_DISPLAY_LABELS if label in text), "")
    if known:
        return known
    candidates = re.findall(r"([\u4e00-\u9fffA-Za-z0-9]{2,12})预警", text)
    excluded = ("当前", "今天", "哪些", "什么", "发布", "相关", "天气", "国家局", "中央", "天津")
    return next(
        (candidate for candidate in reversed(candidates) if not any(word in candidate for word in excluded)),
        "",
    )


def _strict_no_effective_warning_signal(
    warning_bundles: list[dict[str, Any]],
    user_text: str,
) -> str:
    """命中明确预警类型但生效接口无该类型时，返回唯一允许的答复。"""
    label = _warning_query_display_label(user_text)
    if not label:
        return ""
    effective_records = [
        record
        for bundle in warning_bundles
        if bundle.get("tool_name") == "get_effective_warning_info"
        for record in (bundle.get("records") or [])
    ]
    if not effective_records:
        return f"当前无生效{label}预警信号"

    event_keywords = _warning_query_event_keywords(user_text)
    matching_records = effective_records
    if event_keywords:
        matching_records = [
            record
            for record in matching_records
            if any(keyword in str(record.get("eventType") or "") for keyword in event_keywords)
        ]
    else:
        matching_records = [
            record
            for record in matching_records
            if label in str(record.get("eventType") or "")
            or label in str(record.get("content") or "")
        ]
    severities = [level for level in ["红色", "橙色", "黄色", "蓝色"] if level in user_text]
    if severities:
        matching_records = [
            record
            for record in matching_records
            if any(level in str(record.get("severity") or "") for level in severities)
        ]
    return f"当前无生效{label}预警信号" if not matching_records else ""


def _warning_publisher_rank(record: dict[str, str]) -> int:
    """中央气象台在前，天津市气象台其次，其他发布单位最后。"""
    department = re.sub(r"\s+", "", str(record.get("department") or ""))
    if (
        record.get("_source_tool") == "get_national_warning_info"
        or "中央气象台" in department
        or "国家气象中心" in department
    ):
        return 0
    if "天津市气象台" in department:
        return 1
    return 2


def _warning_time_sort_value(value: Any) -> float:
    """将常见预警发布时间转成可降序比较的数值；无法解析时稳定排在组内末尾。"""
    text = str(value or "").strip()
    if not text:
        return 0.0
    normalized = re.sub(r"[年月/.]", "-", text).replace("日", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H",
        "%Y-%m-%d",
        "%Y%m%d%H%M%S",
        "%Y%m%d%H%M",
    ):
        try:
            return datetime.strptime(normalized, fmt).timestamp()
        except ValueError:
            continue
    digits = re.sub(r"\D", "", text)
    return float(digits[:14]) if len(digits) >= 8 else 0.0


def _sort_warning_records(records: list[dict[str, str]]) -> list[dict[str, str]]:
    """统一显示顺序，保证表格、预警正文和防范建议使用同一记录序列。"""
    indexed_records = list(enumerate(records))
    indexed_records.sort(
        key=lambda pair: (
            _warning_publisher_rank(pair[1]),
            -_warning_time_sort_value(pair[1].get("time")),
            pair[0],
        )
    )
    return [record for _, record in indexed_records]


def _build_warning_table_markdown(records: list[dict[str, str]], title: str, show_region_column: bool = True) -> str:
    if not records:
        return f"{title}\n\n未检索到符合条件的预警记录。"
    header = "| 序号 | 发布单位 | 预警类型 | 等级 | 发布时间 | 发布状态 |"
    sep = "| :---: | :--- | :--- | :--- | :--- | :--- |"
    if show_region_column:
        header = "| 序号 | 发布单位 | 预警类型 | 等级 | 影响区域 | 发布时间 | 发布状态 |"
        sep = "| :---: | :--- | :--- | :--- | :--- | :--- | :--- |"
    lines = [f"{title}\n\n", header + "\n", sep + "\n"]
    for index, record in enumerate(records, 1):
        row = f"| {index} | {_clean_table_cell(record.get('department') or '—')} | {_clean_table_cell(record.get('eventType') or '—')} | {_clean_table_cell(record.get('severity') or '—')} |"
        if show_region_column:
            row += f" {_clean_table_cell(record.get('locationName') or _extract_warning_area(record) or '暂未明确')} |"
        row += f" {_clean_table_cell(record.get('time') or '—')} | {_clean_table_cell(record.get('msgType') or '—')} |"
        lines.append(row + "\n")
    return "".join(lines).strip()


def build_warning_bundle(tool_name: str, observation: Any) -> dict[str, Any]:
    records = _warning_records_from_payload(tool_name, observation)
    for record in records:
        record["_source_tool"] = tool_name
    return {"tool_name": tool_name, "records": records, "title": _warning_table_title(tool_name)}


def _merge_warning_bundles(bundles: list[dict[str, Any]]) -> dict[str, Any]:
    records: list[dict[str, str]] = []
    tool_names: list[str] = []
    for bundle in bundles:
        tool_name = str(bundle.get("tool_name") or "")
        if tool_name:
            tool_names.append(tool_name)
        records.extend(bundle.get("records") or [])
    title = _warning_table_title(tool_names[0], multi_tool=len(set(tool_names)) > 1) if tool_names else "【预警清单】"
    return {"records": records, "title": title}


def _is_warning_record_released(record: dict[str, str]) -> bool:
    return "解除" in str(record.get("msgType") or "") or "解除" in str(record.get("content") or "")


def _enforce_single_warning_core(core_text: Any) -> str:
    """将预警核心结论确定性收口为严格一句，并统一使用一个中文句号。"""
    body = re.sub(
        r"^\s*【核心结论】\s*",
        "",
        str(core_text or ""),
        count=1,
    )
    body = re.sub(r"\s+", " ", body).strip()
    if not body:
        body = "已获取预警信息"

    first_sentence = re.match(r"^.*?[。！？!?](?:[”’」』])?", body)
    sentence = first_sentence.group(0).strip() if first_sentence else body
    sentence = re.sub(r"[。！？!?](?:[”’」』])?$", "", sentence).strip()
    sentence = sentence.rstrip("；;，,、 ")
    if sentence.count("**") % 2:
        sentence += "**"
    return f"【核心结论】\n{sentence}。"


def _build_warning_contents(records: list[dict[str, str]], sanitize_text: Callable[[str], str]) -> str:
    """逐条输出正文；缺失正文也保留占位，确保与表格序号一一对应。"""
    if not records:
        return ""
    lines = []
    for index, record in enumerate(records, 1):
        content = str(record.get("content") or "").strip()
        display_content = sanitize_text(content) if content else "接口未返回预警正文。"
        lines.append(f"{index}. {display_content}")
    return "【预警内容】\n" + "\n".join(lines)


def _build_warning_code_fallback(bundles: list[dict[str, Any]], user_text: str, sanitize_text: Callable[[str], str]) -> str:
    merged = _merge_warning_bundles(bundles)
    records = _sort_warning_records(_filter_warning_records_for_user(merged["records"], user_text))
    if not records:
        return _enforce_single_warning_core("未检索到符合条件的预警记录。")
    active = [r for r in records if not _is_warning_record_released(r)]
    labels = list(dict.fromkeys(
        f"{r.get('eventType') or '预警'}{'' if not r.get('severity') or r.get('severity') in str(r.get('eventType')) else r.get('severity')}"
        for r in records
    ))
    areas = list(dict.fromkeys(_extract_warning_area(r) for r in records if _extract_warning_area(r) != "暂未明确"))
    core = f"当前检索到 **{len(active)}条** 正在生效或仍需关注的相关预警" if active else f"当前未检索到仍在生效的相关预警；本次返回 **{len(records)}条** 记录主要为已解除或历史预警"
    if labels:
        core += f"，主要包括 **{'、'.join(labels[:5])}**"
    if areas:
        core += f"，涉及{'、'.join(areas[:6])}"
    sections = [_enforce_single_warning_core(core), _build_warning_table_markdown(records, merged["title"])]
    sections.append(_build_warning_contents(records, sanitize_text))
    return "\n\n".join(sections)


def _is_high_temperature_warning_value_query(user_text: str) -> bool:
    text = user_text or ""
    knowledge_words = ("发布标准", "预警标准", "阈值", "分几级", "颜色等级", "定义", "区别", "达到多少度发布")
    if any(word in text for word in knowledge_words):
        return False
    return (
        "高温预警" in text
        and any(word in text for word in ("最高会到", "最高气温", "最高温度", "多少度"))
    )


def _is_warning_fact_query(user_text: str) -> bool:
    text = user_text or ""
    if "预警" not in text:
        return False
    if _is_high_temperature_warning_value_query(text):
        return True
    forecast_values = ("最高气温", "最低气温", "最高会到", "多少度", "温度", "雨量", "降水量", "风力几级", "风力多大", "影响时段", "未来几天", "未来一周", "未来七天", "未来7天")
    knowledge_words = ("发布标准", "预警标准", "阈值", "分几级", "颜色等级", "定义", "区别")
    return not any(word in text for word in forecast_values + knowledge_words)


def is_warning_fact_query(user_text: str) -> bool:
    return _is_warning_fact_query(user_text)


def _normalize_warning_route(route: dict[str, Any]) -> dict[str, Any]:
    names = route.get("tool_names") if isinstance(route, dict) else []
    if isinstance(names, str):
        names = [names]
    selected = [str(name).strip() for name in names if str(name).strip() in WARNING_TOOL_NAMES] if isinstance(names, list) else []
    return {
        "tool_names": list(dict.fromkeys(selected)) or ["get_effective_warning_info"],
        "national_keywords": str((route or {}).get("national_keywords") or "天津").strip(),
        "reason": str((route or {}).get("reason") or "").strip(),
    }


def _infer_national_warning_keywords(user_text: str, model_keywords: str | None = None) -> str:
    text = user_text or ""
    if "全国" in text: return ""
    if "华北" in text: return "北京,天津,河北,山西,内蒙古"
    if "京津冀" in text: return "北京,天津,河北"
    names = [name.replace("北京市", "北京").replace("河北省", "河北").replace("天津市", "天津") for name in ("北京", "北京市", "河北", "河北省", "天津", "天津市") if name in text]
    if names: return ",".join(dict.fromkeys(names))
    if any(k in text for k in ("周边", "邻近", "附近省市", "周边地区", "周边省市")): return "北京,河北"
    if any(k in text for k in ("国家局", "中央气象台", "国家中央气象台", "国家气象中心", "中央台")): return "天津"
    return str(model_keywords or "").strip() or "天津"


def _warning_tool_args(tool_name: str, route: dict[str, Any]) -> dict[str, Any]:
    if tool_name != "get_national_warning_info":
        return {}
    keywords = route.get("national_keywords")
    return {"keywords": "" if keywords == "" else (keywords or "天津"), "max_items": 30}


def _fill_prompt(template: str, **values: str) -> str:
    """避免提示词中的 JSON 花括号被 ``str.format`` 当作变量。"""
    prompt = template or ""
    for key, value in values.items():
        prompt = prompt.replace("{" + key + "}", str(value))
    return prompt


def _route_warning_tools_rule_based(user_text: str) -> dict | None:
    t = (user_text or "").strip()
    if not t or "预警" not in t:
        return None
    tool_names = ["get_effective_warning_info"]
    # 地名关键词需排除"河北区/北京区"这类天津区县后缀，避免把天津本地区县问法误判为国家。
    national = any(k in t for k in ("国家局", "中央气象台", "中央台", "全国", "周边", "华北", "京津冀"))
    national = national or bool(re.search(r"北京(?!区)", t)) or bool(re.search(r"河北(?!区)", t))
    history = any(k in t for k in ("解除了吗", "解除预警", "已解除", "解除的", "历史预警", "过去", "此前"))
    today = any(k in t for k in ("今天新发", "今日新发", "今日发布", "今天发布", "今日预警", "今天预警", "新发", "动态"))
    if national:
        has_local = any(k in t for k in ("天津", "我市", "全市"))
        tool_names = ["get_effective_warning_info", "get_national_warning_info"] if has_local else ["get_national_warning_info"]
    if history and "get_history_warning_info" not in tool_names:
        tool_names.append("get_history_warning_info")
    if today and "get_today_warning_summary" not in tool_names:
        tool_names.append("get_today_warning_summary")
    route = _normalize_warning_route({
        "tool_names": tool_names,
        "national_keywords": _infer_national_warning_keywords(t, None),
        "reason": "规则路由",
    })
    if "get_national_warning_info" in route["tool_names"]:
        # 镜像 LLM 路径的调用顺序：normalize 会把空串兜底成"天津"，
        # 需再跑一次推理以恢复"全国"→空串（match-all）等语义。
        route["national_keywords"] = _infer_national_warning_keywords(t, route.get("national_keywords"))
    return route


async def _route_warning_tools(answer_chain: Any, user_text: str, callbacks: dict[str, Any]) -> dict[str, Any]:
    rule_route = _route_warning_tools_rule_based(user_text)
    if rule_route:
        print(f"[WarningWorkflow] rule route={json.dumps(rule_route, ensure_ascii=False)}")
        return rule_route
    # 回退：现有 LLM 路由
    prompt = _fill_prompt(
        WARNING_ROUTE_PROMPT,
        current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        user_query=user_text,
    )
    result = await callbacks["ainvoke_chain"](answer_chain, {"messages": [HumanMessage(content=prompt)]})
    route = _normalize_warning_route(_extract_first_json_object(getattr(result, "content", None) or str(result)))
    if _is_high_temperature_warning_value_query(user_text) and "get_effective_warning_info" not in route["tool_names"]:
        # 保留模型原有选择与顺序，仅补齐该业务问法明确要求的生效预警接口。
        route["tool_names"].append("get_effective_warning_info")
    if "get_national_warning_info" in route["tool_names"]:
        route["national_keywords"] = _infer_national_warning_keywords(user_text, route.get("national_keywords"))
    print(f"[WarningWorkflow] route={json.dumps(route, ensure_ascii=False)}")
    return route


def _warning_contents_for_llm(records: list[dict[str, str]]) -> str:
    lines = []
    for index, record in enumerate(records, 1):
        content = str(record.get("content") or "").strip()
        if not content:
            continue
        meta = "；".join(part for part in [
            f"预警类型：{record.get('eventType')}" if record.get("eventType") else "",
            f"等级：{record.get('severity')}" if record.get("severity") else "",
            f"发布单位：{record.get('department')}" if record.get("department") else "",
            f"影响区域：{record.get('locationName')}" if record.get("locationName") else "",
            f"发布时间：{record.get('time')}" if record.get("time") else "",
            f"状态：{record.get('msgType')}" if record.get("msgType") else "",
            f"数据类别：{record.get('_source_tool')}" if record.get("_source_tool") else "",
        ] if part)
        lines.append(f"{index}. {meta}\ncontent：{content}")
    return "\n\n".join(lines) if lines else "无预警正文。"


def _effective_warning_contents_for_llm(records: list[dict[str, str]]) -> str:
    """只把生效预警接口正文作为模型可提取防范建议的候选来源。"""
    effective_records = [
        record
        for record in records
        if record.get("_source_tool") == "get_effective_warning_info"
        and str(record.get("content") or "").strip()
    ]
    if not effective_records:
        return "无可提取的生效预警正文，必须省略【防范建议】。"
    return "\n\n".join(
        f"记录{index}\ncontent：{str(record.get('content') or '').strip()}"
        for index, record in enumerate(effective_records, 1)
    )


def _normalize_advice_for_validation(text: Any) -> str:
    """仅忽略排版差异；不改写语义，供原文包含校验与去重使用。"""
    value = str(text or "").replace("**", "")
    return re.sub(r"\s+", "", value).strip()


def _build_llm_extracted_warning_advice(
    llm_text: str,
    records: list[dict[str, str]],
    sanitize_text: Callable[[str], str],
) -> str:
    """校验模型摘录确实来自相应生效预警 content，再按记录顺序去重组装。"""
    effective_records = [
        record
        for record in records
        if record.get("_source_tool") == "get_effective_warning_info"
        and str(record.get("content") or "").strip()
    ]
    if not effective_records:
        return ""

    match = re.search(
        r"【防范建议】\s*(.*?)(?=\n*【(?:核心结论|预警内容|[^】]*清单)】|\Z)",
        str(llm_text or ""),
        flags=re.DOTALL,
    )
    if not match:
        return ""

    extracted: list[tuple[int, int, str]] = []
    for output_order, raw_line in enumerate(match.group(1).splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        item_match = re.match(
            r"^(?:[-*]\s*)?(?:记录\s*)?(\d+)\s*(?:[|｜:：]|\.\s+)\s*(.+?)\s*$",
            line,
        )
        if not item_match:
            continue
        source_index = int(item_match.group(1))
        if source_index < 1 or source_index > len(effective_records):
            continue
        candidate = item_match.group(2).strip().replace("**", "")
        candidate_normalized = _normalize_advice_for_validation(candidate)
        source_normalized = _normalize_advice_for_validation(
            effective_records[source_index - 1].get("content")
        )
        if not candidate_normalized or candidate_normalized not in source_normalized:
            print(
                f"[WarningWorkflow] 丢弃非原文防范建议：记录{source_index} "
                f"{candidate[:80]!r}"
            )
            continue
        extracted.append((source_index, output_order, candidate))

    extracted.sort(key=lambda item: (item[0], item[1]))
    advice_items: list[str] = []
    seen: set[str] = set()
    for _, _, candidate in extracted:
        normalized = _normalize_advice_for_validation(candidate).rstrip("。；;！!？?")
        if normalized in seen:
            continue
        seen.add(normalized)
        advice_items.append(sanitize_text(candidate).strip())
    if not advice_items:
        return ""
    return "【防范建议】\n" + "\n".join(
        f"{index}. {advice}" for index, advice in enumerate(advice_items, 1)
    )


async def _generate_warning_core_and_advice(
    answer_chain: Any,
    records: list[dict[str, str]],
    user_text: str,
    callbacks: dict[str, Any],
    runtime: WarningRuntime,
) -> str:
    prompt = _fill_prompt(
        WARNING_SUMMARY_PROMPT,
        user_query=user_text,
        contents_text=_warning_contents_for_llm(records),
        advice_contents_text=_effective_warning_contents_for_llm(records),
    )
    result = await callbacks["ainvoke_chain"](answer_chain, {"messages": [HumanMessage(content=prompt)]})
    return runtime.sanitize_display_text(getattr(result, "content", None) or str(result)).strip()


async def finalize_warning_answer(answer_chain: Any, warning_bundles: list[dict[str, Any]], user_text: str, callbacks: dict[str, Any], runtime: WarningRuntime) -> str:
    """两条触发路径共用的唯一回答装配器。"""
    strict_no_effective = _strict_no_effective_warning_signal(warning_bundles, user_text)
    if strict_no_effective:
        return strict_no_effective
    merged = _merge_warning_bundles(warning_bundles)
    records = _sort_warning_records(_filter_warning_records_for_user(merged["records"], user_text))
    records = _trim_warning_regions_for_scope(records, user_text)
    try:
        llm_text = await _generate_warning_core_and_advice(
            answer_chain,
            records,
            user_text,
            callbacks,
            runtime,
        )
        core_match = re.search(r"(【核心结论】.*?)(?=\n*【(?:防范建议|预警内容|[^】]*清单)】|\Z)", llm_text, flags=re.DOTALL)
        raw_core = core_match.group(1).strip() if core_match else (llm_text or "已获取预警信息。")
        core = _enforce_single_warning_core(raw_core)
        sections = [core]
        if records:
            show_region = not _is_broad_scoped_warning_query(user_text)  # 新增辅助：市级问法隐藏区县列
            sections.append(_build_warning_table_markdown(records, merged["title"], show_region_column=show_region))
            sections.append(_build_warning_contents(records, runtime.sanitize_display_text))
        advice = _build_llm_extracted_warning_advice(
            llm_text,
            records,
            runtime.sanitize_display_text,
        )
        if advice:
            sections.append(advice)
        return "\n\n".join(section for section in sections if section).strip()
    except Exception as exc:
        print(f"[WarningWorkflow] 摘要失败，使用代码兜底：{exc}")
        return _build_warning_code_fallback(warning_bundles, user_text, runtime.sanitize_display_text)


async def _collect_routed_warning_bundles(answer_chain: Any, tools: list[Any], user_text: str, callbacks: dict[str, Any], runtime: WarningRuntime) -> list[dict[str, Any]]:
    route = await _route_warning_tools(answer_chain, user_text, callbacks)
    selected = [(name, runtime.find_tool(tools, name)) for name in route["tool_names"]]
    selected = [(name, tool) for name, tool in selected if tool is not None]
    if not selected:
        return []
    bundles: list[dict[str, Any]] = []
    for name, tool in selected:
        args = _warning_tool_args(name, route)
        result = await asyncio.wait_for(runtime.invoke_fast_tool(name, tool, args, user_text), timeout=30)
        bundles.append(build_warning_bundle(name, result))
    return bundles


async def try_warning_fact_fast_path(user_text: str, answer_chain: Any, tools: list[Any], messages: list[Any], callbacks: dict[str, Any], runtime: WarningRuntime) -> bool:
    if not _is_warning_fact_query(user_text):
        return False
    try:
        bundles = await _collect_routed_warning_bundles(answer_chain, tools, user_text, callbacks, runtime)
        if not bundles:
            return False
        final_text = await finalize_warning_answer(answer_chain, bundles, user_text, callbacks, runtime)
        final_text = runtime.sanitize_display_text(callbacks["append_followup_if_needed"](final_text, user_text))
        final_text = runtime.prepend_thinking_summary(final_text, user_text, has_chart=False)
        await callbacks["stream_text_to_message"](final_text)
        messages.extend([HumanMessage(content=user_text), AIMessage(content=final_text)])
        cl.user_session.set("messages", messages)
        return True
    except asyncio.TimeoutError:
        return await runtime.handle_fast_path_error("预警信息", messages, user_text)
    except Exception as exc:
        print(f"[WarningWorkflow] 快捷路径失败，回退通用流程：{exc}")
        traceback.print_exc()
        return False
async def collect_warning_fallback_bundles(answer_chain: Any, tools: list[Any], user_text: str, callbacks: dict[str, Any], runtime: WarningRuntime) -> list[dict[str, Any]]:
    """Planner 漏调预警工具时的补救；不额外创建前端步骤。"""
    if not _is_warning_fact_query(user_text):
        return []
    return await _collect_routed_warning_bundles(answer_chain, tools, user_text, callbacks, runtime)
