"""预警查询、装配与回答工作流。

无论查询由快捷路径的专用路由器触发，还是由 Planner 触发，最终均在本模块
完成：代码生成预警清单和原始正文，模型仅生成核心结论与防范建议。
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
    broad_terms = {"天津", "天津市", "我市", "全市", "本市"}
    areas = list(dict.fromkeys(
        area for area in (_extract_warning_area(r) for r in filtered)
        if area not in broad_terms and area in (user_text or "")
    ))
    if areas:
        filtered = [r for r in filtered if any(a in _extract_warning_area(r) or a in str(r.get("department") or "") for a in areas)]
    text = user_text or ""
    asks_released_list = any(k in text for k in ["已解除预警", "解除预警有哪些", "解除的预警"])
    asks_release_judgement = any(k in text for k in ["解除了吗", "是否解除", "何时解除", "什么时候解除", "到什么时候"])
    if asks_released_list and not asks_release_judgement:
        filtered = [r for r in filtered if "解除" in str(r.get("msgType") or "")]
    return filtered


def _build_warning_table_markdown(records: list[dict[str, str]], title: str) -> str:
    if not records:
        return f"{title}\n\n未检索到符合条件的预警记录。"
    lines = [
        f"{title}\n\n", "| 序号 | 发布单位 | 预警类型 | 等级 | 影响区域 | 发布时间 | 发布状态 |\n",
        "| :---: | :--- | :--- | :--- | :--- | :--- | :--- |\n",
    ]
    for index, record in enumerate(records, 1):
        lines.append(
            f"| {index} | {_clean_table_cell(record.get('department') or '—')} | "
            f"{_clean_table_cell(record.get('eventType') or '—')} | {_clean_table_cell(record.get('severity') or '—')} | "
            f"{_clean_table_cell(record.get('locationName') or _extract_warning_area(record) or '暂未明确')} | "
            f"{_clean_table_cell(record.get('time') or '—')} | {_clean_table_cell(record.get('msgType') or '—')} |\n"
        )
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


def _build_warning_code_fallback(bundles: list[dict[str, Any]], user_text: str, sanitize_text: Callable[[str], str]) -> str:
    merged = _merge_warning_bundles(bundles)
    records = _filter_warning_records_for_user(merged["records"], user_text)
    if not records:
        return "【核心结论】\n未检索到符合条件的预警记录。"
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
    sections = ["【核心结论】\n" + core + "。", _build_warning_table_markdown(records, merged["title"])]
    contents = [f"{idx}. {sanitize_text(str(r.get('content') or '').strip())}" for idx, r in enumerate(records, 1) if str(r.get("content") or "").strip()]
    if contents:
        sections.append("【预警内容】\n" + "\n".join(contents))
    advice: list[str] = []
    for record in active:
        for part in re.split(r"[。；;！!？?]\s*", str(record.get("content") or "")):
            if any(k in part for k in ["请", "注意", "加强", "避免", "防范", "转移", "做好", "减少", "远离"]):
                advice.append(part.strip().rstrip("。") + "。")
    if advice:
        sections.append("【防范建议】\n" + "\n".join(f"{idx}. {item}" for idx, item in enumerate(dict.fromkeys(advice), 1)))
    return "\n\n".join(sections)


def _is_warning_fact_query(user_text: str) -> bool:
    text = user_text or ""
    if "预警" not in text:
        return False
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


async def _route_warning_tools(answer_chain: Any, user_text: str, callbacks: dict[str, Any]) -> dict[str, Any]:
    prompt = _fill_prompt(
        WARNING_ROUTE_PROMPT,
        current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        user_query=user_text,
    )
    result = await callbacks["ainvoke_chain"](answer_chain, {"messages": [HumanMessage(content=prompt)]})
    route = _normalize_warning_route(_extract_first_json_object(getattr(result, "content", None) or str(result)))
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


async def _generate_warning_core_and_advice(answer_chain: Any, records: list[dict[str, str]], user_text: str, callbacks: dict[str, Any], runtime: WarningRuntime) -> str:
    prompt = _fill_prompt(
        WARNING_SUMMARY_PROMPT,
        user_query=user_text,
        contents_text=_warning_contents_for_llm(records),
    )
    result = await callbacks["ainvoke_chain"](answer_chain, {"messages": [HumanMessage(content=prompt)]})
    return runtime.sanitize_display_text(getattr(result, "content", None) or str(result)).strip()


async def finalize_warning_answer(answer_chain: Any, warning_bundles: list[dict[str, Any]], user_text: str, callbacks: dict[str, Any], runtime: WarningRuntime) -> str:
    """两条触发路径共用的唯一回答装配器。"""
    merged = _merge_warning_bundles(warning_bundles)
    records = _filter_warning_records_for_user(merged["records"], user_text)
    try:
        llm_text = await _generate_warning_core_and_advice(answer_chain, records, user_text, callbacks, runtime)
        core_match = re.search(r"(【核心结论】.*?)(?=\n*【防范建议】|\Z)", llm_text, flags=re.DOTALL)
        advice_match = re.search(r"(【防范建议】.*)\Z", llm_text, flags=re.DOTALL)
        core = core_match.group(1).strip() if core_match else (llm_text or "【核心结论】\n已获取预警信息。")
        sections = [core]
        if records:
            sections.append(_build_warning_table_markdown(records, merged["title"]))
            contents = [f"{idx}. {runtime.sanitize_display_text(str(r.get('content') or '').strip())}" for idx, r in enumerate(records, 1) if str(r.get("content") or "").strip()]
            if contents:
                sections.append("【预警内容】\n" + "\n".join(contents))
        if advice_match:
            sections.append(advice_match.group(1).strip())
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
