import re
import json
import os
import time
import base64
import inspect
import traceback
import asyncio
import uuid
import httpx
import chainlit as cl
from collections import Counter
from datetime import datetime, timedelta
from typing import Any
from langchain_core.messages import ToolMessage, HumanMessage, AIMessage

try:
    from prompts import WARNING_ROUTE_PROMPT, WARNING_SUMMARY_PROMPT, THINKING_PROMPT, FAST_PATH_THINKING_PROMPT
except Exception:
    WARNING_ROUTE_PROMPT = ""
    WARNING_SUMMARY_PROMPT = ""
    THINKING_PROMPT = ""
    FAST_PATH_THINKING_PROMPT = ""

try:
    from timing_logger import TimingLogger
except Exception:
    class TimingLogger:
        @staticmethod
        def log_tool(*args, **kwargs):
            pass

        @staticmethod
        def log_query(*args, **kwargs):
            pass

        @staticmethod
        def _safe_summary(text, max_len=40):
            return str(text)[:max_len] if text else ""

from utils.tool_result import _unwrap_tool_result

# Feature flag: when false (default), all fast-path pre-routing is disabled and every query flows through the planner LLM.
ENABLE_FAST_PATHS = os.environ.get("ENABLE_FAST_PATHS", "false").strip().lower() in ("1", "true", "yes")

from tools.decision_weather_core import (
    _compact_decision_forecast_facts,
    _decision_hourly_window,
    _decision_period_args,
    _decision_pick_first_poi,
    _decision_weather_prefilter,
    _extract_decision_weather_slots,
    _extract_first_json_object,
    _generate_decision_weather_answer,
    _nearest_decision_station,
    _normalize_decision_weather_slots,
    _parse_decision_dt,
    _select_decision_fcst_time,
)


def _chainlit_step_accepts_auto_collapse() -> bool:
    """Return True if the current Chainlit Step accepts an auto_collapse kwarg."""
    try:
        return "auto_collapse" in inspect.signature(cl.Step.__init__).parameters
    except (TypeError, ValueError):
        return False


class ReasoningStep:
    """
    DeepSeek 式实时思考步骤：在 Chainlit 界面展示可展开的推理过程。
    通过 append/update 实时刷新，让业务人员看到系统每一步在做什么。
    业务阶段（stage）以 Markdown 标题形式直接写入父 step output，
    避免创建嵌套子 stage 导致前端重复渲染。
    """

    _warned_low_version: bool = False

    def __init__(self, name: str = "🤔 思考过程"):
        self.name = name
        self.step: cl.Step | None = None
        self._buffer: str = ""
        self._closed: bool = False
        self._step_supports_stream: bool = False
        self._step_accepts_auto_collapse: bool = _chainlit_step_accepts_auto_collapse()

        if not ReasoningStep._warned_low_version and not self._step_accepts_auto_collapse:
            version = getattr(cl, "__version__", "unknown")
            print(
                f"[ReasoningStep] 当前 Chainlit {version} 不支持 auto_collapse，"
                f"思考过程在回答结束后不会自动折叠；建议升级到 >= 2.10.0"
            )
            ReasoningStep._warned_low_version = True

    async def __aenter__(self):
        # 把思考步骤挂到当前 run 下面，否则 parent_id=None 会被当成 root step，Chainlit CoT 不渲染
        parent_id = None
        try:
            current_run = cl.context.current_run
            if current_run is not None:
                parent_id = current_run.id
        except Exception as exc:
            print(f"[ReasoningStep] 无法获取 current_run，回退到 root step：{exc}")

        # 重置状态以支持实例重用；如果旧 step 未关闭，保留旧引用但不再写入
        self._closed = False
        self._buffer = ""
        step_kwargs = {
            "name": self.name,
            "type": "llm",
            "parent_id": parent_id,
        }
        if self._step_accepts_auto_collapse:
            step_kwargs["auto_collapse"] = True
        self.step = cl.Step(**step_kwargs)
        self._step_supports_stream = hasattr(self.step, "stream_token")
        self.step.show_input = "markdown"
        self.step.input = ""
        self.step.output = ""
        self.step.default_open = True  # 初始展开，让用户直接看到思考过程
        await self.step.send()
        print(f"[ReasoningStep] created: parent_id={self.step.parent_id} default_open={self.step.default_open}")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            await self.close()
        except Exception as close_err:
            if exc_type is None:
                raise close_err
            # 如果已有异常在传播，不要用它替换原始异常；
            # 可以打印一条简短日志帮助排查，但不破坏原始 traceback。
            print(f"[ReasoningStep] close failed during exception handling: {close_err}")

    async def stage(self, title: str, detail: str = ""):
        """在父 step output 中追加一个业务化阶段标题，避免创建嵌套子 stage 造成重复渲染。"""
        if self._closed:
            return
        if self.step is None:
            return

        header = f"\n\n**{title}**"
        if detail:
            header += f"\n{detail}"
        header += "\n"
        self._buffer += header
        self.step.output = self._buffer
        await self.step.update()

    async def append(self, text: str):
        if self._closed or self.step is None or not text:
            return

        # 所有 token 都必须落到父 step output，Chainlit 的 CoT 视图以父 step output 为展示主体
        self._buffer += text
        if self._step_supports_stream:
            await self.step.stream_token(text)
        self.step.output = self._buffer
        if not self._step_supports_stream:
            await self.step.update()

    async def line(self, text: str):
        await self.append(text + "\n")

    async def close(self):
        if self.step is None or self._closed:
            return
        self._closed = True
        self.step.output = self._buffer or "思考完成"
        # 旧版本 Chainlit 不支持 auto_collapse，通过 default_open=False 回退折叠
        if not self._step_accepts_auto_collapse:
            self.step.default_open = False
        await self.step.update()


async def _maybe_close_reasoning(reasoning: ReasoningStep | None):
    """在发送最终答案前关闭思考步骤，确保思考先折叠再展示答案。"""
    if reasoning is not None and not reasoning._closed:
        await reasoning.close()


async def _safe_remove_chainlit_element(element) -> None:
    """安全移除 Chainlit UI 元素，忽略不存在或已移除时的异常。"""
    if element is None:
        return
    try:
        await element.remove()
    except Exception:
        pass


def _compress_messages(messages: list, max_tool_len: int = 500, max_ai_len: int = 1500):
    """
    原地压缩 messages 中过长的历史内容，防止多轮对话上下文膨胀。
    - 最近一轮（最后1个HumanMessage及其之后的ToolMessage/AIMessage）保持完整
    - 更早的历史消息按阈值截断
    """
    if not messages:
        return

    # 找到最后一个 HumanMessage 的位置
    last_human_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            last_human_idx = i
            break

    for i, msg in enumerate(messages):
        # 最近一轮保留完整
        if last_human_idx >= 0 and i >= last_human_idx:
            continue

        content = getattr(msg, "content", None)
        if isinstance(content, str) and content:
            if isinstance(msg, ToolMessage) and len(content) > max_tool_len:
                msg.content = content[:max_tool_len] + "\n...(因历史过长已截断)"
            elif isinstance(msg, AIMessage) and len(content) > max_ai_len:
                msg.content = content[:max_ai_len] + "\n...(因历史过长已截断)"


def _clean_table_cell(text) -> str:
    """清理 Markdown 表格单元格中的换行、管道符、HTML 标签等会破坏表格的字符。"""
    if text is None:
        return ""
    text = str(text)
    # 统一处理各类换行与 HTML 换行标签
    text = text.replace("\r", " ").replace("\n", " ")
    text = text.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
    # 移除其他 HTML 标签
    text = re.sub(r"<[^>]+>", "", text)
    # 管道符会破坏 Markdown 表格，替换为全角竖线
    text = text.replace("|", "｜")
    # 压缩多余空格
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _find_tool(tools, tool_name: str):
    return next((t for t in tools if t.name == tool_name), None)


def _find_rainfall_tool(tools):
    """优先使用本地降雨分析工具，回退到 MCP 同名工具。"""
    return _find_tool(tools, "local_analyze_rainfall_by_time") or _find_tool(tools, "analyze_rainfall_by_time")


def _extract_emergency_response_time(user_text: str) -> tuple[bool, str]:
    """
    从用户问题中提取应急响应判定所需的时间参数，返回 (是否匹配, times)。
    只有同时包含“应急响应”相关意图 + 可选时间词时才匹配，避免普通天气查询误触发。
    支持：
      - 2023年7月30日 / 2023-07-30 → YYYYMMDD080000
      - 今天 / 昨天 / 前天 → 相对日期 08 时
      - 现在/当前/目前 → 当前时刻 YYYYMMDDHHMMSS
      - 未识别到时间但包含应急响应关键词 → 当前时刻
    """
    t = (user_text or "").strip()
    if not t:
        return False, ""

    now = datetime.now()

    # 必须有明确的应急响应意图关键词
    emergency_keywords = ("防汛应急响应", "应急响应", "启动响应", "是否启动", "应急等级", "响应等级")
    has_emergency_kw = any(k in t for k in emergency_keywords)
    if not has_emergency_kw:
        return False, ""

    # 完整日期时间：2023年7月30日14时 / 2023-07-30 14:00 / 2023073014
    m = re.search(r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})[日\s]*(\d{1,2})?\s*[时:]?", t)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hour = int(m.group(4)) if m.group(4) else 8
        dt = datetime(year, month, day, hour, 0, 0)
        return True, dt.strftime("%Y%m%d%H%M%S")

    # 相对日期
    if "前天" in t:
        dt = now - timedelta(days=2)
        return True, dt.strftime("%Y%m%d") + "080000"
    if "昨天" in t:
        dt = now - timedelta(days=1)
        return True, dt.strftime("%Y%m%d") + "080000"
    if "今天" in t:
        return True, now.strftime("%Y%m%d") + "080000"

    # 现在/当前/目前
    if any(k in t for k in ("现在", "当前", "目前", "此刻")):
        return True, now.strftime("%Y%m%d%H%M%S")

    # 只有应急响应关键词，无明确时间 → 默认当前时刻
    return True, now.strftime("%Y%m%d%H%M%S")

class DecisionWeatherQAService:
    """点位决策天气问答：LLM 抽槽，代码定位点位、匹配代表站并查询滚动预报。"""

    def __init__(self, answer_chain, tools, callbacks):
        self.answer_chain = answer_chain
        self.tools = tools
        self.callbacks = callbacks
        self.status_msg = None

    async def try_handle(self, user_text: str, messages: list, reasoning: ReasoningStep | None = None) -> bool:
        if not user_text or not _decision_weather_prefilter(user_text):
            return False
        poi_tool = _find_tool(self.tools, "search_poi")
        forecast_tool = _find_tool(self.tools, "query_rolling_forecast")
        if not poi_tool or not forecast_tool:
            return False

        status_msg = cl.Message(content="🔎 正在分析问题，请稍候...")
        self.status_msg = status_msg
        await status_msg.send()

        try:
            slots = await self._extract_slots(user_text)
        except Exception as exc:
            print(f"[DecisionWeather] LLM 抽取失败：{exc}")
            await status_msg.remove()
            return False

        if not bool(slots.get("is_decision_weather")):
            await status_msg.remove()
            return False

        status_msg.content = "✅ 已识别为点位天气问题，正在校验时间和位置..."
        await status_msg.update()

        async with cl.Step(name="点位天气查询进度", type="tool") as step:
            step.show_input = "markdown"
            step.input = user_text
            step.output = "✅ 已识别为点位天气问题，正在校验时间和位置...\n"
            await step.update()

            print(f"[DecisionWeather] LLM slots: {json.dumps(slots, ensure_ascii=False)}")

            if bool(slots.get("need_clarification")):
                question = str(slots.get("clarification_question") or "请补充具体位置和查询时段。").strip()
                await status_msg.remove()
                await _maybe_close_reasoning(reasoning)
                await cl.Message(content=question).send()
                messages.append(HumanMessage(content=user_text))
                messages.append(AIMessage(content=question))
                cl.user_session.set("messages", messages)
                return True

            hourly_request = _decision_hourly_window(user_text, slots.get("question_type"), datetime.now())
            normalized = self._normalize_slots(slots, hourly_request)
            if normalized.get("error"):
                await status_msg.remove()
                await _maybe_close_reasoning(reasoning)
                await cl.Message(content=normalized["error"]).send()
                messages.append(HumanMessage(content=user_text))
                messages.append(AIMessage(content=normalized["error"]))
                cl.user_session.set("messages", messages)
                return True

            location_name = normalized["location_name"]
            target_start = normalized["target_start"]
            target_end = normalized["target_end"]
            interval = normalized["interval"]
            fcst_time = _select_decision_fcst_time()
            start_period, end_period = _decision_period_args(fcst_time, target_start, target_end)

            print(
                "[DecisionWeather] normalized time: "
                f"target_start={target_start}, target_end={target_end}, "
                f"interval={interval}, fcst_time={fcst_time}, "
                f"startPeriod={start_period}, endPeriod={end_period}"
            )

            await self._update_step(step, status_msg, f"📍 正在查询位置：{location_name} ...")
            if reasoning:
                await reasoning.stage("📍 定位点位", f"正在查询位置：{location_name} ...")
            poi_raw = await _invoke_tool_for_fast_path(
                poi_tool.name, poi_tool, {"keyword": location_name, "size": 5}, user_text
            )
            poi_payload = _unwrap_tool_result(poi_raw)
            poi = _decision_pick_first_poi(poi_payload if isinstance(poi_payload, dict) else {})
            if not poi:
                text = f"未检索到“{_clean_table_cell(location_name)}”的可用经纬度信息，请换一个更明确的位置名称。"
                await status_msg.remove()
                await _maybe_close_reasoning(reasoning)
                await cl.Message(content=text).send()
                messages.append(HumanMessage(content=user_text))
                messages.append(AIMessage(content=text))
                cl.user_session.set("messages", messages)
                return True

            poi_lon = float(poi["longitude"])
            poi_lat = float(poi["latitude"])
            nearest = _nearest_decision_station(poi_lon, poi_lat)
            point_name = str(poi.get("name") or location_name)
            poi_address = str(poi.get("address") or "")

            print(
                "[DecisionWeather] POI定位: "
                f"name={point_name}, address={poi_address}, lon={poi_lon}, lat={poi_lat}; "
                f"nearest_region={nearest['region']}, nearest_lon={nearest['lon']}, "
                f"nearest_lat={nearest['lat']}, distance_km={nearest['distance_km']:.2f}"
            )

            await self._update_step(
                step,
                status_msg,
                f"🧭 已定位到 {point_name}，正在匹配滚动预报代表区域..."
            )

            forecast_args = {
                "user_query": user_text,
                "regions": "",
                "lon": nearest["lon"],
                "lat": nearest["lat"],
                "point_name": f"{point_name}附近（{nearest['region']}代表点）",
                "matched_region": nearest["region"],
                "fcst_time": fcst_time,
                "start_period": start_period,
                "end_period": end_period,
                "interval": interval,
            }
            print(f"[DecisionWeather] query_rolling_forecast args: {json.dumps(forecast_args, ensure_ascii=False)}")

            await self._update_step(step, status_msg, "🛰️ 正在调用滚动预报数据...")
            if reasoning:
                await reasoning.stage("🛰️ 查询滚动预报", "正在调用滚动预报数据...")
            forecast_raw = await _invoke_tool_for_fast_path(
                forecast_tool.name, forecast_tool, forecast_args, user_text
            )
            forecast_payload = _unwrap_tool_result(forecast_raw)
            if not isinstance(forecast_payload, dict) or forecast_payload.get("api_code") not in (None, "200", 200):
                print(f"[DecisionWeather] forecast raw payload: {forecast_payload}")

            facts = _compact_decision_forecast_facts(
                forecast_payload if isinstance(forecast_payload, dict) else {},
                target_start,
                target_end,
                hourly_request,
            )
            facts["poi"] = {
                "name": point_name,
                "address": poi_address,
                "lon": poi_lon,
                "lat": poi_lat,
            }
            facts["matched_station"] = nearest
            facts["question_type"] = slots.get("question_type") or "general_weather"
            if hourly_request:
                facts["question_type"] = hourly_request["mode"]

            await self._update_step(step, status_msg, "✍️ 数据已返回，正在生成面向用户的回答...")
            final_text = await self._generate_answer(user_text, facts)
            final_text = _sanitize_display_text(
                self.callbacks["append_followup_if_needed"](final_text or "", user_text)
            )

            await status_msg.remove()
            await _maybe_close_reasoning(reasoning)
            final_text = _prepend_thinking_summary(final_text, user_text, has_chart=False)
            await self.callbacks["stream_text_to_message"](final_text)
            messages.append(HumanMessage(content=user_text))
            messages.append(AIMessage(content=final_text))
            cl.user_session.set("messages", messages)
            await self._update_step(step, None, "✅ 点位天气回答已完成。")
            return True

    async def _update_step(self, step, status_msg, text: str):
        print(f"[DecisionWeather] {text}")
        if status_msg is not None:
            status_msg.content = text
            await status_msg.update()
        step.output = (step.output or "") + text + "\n"
        await step.update()

    async def _extract_slots(self, user_text: str) -> dict:
        return await _extract_decision_weather_slots(user_text, self.answer_chain, self.callbacks)

    def _normalize_slots(self, slots: dict, hourly_request: dict | None = None) -> dict:
        return _normalize_decision_weather_slots(slots, hourly_request)

    async def _generate_answer(self, user_text: str, facts: dict) -> str:
        return await _generate_decision_weather_answer(user_text, facts, self.answer_chain, self.callbacks)


async def _try_decision_weather_fast_path(user_text: str, thinking_chain, answer_chain, tools, messages, callbacks) -> bool:
    # 先过前置过滤器，不匹配就直接跳过，避免创建无关的 reasoning 块
    if not _decision_weather_prefilter(user_text):
        return False

    service = DecisionWeatherQAService(answer_chain=answer_chain, tools=tools, callbacks=callbacks)
    intent = "查询具体点位决策天气"
    data_sources = ["点位天气预报数据"]
    reasoning = await _show_business_reasoning(
        intent,
        data_sources,
        "将给出该点位的天气影响评估",
    )
    await generate_fast_path_thinking(
        thinking_chain, user_text, intent, data_sources, reasoning
    )
    handled = False
    exc_occurred = False
    try:
        handled = await service.try_handle(user_text, messages, reasoning=reasoning)
        return handled
    except Exception as exc:
        exc_occurred = True
        print(f"[DecisionWeather] fast path 失败，回退通用流程：{exc}")
        traceback.print_exc()
        await _safe_remove_chainlit_element(service.status_msg)
        # 异常时保留 reasoning 内容，让用户看到失败前的思考过程
        await reasoning.line(f"\n\n（点位天气查询遇到异常：{str(exc)[:100]}，回退通用流程...）")
        await reasoning.close()
        return False
    finally:
        if not exc_occurred:
            if handled:
                await reasoning.close()
            else:
                # 没有真正处理当前问题，把已经创建的思考步骤移除，避免答非所问
                await _safe_remove_chainlit_element(reasoning.step)


WARNING_TOOL_NAMES = {
    "get_effective_warning_info",
    "get_history_warning_info",
    "get_today_warning_summary",
    "get_national_warning_info",
}

EMERGENCY_RESPONSE_TOOL_NAMES = {
    "safe_evaluate_haihe_emergency_response",
    "evaluate_haihe_forecast_emergency_response",
}


def _save_to_history(user_text: str, assistant_text: str, messages: list):
    """追加用户问题与助手回复到对话历史。"""
    messages.append(HumanMessage(content=user_text))
    messages.append(AIMessage(content=assistant_text))
    cl.user_session.set("messages", messages)


def _compact_warning_record_for_table(item) -> dict:
    """预警记录只保留本轮输出所需字段。"""
    if not isinstance(item, dict):
        return {
            "content": str(item),
            "eventType": "",
            "department": "",
            "msgType": "",
            "time": "",
            "severity": "",
            "locationName": "",
        }
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    province = str(item.get("province") or raw.get("province") or "").strip()
    city = str(item.get("city") or raw.get("city") or "").strip()
    county = str(item.get("county") or raw.get("county") or "").strip()
    national_area = "".join(part for part in [province, city, county] if part)
    return {
        "content": str(item.get("content") or raw.get("content") or ""),
        "eventType": str(item.get("eventType") or item.get("event_type") or raw.get("eventType") or ""),
        "department": str(item.get("department") or raw.get("department") or item.get("source") or raw.get("source") or ""),
        "msgType": str(item.get("msgType") or item.get("msg_type") or raw.get("msgType") or ""),
        "time": str(item.get("time") or item.get("publish_time") or raw.get("time") or ""),
        "severity": str(item.get("severity") or raw.get("severity") or ""),
        "locationName": str(item.get("locationName") or item.get("location_name") or raw.get("locationName") or national_area),
    }


def _warning_records_from_payload(tool_name: str, payload) -> list[dict]:
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
        source = "中央气象台"
        return [
            _compact_warning_record_for_table({**item, "department": item.get("department") or source})
            if isinstance(item, dict)
            else _compact_warning_record_for_table(item)
            for item in items
        ]
    return [_compact_warning_record_for_table(item) for item in items]


def _warning_table_title(tool_name: str, multi_tool: bool = False) -> str:
    if multi_tool:
        return "【相关预警清单】"
    if tool_name == "get_national_warning_info":
        return "【国家预警清单】"
    if tool_name == "get_today_warning_summary":
        return "【今日发布预警清单】"
    if tool_name == "get_history_warning_info":
        return "【历史预警清单】"
    return "【生效预警清单】"


def _warning_department_area(department: str) -> str:
    dept = (department or "").strip()
    if not dept:
        return ""
    area = re.sub(r"(气象台|气象局|预警发布中心|发布中心|台)$", "", dept).strip()
    if area == "天津市":
        return "天津市"
    if "海洋中心" in area:
        return "天津海域"
    return area


def _extract_warning_area(record: dict) -> str:
    location_name = str(record.get("locationName") or "").strip()
    if location_name:
        return location_name
    dept_area = _warning_department_area(str(record.get("department") or ""))
    return dept_area or "暂未明确"


def _warning_query_event_keywords(user_text: str) -> list[str]:
    t = user_text or ""
    keywords = []
    if any(k in t for k in ["暴雨", "大暴雨", "强降雨", "短时强降水"]):
        keywords.append("暴雨")
    if "海上大风" in t:
        keywords.append("海上大风")
    elif any(k in t for k in ["雷雨大风", "雷暴大风", "大风"]):
        keywords.append("雷雨大风")
    if any(k in t for k in ["冰雹", "雹"]):
        keywords.append("冰雹")
    if "高温" in t:
        keywords.append("高温")
    if any(k in t for k in ["雷电", "雷雨"]):
        keywords.append("雷")
    if "寒潮" in t:
        keywords.append("寒潮")
    if any(k in t for k in ["大雾", "低能见度"]):
        keywords.append("大雾")
    if any(k in t for k in ["道路结冰", "结冰"]):
        keywords.append("道路结冰")
    if "霾" in t:
        keywords.append("霾")
    if any(k in t for k in ["地质灾害", "山洪"]):
        keywords.extend(["地质灾害", "山洪"])
    return list(dict.fromkeys(keywords))


def _warning_record_matches_events(record: dict, event_keywords: list[str]) -> bool:
    if not event_keywords:
        return True
    event = str(record.get("eventType") or "")
    return any(keyword in event for keyword in event_keywords)



def _warning_query_severities(user_text: str) -> list[str]:
    return [severity for severity in ["红色", "橙色", "黄色", "蓝色"] if severity in (user_text or "")]


def _warning_record_matches_area(record: dict, area_keywords: list[str]) -> bool:
    if not area_keywords:
        return True
    area = _extract_warning_area(record)
    department = str(record.get("department") or "")
    return any(keyword in area or keyword in department for keyword in area_keywords)


def _warning_query_area_keywords(records: list[dict], user_text: str) -> list[str]:
    t = user_text or ""
    broad_terms = {"天津", "天津市", "我市", "全市", "本市"}
    area_keywords = []
    for record in records:
        area = _extract_warning_area(record)
        if area and area not in broad_terms and area in t:
            area_keywords.append(area)
    return list(dict.fromkeys(area_keywords))


def _filter_warning_records_for_user(records: list[dict], user_text: str) -> list[dict]:
    """让代码表格和 LLM 正文使用同一批、同一顺序的预警记录。"""
    if not records:
        return []

    filtered = list(records)

    event_keywords = _warning_query_event_keywords(user_text)
    if event_keywords:
        filtered = [record for record in filtered if _warning_record_matches_events(record, event_keywords)]

    severities = _warning_query_severities(user_text)
    if severities:
        filtered = [
            record for record in filtered
            if any(severity in str(record.get("severity") or "") for severity in severities)
        ]

    area_keywords = _warning_query_area_keywords(filtered, user_text)
    if area_keywords:
        filtered = [record for record in filtered if _warning_record_matches_area(record, area_keywords)]

    t = user_text or ""
    asks_released_list = any(k in t for k in ["已解除预警", "解除预警有哪些", "解除的预警"])
    asks_release_judgement = any(k in t for k in ["解除了吗", "是否解除", "何时解除", "什么时候解除", "到什么时候"])
    if asks_released_list and not asks_release_judgement:
        filtered = [
            record for record in filtered
            if "解除" in str(record.get("msgType") or "")
        ]

    return filtered


def _build_warning_table_markdown(records: list[dict], title: str) -> str:
    if not records:
        return f"{title}\n\n未检索到符合条件的预警记录。"
    lines = [
        f"{title}\n\n",
        "| 序号 | 发布单位 | 预警类型 | 等级 | 影响区域 | 发布时间 | 发布状态 |\n",
        "| :---: | :--- | :--- | :--- | :--- | :--- | :--- |\n",
    ]
    for idx, record in enumerate(records, 1):
        lines.append(
            "| "
            f"{idx} | "
            f"{_clean_table_cell(record.get('department') or '—')} | "
            f"{_clean_table_cell(record.get('eventType') or '—')} | "
            f"{_clean_table_cell(record.get('severity') or '—')} | "
            f"{_clean_table_cell(record.get('locationName') or _extract_warning_area(record) or '暂未明确')} | "
            f"{_clean_table_cell(record.get('time') or '—')} | "
            f"{_clean_table_cell(record.get('msgType') or '—')} |\n"
        )
    return "".join(lines).strip()


def _build_warning_bundle(tool_name: str, observation) -> dict:
    records = _warning_records_from_payload(tool_name, observation)
    for record in records:
        if isinstance(record, dict):
            record["_source_tool"] = tool_name
    return {
        "tool_name": tool_name,
        "records": records,
        "title": _warning_table_title(tool_name),
    }


def _merge_warning_bundles(bundles: list[dict]) -> dict:
    records = []
    tool_names = []
    for bundle in bundles:
        if not isinstance(bundle, dict):
            continue
        tool_name = str(bundle.get("tool_name") or "")
        if tool_name:
            tool_names.append(tool_name)
        records.extend(bundle.get("records") or [])
    title = _warning_table_title(tool_names[0], multi_tool=len(set(tool_names)) > 1) if tool_names else "【预警清单】"
    return {"records": records, "title": title}


def _build_warning_llm_messages(records: list[dict], user_text: str) -> list:
    content_lines = []
    for idx, record in enumerate(records, 1):
        content = str(record.get("content") or "").strip()
        if content:
            content_lines.append(f"{idx}. {content}")
    contents_text = "\n".join(content_lines) if content_lines else "无预警正文。"
    instruction = (
        "请仅根据下面按顺序给出的预警正文 content 回答用户问题。\n"
        "除编号外，下面不提供其他结构化字段；不要自行编造发布单位、等级、时间、数量或区域。\n"
        "请只生成以下模块：`【核心结论】`、`【预警内容】`、`【防范建议】`。\n"
        "如果没有预警正文，只输出`【核心结论】`，说明未检索到符合条件的预警记录。\n"
        "不要生成或复述预警清单表格；预警清单将由代码生成。\n"
        "【预警内容】中的条目顺序必须与下面 content 编号顺序一致，不得重排。\n\n"
        f"用户问题：{user_text}\n\n"
        "预警正文 content：\n"
        f"{contents_text}"
    )
    return [HumanMessage(content=instruction)]


def _remove_llm_warning_table_sections(text: str) -> str:
    table_heads = "生效预警清单|今日发布预警清单|历史预警清单|相关预警清单|预警清单"
    return re.sub(
        rf"\n*【(?:{table_heads})】.*?(?=\n*【(?:预警内容|防范建议)】|\Z)",
        "\n",
        text,
        flags=re.DOTALL,
    ).strip()


def _assemble_warning_hybrid_answer(llm_text: str, table_text: str) -> str:
    cleaned = _remove_llm_warning_table_sections(_sanitize_display_text(llm_text or ""))
    if not table_text:
        return cleaned

    match = re.search(r"(【核心结论】.*?)(?=\n*【(?:预警内容|防范建议)】|\Z)", cleaned, flags=re.DOTALL)
    if not match:
        return f"{table_text}\n\n{cleaned}".strip()

    core = match.group(1).strip()
    rest = (cleaned[:match.start()] + cleaned[match.end():]).strip()
    if rest:
        return f"{core}\n\n{table_text}\n\n{rest}".strip()
    return f"{core}\n\n{table_text}".strip()


def _warning_record_is_released(record: dict) -> bool:
    msg_type = str(record.get("msgType") or "")
    content = str(record.get("content") or "")
    return "解除" in msg_type or "解除" in content


def _warning_record_label(record: dict) -> str:
    event = str(record.get("eventType") or "预警").strip() or "预警"
    severity = str(record.get("severity") or "").strip()
    if not severity or severity in event:
        return event
    if "预警" in event:
        return f"{event}{severity}"
    return f"{event}{severity}预警"


def _warning_key_phrases(records: list[dict]) -> tuple[list[str], list[str], list[str]]:
    labels = []
    areas = []
    times = []
    for record in records:
        label = _warning_record_label(record)
        if label:
            labels.append(label)
        area = str(record.get("locationName") or _extract_warning_area(record) or "").strip()
        if area and area != "暂未明确":
            areas.append(area)
        t = str(record.get("time") or "").strip()
        if t:
            times.append(t)
    return list(dict.fromkeys(labels)), list(dict.fromkeys(areas)), list(dict.fromkeys(times))


def _build_warning_core_conclusion(records: list[dict], user_text: str, title: str) -> str:
    if not records:
        return "【核心结论】\n未检索到符合条件的预警记录。"

    labels, areas, times = _warning_key_phrases(records)
    active_count = sum(1 for r in records if not _warning_record_is_released(r))
    released_count = len(records) - active_count
    label_text = "、".join(labels[:5]) if labels else "预警信息"
    area_text = "，涉及" + "、".join(areas[:6]) if areas else ""
    time_text = f"，最新发布时间为{times[0]}" if times else ""

    if "今日" in title or "今天" in user_text or "今日" in user_text:
        detail = f"今日检索到 **{len(records)}条** 相关预警动态"
        if released_count:
            detail += f"，其中 **{active_count}条** 未解除、**{released_count}条** 已解除"
        return f"【核心结论】\n{detail}，主要包括 **{label_text}**{area_text}{time_text}。"

    if any(k in user_text for k in ["解除了吗", "是否解除", "已解除", "解除预警"]):
        if active_count:
            return f"【核心结论】\n当前仍检索到 **{active_count}条** 未解除的相关预警，主要包括 **{label_text}**{area_text}{time_text}。"
        return f"【核心结论】\n检索到的 **{len(records)}条** 相关预警均为已解除或解除类记录，主要包括 **{label_text}**{area_text}{time_text}。"

    if active_count:
        return f"【核心结论】\n当前检索到 **{active_count}条** 正在生效或仍需关注的相关预警，主要包括 **{label_text}**{area_text}{time_text}。"
    return f"【核心结论】\n当前未检索到仍在生效的相关预警；本次返回的 **{len(records)}条** 记录主要为已解除或历史预警，涉及 **{label_text}**{area_text}{time_text}。"


def _extract_warning_advice(records: list[dict]) -> list[str]:
    advice = []
    for record in records:
        if _warning_record_is_released(record):
            continue
        content = str(record.get("content") or "").strip()
        if not content:
            continue
        parts = re.split(r"[。；;！!？?]\s*", content)
        for part in parts:
            p = part.strip()
            if not p:
                continue
            if any(k in p for k in ["请", "注意", "加强", "避免", "防范", "转移", "做好", "减少", "远离"]):
                if not p.endswith("。"):
                    p += "。"
                advice.append(p)
    return list(dict.fromkeys(advice))[:5]


def _build_warning_code_answer(warning_bundles: list[dict], user_text: str) -> str:
    merged = _merge_warning_bundles(warning_bundles)
    records = _filter_warning_records_for_user(merged["records"], user_text)
    core_text = _build_warning_core_conclusion(records, user_text, merged["title"])
    if not records:
        return core_text

    table_text = _build_warning_table_markdown(records, merged["title"])
    content_lines = [
        f"{idx}. {_sanitize_display_text(str(record.get('content') or '').strip())}"
        for idx, record in enumerate(records, 1)
        if str(record.get("content") or "").strip()
    ]

    sections = [core_text, table_text]
    if content_lines:
        sections.append("【预警内容】\n" + "\n".join(content_lines))

    advice = _extract_warning_advice(records)
    if advice:
        sections.append("【防范建议】\n" + "\n".join(f"{idx}. {item}" for idx, item in enumerate(advice, 1)))

    return "\n\n".join(section for section in sections if section).strip()


async def _generate_warning_hybrid_answer(answer_chain, warning_bundles: list[dict], user_text: str, callbacks) -> str:
    merged = _merge_warning_bundles(warning_bundles)
    records = _filter_warning_records_for_user(merged["records"], user_text)
    if not records:
        llm_text = await _generate_warning_core_and_advice(answer_chain, [], user_text, callbacks)
        return _assemble_warning_final_answer(llm_text=llm_text, table_text="", content_text="")

    table_text = _build_warning_table_markdown(records, merged["title"])
    content_text = _build_warning_content_section(records)
    llm_text = await _generate_warning_core_and_advice(answer_chain, records, user_text, callbacks)
    return _assemble_warning_final_answer(llm_text=llm_text, table_text=table_text, content_text=content_text)


def _is_warning_fact_query(user_text: str) -> bool:
    text = user_text or ""
    return "预警" in text



def _normalize_warning_route(route: dict) -> dict:
    allowed = {
        "get_effective_warning_info",
        "get_history_warning_info",
        "get_today_warning_summary",
        "get_national_warning_info",
    }
    names = route.get("tool_names") if isinstance(route, dict) else None
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list):
        names = []
    tool_names = [str(name).strip() for name in names if str(name).strip() in allowed]
    if not tool_names:
        tool_names = ["get_effective_warning_info"]
    national_keywords = str((route or {}).get("national_keywords") or "天津").strip()
    return {
        "tool_names": list(dict.fromkeys(tool_names)),
        "national_keywords": national_keywords,
        "reason": str((route or {}).get("reason") or "").strip(),
    }


def _fill_warning_prompt(template: str, **values) -> str:
    prompt = template or ""
    for key, value in values.items():
        prompt = prompt.replace("{" + key + "}", str(value))
    return prompt


async def _route_warning_tools(answer_chain, user_text: str, callbacks) -> dict:
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt_template = WARNING_ROUTE_PROMPT or ""
    if not prompt_template.strip():
        prompt_template = (
            "请根据用户问题选择预警接口，只输出JSON："
            '{"tool_names":["get_effective_warning_info"],"national_keywords":"天津","reason":""}\n'
            "当前时间：{current_time}\n用户问题：{user_query}"
        )
    prompt = _fill_warning_prompt(prompt_template, current_time=current_time, user_query=user_text)
    result = await callbacks["ainvoke_chain"](answer_chain, {"messages": [HumanMessage(content=prompt)]})
    content = getattr(result, "content", None) or str(result)
    route = _extract_first_json_object(content)
    normalized = _normalize_warning_route(route)
    if "get_national_warning_info" in normalized["tool_names"]:
        normalized["national_keywords"] = _infer_national_warning_keywords(
            user_text,
            normalized.get("national_keywords"),
        )
    print(f"[WarningFastPath] route={json.dumps(normalized, ensure_ascii=False)} raw={content}")
    return normalized


def _infer_national_warning_keywords(user_text: str, model_keywords: str | None = None) -> str:
    text = user_text or ""
    if "全国" in text:
        return ""
    if "华北" in text:
        return "北京,天津,河北,山西,内蒙古"
    if "京津冀" in text:
        return "北京,天津,河北"
    explicit_parts = []
    for name in ("北京", "北京市", "河北", "河北省", "天津", "天津市"):
        if name in text:
            explicit_parts.append(name.replace("北京市", "北京").replace("河北省", "河北").replace("天津市", "天津"))
    if explicit_parts:
        return ",".join(dict.fromkeys(explicit_parts))
    if any(keyword in text for keyword in ("周边", "邻近", "附近省市", "周边地区", "周边省市")):
        return "北京,河北"
    if any(keyword in text for keyword in ("国家局", "中央气象台", "国家中央气象台", "国家气象中心", "中央台")):
        return "天津"
    cleaned = str(model_keywords or "").strip()
    return cleaned or "天津"


def _warning_tool_args(tool_name: str, route: dict) -> dict:
    if tool_name == "get_national_warning_info":
        keywords = route.get("national_keywords")
        return {
            "keywords": "" if keywords == "" else (keywords or "天津"),
            "max_items": 30,
        }
    return {}


def _build_warning_content_section(records: list[dict]) -> str:
    content_lines = [
        f"{idx}. {_sanitize_display_text(str(record.get('content') or '').strip())}"
        for idx, record in enumerate(records, 1)
        if str(record.get("content") or "").strip()
    ]
    if not content_lines:
        return ""
    return "【预警内容】\n" + "\n".join(content_lines)


def _warning_contents_for_llm(records: list[dict]) -> str:
    lines = []
    for idx, record in enumerate(records, 1):
        content = str(record.get("content") or "").strip()
        if not content:
            continue
        meta = "；".join(
            part for part in [
                f"预警类型：{record.get('eventType')}" if record.get("eventType") else "",
                f"等级：{record.get('severity')}" if record.get("severity") else "",
                f"发布单位：{record.get('department')}" if record.get("department") else "",
                f"影响区域：{record.get('locationName')}" if record.get("locationName") else "",
                f"发布时间：{record.get('time')}" if record.get("time") else "",
                f"状态：{record.get('msgType')}" if record.get("msgType") else "",
                f"数据类别：{record.get('_source_tool')}" if record.get("_source_tool") else "",
            ]
            if part
        )
        lines.append(f"{idx}. {meta}\ncontent：{content}")
    return "\n\n".join(lines) if lines else "无预警正文。"


async def _generate_warning_core_and_advice(
    answer_chain,
    records: list[dict],
    user_text: str,
    callbacks,
) -> str:
    contents_text = _warning_contents_for_llm(records)
    prompt_template = WARNING_SUMMARY_PROMPT or ""
    if not prompt_template.strip():
        prompt_template = (
            "请仅依据预警正文生成【核心结论】和【防范建议】两个模块，不要输出表格和预警清单。\n"
            "用户问题：{user_query}\n预警正文 content：\n{contents_text}"
        )
    prompt = _fill_warning_prompt(prompt_template, user_query=user_text, contents_text=contents_text)
    result = await callbacks["ainvoke_chain"](answer_chain, {"messages": [HumanMessage(content=prompt)]})
    text = getattr(result, "content", None) or str(result)
    text = _sanitize_display_text(text)
    text = _remove_llm_warning_table_sections(text)
    return text.strip()


def _assemble_warning_final_answer(llm_text: str, table_text: str, content_text: str) -> str:
    cleaned = _sanitize_display_text(llm_text or "").strip()
    core_match = re.search(r"(【核心结论】.*?)(?=\n*【防范建议】|\Z)", cleaned, flags=re.DOTALL)
    advice_match = re.search(r"(【防范建议】.*)\Z", cleaned, flags=re.DOTALL)
    core = core_match.group(1).strip() if core_match else (cleaned or "【核心结论】\n已获取预警信息。")
    advice = advice_match.group(1).strip() if advice_match else ""
    sections = [core, table_text, content_text, advice]
    return "\n\n".join(section for section in sections if section).strip()


async def _try_warning_fact_fast_path(user_text: str, thinking_chain, answer_chain, tools, messages, callbacks) -> bool:
    if not _is_warning_fact_query(user_text):
        return False

    reasoning = await _show_business_reasoning(
        "查询天津气象预警信息",
        ["预警数据"],
        "将整理预警清单、核心结论与防范建议",
    )
    await reasoning.stage("📡 查询数据", "正在判断预警接口...")
    await generate_fast_path_thinking(
        thinking_chain, user_text, "查询天津气象预警信息", ["预警数据"], reasoning
    )
    bundles = []
    try:
        route = await _route_warning_tools(answer_chain, user_text, callbacks)
        tool_names = route["tool_names"]
        selected_tools = [(name, _find_tool(tools, name)) for name in tool_names]
        selected_tools = [(name, tool) for name, tool in selected_tools if tool is not None]
        if not selected_tools:
            return False

        display_names = "、".join(TOOL_DISPLAY_NAMES.get(name, name) for name, _ in selected_tools)
        await reasoning.stage("📡 查询数据", f"正在调用{display_names}...")

        async with cl.Step(name="预警信息查询", type="tool") as step:
            step.show_input = False
            step.output = f"🔎 已选择接口：{display_names}\n"
            await step.update()

            for name, tool in selected_tools:
                args = _warning_tool_args(name, route)
                print(f"[WarningFastPath] 调用 {name} 参数: {json.dumps(args, ensure_ascii=False)}")
                step.output += f"📡 正在调用{TOOL_DISPLAY_NAMES.get(name, name)}...\n"
                await step.update()
                result = await asyncio.wait_for(
                    _invoke_tool_for_fast_path(name, tool, args, user_text), timeout=30
                )
                bundles.append(_build_warning_bundle(name, result))
                step.output += f"✅ {TOOL_DISPLAY_NAMES.get(name, name)}查询完成。\n"
                await step.update()

        await reasoning.stage("📡 查询数据", "正在生成回答...")
        final_text = await _generate_warning_hybrid_answer(answer_chain, bundles, user_text, callbacks)
        final_text = _sanitize_display_text(callbacks["append_followup_if_needed"](final_text or "", user_text))
        final_text = _prepend_thinking_summary(final_text, user_text, has_chart=False)
        await _maybe_close_reasoning(reasoning)
        await callbacks["stream_text_to_message"](final_text)
        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content=final_text))
        cl.user_session.set("messages", messages)
        return True
    except asyncio.TimeoutError:
        return await _handle_fast_path_error("预警信息", messages, user_text, reasoning=reasoning)
    except Exception as exc:
        print(f"[WarningFastPath] 失败，回退通用流程：{exc}")
        traceback.print_exc()
        return False
    finally:
        await reasoning.close()


# 内部数据模式：IP地址、端口、凭据片段等不应出现在用户可见文本中
_INTERNAL_DATA_PATTERNS = [
    (re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d{2,5})?\b'), '[内部地址]'),
    (re.compile(r'sk-[a-zA-Z0-9]{20,}'), '[已隐藏]'),
    (re.compile(r'(api[_-]?key|api[_-]?secret|password|token)\s*[:=]\s*\S+', re.IGNORECASE), r'\1=[已隐藏]'),
]


def _scrub_internal_data(text: str) -> str:
    """移除或替换可能泄露内部基础设施信息的字符串。"""
    if not isinstance(text, str):
        return text
    for pattern, replacement in _INTERNAL_DATA_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _sanitize_display_text(text: str) -> str:
    """
    清理可能泄露给业务用户的工具调用标记。
    移除 XML 工具调用、JSON tool_calls、以及函数/参数标签残留。
    """
    if not isinstance(text, str):
        return text
    # XML/标签 形式 tool_calls 泄露
    text = re.sub(r"\s*<tool_call\s*>.*?</\s*tool_call\s*>", "", text, flags=re.DOTALL)
    text = re.sub(r"\s*<tool_code\s*>.*?</\s*tool_code\s*>", "", text, flags=re.DOTALL)
    text = re.sub(r"\s*<function\s*=\s*[^>]+>.*?</\s*function\s*>", "", text, flags=re.DOTALL)
    text = re.sub(r"\s*<parameter\s*=\s*[^>]+>.*?</\s*parameter\s*>", "", text, flags=re.DOTALL)
    # 可能残留的裸 <function=...> / <parameter=...> 单行
    text = re.sub(r"\s*<function\s*=\s*[^>]+>\s*", " ", text)
    text = re.sub(r"\s*</\s*function\s*>\s*", " ", text)
    text = re.sub(r"\s*<parameter\s*=\s*[^>]+>\s*", " ", text)
    text = re.sub(r"\s*</\s*parameter\s*>\s*", " ", text)
    # JSON 形式 tool_calls 泄露（LangChain 标准格式）
    text = re.sub(r"\[\s*\{\s*\"id\"\s*:\s*\"[^\"]+\"\s*,\s*\"name\"\s*:\s*\"[^\"]+\".*?\}\s*\]", "", text, flags=re.DOTALL)
    # JSON 形式 tool_calls 泄露（模型自定义 tool/params / tool_code/parameters 格式）
    text = re.sub(r"json\s*\[\s*\{.*?\"tool\"\s*:\s*\"[^\"]+\".*?\"params\"\s*:\s*\{.*?\}.*?\}\s*\]", "", text, flags=re.DOTALL)
    text = re.sub(r"json\s*\{.*?\"tool_code\"\s*:\s*\"[^\"]+\".*?\"parameters\"\s*:\s*\{.*?\}.*?\}", "", text, flags=re.DOTALL)

    # Markdown 格式修正（模型输出常见缺陷）
    # 先处理 HTML 换行标签，避免它们破坏表格或正文格式
    text = text.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
    # 0. emoji 后紧跟中文时插入换行，避免标题和正文粘在一起
    text = re.sub(r"([\U0001F300-\U0001F9FF\U00002600-\U000027BF])([^\s\n])", r"\1\n\2", text)
    # 0a. 加粗标记被换行拆开：**标题\n**正文 -> **标题**\n正文
    # 避免误伤两个独立加粗标题（如 **标题1**\n**标题2**：内容）
    text = re.sub(r"\*\*([^\*\n]+)\n\*\*(?![^\n]*?\*\*：)", r"**\1**\n", text)
    # 0b. 将错误的星号标题块转换为普通加粗或删除多余星号：****降雨预报详情 -> **降雨预报详情**
    text = re.sub(r"\*{3,}\s*([^\*\n]+?)\s*\*{0,}", r"**\1**", text)
    # 0c. "核心结论""详情"等短标签后紧跟文字时加换行（避开被 ** 包裹的加粗标题）
    text = re.sub(r"(核心结论|预报详情|降雨详情|数据详情)([^\s\n：:*\]】)）])", r"\1\n\2", text)
    # 0d. 修复括号内被换行拆开的情况：标题（\n内容） -> 标题（内容）
    text = re.sub(r"([（(])\n([^\n]*[）)])", r"\1\2", text)
    # 1. 标题 # 后缺少空格：##标题 -> ## 标题（行首及标点/空格后）
    text = re.sub(r"^(#{2,6})([^#\s])", lambda m: f"{m.group(1)} {m.group(2)}", text, flags=re.MULTILINE)
    text = re.sub(r"([。：；！？\s])(#{2,6})([^#\s])", r"\1\2 \3", text)
    # 1b. 标题紧跟正文时换行：...如下：### 标题 -> ...如下：\n\n### 标题
    text = re.sub(r"([^\n#])(#{2,6}\s)", r"\1\n\n\2", text)
    # 2. 标题行过长且与正文粘在一起时拆分：## 标题截至... -> ## 标题\n\n截至...
    # 避开括号内的截至/根据等词，避免把"标题（截至...）"拆断
    text = re.sub(r"^(#{2,6}\s+.{3,30}?)(?<![（(])((?:截至|根据|目前|当前)\d)", r"\1\n\n\2", text, flags=re.MULTILINE)
    # 3. 正文末尾直接接表格时插入空行（。...|表头|）
    # 负向前瞻避免破坏表格内部单元格分隔（如 (mm)| ）
    text = re.sub(r"([。：；！？)）\s])\|(?![\s|])", r"\1\n\n|", text)
    # 3b. 标题/短文本后紧跟表格起始时插入空行
    text = re.sub(r"(#{2,6}\s+[^\n]{1,40})(\|[^\n]*\|)", r"\1\n\n\2", text)
    # 3c. 无标点分隔时，正文直接粘到表格也换行（只匹配行首的表格起始，避免破坏表格内部单元格或行）
    text = re.sub(r"([^|\n])(\n\|[^|\n]*\|[^|\n]*\|)", r"\1\n\n\2", text)
    # 4. 修复表格行被粘在一起的情况：| A | B || C | D | -> | A | B |\n| C | D |
    # 仅在检测到 "||" 这种明显粘行时才处理，避免破坏已格式化的表格
    if "||" in text:
        _concat_row_pattern = re.compile(r"(\|(?:[^\n|]*?\|)+?)(\|(?:[^\n|]*\|)+)(?!\n)")
        _changed = True
        while _changed:
            _changed = False
            def _split_table_rows(m: re.Match) -> str:
                row1, row2 = m.group(1), m.group(2)
                if row1.count("|") == row2.count("|"):
                    return f"{row1}\n{row2}"
                return m.group(0)
            _new_text = _concat_row_pattern.sub(_split_table_rows, text)
            if _new_text != text:
                text = _new_text
                _changed = True
    # 5. 表格后紧跟非表格文本时插入空行（避免数据来源粘到表格）
    text = re.sub(r"(\|[^\n]*\|\n)([^\n|])", r"\1\n\2", text)
    # 6. 表格最后一行末尾直接跟"数据来源/说明/数据解读/注"等，插入空行
    text = re.sub(r"(\|[^\n|]*\|)(数据来源[:：]|说明[:：]|数据解读|注[:：])", r"\1\n\n\2", text)
    # 6b. 正文中的"数据来源："前也换行
    text = re.sub(r"([^\n])(数据来源[:：])", r"\1\n\n\2", text)
    # 6c. 独立成行的常见小标题自动加粗
    text = re.sub(r"^((?:核心结论|详细情况|气象建议|数据解读))$", r"**\1**", text, flags=re.MULTILINE)

    # 压缩多余空行
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _extract_xml_tool_calls(content: str) -> list[dict]:
    """
    兼容 Qwen 等模型输出的 XML 格式工具调用，例如：
      <tool_call>
      <function=get_city_rainfall_time_range>
        <parameter=city_name>沧州市</parameter>
      </function>
      </tool_call>
    将其解析为 LangChain 的 tool_calls 列表。
    """
    if not isinstance(content, str) or "<tool_call" not in content:
        return []

    calls = []
    call_id = 0

    # 1. 匹配 <tool_call>...</tool_call> 块
    for block_match in re.finditer(r"\s*<tool_call\s*>(.*?)\s*</\s*tool_call\s*>", content, re.DOTALL):
        block = block_match.group(1).strip()
        if not block:
            continue

        # 2. 匹配 <function=tool_name>...</function>
        func_match = re.match(r"\s*<function\s*=\s*([^\s>]+)\s*>(.*?)\s*</\s*function\s*>", block, re.DOTALL)
        if not func_match:
            # 也兼容 <function><name>...</name>...</function>
            func_match = re.match(r"\s*<function\s*>(.*?)\s*</\s*function\s*>", block, re.DOTALL)
            if not func_match:
                continue
            inner = func_match.group(1).strip()
            name_match = re.search(r"<name\s*>([^<]+)</\s*name\s*>", inner)
            args_match = re.search(r"<arguments\s*>(.*?)</\s*arguments\s*>", inner, re.DOTALL)
            tool_name = name_match.group(1).strip() if name_match else ""
            args_text = args_match.group(1).strip() if args_match else "{}"
        else:
            tool_name = func_match.group(1).strip()
            inner = func_match.group(2).strip()
            args_text = ""

        if not tool_name:
            continue

        # 3. 解析参数
        args = {}
        if args_text:
            # 尝试 JSON 参数
            try:
                args = json.loads(args_text)
            except Exception:
                pass
        else:
            # 解析 <parameter=key>value</parameter>
            for param_match in re.finditer(r"\s*<parameter\s*=\s*([^\s>]+)\s*>(.*?)\s*</\s*parameter\s*>", inner, re.DOTALL):
                key = param_match.group(1).strip()
                val = param_match.group(2).strip()
                # 尝试数字/bool 转换
                if re.fullmatch(r"-?\d+", val):
                    val = int(val)
                elif re.fullmatch(r"-?\d+\.\d+", val):
                    val = float(val)
                elif val.lower() == "true":
                    val = True
                elif val.lower() == "false":
                    val = False
                args[key] = val

        call_id += 1
        calls.append({
            "id": f"xml_tool_call_{call_id}",
            "name": tool_name,
            "args": args,
            "type": "tool_call",
        })

    return calls


def _extract_json_tool_calls(content: str) -> list[dict]:
    """
    兼容模型输出的 JSON 数组/对象格式工具调用，例如：
      json[{"tool":"get_city_rainfall_time_range","params":{"city":"...","start_time":"..."}}]
      [{"tool":"...","params":{...}}]
      <tool_code>json{"tool_code":"analyze_rainfall_by_time","parameters":{"time_range_type":"current"}}</tool_code>
    将其解析为 LangChain 的 tool_calls 列表。
    """
    if not isinstance(content, str):
        return []

    calls = []
    call_id = 0

    # 1. 匹配 <tool_code>json{...}</tool_code> 或 <tool_code>json[...]</tool_code>
    for tc_match in re.finditer(r"<tool_code\s*>(.*?)<\s*/\s*tool_code\s*>", content, flags=re.DOTALL):
        inner = tc_match.group(1).strip()
        # 去掉可选的 json 前缀
        if inner.lower().startswith("json"):
            inner = inner[4:].strip()
        try:
            payload = json.loads(inner)
        except Exception:
            continue
        if isinstance(payload, dict):
            payload = [payload]
        elif not isinstance(payload, list):
            continue
        for item in payload:
            if not isinstance(item, dict):
                continue
            tool_name = item.get("tool_code") or item.get("tool") or item.get("name")
            params = item.get("parameters") or item.get("params") or item.get("args") or item.get("arguments") or {}
            if not isinstance(tool_name, str) or not tool_name:
                continue
            if not isinstance(params, dict):
                params = {}
            call_id += 1
            calls.append({
                "id": f"json_tool_call_{call_id}",
                "name": tool_name,
                "args": params,
                "type": "tool_call",
            })

    if calls:
        return calls

    # 2. 匹配 json[...] 前缀或裸 [...] 数组，内部为含 tool/params 的对象
    for match in re.finditer(r"(?:json\s*)?(\[\s*\{.*?\}\s*\])", content, flags=re.DOTALL):
        array_text = match.group(1)
        try:
            items = json.loads(array_text)
        except Exception:
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            tool_name = item.get("tool") or item.get("name")
            params = item.get("params") or item.get("args") or item.get("arguments") or {}
            if not isinstance(tool_name, str) or not tool_name:
                continue
            if not isinstance(params, dict):
                params = {}
            call_id += 1
            calls.append({
                "id": f"json_tool_call_{call_id}",
                "name": tool_name,
                "args": params,
                "type": "tool_call",
            })

    return calls


def _normalize_tool_call(tc, index: int) -> dict | None:
    """把可能是 dict、Pydantic ToolCall 或其他可序列化对象的 tool_call 统一转成 dict。"""
    raw = tc
    if not isinstance(tc, dict):
        if hasattr(tc, "model_dump"):
            raw = tc.model_dump()
        elif hasattr(tc, "__dict__"):
            raw = tc.__dict__
        else:
            print(f"[工具调用解析] 跳过不可识别的 tool_call: {tc}")
            return None

    name = raw.get("name") or ""
    args = raw.get("args")
    tid = raw.get("id")

    if not isinstance(tid, str) or not tid:
        tid = f"tool_call_{index}_{str(uuid.uuid4())[:8]}"
    if not isinstance(name, str):
        name = str(name)
    if not isinstance(args, dict):
        if hasattr(args, "model_dump"):
            args = args.model_dump()
        elif hasattr(args, "__dict__"):
            args = args.__dict__
        else:
            args = {}
    if not name.strip():
        print(f"[工具调用解析] 忽略无 name 的 tool_call: {raw}")
        return None

    return {
        "id": tid,
        "name": name,
        "args": args,
        "type": raw.get("type", "tool_call"),
    }


def _set_tool_calls(msg, calls: list[dict]) -> None:
    """安全地设置消息对象的 tool_calls 属性，兼容不同 LangChain 消息实现。"""
    if hasattr(msg, "tool_calls") and msg.tool_calls is not None:
        msg.tool_calls = calls
    else:
        object.__setattr__(msg, "tool_calls", calls)


def _clean_tool_calls_from_content(content: str, is_json: bool) -> str:
    """从 content 中移除已提取的工具调用文本，避免把原始调用文本展示给用户。"""
    if is_json:
        cleaned = re.sub(r"<tool_code\s*>.*?</\s*tool_code\s*>", "", content, flags=re.DOTALL).strip()
        return re.sub(r"json\s*\[\s*\{.*?\}\s*\]", "", cleaned, flags=re.DOTALL).strip()
    return re.sub(r"\s*<tool_call\s*>.*?</\s*tool_call\s*>", "", content, flags=re.DOTALL).strip()


def _ensure_tool_calls_from_content(planner_msg):
    """
    若模型把工具调用以 XML 或 JSON 形式写在 content 里（而非标准 tool_calls），
    解析出来并填到 planner_msg.tool_calls；同时规范化已有 tool_calls，
    补全缺失的 id、过滤无 name 的无效调用，避免后续 ToolMessage/Pydantic 校验失败。
    """
    try:
        existing = getattr(planner_msg, "tool_calls", None) or []

        if not existing:
            content = getattr(planner_msg, "content", None) or ""
            extracted_calls = _extract_xml_tool_calls(content)
            is_json = False
            if not extracted_calls:
                extracted_calls = _extract_json_tool_calls(content)
                is_json = bool(extracted_calls)

            if extracted_calls:
                print(f"[工具调用解析] 从 {'JSON' if is_json else 'XML'} 中提取到 {len(extracted_calls)} 个工具调用：{extracted_calls}")
                _set_tool_calls(planner_msg, extracted_calls)
                planner_msg.content = _clean_tool_calls_from_content(content, is_json) or ""
                print(f"[工具调用解析] 清理后 content：{planner_msg.content!r}")
                existing = planner_msg.tool_calls

        normalized = [_normalize_tool_call(tc, i) for i, tc in enumerate(existing)]
        normalized = [tc for tc in normalized if tc is not None]
        if normalized != existing:
            print(f"[工具调用解析] 规范化后 tool_calls: {normalized}")
            _set_tool_calls(planner_msg, normalized)
    except Exception as e:
        print(f"[工具调用解析] 失败，跳过：{e}")
        traceback.print_exc()
    return planner_msg


def _tool_call_names(planner_msg) -> set[str]:
    """Extract tool-call names from a planner message, tolerating dicts and objects."""
    calls = getattr(planner_msg, "tool_calls", None) or []
    names: set[str] = set()
    for tc in calls:
        if isinstance(tc, dict):
            name = tc.get("name")
        else:
            name = getattr(tc, "name", None)
        if name:
            names.add(str(name))
    return names


def _friendly_llm_error_text(err: Exception) -> str:
    t = str(err).strip()
    lower_t = t.lower()
    err_type = type(err).__name__
    if err_type in ("TimeoutError", "CancelledError") or "timeout" in lower_t:
        return "⏱️ 大模型响应超时，请稍后重试。如果多次超时，可能是模型服务繁忙或网络不稳定。"
    if not t:
        return f"❌ 大模型调用失败：{err_type}（无详细错误信息），请查看控制台日志。"
    if "arrearage" in lower_t or "overdue-payment" in lower_t:
        return "❌ 当前大模型服务不可用：账户欠费或已停用（Arrearage）。请先在阿里云百炼控制台完成续费/结清后重试。"
    if "access denied" in lower_t or "api_key" in lower_t or "unauthorized" in lower_t:
        return "❌ 当前大模型服务鉴权失败。请检查 API Key 是否正确、是否过期以及对应模型权限。"
    return f"❌ 大模型调用失败：{err_type}: {t}"


def _nearest_valid_hour(hour_value: int) -> int:
    valid_hours = [2, 8, 14, 20]
    return min(valid_hours, key=lambda h: abs(h - hour_value))


def _build_hour_tolerant_args(tool_args):
    if not isinstance(tool_args, dict):
        return None, None, None

    candidate_keys = []
    for key, value in tool_args.items():
        if not isinstance(key, str):
            continue
        if key == "hour" or key.endswith("_hour"):
            if isinstance(value, int):
                candidate_keys.append((key, value))
            elif isinstance(value, str) and value.strip().isdigit():
                candidate_keys.append((key, int(value.strip())))

    if not candidate_keys:
        return None, None, None

    # 优先修正最常见的 hour 参数
    key, old_hour = sorted(candidate_keys, key=lambda kv: (0 if kv[0] == "hour" else 1, kv[0]))[0]
    new_hour = _nearest_valid_hour(old_hour)
    if new_hour == old_hour:
        return None, None, None

    new_args = dict(tool_args)
    new_args[key] = new_hour
    return new_args, old_hour, new_hour


async def _invoke_tool_with_tolerance(tool_name: str, tool, tool_args, step, user_text: str = "") -> tuple[Any, float]:
    session_id = cl.user_session.get("id") or ""
    query_summary = TimingLogger._safe_summary(user_text) if user_text else ""

    start_time = time.time()
    try:
        result = await tool.ainvoke(tool_args)
        elapsed = time.time() - start_time
        print(f"[工具耗时] {tool_name}: {elapsed:.2f}s")
        TimingLogger.log_tool(session_id, query_summary, tool_name, elapsed, status="ok")
        return result, elapsed
    except Exception as e:
        elapsed = time.time() - start_time
        print(f"[工具耗时] {tool_name}: {elapsed:.2f}s (失败)")
        TimingLogger.log_tool(session_id, query_summary, tool_name, elapsed, status="fail")
        err_text = str(e)
        if tool_name != "get_city_rainfall_time_range" or "hour%6==2" not in err_text:
            raise

        retry_args, old_hour, new_hour = _build_hour_tolerant_args(tool_args)
        if not retry_args:
            raise

        step.input = (step.input or "") + (
            f"⚠️ 检测到小时参数不合法：{old_hour}，"
            f"已自动纠偏为 {new_hour} 并重试。\n"
        )
        print(f"[容错重试] {tool_name}: hour {old_hour} -> {new_hour}")
        retry_start = time.time()
        try:
            result = await tool.ainvoke(retry_args)
            retry_elapsed = time.time() - retry_start
            print(f"[工具耗时] {tool_name}(重试): {retry_elapsed:.2f}s")
            TimingLogger.log_tool(session_id, query_summary, f"{tool_name}(retry)", retry_elapsed, status="ok")
            return result, retry_elapsed
        except Exception:
            retry_elapsed = time.time() - retry_start
            print(f"[工具耗时] {tool_name}(重试): {retry_elapsed:.2f}s (失败)")
            TimingLogger.log_tool(session_id, query_summary, f"{tool_name}(retry)", retry_elapsed, status="fail")
            raise


async def _render_river_plot_with_overlay(tools, river_observation, river_name: str, callbacks, user_text: str = ""):
    admin_observation = await callbacks["build_admin_overlay_for_plot"](tools, river_observation, user_text)
    await callbacks["render_and_send_plot"](
        river_observation,
        title_suffix=river_name,
        admin_raw_result=admin_observation,
    )


async def _show_business_reasoning(intent_text: str, data_sources: list[str],
                                   conclusion_hint: str) -> ReasoningStep:
    """为 fast path 创建一段业务化的思考过程，包含理解问题、查询数据、生成结论三个阶段。"""
    reasoning = ReasoningStep("🤔 思考过程")
    await reasoning.__aenter__()
    await reasoning.stage("🔍 理解问题", f"识别到您的问题意图：{intent_text}")
    await reasoning.stage("📡 查询数据", "将查询以下数据：" + "、".join(data_sources))
    await reasoning.stage("✍️ 生成结论", conclusion_hint)
    return reasoning


async def generate_fast_path_thinking(
    thinking_chain,
    user_text: str,
    intent_text: str,
    data_sources: list[str],
    reasoning: ReasoningStep,
) -> None:
    """为 fast path 流式生成并追加自然语言深度思考。"""
    prompt = FAST_PATH_THINKING_PROMPT.format(
        current_time=datetime.now().strftime("%Y-%m-%d %H:%M"),
        user_query=user_text,
        intent=intent_text,
        data_sources="、".join(data_sources),
    )
    print(f"[THINKING_FAST] streaming thinking for intent='{intent_text}'")

    async def _stream():
        async for chunk in thinking_chain.astream({
            "system_message": prompt,
            "messages": [HumanMessage(content=user_text)],
        }):
            token = getattr(chunk, "content", None)
            if token:
                await reasoning.append(token)

    try:
        await asyncio.wait_for(_stream(), timeout=30)
    except (asyncio.TimeoutError, TimeoutError):
        await reasoning.line("\n\n（思考生成超时，继续为您查询数据...）")
        print(f"[THINKING_FAST] timeout for '{user_text[:30]}...'")
    except Exception as e:
        await reasoning.line(f"\n\n（思考生成遇到异常：{str(e)[:100]}，继续为您查询数据...）")
        print(f"[THINKING_FAST] error for '{user_text[:30]}...': {e}")


def _build_thinking_summary(query: str, has_chart: bool = False) -> str:
    """根据用户问题生成一句业务化前缀，放在最终回答开头。"""
    if not query:
        return ""

    q = query.strip().lower()

    if any(k in q for k in ["降雨分布图", "降水实况图", "面雨量分布图", "实况图", "降雨图", "雨区", "落区"]):
        base = "已生成海河流域降水实况分布图，说明如下："
    elif any(k in q for k in ["预警", "警报"]):
        base = "已查询相关气象预警信息，整理结论如下："
    elif any(k in q for k in ["河网", "水系", "河流", "暴雨影响", "河系"]):
        base = "已绘制河网可视化并叠加行政区划底图，分析如下："
    elif any(k in q for k in ["水位", "水文"]):
        base = "已查询河网水位数据，整理如下："
    elif any(k in q for k in ["应急响应", "防汛"]):
        base = "已查询防汛应急响应信息，结论如下："
    elif any(k in q for k in ["面雨量", "子流域"]):
        base = "已查询各子流域面雨量数据，分析如下："
    elif any(k in q for k in ["平均降雨", "全市", "城市"]):
        base = "已查询城市平均降雨数据，整理如下："
    elif any(k in q for k in ["降雨时长", "累计雨时", "雨时"]):
        base = "已统计降雨时长信息，整理如下："
    elif any(k in q for k in ["雨情", "降雨分析"]):
        base = "已完成降雨分析，说明如下："
    elif any(k in q for k in ["未来", "预报", "明天", "后天", "周末"]):
        base = "已结合预报数据完成分析，为您整理结论如下："
    elif any(k in q for k in ["今天", "今日", "实况", "现在", "当前", "刚才"]):
        base = "已结合实况观测数据完成分析，为您整理结论如下："
    else:
        base = "已理解您的问题，为您解答如下："

    if has_chart:
        base = base.replace("，说明如下：", "，并生成相关图表，说明如下：")
        base = base.replace("，分析如下：", "，并生成相关图表，分析如下：")
        base = base.replace("，整理如下：", "，并生成相关图表，整理如下：")
        base = base.replace("，结论如下：", "，并生成相关图表，结论如下：")
        base = base.replace("，为您解答如下：", "，并生成相关图表，为您解答如下：")
        base = base.replace("，为您整理结论如下：", "，并生成相关图表，为您整理结论如下：")

    return base


def _prepend_thinking_summary(text: str, query: str, has_chart: bool = False) -> str:
    summary = _build_thinking_summary(query, has_chart=has_chart)
    return summary + "\n\n" + text if summary else text


async def _invoke_tool_for_fast_path(tool_name: str, tool, tool_args, user_text: str):
    """Fast path 中统一调用工具并记录 [TOOL_TIMING]。"""
    session_id = cl.user_session.get("id") or ""
    start = time.time()
    try:
        result = await tool.ainvoke(tool_args)
        elapsed = time.time() - start
        TimingLogger.log_tool(session_id, user_text, tool_name, elapsed, status="ok")
        return result
    except Exception as e:
        elapsed = time.time() - start
        TimingLogger.log_tool(session_id, user_text, tool_name, elapsed, status="fail")
        raise


async def _emit_fast_path_result(
    text: str,
    messages: list,
    user_text: str,
    images: list = None,
    append_followup: bool = True,
    has_chart: bool = False,
    reasoning: ReasoningStep | None = None,
):
    """统一 fast path 结果出口：先折叠思考步骤，再发送最终结果，追加到对话历史。
    Fast path 自己生成规范文本，不再经过 _sanitize_display_text，避免表格等格式被误修复。"""
    if has_chart:
        cl.user_session.set("has_chart_generated", True)
    await _maybe_close_reasoning(reasoning)
    final_text = _prepend_thinking_summary(text, user_text, has_chart=has_chart)
    if images:
        await cl.Message(content=final_text, elements=images).send()
    else:
        await cl.Message(content=final_text).send()
    _save_to_history(user_text, final_text, messages)


def _log_query_exit(query_start_time: float, session_id: str, query_summary: str, status: str = "ok"):
    if cl.user_session.get("query_timing_logged"):
        return
    try:
        total_elapsed = time.time() - query_start_time
        TimingLogger.log_query(session_id, query_summary, total_elapsed, status=status)
    except Exception:
        pass
    finally:
        cl.user_session.set("query_timing_logged", True)


async def _handle_fast_path_error(
    tag: str,
    messages: list,
    user_text: str,
    exc: Exception | None = None,
    reasoning: ReasoningStep | None = None,
) -> bool:
    """统一 fast path 错误出口：timeout 时提醒用户并记录历史，一般异常打印回溯。返回 True 表示已处理。"""
    session_id = cl.user_session.get("id") or ""
    query_summary = user_text or ""
    query_start_time = cl.user_session.get("query_start_time")
    await _maybe_close_reasoning(reasoning)
    if exc is None:
        print(f"[{tag}] 查询超时")
        text = f"⏱️ {tag}查询超时，请稍后重试。"
        await cl.Message(content=text).send()
        if query_start_time:
            _log_query_exit(query_start_time, session_id, query_summary, "fail")
        _save_to_history(user_text, text, messages)
        return True
    print(f"[{tag}] 失败：{exc}")
    traceback.print_exc()
    return False


async def _try_river_plot_fast_path(user_text: str, thinking_chain, tools, messages, callbacks, reasoning: ReasoningStep | None = None) -> bool:
    if not callbacks["need_river_plot"](user_text):
        return False

    if reasoning is None:
        reasoning = await _show_business_reasoning(
            "绘制河网可视化图",
            ["河网水系数据", "行政区划底图数据"],
            "将绘制河网图并叠加行政区划底图",
        )
        await generate_fast_path_thinking(
            thinking_chain, user_text, "绘制河网可视化图", ["河网水系数据", "行政区划底图数据"], reasoning
        )

    await reasoning.stage("📡 查询数据", "正在绘制河网可视化图，请稍候...")
    try:
        river_tool = _find_tool(tools, "get_river_network_for_plot")
        if not river_tool:
            return False

        river_name = callbacks["extract_river_name"](user_text)
        river_observation = await _invoke_tool_for_fast_path(
            river_tool.name, river_tool, {"start_river": river_name}, user_text
        )
        await _render_river_plot_with_overlay(tools, river_observation, river_name, callbacks, user_text)

        brief = callbacks["build_river_network_brief"](river_observation, river_name)
        brief = callbacks["append_followup_if_needed"](brief, user_text)
        await _emit_fast_path_result(brief, messages, user_text, has_chart=True, reasoning=reasoning)
        return True
    except Exception as e:
        print(f"河网快路径失败，回退到通用流程：{e}")
        return False
    finally:
        if reasoning is not None:
            await reasoning.close()


def _need_affected_river_network_by_rainfall(user_text: str) -> bool:
    """识别用户是否在问暴雨影响的河系/河流（需出专题图）"""
    if not user_text:
        return False
    text = user_text.strip()
    # 核心意图：暴雨影响/波及/涉及
    core_keywords = ["暴雨影响", "暴雨会影", "暴雨波及", "暴雨涉及", "暴雨下"]
    if not any(k in text for k in core_keywords):
        return False
    # 目标对象必须是河系、河流、河网、水系、河道等
    river_keywords = ["河系", "河流", "河网", "水系", "河道", "哪些河"]
    return any(k in text for k in river_keywords)


def _build_affected_river_network_brief(result_data: dict, user_text: str) -> str:
    """基于暴雨影响河系工具结果生成业务化简报"""
    time_range = result_data.get("time_range_readable", "")
    threshold = result_data.get("rainfall_threshold_mm", 50.0)
    affected_rivers = result_data.get("affected_rivers", []) or []
    affected_zones = result_data.get("affected_zone_77_regions", []) or []
    affected_admins = result_data.get("affected_admin_divisions", []) or []
    total_segments = result_data.get("total_segments", 0)
    affected_segments = result_data.get("affected_segments", 0)

    if not affected_rivers:
        return (
            f"统计时段 {time_range} 内，降雨量未达到 {threshold}mm 暴雨阈值，"
            "未识别到受暴雨显著影响的河系。"
        )

    lines = [
        f"统计时段 {time_range} 内，降雨量≥{threshold}mm 的暴雨区域共影响 **{len(affected_rivers)} 条河流**，"
        f"涉及 **{len(affected_zones)} 个 77 分区子流域**、**{len(affected_admins)} 个行政区划**。",
        "",
        "**受影响河系列表（已全部列出）**",
        "",
        "| 序号 | 河流名称 |",
        "| :--- | :--- |",
    ]
    for idx, rname in enumerate(sorted(affected_rivers), 1):
        lines.append(f"| {idx} | {rname} |")

    if affected_zones:
        lines.extend(["", "**涉及 77 分区子流域**", "", ", ".join(sorted(affected_zones))])
    if affected_admins:
        lines.extend(["", "**涉及行政区划**", "", ", ".join(sorted(affected_admins))])

    lines.extend([
        "",
        f"专题图已按受影响河段高亮渲染（共 {affected_segments}/{total_segments} 条河段）。",
    ])
    return "\n".join(lines)


async def _try_affected_river_network_by_rainfall_fast_path(
    user_text: str, thinking_chain, tools, messages, callbacks
) -> bool:
    """暴雨影响河系快速路径：调用聚合工具并高亮渲染河网专题图"""
    if not _need_affected_river_network_by_rainfall(user_text):
        return False

    reasoning = None
    try:
        tool = _find_tool(tools, "get_affected_river_network_by_rainfall")
        if not tool:
            return False

        time_str = _detect_rainfall_time(user_text)
        start_time = ""
        end_time = ""
        if not time_str:
            # 如果用户未指定明确历史时间，默认查最近 24 小时到当前
            now = datetime.now()
            end_time = now.strftime("%Y%m%d%H%M%S")
            start_time = (now - timedelta(hours=24)).strftime("%Y%m%d%H%M%S")
            time_str = end_time

            # 未来时间交给 LLM/预报工具处理，不走实况快速路径
            future_keywords = ["明天", "明日", "后天", "未来", "今后", "接下来"]
            if any(k in user_text for k in future_keywords):
                return False

        reasoning = await _show_business_reasoning(
            "分析暴雨影响河系并绘制专题图",
            ["降雨实况数据", "河网水系数据"],
            "将绘制暴雨影响河系专题图并给出文字分析",
        )
        await reasoning.stage("📡 查询数据", "正在分析暴雨影响河系并绘制专题图...")
        await generate_fast_path_thinking(
            thinking_chain, user_text, "分析暴雨影响河系并绘制专题图", ["降雨实况数据", "河网水系数据"], reasoning
        )

        result = await _invoke_tool_for_fast_path(
            "get_affected_river_network_by_rainfall",
            tool,
            {
                "time_str": time_str,
                "start_time": start_time,
                "end_time": end_time,
                "rainfall_threshold_mm": 50.0,
                "include_background": True,
            },
            user_text,
        )

        # 兼容 MCP 工具返回的 list/text/dict/ToolMessage 多种包装
        result_data = _unwrap_tool_result(result)
        if result_data is None:
            result_data = {}
        if not isinstance(result_data, dict):
            raise ValueError(f"工具返回格式异常：{type(result_data)}")

        affected_rivers = result_data.get("affected_rivers", []) or []
        segments = result_data.get("segments", [])

        # 提取暴雨及以上站点，用于在专题图上叠加显示
        stations = []
        heavy_rain_levels = {"暴雨", "大暴雨", "特大暴雨"}

        # 优先使用后端直接返回的 stations 字段
        raw_stations = result_data.get("stations", [])
        if isinstance(raw_stations, list):
            for s in raw_stations:
                if isinstance(s, dict):
                    level = str(s.get("level", "暴雨")).strip()
                    if level in heavy_rain_levels:
                        stations.append({
                            "lon": s.get("lon"),
                            "lat": s.get("lat"),
                            "rainfall": s.get("rainfall"),
                            "level": level,
                            "name": s.get("name", ""),
                        })

        # 兼容旧格式：从 level_analysis 中提取
        if not stations:
            for group in result_data.get("level_analysis", []) or []:
                level = str(group.get("level", "")).strip()
                if level not in heavy_rain_levels:
                    continue
                for s in group.get("stations", []) or []:
                    if isinstance(s, dict):
                        stations.append({
                            "lon": s.get("lon"),
                            "lat": s.get("lat"),
                            "rainfall": s.get("rainfall"),
                            "level": level,
                            "name": s.get("name", ""),
                        })

        print(f"[暴雨影响河系快路径] 受影响河流={len(affected_rivers)}, 河段={len(segments)}, 暴雨站点={len(stations)}")

        if segments or stations:
            admin_observation = None
            if segments:
                admin_observation = await callbacks["build_admin_overlay_for_plot"](tools, segments, user_text)
            await callbacks["render_and_send_plot"](
                segments,
                title_suffix=result_data.get("time_range_readable", time_str),
                admin_raw_result=admin_observation,
                highlight_rivers=affected_rivers,
                stations=stations,
            )

        brief = _build_affected_river_network_brief(result_data, user_text)
        brief = callbacks["append_followup_if_needed"](brief, user_text)
        await _emit_fast_path_result(brief, messages, user_text, has_chart=True, reasoning=reasoning)
        return True
    except Exception as e:
        print(f"暴雨影响河系快速路径失败，回退到通用流程：{e}")
        return False
    finally:
        if reasoning is not None:
            await reasoning.close()


async def _try_manual_plot_fallback(user_text: str, tools, stream_msg: cl.Message, callbacks, reasoning: ReasoningStep | None = None) -> bool:
    try:
        river_tool = _find_tool(tools, "get_river_network_for_plot")
        if not river_tool:
            return False

        river_name = callbacks["extract_river_name"](user_text)
        river_observation = await _invoke_tool_for_fast_path("get_river_network_for_plot", river_tool, {"start_river": river_name}, user_text)
        await _render_river_plot_with_overlay(tools, river_observation, river_name, callbacks, user_text)

        if stream_msg.content.strip():
            await stream_msg.remove()
            stream_msg = cl.Message(content="")
            await stream_msg.send()

        fallback_text = callbacks["build_river_network_brief"](river_observation, river_name)
        fallback_text = callbacks["append_followup_if_needed"](fallback_text, user_text)
        fallback_text = _prepend_thinking_summary(fallback_text, user_text, has_chart=True)
        await _maybe_close_reasoning(reasoning)
        await callbacks["stream_text_to_message"](fallback_text, stream_msg=stream_msg)
        return True
    except Exception as e:
        print(f"河网图兜底绘制失败：{e}")
        return False


def _detect_rainfall_time(text: str) -> str | None:
    """
    提取时间并与当前时间比较：仅当时刻为过去（含当前）时才返回时间字符串，走实况工具。
    未来时间或无法识别的时间放给LLM走预报工具。
    """
    if not text:
        return None
    t = text.strip()

    # 降雨类或天气类问题都拦截（"天气"不问雨也拦截，因为流域场景默认关注降水）
    rain_keywords = ["降雨", "雨情", "雨量", "降水", "雨势", "雨分析", "雨数据", "雨情况"]
    if not any(k in t for k in rain_keywords) and "雨" not in t:
        return None
    now = datetime.now()

    # 解析相对时间词 → 绝对 datetime
    def parse_relative_time(text: str) -> datetime | None:
        """解析相对时间表述，返回绝对时间；无法解析返回 None"""
        now_date = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # "昨天/前天/前天的" + 可选的 "上午/下午/晚上/早上/白天/夜间"
        prefix_hour_map = {
            "凌晨": 2, "早上": 8, "上午": 8, "白天": 8,
            "中午": 14, "下午": 14, "晚上": 20, "夜间": 20, "今夜": 20,
        }

        # 先找小时词缀（上午/下午/晚上等）
        hour = 8  # 默认早上8点（标准观测时次），避免取 now.hour 导致时间范围飘移
        has_hour_prefix = False
        for kw, h in prefix_hour_map.items():
            if kw in text:
                hour = h
                has_hour_prefix = True
                break

        # 日期偏移
        day_found = None
        if any(k in t for k in ["前天", "前天的"]):
            dt = now_date - timedelta(days=2)
            day_found = "前天"
        elif any(k in t for k in ["昨天", "昨日的", "昨"]):
            dt = now_date  # 返回今日日期，配合 -32h/-8h 窗口刚好覆盖昨日00:00~今日00:00
            day_found = "昨天"
        elif any(k in t for k in ["今天", "今日", "今"]):
            dt = now_date
            day_found = "今天"
        elif any(k in t for k in ["明天", "明日", "明"]):
            dt = now_date + timedelta(days=1)
            day_found = "明天"
        elif any(k in t for k in ["后天"]):
            dt = now_date + timedelta(days=2)
            day_found = "后天"
        else:
            # 没有相对时间词，无法判断
            return None

        # "今天"且没有小时前缀 → 不拦截，放给LLM走预报工具
        if day_found == "今天" and not has_hour_prefix:
            return None

        return dt + timedelta(hours=hour)

    # 尝试解析绝对时间 YYYY年MM月DD日HH点
    patterns = [
        (r"(\d{4})年(\d{1,2})月(\d{1,2})日(\d{1,2})点", lambda m: datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]))),
        (r"(\d{4})年(\d{1,2})月(\d{1,2})日", lambda m: datetime(int(m[1]), int(m[2]), int(m[3]), 8) + timedelta(days=1)),
        (r"(\d{4})-(\d{1,2})-(\d{1,2}) (\d{1,2}):(\d{1,2}):(\d{1,2})", lambda m: datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6]))),
        (r"(\d{4})(\d{2})(\d{2})(\d{2})", lambda m: datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]))),
    ]
    for pat, builder in patterns:
        m = re.search(pat, t)
        if m:
            try:
                dt = builder(m.groups())
                if dt <= now:
                    return dt.strftime("%Y%m%d%H%M%S")
                else:
                    return None  # 未来时间放给LLM
            except Exception:
                continue

    # 尝试相对时间
    dt = parse_relative_time(t)
    if dt:
        if dt <= now:
            return dt.strftime("%Y%m%d%H%M%S")
        else:
            return None  # 未来时间放给LLM

    # 无明确时间 → 不拦截，放给LLM
    return None


def _build_rainfall_time_window(user_text: str, time_str: str) -> tuple[str | None, str | None]:
    """
    根据用户问题与时间参考点，返回显式的 start_time/end_time（YYYYMMDDHHMMSS）。
    目前主要针对“昨天/昨日/昨”补齐完整 00:00~24:00 窗口，避免默认 -32h/-8h 截断。
    """
    if not user_text or not time_str:
        return None, None

    t = user_text.strip()
    try:
        ref = datetime.strptime(time_str, "%Y%m%d%H%M%S")
    except Exception:
        return None, None

    if any(k in t for k in ["昨天", "昨日", "昨"]):
        # 昨日 00:00 ~ 今日 00:00
        start = (ref.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)).strftime("%Y%m%d%H%M%S")
        end = ref.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y%m%d%H%M%S")
        return start, end

    if any(k in t for k in ["前天", "前天的"]):
        # 前天 00:00 ~ 昨天 00:00
        start = (ref.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)).strftime("%Y%m%d%H%M%S")
        end = (ref.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)).strftime("%Y%m%d%H%M%S")
        return start, end

    return None, None


async def _try_rainfall_analysis_fast_path(user_text: str, thinking_chain, tools, messages, callbacks) -> bool:
    """降雨分析快速路径：在 LLM 拒绝之前直接调用降雨分析工具"""
    tool = _find_rainfall_tool(tools)
    if not tool:
        return False

    time_str = _detect_rainfall_time(user_text)

    # 对"最大雨强/雨强最大/最大小时雨强"等明确降雨分析类问题，未识别到时间时默认查最近24小时
    intensity_keywords = ["最大雨强", "雨强最大", "最大小时雨强", "小时雨强最大", "雨强出现在"]
    if not time_str and any(k in user_text for k in intensity_keywords):
        time_str = datetime.now().strftime("%Y%m%d%H%M%S")

    if not time_str:
        return False

    start_time, end_time = _build_rainfall_time_window(user_text, time_str)
    print(f"\n=== 降雨分析快速路径：检测到时间 {time_str}，窗口 {start_time} ~ {end_time} ===")

    # 发送"正在思考"提示
    reasoning = await _show_business_reasoning(
        "分析指定时段降雨特征",
        ["实况降雨站点数据"],
        "将统计降雨分布、极值、持续时间等特征",
    )
    await reasoning.stage("📡 查询数据", "我正在思考，请稍候...")
    await generate_fast_path_thinking(
        thinking_chain, user_text, "分析指定时段降雨特征", ["实况降雨站点数据"], reasoning
    )

    try:
        # 设置超时，防止后端卡死
        invoke_args = {"time_str": time_str}
        if start_time and end_time:
            invoke_args["start_time"] = start_time
            invoke_args["end_time"] = end_time
        result = await asyncio.wait_for(
            _invoke_tool_for_fast_path(tool.name, tool, invoke_args, user_text),
            timeout=30,
        )
        data = _unwrap_tool_result(result)

        # 如果查询含"全市"，将数据过滤为仅天津市站点
        if "全市" in user_text:
            city_name = "天津"
            levels_raw = data.get("level_analysis", [])
            filtered_levels = []
            for lv in levels_raw:
                stations = lv.get("stations", [])
                city_stations = [s for s in stations if city_name in str(s.get("province", "")) or city_name in str(s.get("city", ""))]
                if not city_stations:
                    continue
                admin_set = set()
                for s in city_stations:
                    if s.get("province"):
                        admin_set.add(f"{s.get('province','')} {s.get('city','')} {s.get('cnty','')}")
                # 保留原 level 中的空间分析字段（从原始数据继承）
                lv_copy = dict(lv)
                lv_copy["stations"] = city_stations
                lv_copy["station_count"] = len(city_stations)
                # 空间分析字段暂时沿用原值，简化处理
                lv_copy["admin_divisions"] = sorted(admin_set) if admin_set else lv.get("admin_divisions", [])
                filtered_levels.append(lv_copy)

            if filtered_levels:
                data["level_analysis"] = filtered_levels
                # 重新计算最大值
                all_city_stations = []
                for lv in filtered_levels:
                    all_city_stations.extend(lv["stations"])
                if all_city_stations:
                    by_rain = sorted(all_city_stations, key=lambda s: float(s.get("rainfall", 0)), reverse=True)
                    top_city = by_rain[0]
                    data["max_rainfall"] = float(top_city.get("rainfall", 0))
                    data["max_station"] = top_city
                    data["total_stations"] = len(all_city_stations)
            else:
                data["level_analysis"] = []
                data["total_stations"] = 0

        # 构造格式化输出
        time_str_display = data.get("data_time", time_str)
        time_range_readable = data.get("time_range_readable", "")
        max_rainfall = data.get("max_rainfall", 0)
        max_level = data.get("max_level", "无")
        total_stations = data.get("total_stations", 0)

        if time_range_readable:
            title_line = f"**统计时段**：{time_range_readable}（北京时）　**数据来源**：天擎自动站\n\n"
        else:
            title_line = f"**分析时刻**：{time_str_display}（北京时）　**数据来源**：天擎自动站\n\n"

        msg_parts = [
            f"## 降雨分析结果\n\n",
            title_line,
        ]

        # ===== 1. 最大雨量级别总结 =====
        level_analysis = data.get("level_analysis", [])
        if level_analysis:
            top = level_analysis[0]
            top_level = _clean_table_cell(top.get("level", ""))
            top_count = top.get("station_count", 0)
            top_admins = [_clean_table_cell(a) for a in top.get("admin_divisions", [])]
            top_zones = [_clean_table_cell(z) for z in top.get("zone_77_regions", [])]
            top_rivers = [_clean_table_cell(r) for r in top.get("affected_rivers", [])]

            msg_parts.append(
                f"**最大降雨量 {max_rainfall:.1f}mm，达到「{top_level}」级别**"
                f"（{top_level}级站点共{top_count}个）\n\n"
            )

            msg_parts.append("### 最大雨量级别详情\n\n")
            msg_parts.append(f"| 维度 | 内容 |\n")
            msg_parts.append(f"| :--- | :--- |\n")
            msg_parts.append(
                f"| 降雨等级 | {top_level} |\n"
                f"| 站点数 | {top_count} 个 |\n"
                f"| 涉及行政区划 | {'、'.join(top_admins) if top_admins else '—'} |\n"
                f"| 涉及77分区河系 | {'、'.join(top_zones) if top_zones else '—'} |\n"
                f"| 影响河流 | {'、'.join(top_rivers) if top_rivers else '—'} |\n"
            )

        # 最大站点
        max_station = data.get("max_station")
        if max_station:
            station_name = _clean_table_cell(max_station.get("name", ""))
            station_id = _clean_table_cell(max_station.get("station_id", ""))
            rainfall = max_station.get("rainfall", 0)
            province = _clean_table_cell(max_station.get("province", ""))
            city = _clean_table_cell(max_station.get("city", ""))
            cnty = _clean_table_cell(max_station.get("cnty", ""))
            msg_parts.append(
                f"| 最大雨量站 | {station_name}（{station_id}），"
                f"{province}{city}{cnty}，{rainfall:.1f}mm |\n"
            )

        msg_parts.append("\n")

        # ===== 2. 各等级明细表 =====
        msg_parts.append("### 各降雨等级明细\n\n")
        msg_parts.append("| 降雨等级 | 站点数 | 涉及行政区划 | 涉及77分区河系 | 影响河流 |\n")
        msg_parts.append("| :--- | :--- | :--- | :--- | :--- |\n")

        has_detail = False
        for level_item in level_analysis:
            level = _clean_table_cell(level_item["level"])
            count = level_item["station_count"]
            if count == 0:
                continue
            has_detail = True

            admins = level_item.get("admin_divisions", [])
            zones = level_item.get("zone_77_regions", [])
            rivers = level_item.get("affected_rivers", [])

            admin_text = "、".join(_clean_table_cell(a) for a in admins[:5])
            if len(admins) > 5:
                admin_text += f" 等{len(admins)}个"
            zone_text = "、".join(_clean_table_cell(z) for z in zones[:3])
            if len(zones) > 3:
                zone_text += f" 等{len(zones)}个"
            river_text = "、".join(_clean_table_cell(r) for r in rivers[:3])
            if len(rivers) > 3:
                river_text += f" 等{len(rivers)}条"

            msg_parts.append(f"| {level} | {count} | {admin_text or '—'} | {zone_text or '—'} | {river_text or '—'} |\n")

        if not has_detail:
            msg_parts.append("| — | 0 | — | — | — |\n")

        text = "".join(msg_parts)
        # 确保没有残留 HTML 标签
        text = re.sub(r"<[^>]+>", "", text)
        text = callbacks.get("append_followup_if_needed", lambda t, u: t)(text, user_text)

        text = _prepend_thinking_summary(text, user_text, has_chart=False)
        await _maybe_close_reasoning(reasoning)
        await cl.Message(content=text).send()

        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content=text))
        cl.user_session.set("messages", messages)
        return True

    except asyncio.TimeoutError:
        print(f"降雨分析快速路径超时（30秒）")
        await _maybe_close_reasoning(reasoning)
        await cl.Message(content="⏱️ 降雨数据查询超时，请稍后重试。").send()
        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content="降雨数据查询超时，请稍后重试。"))
        cl.user_session.set("messages", messages)
        return True
    except Exception as e:

        print(f"[降雨分析快速路径] 失败：{e}")
        traceback.print_exc()
        # 返回 False，让主 planner 尝试其他工具/路径回答用户问题
        # （例如用面雨量分布图来展示昨日降雨情况）
        return False
    finally:
        await reasoning.close()


# 工具名 -> 业务可读名称映射，供思考过程和工具步骤展示使用
TOOL_DISPLAY_NAMES = {
    "get_river_network_for_plot": "查询河网数据",
    "get_affected_river_network_by_rainfall": "暴雨影响河道分析",
    "analyze_rainstorm_impact": "暴雨影响分析",
    "analyze_rainfall_by_time": "降雨量时段分析",
    "get_station_rainfall_real_img": "生成降雨实况分布图",
    "get_city_rainfall_time_range": "查询城市降雨时段",
    "query_rolling_forecast": "查询滚动预报",
    "query_basin_areal_rainfall": "查询流域面雨量",
    "get_effective_warning_info": "查询当前生效预警",
    "get_history_warning_info": "查询历史预警信息",
    "get_today_warning_summary": "查询今日预警概况",
    "get_national_warning_info": "查询全国预警信息",
    "search_poi": "搜索关注点位",
    "search_poi_by_distance": "搜索周边点位",
    "rag_search": "知识库检索",
    "get_admin_division_for_plot": "加载行政区划底图",
    "locate_region_rivers": "定位区域河道",
    "estimate_river_impact_time": "估算河道影响时间",
    "get_tianjin_wind_warning_assessment": "天津大风预警评估",
    "route_partner_skill": "调度合作单位技能",
    "invoke_partner_skill_alpha_hydro": "调用水文合作单位",
    "invoke_partner_skill_beta_emergency": "调用应急合作单位",
    "invoke_partner_skill_shortterm": "调用短临预报合作单位",
    "local_analyze_rainfall_by_time": "降雨量时段分析",
    "query_decision_weather_for_poi": "查询决策天气点位",
}


def _extract_historical_weather_images(data):
    """从 historical_weather_* 工具返回中提取图片和观测文本。返回 (images, observation_text)。"""
    img_msgs = []

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "image" or "image" in str(item.get("mimeType", "")):
                src = item.get("source", item)
                b64 = src.get("data", src.get("base64", "")) if isinstance(src, dict) else str(src)
                if not b64:
                    continue
                try:
                    if "," in b64:
                        b64 = b64.split(",")[1]
                    img_bytes = base64.b64decode(b64)
                    img_msgs.append(cl.Image(content=img_bytes, name=f"chart_{len(img_msgs)}"))
                except Exception:
                    pass

    if not img_msgs:
        data = _unwrap_tool_result(data)
        if isinstance(data, dict):
            for key, val in data.items():
                if not isinstance(val, str):
                    continue
                if "png" not in val.lower() and "chart" not in key.lower() and "image" not in key.lower():
                    continue
                if val.startswith("data:image") or val.startswith("/9j/"):
                    try:
                        b64 = val.split(",")[1] if "," in val else val
                        img_bytes = base64.b64decode(b64)
                        img_msgs.append(cl.Image(content=img_bytes, name=f"chart_{key}"))
                    except Exception:
                        pass
                elif val.endswith(".png") and os.path.isfile(val):
                    with open(val, "rb") as f:
                        img_msgs.append(cl.Image(content=f.read(), name=f"chart_{key}"))

            if not img_msgs:
                for m in re.finditer(r'[\w\-]+\.png', str(data)):
                    fname = m.group()
                    for base_url in ["http://10.226.107.133:8000",
                                     "http://10.226.107.133:8000/output",
                                     "http://10.226.107.133:8000/files"]:
                        try:
                            resp = httpx.get(f"{base_url}/{fname}", timeout=5)
                            if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image"):
                                img_msgs.append(cl.Image(content=resp.content, name=f"chart_{fname.replace('.','_')}"))
                                break
                        except Exception:
                            continue

    if img_msgs:
        return img_msgs, "（系统消息：历史极端天气图表已生成并展示）"
    if isinstance(data, dict) and data.get("text"):
        return [], data["text"][:2000]
    return [], str(data)[:2000]


async def _run_tool_round(planner_msg, tools, messages, user_text: str, iteration: int, callbacks):
    ree = None
    forced_final_text = None
    warning_bundles = []
    tool_names = [tc['name'] for tc in planner_msg.tool_calls]
    print(f"\n=== 第 {iteration} 轮工具调用 ===")

    round_start = time.time()
    async with cl.Step(name=f"第 {iteration} 轮数据查询（共 {len(planner_msg.tool_calls)} 项）", type="tool") as step:
        step.show_input = False
        # 开发者调试信息保留在控制台，不暴露给业务用户
        print(f"\n=== 准备执行工具 ===")
        print(f"工具列表：{tool_names}")
        print(f"参数：{[tc['args'] for tc in planner_msg.tool_calls]}")
        print(f"====================\n")

        for tool_call in planner_msg.tool_calls:
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]
            tool = _find_tool(tools, tool_name)
            display_name = TOOL_DISPLAY_NAMES.get(tool_name, tool_name)

            async with cl.Step(name=display_name, parent_id=step.id, type="tool") as tool_step:
                tool_step.show_input = False
                print(f"[工具] {tool_name} 参数: {tool_args}")

                if tool is None:
                    observation_text = f"工具未找到：{tool_name}"
                    messages.append(ToolMessage(content=observation_text, tool_call_id=tool_call["id"], role="tool"))
                    tool_step.output = f"❌ {observation_text}"
                    continue
                try:
                    observation, tool_elapsed = await _invoke_tool_with_tolerance(tool_name, tool, tool_args, tool_step, user_text=user_text)
                    if tool_name == "analyze_rainstorm_impact":
                        observation = await callbacks["enrich_with_impact_time_tool"](
                            observation=observation,
                            tool_args=tool_args,
                            tools=tools,
                            step=tool_step,
                        )
                    maybe_send_gis = callbacks.get("send_gis_linkage")
                    if maybe_send_gis:
                        try:
                            ree = await maybe_send_gis(
                                tool_name=tool_name,
                                tool_args=tool_args,
                                observation=observation,
                                user_text=user_text,
                                tools=tools,
                            )


                        except Exception as gis_err:
                            # GIS 联动失败不应中断主问答流程
                            print(f"[GIS联动] 发送失败：{gis_err}")
                    if tool_name == "analyze_rainstorm_impact" and callbacks["should_force_admin_units_reply"](user_text):
                        forced_final_text = callbacks["build_admin_units_only_reply"](observation)
                    elif tool_name == "analyze_rainstorm_impact" and callbacks["should_force_partition_table_reply"](user_text):
                        forced_final_text = callbacks["build_partition_only_reply"](observation)
                    elif tool_name == "analyze_rainstorm_impact" and callbacks["should_force_structured_impact_reply"](user_text):
                        forced_final_text = callbacks["build_structured_impact_reply"](observation)

                    if tool_name in WARNING_TOOL_NAMES:
                        warning_bundles.append(_build_warning_bundle(tool_name, observation))
                        observation_text = (
                            "预警数据已进入专用组装流程：预警清单表格由代码根据 "
                            "eventType、department、time、severity、locationName 生成；"
                            "预警内容由 content 组装，核心结论和防范建议由大模型基于 content 生成。"
                        )
                    elif tool_name == "get_river_network_for_plot":
                        river_name = tool_args.get("start_river", "全流域")
                        try:
                            await _render_river_plot_with_overlay(tools, observation, river_name, callbacks, user_text)
                        except Exception as e:
                            print(f"加载行政区划底图失败：{e}")
                            await callbacks["render_and_send_plot"](observation, title_suffix=river_name, admin_raw_result=None)

                        cl.user_session.set("has_chart_generated", True)
                        observation_text = (
                            f"（系统消息：已成功在前端为用户绘制了 {river_name} 的"
                            f"河网可视化图，并叠加行政区划底图。不要输出坐标数据，请继续用自然语言回答分析结果）"
                        )
                    elif tool_name == "get_station_rainfall_real_img":
                        data = _unwrap_tool_result(observation)
                        if isinstance(data, dict) and "base64" in data:
                            b64_str = data["base64"]
                            # 去掉 data:image/...;base64, 前缀（如有）
                            if "," in b64_str:
                                b64_str = b64_str.split(",")[1]

                            try:
                                img_bytes = base64.b64decode(b64_str)
                                begin_time = data.get("beginTime", "")
                                end_time = data.get("endTime", "")
                                range_type = data.get("range", "9")
                                title = f"九分区面雨量分布图（{begin_time} ~ {end_time}）"
                                cl.user_session.set("has_chart_generated", True)
                                await cl.Message(
                                    content=f"📊 已生成{title}：",
                                    elements=[cl.Image(content=img_bytes, name="station_rainfall_real_img")],
                                ).send()
                                observation_text = (
                                    f"（系统消息：已成功在前端为用户绘制了{title}。"
                                    f"区间{range_type}分区。不要输出坐标数据，请继续用自然语言简要说明时间范围和分区类型）"
                                )
                            except Exception as decode_err:
                                print(f"base64解码失败：{decode_err}")
                                observation_text = "已获取降水实况图，但图片数据解码失败。"
                        elif isinstance(data, dict) and "error" in data:
                            raw_err = str(data["error"])
                            print(f"[降水实况图] 后端返回错误（已隐藏）：{raw_err}")
                            observation_text = "获取降水实况图失败，请稍后重试。"
                        else:
                            observation_text = "已获取降水实况图数据。"
                    elif tool_name.startswith("historical_weather_"):
                        img_msgs, observation_text = _extract_historical_weather_images(observation)
                        if img_msgs:
                            cl.user_session.set("has_chart_generated", True)
                            await cl.Message(content="📊 图表已生成：", elements=img_msgs).send()
                    else:
                        observation_text = callbacks["tool_observation_to_text"](observation)

                    tool_step.output = f"查询完成（耗时 {tool_elapsed:.1f} 秒）"
                except Exception as e:
                    # 控制台保留详细错误（已脱敏）；不再单独向用户发送通用错误消息，
                    # 而是由 LLM 根据 ToolMessage 中的失败说明统一组织回答。
                    err_summary = _scrub_internal_data(str(e)) or "未知错误"
                    print(f"[工具错误] {tool_name}: {err_summary}")
                    observation_text = (
                        f"工具 {tool_name} 执行失败（{type(e).__name__}），"
                        f"该数据暂不可用。错误摘要：{err_summary}"
                    )
                    tool_step.output = f"查询失败：{err_summary[:120]}"

                messages.append(
                    ToolMessage(
                        content=observation_text,
                        tool_call_id=tool_call["id"],
                        role="tool",
                    )
                )

    round_elapsed = time.time() - round_start
    print(f"[本轮耗时] 第 {iteration} 轮工具调用总耗时: {round_elapsed:.2f}s")

    return forced_final_text, ree, warning_bundles


async def _try_rainfall_img_fast_path(user_text: str, thinking_chain, tools, messages, callbacks) -> bool:
    """降雨分布图快速路径：直接调用 get_station_rainfall_real_img 并展示图片+文字说明"""
    if not user_text:
        return False
    t = user_text.strip()
    img_keywords = [
        "降雨分布图", "降水实况图", "面雨量分布图", "实况图",
        "子流域降雨分布", "九分区雨量分布", "累计降雨图",
        "累计雨图", "降雨图", "降雨实况",
        "落区分布", "降雨落区", "雨区分布", "雨区",
    ]
    if not any(k in t for k in img_keywords):
        return False

    tool = _find_tool(tools, "get_station_rainfall_real_img")
    if not tool:
        return False

    reasoning = None
    print(f"\n=== 降雨分布图快速路径 ===")
    reasoning = await _show_business_reasoning(
        "生成海河流域降水实况分布图",
        ["实况降雨站点数据"],
        "将生成降雨分布图并简要说明时间范围和分区",
    )
    await reasoning.stage("📡 查询数据", "正在生成降水实况图，请稍候...")
    await generate_fast_path_thinking(
        thinking_chain, user_text, "生成海河流域降水实况分布图", ["实况降雨站点数据"], reasoning
    )

    try:
        now = datetime.now()
        interval = 24
        beginTime = ""
        endTime = ""

        # 尝试解析"昨天下午3点到7点"这种时间范围
        range_match = re.search(
            r"(前天|昨天|今天|前天\s*的|昨天\s*的|今天\s*的|)"
            r"(下午|上午|早上|凌晨|中午|晚上|夜间|)"
            r"(\d{1,2})\s*点"
            r"\s*(?:到|至|~|-)\s*"
            r"(下午|上午|早上|凌晨|中午|晚上|夜间|)"
            r"(\d{1,2})\s*点",
            t
        )
        if range_match:
            day_word = range_match.group(1).strip() or "今天"
            prefix1 = range_match.group(2) or ""
            hour1 = int(range_match.group(3))
            prefix2 = range_match.group(4) or ""
            hour2 = int(range_match.group(5))

            now_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if "前天" in day_word:
                base_date = now_date - timedelta(days=2)
            elif "昨天" in day_word:
                base_date = now_date - timedelta(days=1)
            else:
                base_date = now_date

            def resolve_hour(h: int, prefix: str) -> int:
                if prefix in ("下午", "晚上", "夜间") and h < 12:
                    h += 12
                return h

            h1 = resolve_hour(hour1, prefix1 or prefix2 or "上午")
            h2 = resolve_hour(hour2, prefix2 or prefix1 or "下午")

            start_dt = base_date + timedelta(hours=h1)
            end_dt = base_date + timedelta(hours=h2)

            if end_dt <= start_dt:
                end_dt = start_dt + timedelta(hours=3)

            beginTime = start_dt.strftime("%Y-%m-%d %H:%M:%S")
            endTime = end_dt.strftime("%Y-%m-%d %H:%M:%S")
            interval = int((end_dt - start_dt).total_seconds() / 3600)
        else:
            # 优先用 _detect_rainfall_time 解析；若未命中但含"今天"，默认取今天0点到现在
            time_str = _detect_rainfall_time(t)
            if time_str:
                try:
                    end_dt = datetime.strptime(time_str, "%Y%m%d%H%M%S")
                    begin_dt = end_dt - timedelta(hours=interval)
                    beginTime = begin_dt.strftime("%Y-%m-%d %H:%M:%S")
                    endTime = end_dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass
            elif any(k in t for k in ["今天", "今日", "现在", "当前"]):
                begin_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
                end_dt = now
                beginTime = begin_dt.strftime("%Y-%m-%d %H:%M:%S")
                endTime = end_dt.strftime("%Y-%m-%d %H:%M:%S")
                interval = int((end_dt - begin_dt).total_seconds() / 3600)

        args = {"beginTime": beginTime, "endTime": endTime, "interval": max(interval, 1)}
        result = await _invoke_tool_for_fast_path("get_station_rainfall_real_img", tool, args, user_text)
        # 显示图片
        data = _unwrap_tool_result(result)

        if isinstance(data, dict) and "base64" in data:
            b64_str = data["base64"]
            if "," in b64_str:
                b64_str = b64_str.split(",")[1]

            try:
                img_bytes = base64.b64decode(b64_str)
                bt = data.get("beginTime", beginTime) or beginTime
                et = data.get("endTime", endTime) or endTime
                rng = data.get("range", "9")
                rng_desc = {"9": "九", "11": "十一", "77": "七十七"}.get(str(rng), str(rng))

                title = f"海河流域{rng_desc}分区面雨量分布"

                text = (
                    f"📊 已生成{title}。\n\n"
                    f"**统计时段**：{bt} ~ {et}（北京时）\n\n"
                    f"上图展示了海河流域各{rng_desc}分区的累计面雨量空间分布，"
                    f"颜色越深表示该分区累计雨量越大。"
                    f"如需具体数值或单站详情，可继续问“各子流域面雨量对比”或“最大雨强出现在哪里”。"
                )
                text = callbacks["append_followup_if_needed"](text, user_text)
                await _emit_fast_path_result(
                    text,
                    messages,
                    user_text,
                    images=[cl.Image(content=img_bytes, name="station_rainfall_real_img")],
                    has_chart=True,
                    reasoning=reasoning,
                )
                return True
            except Exception as decode_err:
                print(f"base64解码失败：{decode_err}")

        error_msg = "获取降水实况图失败，请稍后重试。"
        if isinstance(data, dict) and "error" in data:
            raw_err = str(data["error"])
            print(f"[降雨分布图] 后端返回错误（已隐藏）：{raw_err}")
            lower = raw_err.lower()
            if "no record" in lower or "无记录" in raw_err or "暂无数据" in raw_err:
                error_msg = "所选时段暂无降水实况图数据，请确认时段是否正确或稍后重试。"
            elif any(k in lower for k in ["timeout", "timed out", "连接", "connect", "refused", "unreachable"]):
                error_msg = "降水实况图查询服务连接超时，请稍后重试。"
            elif any(k in lower for k in ["unauthorized", "auth", "forbidden", "permission", "鉴权", "权限", "欠费"]):
                error_msg = "降水实况图查询服务鉴权失败，请联系管理员检查服务配置。"
        await _maybe_close_reasoning(reasoning)
        await cl.Message(content=error_msg).send()
        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content=error_msg))
        cl.user_session.set("messages", messages)
        return True

    except Exception as e:
        print(f"降雨分布图快速路径失败，回退到通用流程：{e}")
        return False
    finally:
        if reasoning is not None:
            await reasoning.close()


async def _try_city_avg_rainfall_fast_path(user_text: str, thinking_chain, tools, messages, callbacks) -> bool:
    """全市平均降雨量快速路径：调用 analyze_rainfall_by_time 获取天擎实况数据"""
    if not user_text:
        return False
    t = user_text.strip()
    avg_keywords = ["全市平均降雨量", "全市平均雨量", "我市平均降雨量",
                    "全市平均降水量", "当前平均降雨量"]
    if not any(k in t for k in avg_keywords):
        return False

    tool = _find_rainfall_tool(tools)
    if not tool:
        return False

    city = "天津市"

    print(f"\n=== 城市平均降雨量快速路径：{city} ===")
    reasoning = await _show_business_reasoning(
        "查询城市平均降雨量",
        ["城市面雨量数据"],
        "将给出各城市平均降雨量排名或对比",
    )
    await generate_fast_path_thinking(
        thinking_chain, user_text, "查询城市平均降雨量", ["城市面雨量数据"], reasoning
    )
    await reasoning.stage("📡 查询数据", "正在查询实况降雨数据...")

    try:
        now = datetime.now()
        time_str = now.strftime("%Y%m%d%H%M%S")
        # 使用自定义时间范围：[当前-24h, 当前]，避免 -32h/-8h 偏移
        start_dt = now - timedelta(hours=24)
        start_s = start_dt.strftime("%Y%m%d%H%M%S")
        end_s = now.strftime("%Y%m%d%H%M%S")

        result = await asyncio.wait_for(
            _invoke_tool_for_fast_path(
                tool.name, tool, {"time_str": time_str, "start_time": start_s, "end_time": end_s}, user_text
            ),
            timeout=30,
        )
        data = _unwrap_tool_result(result)

        if isinstance(data, dict) and data.get("total_stations", 0) > 0:
            # 从 level_analysis 计算全市平均
            levels = data.get("level_analysis", [])
            total_rain = 0.0
            total_stations = 0
            max_rain = 0.0
            max_station_name = ""
            for lv in levels:
                for st in lv.get("stations", []):
                    r = float(st.get("rainfall", 0))
                    total_rain += r
                    total_stations += 1
                    if r > max_rain:
                        max_rain = r
                        max_station_name = st.get("name", "")

            avg_rain = round(total_rain / total_stations, 2) if total_stations > 0 else 0

            time_range_readable = _clean_table_cell(data.get("time_range_readable", f"{time_str}"))
            text = (
                f"## {city}实况降雨量\n\n"
                f"**统计时段**：{time_range_readable}（北京时）　**数据来源**：天擎自动站\n\n"
                f"| 指标 | 数值 |\n"
                f"| :--- | :--- |\n"
                f"| 平均降雨量 | {avg_rain} mm |\n"
                f"| 最大降雨量 | {round(max_rain, 1)} mm |\n"
                f"| 监测站总数 | {total_stations} 站 |\n"
            )
            if max_station_name:
                text += f"| 最大雨量站 | {_clean_table_cell(max_station_name)} |"
        else:
            text = f"当前{city}无有效实况降雨数据。"

        text = _prepend_thinking_summary(text, user_text, has_chart=False)
        await _maybe_close_reasoning(reasoning)
        await callbacks["stream_text_to_message"](text)
        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content=text))
        cl.user_session.set("messages", messages)
        return True

    except asyncio.TimeoutError:
        print("城市降雨量查询超时")
        await _maybe_close_reasoning(reasoning)
        await cl.Message(content="⏱️ 查询超时，请稍后重试。").send()
        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content="查询超时，请稍后重试。"))
        cl.user_session.set("messages", messages)
        return True
    except Exception as e:
        print(f"城市平均降雨量快速路径失败：{e}")
        return False
    finally:
        await reasoning.close()


async def _try_today_rainfall_fast_path(user_text: str, thinking_chain, tools, messages, callbacks) -> bool:
    """今天降雨快速路径：今天0点~现在用实况，现在~明天0点用预报"""
    if not user_text:
        return False
    t = user_text.strip()
    # 必须同时包含"今天"和降雨关键词（含口语化的"下雨""有雨"）
    today_keywords = ["今天", "今日"]
    rain_keywords = ["下雨", "有雨", "降雨", "降水", "雨量", "雨情"]
    if not (any(k in t for k in today_keywords) and any(k in t for k in rain_keywords)):
        return False

    tool = _find_rainfall_tool(tools)
    fc_tool = _find_tool(tools, "get_city_rainfall_time_range")
    if not tool:
        return False

    print(f"\n=== 今天降雨快速路径 ===")
    reasoning = await _show_business_reasoning(
        "查询今日降雨情况",
        ["实况降雨数据", "预报降雨数据"],
        "将分时段说明今日已下和将下的降雨",
    )
    await generate_fast_path_thinking(
        thinking_chain, user_text, "查询今日降雨情况", ["实况降雨数据", "预报降雨数据"], reasoning
    )
    await reasoning.stage("📡 查询数据", "正在查询今日降雨数据...")

    try:
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # 实况与预报的分界时次：取最近一个过去的整点（2/8/14/20）
        valid_h = [2, 8, 14, 20]
        split_h = [h for h in valid_h if h <= now.hour][-1] if any(h <= now.hour for h in valid_h) else valid_h[-1]
        split_dt = now.replace(hour=split_h, minute=0, second=0, microsecond=0)

        # 1. 实况：今天0点 ~ 最近整点
        obs_start_s = today_start.strftime("%Y%m%d%H%M%S")
        obs_end_s = split_dt.strftime("%Y%m%d%H%M%S")
        if obs_start_s == obs_end_s:
            # 如果0点=整点（刚过0点），用当前时间
            obs_end_s = now.strftime("%Y%m%d%H%M%S")

        obs_result = await asyncio.wait_for(
            _invoke_tool_for_fast_path(
                tool.name,
                tool,
                {"time_str": now.strftime("%Y%m%d%H%M%S"), "start_time": obs_start_s, "end_time": obs_end_s},
                user_text,
            ),
            timeout=30,
        )
        obs_data = _unwrap_tool_result(obs_result)

        # 2. 预报：从分界时次起未来24h
        fc_text = ""
        if fc_tool:
            try:
                start_fc = split_dt.strftime("%Y-%m-%d %H:%M:%S")
                fc_end_dt = split_dt + timedelta(hours=24)
                fc_period = f"{split_dt.strftime('%Y-%m-%d %H:%M')} ~ {fc_end_dt.strftime('%Y-%m-%d %H:%M')}"
                fc_result = await asyncio.wait_for(
                    _invoke_tool_for_fast_path(
                        fc_tool.name,
                        fc_tool,
                        {"city_name": "天津市", "start_time": start_fc, "forecast_hours": 24},
                        user_text,
                    ),
                    timeout=15,
                )
                fc_data = _unwrap_tool_result(fc_result)
                if isinstance(fc_data, dict) and fc_data.get("average_rainfall_mm") is not None:
                    avg_fc = float(fc_data['average_rainfall_mm'])
                    if avg_fc < 0.1:
                        fc_conclusion = "预计天津市未来 24 小时无明显降雨。"
                    elif avg_fc < 10:
                        fc_conclusion = "预计天津市未来 24 小时有微量降雨。"
                    elif avg_fc < 25:
                        fc_conclusion = "预计天津市未来 24 小时有小雨。"
                    elif avg_fc < 50:
                        fc_conclusion = "预计天津市未来 24 小时有中雨。"
                    else:
                        fc_conclusion = "预计天津市未来 24 小时有大雨及以上降雨。"
                    fc_text = (
                        f"\n\n### 预报（{_clean_table_cell(fc_period)}）\n\n"
                        f"**{fc_conclusion}**\n\n"
                        f"| 指标 | 数值 |\n| :--- | :--- |\n"
                        f"| 平均雨量 | {fc_data['average_rainfall_mm']} mm |\n"
                        f"| 最大雨量 | {fc_data['max_rainfall_mm']} mm |\n"
                        f"| 最小雨量 | {fc_data['min_rainfall_mm']} mm |\n"
                        f"**数据来源**：{_clean_table_cell(fc_data.get('data_resource', 'EC_AIFS'))}"
                    )
            except Exception as fce:
                print(f"今天预报查询失败：{fce}")

        # 3. 组装输出：实况与预报分段展示，任一 available 都要展示
        obs_text = ""
        if isinstance(obs_data, dict) and obs_data.get("total_stations", 0) > 0:
            levels = obs_data.get("level_analysis", [])
            total_rain = 0.0
            total_st = 0
            max_r = 0.0
            max_s = ""
            for lv in levels:
                for st in lv.get("stations", []):
                    r = float(st.get("rainfall", 0))
                    total_rain += r
                    total_st += 1
                    if r > max_r:
                        max_r = r
                        max_s = st.get("name", "")
            avg_r = round(total_rain / total_st, 2) if total_st > 0 else 0

            time_label = f"{today_start.strftime('%Y-%m-%d %H:%M')} ~ {split_dt.strftime('%Y-%m-%d %H:%M')}"
            obs_text = (
                f"## 今日实况降雨\n\n"
                f"**统计时段**：{_clean_table_cell(time_label)}（北京时）　**数据来源**：天擎自动站\n\n"
                f"| 指标 | 数值 |\n| :--- | :--- |\n"
                f"| 平均降雨量 | {avg_r} mm |\n"
                f"| 最大降雨量 | {round(max_r, 1)} mm |\n"
                f"| 监测站总数 | {total_st} 站 |\n"
                f"| 最大雨量站 | {_clean_table_cell(max_s) if max_s else '—'} |"
            )

        if obs_text or fc_text:
            text = obs_text + fc_text
        else:
            text = "今日实况降雨数据暂无，预报数据暂不可用。"

        text = _prepend_thinking_summary(text, user_text, has_chart=False)
        await _maybe_close_reasoning(reasoning)
        await callbacks["stream_text_to_message"](text)
        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content=text))
        cl.user_session.set("messages", messages)
        return True

    except asyncio.TimeoutError:
        print("今天降雨查询超时")
        await _maybe_close_reasoning(reasoning)
        await cl.Message(content="⏱️ 查询超时，请稍后重试。").send()
        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content="查询超时，请稍后重试。"))
        cl.user_session.set("messages", messages)
        return True
    except Exception as e:
        print(f"今天降雨快速路径失败：{e}")
        return False
    finally:
        await reasoning.close()


async def _try_today_rain_duration_fast_path(user_text: str, thinking_chain, tools, messages, callbacks) -> bool:
    """今日累计降雨时长快速路径"""
    if not user_text:
        return False
    t = user_text.strip()
    dur_keywords = ["累计降雨时长", "降雨时长", "累计雨时", "下雨时长",
                    "降雨时间多长", "雨下了多久"]
    if not any(k in t for k in dur_keywords):
        return False

    tool = _find_rainfall_tool(tools)
    if not tool:
        return False

    print(f"\n=== 今日累计降雨时长快速路径 ===")
    reasoning = await _show_business_reasoning(
        "统计今日降雨时长",
        ["实况降雨站点数据"],
        "将统计今日各站累计降雨时长",
    )
    await generate_fast_path_thinking(
        thinking_chain, user_text, "统计今日降雨时长", ["实况降雨站点数据"], reasoning
    )
    await reasoning.stage("📡 查询数据", "正在统计今日降雨时长...")

    try:
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        period_hours = max(now.hour, 1)  # 已过小时数，最少1h

        result = await asyncio.wait_for(
            _invoke_tool_for_fast_path(
                tool.name,
                tool,
                {
                    "time_str": now.strftime("%Y%m%d%H%M%S"),
                    "start_time": today_start.strftime("%Y%m%d%H%M%S"),
                    "end_time": now.strftime("%Y%m%d%H%M%S"),
                },
                user_text,
            ),
            timeout=30,
        )
        data = _unwrap_tool_result(result)

        if isinstance(data, dict) and data.get("total_stations", 0) > 0:
            levels = data.get("level_analysis", [])
            total_wet = 0
            total_stations = 0
            max_r = 0.0
            for lv in levels:
                for st in lv.get("stations", []):
                    r = float(st.get("rainfall", 0))
                    total_stations += 1
                    total_wet += 1
                    if r > max_r:
                        max_r = r

            # 估算有雨时长：根据有雨站点占比推算
            if total_wet > 0 and total_stations > 0:
                rain_ratio = total_wet / total_stations  # 全是天津市站点
                est_duration = round(period_hours * rain_ratio, 1)
                text = (
                    f"## 今日累计降雨时长\n\n"
                    f"**统计时段**：{today_start.strftime('%Y-%m-%d %H:%M')} ~ {now.strftime('%Y-%m-%d %H:%M')}（北京时）\n\n"
                    f"| 指标 | 数值 |\n"
                    f"| :--- | :--- |\n"
                    f"| 监测时段 | {period_hours} 小时 |\n"
                    f"| 有雨时长（估算） | 约 {est_duration} 小时 |\n"
                    f"| 占监测时段 | {round(rain_ratio*100)}% |\n"
                    f"| 最大小时雨强 | {round(max_r / max(period_hours,1), 1)} mm/h |\n"
                    f"**数据来源**：天擎自动站"
                )
            else:
                text = "今日天津市暂无有效降雨记录。"
        else:
            text = "今日降雨数据暂无。"

        text = _prepend_thinking_summary(text, user_text, has_chart=False)
        await _maybe_close_reasoning(reasoning)
        await callbacks["stream_text_to_message"](text)
        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content=text))
        cl.user_session.set("messages", messages)
        return True

    except asyncio.TimeoutError:
        print("降雨时长查询超时")
        await _maybe_close_reasoning(reasoning)
        await cl.Message(content="⏱️ 查询超时，请稍后重试。").send()
        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content="查询超时，请稍后重试。"))
        cl.user_session.set("messages", messages)
        return True
    except Exception as e:
        print(f"今日降雨时长快速路径失败：{e}")
        return False
    finally:
        await reasoning.close()

def _format_decision_dt(dt: datetime) -> str:
    return f"{dt.month}月{dt.day}日{dt.hour:02d}时{dt.minute:02d}分"


def _fmt_forecast_period_label(start_text: str, end_text: str) -> str:
    start_dt = _parse_decision_dt(start_text)
    end_dt = _parse_decision_dt(end_text)
    if not start_dt or not end_dt:
        return f"{_clean_table_cell(start_text)}-{_clean_table_cell(end_text)}"
    return f"{_format_decision_dt(start_dt)}-{_format_decision_dt(end_dt)}"


def _to_float_or_none(value) -> float | None:
    try:
        if value in {None, "", "-", "—"}:
            return None
        return float(value)
    except Exception:
        return None


def _format_number_text(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _split_weather_tokens(value: str | None) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    parts = re.split(r"[、,，/]|转|到", text)
    return [p.strip() for p in parts if p.strip()]


def _extract_wind_parts(value: str | None) -> tuple[list[str], list[int]]:
    text = str(value or "").strip()
    directions = list(dict.fromkeys(re.findall(r"([东北西南中]{1,3}风)", text)))
    levels: list[int] = []
    for low, high in re.findall(r"(\d+)\s*-\s*(\d+)\s*级", text):
        levels.extend((int(low), int(high)))
    for single in re.findall(r"(?<!-)(\d+)\s*级", text):
        levels.append(int(single))
    return directions, levels


def _top_items(items: list[str], limit: int = 3) -> list[str]:
    return [name for name, _ in Counter(item for item in items if item).most_common(limit)]


def _temperature_trend(day_rows: list[dict]) -> str:
    mids = []
    for row in day_rows:
        vals = [_to_float_or_none(row.get(k)) for k in ("temp_min", "temp_max")]
        vals = [v for v in vals if v is not None]
        if vals:
            mids.append(sum(vals) / len(vals))
    if len(mids) < 3:
        return "气温变化不明显"
    first = sum(mids[:2]) / 2
    last = sum(mids[-2:]) / 2
    peak_idx = max(range(len(mids)), key=lambda i: mids[i])
    valley_idx = min(range(len(mids)), key=lambda i: mids[i])
    peak = mids[peak_idx]
    valley = mids[valley_idx]
    if 0 < peak_idx < len(mids) - 1 and peak - first >= 1.0 and peak - last >= 1.0:
        return "气温先升后降"
    if 0 < valley_idx < len(mids) - 1 and first - valley >= 1.0 and last - valley >= 1.0:
        return "气温先降后升"
    if last - first >= 1.0:
        return "气温总体上升"
    if first - last >= 1.0:
        return "气温总体下降"
    return "气温总体平稳"


def _aggregate_rolling_daily_rows(payload: dict) -> list[dict]:
    periods = payload.get("periods") if isinstance(payload, dict) else []
    if not isinstance(periods, list):
        return []

    groups: dict[tuple[str, str], list[dict]] = {}
    for item in periods:
        if not isinstance(item, dict):
            continue
        start = str(item.get("start_time") or "")
        end = str(item.get("end_time") or "")
        if start and end:
            groups.setdefault((start, end), []).append(item)

    rows = []
    sorted_groups = sorted(
        groups.items(),
        key=lambda kv: _parse_decision_dt(kv[0][0]) or datetime.min,
    )
    for (start_text, end_text), items in sorted_groups:
        weather_tokens: list[str] = []
        wind_dirs: list[str] = []
        wind_dir_seen: set[str] = set()
        wind_levels: list[int] = []
        tmax_values: list[float] = []
        tmin_values: list[float] = []
        rain_values: list[float] = []
        rain_regions: list[tuple[str, float]] = []
        for item in items:
            weather_tokens.extend(_split_weather_tokens(item.get("WEA")))
            if (tmax := _to_float_or_none(item.get("TMAX"))) is not None:
                tmax_values.append(tmax)
            if (tmin := _to_float_or_none(item.get("TMIN"))) is not None:
                tmin_values.append(tmin)
            if (rain := _to_float_or_none(item.get("TP1H"))) is not None:
                rain_values.append(rain)
                rain_regions.append((str(item.get("region") or ""), rain))
            dirs, levels = _extract_wind_parts(item.get("EDA"))
            for d in dirs:
                if d not in wind_dir_seen:
                    wind_dir_seen.add(d)
                    wind_dirs.append(d)
            wind_levels.extend(levels)

        top_weather = _top_items(weather_tokens, 3)
        max_rain = max(rain_values) if rain_values else None
        max_rain_regions = list(dict.fromkeys(
            region for region, value in rain_regions
            if max_rain is not None and abs(value - max_rain) < 0.05 and region
        ))[:5]
        rows.append({
            "period": _fmt_forecast_period_label(start_text, end_text),
            "start_time": start_text,
            "end_time": end_text,
            "weather": "、".join(top_weather) if top_weather else "—",
            "temp_min": min(tmin_values) if tmin_values else None,
            "temp_max": max(tmax_values) if tmax_values else None,
            "temp_range": (
                f"{_format_number_text(min(tmin_values))}~{_format_number_text(max(tmax_values))}℃"
            ) if tmin_values and tmax_values else "—",
            "wind_direction": "、".join(_top_items(wind_dirs, 4)) if wind_dirs else "—",
            "wind_level": f"{min(wind_levels)}~{max(wind_levels)}级" if wind_levels else "—",
            "max_rain_mm": round(max_rain, 1) if max_rain is not None else None,
            "max_rain_text": _format_number_text(max_rain),
            "max_rain_regions": max_rain_regions,
        })
    return rows


def _weekly_trend_sentence(rows: list[dict], user_text: str) -> str:
    weather_tokens = [
        p
        for row in rows
        for p in str(row.get("weather") or "").split("、")
        if p and p != "—"
    ]
    weather = "和".join(_top_items(weather_tokens, 2)) or "天气变化"
    trend = _temperature_trend(rows)
    scope = "天津" if "天津" in (user_text or "") else "我市"
    return f"预计未来一周{scope}天气以{weather}为主，{trend}。"


def _weekly_focus_text(rows: list[dict]) -> str:
    focus = []
    rain_rows = [
        r for r in rows
        if (rain := _to_float_or_none(r.get("max_rain_mm"))) is not None and rain >= 10
    ]
    if rain_rows:
        show = "、".join(f"{r['period']}局地最大降水{r['max_rain_text']}毫米" for r in rain_rows[:3])
        focus.append(f"降水较明显时段：{show}。")
    wind_rows = [r for r in rows if re.search(r"([5-9]|1\d)级", str(r.get("wind_level") or ""))]
    if wind_rows:
        focus.append("部分时段风力偏大，需关注临近预报更新。")
    return "\n".join(f"- {item}" for item in focus)


def _build_weekly_rolling_forecast_text(payload: dict, user_text: str) -> str:
    rows = _aggregate_rolling_daily_rows(payload)
    if not rows:
        return "当前暂未获取到天津滚动预报数据。"
    first_week = rows[:7]
    lines = [
        "【核心结论】\n",
        _weekly_trend_sentence(first_week, user_text),
        "\n\n【逐日预报】\n",
        "| 日期时段 | 天气现象 | 气温区间(℃) | 风向 | 风力 |\n",
        "| :--- | :--- | :--- | :--- | :--- |\n",
    ]
    for row in first_week:
        lines.append(
            f"| {_clean_table_cell(row['period'])} | {_clean_table_cell(row['weather'])} | "
            f"{_clean_table_cell(row['temp_range'])} | {_clean_table_cell(row['wind_direction'])} | "
            f"{_clean_table_cell(row['wind_level'])} |\n"
        )
    focus = _weekly_focus_text(first_week)
    if focus:
        lines.append(f"\n【重点关注】\n{focus}\n")
    lines.append("\n数据来源：天津市气象台滚动预报。")
    return "".join(lines)


def _build_big_rain_forecast_text(payload: dict) -> str:
    rows = _aggregate_rolling_daily_rows(payload)
    if not rows:
        return "当前暂未获取到天津滚动预报数据。"
    first_week = rows[:7]
    risk_rows = [
        row for row in first_week
        if (rain := _to_float_or_none(row.get("max_rain_mm"))) is not None and rain >= 100.0
    ]
    if not risk_rows:
        return "根据最新天气预报，未来一周，我市无大暴雨天气。\n\n数据来源：天津市气象台滚动预报。"

    lines = ["【核心结论】\n预计未来一周我市有大暴雨天气风险。\n\n【重点关注】\n"]
    for row in risk_rows[:5]:
        regions = "、".join(row.get("max_rain_regions") or [])
        region_text = f"，主要区域：{regions}" if regions else ""
        lines.append(f"- {row['period']}：局地最大降水约{row['max_rain_text']}毫米{region_text}。\n")
    lines.append("\n数据来源：天津市气象台滚动预报。")
    return "".join(lines)


async def _query_weekly_rolling_forecast(tools, user_text: str):
    tool = _find_tool(tools, "query_rolling_forecast")
    if not tool:
        return None
    result = await _invoke_tool_for_fast_path(
        tool.name,
        tool,
        {
            "user_query": user_text,
            "regions": "",
            "start_period": 0,
            "end_period": 168,
            "interval": 24,
        },
        user_text,
    )
    return _unwrap_tool_result(result)


async def _try_weekly_forecast_fast_path(user_text: str, thinking_chain, tools, messages, callbacks) -> bool:
    """未来一周天气预报快速路径"""
    if not user_text:
        return False
    t = user_text.strip()
    wk_keywords = ["一周天气", "未来一周", "未来7天", "7天天气", "七天天气", "天气预报汇总"]
    if not any(k in t for k in wk_keywords):
        return False

    if not _find_tool(tools, "query_rolling_forecast"):
        return False

    print(f"\n=== 未来一周滚动预报快速路径 ===")
    reasoning = await _show_business_reasoning(
        "查询未来一周天气预报",
        ["天津市气象台滚动预报"],
        "将给出未来一周天气趋势与重点关注",
    )
    await generate_fast_path_thinking(
        thinking_chain, user_text, "查询未来一周天气预报", ["天津市气象台滚动预报"], reasoning
    )
    await reasoning.stage("📡 查询数据", "正在查询未来一周天气预报...")

    try:
        payload = await _query_weekly_rolling_forecast(tools, user_text)
        text = _build_weekly_rolling_forecast_text(payload if isinstance(payload, dict) else {}, user_text)

        text = _prepend_thinking_summary(text, user_text, has_chart=False)
        await _maybe_close_reasoning(reasoning)
        await callbacks["stream_text_to_message"](text)
        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content=text))
        cl.user_session.set("messages", messages)
        return True

    except Exception as e:
        print(f"未来一周预报快速路径失败：{e}")
        return False
    finally:
        await reasoning.close()

async def _try_big_rain_forecast_fast_path(user_text: str, thinking_chain, tools, messages, callbacks) -> bool:
    """未来一周是否有大暴雨：滚动预报确定性收口。"""
    if not user_text:
        return False
    t = user_text.strip()
    if "大暴雨" not in t and "特大暴雨" not in t:
        return False
    forecast_words = ("会", "会不会", "会有", "有没有", "是否", "预计", "未来", "接下来", "最近", "近期")
    past_words = ("过去", "已出现", "已经", "发生过", "实况", "昨天", "昨日")
    if not any(k in t for k in forecast_words) or any(k in t for k in past_words):
        return False
    if not _find_tool(tools, "query_rolling_forecast"):
        return False

    print(f"\n=== 大暴雨滚动预报快速路径 ===")
    reasoning = await _show_business_reasoning(
        "查询未来一周大暴雨预报",
        ["天津市气象台滚动预报"],
        "将给出未来一周大暴雨风险判断",
    )
    await generate_fast_path_thinking(
        thinking_chain, user_text, "查询未来一周大暴雨预报", ["天津市气象台滚动预报"], reasoning
    )
    await reasoning.stage("📡 查询数据", "正在查询未来一周大暴雨预报...")

    try:
        payload = await _query_weekly_rolling_forecast(tools, user_text)
        text = _build_big_rain_forecast_text(payload if isinstance(payload, dict) else {})
        text = _prepend_thinking_summary(text, user_text, has_chart=False)
        await _maybe_close_reasoning(reasoning)
        await callbacks["stream_text_to_message"](text)
        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content=text))
        cl.user_session.set("messages", messages)
        return True
    except Exception as e:
        print(f"大暴雨滚动预报快速路径失败：{e}")
        return False
    finally:
        await reasoning.close()

async def _try_heavy_rain_check_fast_path(user_text: str, thinking_chain, tools, messages, callbacks) -> bool:
    """近期是否有强降雨/暴雨快速路径"""
    if not user_text:
        return False
    t = user_text.strip()
    # 必须同时包含"近期/近日/近几天/最近/连日"才拦截，避免与暴雨影响分析冲突
    recent_words = ["近期", "近日", "近几天", "最近", "连日", "近72", "过去"]
    heavy_kw = ["强降雨", "暴雨", "暴雨天气", "强降水", "大雨过程"]
    if not (any(k in t for k in recent_words) and any(k in t for k in heavy_kw)):
        return False

    # 排除未来时间查询（明天/后天/未来）→ 这些应该走预报，不走实况检查
    future_words = ["明天", "后天", "未来", "下周"]
    if any(k in t for k in future_words):
        return False

    # 带有未来研判语义的“最近会有大暴雨吗”应交给天津滚动预报；
    # 明确过去/实况语义的问题仍保留在近72小时降雨检查路径。
    forecast_intent_words = ["会", "会不会", "会有", "有没有", "是否", "预计", "接下来"]
    past_observation_words = ["过去", "已出现", "已经", "近72", "实况", "发生过"]
    if any(k in t for k in forecast_intent_words) and not any(k in t for k in past_observation_words):
        return False

    # 先查实况，再查预报
    obs_tool = _find_rainfall_tool(tools)
    if not obs_tool:
        return False

    print(f"\n=== 强降雨检查快速路径 ===")
    reasoning = await _show_business_reasoning(
        "检查近期是否出现强降雨",
        ["实况降雨数据"],
        "将给出近72小时强降雨出现时段、区域与强度判断",
    )
    await generate_fast_path_thinking(
        thinking_chain, user_text, "检查近期是否出现强降雨", ["实况降雨数据"], reasoning
    )
    await reasoning.stage("📡 查询数据", "正在检查近期降雨情况...")

    try:
        now = datetime.now()
        start_72h = now - timedelta(hours=72)
        time_str = now.strftime("%Y%m%d%H%M%S")
        start_s = start_72h.strftime("%Y%m%d%H%M%S")
        end_s = now.strftime("%Y%m%d%H%M%S")

        result = await asyncio.wait_for(
            _invoke_tool_for_fast_path(
                obs_tool.name, obs_tool, {"time_str": time_str, "start_time": start_s, "end_time": end_s}, user_text
            ),
            timeout=30,
        )
        data = result
        data = _unwrap_tool_result(result)

        if isinstance(data, dict):
            max_r = data.get("max_rainfall", 0)
            max_level = data.get("max_level", "无")
            levels = data.get("level_analysis", [])
            heavy_counts = {}
            for lv in levels:
                lv_name = _clean_table_cell(lv.get("level", ""))
                cnt = lv.get("station_count", 0)
                if cnt > 0:
                    heavy_counts[lv_name] = cnt

            if max_r >= 50:
                conc = f"✅ 近72小时内有暴雨及以上降雨过程（最大{max_r:.1f}mm，达「{max_level}」级别）"
            elif max_r >= 25:
                conc = f"⚠️ 近72小时有大雨过程（最大{max_r:.1f}mm，未达暴雨级别）"
            else:
                conc = f"近72小时无强降雨过程（最大{max_r:.1f}mm）"

            lines = [f"## 近期降雨检查\n\n**统计时段**：{start_72h.strftime('%Y-%m-%d %H:%M')} ~ {now.strftime('%Y-%m-%d %H:%M')}（北京时）\n\n"]
            lines.append(f"{conc}\n\n")
            if heavy_counts:
                lines.append("| 降雨等级 | 站数 |\n| :--- | :--- |\n")
                for lv_name, cnt in heavy_counts.items():
                    lines.append(f"| {lv_name} | {cnt} |\n")
                lines.append("\n数据来源：天擎自动站")
            text = "".join(lines)
        else:
            text = "近期降雨数据暂不可用。"

        text = _prepend_thinking_summary(text, user_text, has_chart=False)
        await _maybe_close_reasoning(reasoning)
        await callbacks["stream_text_to_message"](text)
        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content=text))
        cl.user_session.set("messages", messages)
        return True

    except Exception as e:
        print(f"强降雨检查快速路径失败：{e}")
        return False
    finally:
        await reasoning.close()


_BASIN_REP_CITIES = ["北京", "天津", "石家庄", "保定", "唐山", "沧州"]


async def _try_basin_weather_fast_path(user_text: str, thinking_chain, tools, messages, callbacks) -> bool:
    """
    海河流域整体天气快速路径：
    例如"今天海河流域天气如何""明天海河天气怎么样"，固定查流域代表城市 24h 降雨预报，
    以规范表格返回，避免 Planner 输出格式混乱或附加无关建议。
    """
    if not user_text:
        return False
    t = user_text.strip()

    # 必须同时包含明确流域意向 + 时间词 + 天气意向。
    # 天津/本市/我市预报类问题不在这里拦截，交给滚动预报。
    # "流域"单独出现时，在本系统语境下默认为海河流域
    basin_kw = ["海河流域", "海河", "流域"]
    if not any(k in t for k in basin_kw):
        return False

    time_kw = ["今天", "今日", "明天", "明日", "后天"]
    if not any(k in t for k in time_kw):
        return False

    weather_kw = ["天气", "下雨", "有雨", "降水", "降雨", "雨量", "雨情", "暴雨", "大雨", "中雨", "小雨"]
    if not any(k in t for k in weather_kw):
        return False

    # 避免和子流域快速路径冲突：如果点到了具体子流域，交给子流域路径
    sub_basins = ["大清河", "子牙河", "永定河", "北三河", "漳卫南运河",
                  "徒骇马颊河", "黑龙港", "滦河", "潮白河", "蓟运河", "海河干流"]
    if any(k in t for k in sub_basins):
        return False

    fc_tool = _find_tool(tools, "get_city_rainfall_time_range")
    if not fc_tool:
        return False

    # 解析时间偏移
    now = datetime.now()
    if "后天" in t:
        day_off = 2
        label = "后天"
    elif "明天" in t or "明日" in t:
        day_off = 1
        label = "明天"
    else:
        day_off = 0
        label = "今天"

    day_dt = now.replace(hour=2, minute=0, second=0, microsecond=0) + timedelta(days=day_off)
    day_str = day_dt.strftime("%Y-%m-%d %H:%M:%S")
    date_label = day_dt.strftime("%m月%d日")

    print(f"\n=== 海河流域天气快速路径：{label}（{date_label}）===")
    reasoning = await _show_business_reasoning(
        "查询海河流域整体天气",
        ["流域天气预报数据"],
        "将给出海河流域今明后天气概况",
    )
    await generate_fast_path_thinking(
        thinking_chain, user_text, "查询海河流域整体天气", ["流域天气预报数据"], reasoning
    )
    await reasoning.stage("📡 查询数据", f"正在查询海河流域代表城市{label}降雨预报...")

    try:
        rows = []
        for city in _BASIN_REP_CITIES:
            try:
                r = await asyncio.wait_for(
                    _invoke_tool_for_fast_path(
                        fc_tool.name,
                        fc_tool,
                        {"city_name": city, "start_time": day_str, "forecast_hours": 24},
                        user_text,
                    ),
                    timeout=15,
                )
                rd = _unwrap_tool_result(r)
                if isinstance(rd, dict) and rd.get("average_rainfall_mm") is not None:
                    rows.append({
                        "city": city,
                        "avg": rd.get("average_rainfall_mm", "—"),
                        "max": rd.get("max_rainfall_mm", "—"),
                        "min": rd.get("min_rainfall_mm", "—"),
                        "judgment": _weather_judgment(rd.get("average_rainfall_mm")),
                        "data_resource": rd.get("data_resource", ""),
                    })
                else:
                    rows.append({"city": city, "avg": "—", "max": "—", "min": "—", "judgment": "无数据", "data_resource": ""})
            except Exception as e:
                print(f"[海河流域天气] {city} 查询失败：{e}")
                rows.append({"city": city, "avg": "—", "max": "—", "min": "—", "judgment": "查询失败"})
            await asyncio.sleep(0.1)

        valid = [r for r in rows if isinstance(r["avg"], (int, float))]
        max_avg = max((float(r["avg"]) for r in valid), default=0.0)
        rainy_cities = [r["city"] for r in valid if float(r["avg"]) >= 0.1]
        # 数据来源以工具返回的 data_resource 为准（汛期滚动预报 / 平时 EC）
        data_resource = next((str(r.get("data_resource") or "") for r in rows if r.get("data_resource")), "") or "ECMWF AIFS"

        if max_avg < 0.1:
            conclusion = f"预计{label}海河流域代表城市整体无明显降雨。"
        elif len(rainy_cities) <= 2:
            conclusion = f"预计{label}海河流域部分地区有降雨，主要出现在{'、'.join(rainy_cities)}。"
        else:
            conclusion = f"预计{label}海河流域大部分代表城市有降雨。"

        lines = [
            f"## 海河流域{label}（{date_label}）降雨预报\n\n",
            f"**核心结论**：{conclusion}\n\n",
            "本系统当前主要提供降雨实况监测与预报，以下从降雨角度回答：\n\n",
            f"**预报时效**：{day_str} 起未来 24 小时（北京时）\n",
            f"**数据来源**：{data_resource}\n\n",
            "| 代表城市 | 平均雨量(mm) | 最大雨量(mm) | 最小雨量(mm) | 降雨趋势 |\n",
            "| :--- | :--- | :--- | :--- | :--- |\n",
        ]
        for r in rows:
            lines.append(f"| {r['city']} | {r['avg']} | {r['max']} | {r['min']} | {r['judgment']} |\n")

        # 一句话补充：按平均雨量最大城市描述
        if valid:
            max_row = max(valid, key=lambda x: float(x["avg"]))
            if max_avg >= 0.1:
                summary = f"\n其中，'{max_row['city']}'平均雨量相对最大（{max_row['avg']} mm），为{max_row['judgment']}。"
                lines.append(summary)
        lines.append("\n\n**说明**：以上为代表城市格点预报，具体点位可能有差异；预报具有不确定性，请以临近预报为准。")

        text = "".join(lines)
        text = callbacks.get("append_followup_if_needed", lambda txt, u: txt)(text, user_text)
        text = _prepend_thinking_summary(text, user_text, has_chart=False)
        await _maybe_close_reasoning(reasoning)
        await callbacks["stream_text_to_message"](text)
        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content=text))
        cl.user_session.set("messages", messages)
        return True

    except asyncio.TimeoutError:
        await _maybe_close_reasoning(reasoning)
        await cl.Message(content="⏱️ 预报查询超时，请稍后重试。").send()
        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content="预报查询超时，请稍后重试。"))
        cl.user_session.set("messages", messages)
        return True
    except Exception as e:
        print(f"海河流域天气快速路径失败：{e}")
        return False
    finally:
        await reasoning.close()


async def _try_weekend_activity_fast_path(user_text: str, thinking_chain, tools, messages, callbacks) -> bool:
    """
    周末户外活动建议快速路径：
    例如"周末适合户外活动吗""周六能出去玩吗"，查周六日降雨预报并给出活动建议。
    """
    if not user_text:
        return False
    t = user_text.strip()

    # 周末关键词
    weekend_kw = ["周末", "周六", "周日", "星期日", "星期天", "周六日"]
    if not any(k in t for k in weekend_kw):
        return False

    # 流域范围：指定流域 → 查代表城市 + 活动建议；未指定 → 默认天津 + 天气预报
    basin_scope_kw = ["海河流域", "海河", "流域"]
    is_basin_scope = any(k in t for k in basin_scope_kw)

    # 天气查询意图：天气/降雨/如何/怎么样/预报
    weather_intent_kw = ["天气", "下雨", "有雨", "降雨", "雨", "如何", "怎么样", "预报", "什么天"]
    has_weather_intent = any(k in t for k in weather_intent_kw)

    # 活动意图
    activity_kw = ["户外", "活动", "出行", "旅游", "游玩", "适合", "能去", "出去", "玩",
                   "爬山", "跑步", "骑行", "露营", "野餐", "运动", "散步", "郊游"]
    has_activity_intent = any(k in t for k in activity_kw)
    has_suitability_pattern = bool(re.search(r"(适合|能|可以|好不好|行不行|好).{0,6}(吗|么|呢)", t))

    # 流域范围：需活动/天气意图；非流域：天气意图或活动意图均可，默认查天津
    if is_basin_scope:
        if not (has_activity_intent or has_weather_intent or has_suitability_pattern):
            return False
    else:
        if not (has_weather_intent or has_activity_intent or has_suitability_pattern):
            return False

    fc_tool = _find_tool(tools, "get_city_rainfall_time_range")
    if not fc_tool:
        return False

    # 计算 upcoming 周六、周日
    now = datetime.now()
    weekday = now.weekday()  # 0=周一
    if weekday < 5:  # 周一到周五
        sat_offset = 5 - weekday
        sun_offset = 6 - weekday
    elif weekday == 5:  # 周六
        sat_offset = 0
        sun_offset = 1
    else:  # 周日
        sat_offset = 6
        sun_offset = 0

    days = []
    if "周六" in t and "周日" not in t and "周末" not in t and "周日" not in t and "星期天" not in t and "星期日" not in t:
        days = [("周六", sat_offset)]
    elif ("周日" in t or "星期天" in t or "星期日" in t) and "周六" not in t and "周末" not in t:
        days = [("周日", sun_offset)]
    else:
        days = [("周六", sat_offset), ("周日", sun_offset)]

    if is_basin_scope:
        cities = _BASIN_REP_CITIES[:3]
    else:
        cities = ["天津市"]

    print(f"\n=== 周末{'活动' if is_basin_scope else '天气'}快速路径：{[d[0] for d in days]} ===")
    reasoning = await _show_business_reasoning(
        "获取周末户外活动天气建议",
        ["周末天气预报数据"],
        "将给出周末天气适合度与活动建议",
    )
    await generate_fast_path_thinking(
        thinking_chain, user_text, "获取周末户外活动天气建议", ["周末天气预报数据"], reasoning
    )
    await reasoning.stage("📡 查询数据", "正在查询周末降雨预报...")

    try:
        day_results = {}
        for day_label, day_off in days:
            day_dt = now.replace(hour=2, minute=0, second=0, microsecond=0) + timedelta(days=day_off)
            day_str = day_dt.strftime("%Y-%m-%d %H:%M:%S")
            date_label = day_dt.strftime("%m月%d日")
            city_rows = []
            for city in cities:
                try:
                    r = await asyncio.wait_for(
                        _invoke_tool_for_fast_path(
                            fc_tool.name,
                            fc_tool,
                            {"city_name": city, "start_time": day_str, "forecast_hours": 24},
                            user_text,
                        ),
                        timeout=15,
                    )
                    rd = _unwrap_tool_result(r)
                    if isinstance(rd, dict) and rd.get("average_rainfall_mm") is not None:
                        city_rows.append({
                            "city": city,
                            "avg": rd.get("average_rainfall_mm", "—"),
                            "max": rd.get("max_rainfall_mm", "—"),
                            "judgment": _weather_judgment(rd.get("average_rainfall_mm")),
                            "data_resource": rd.get("data_resource", ""),
                        })
                    else:
                        city_rows.append({"city": city, "avg": "—", "max": "—", "judgment": "无数据", "data_resource": ""})
                except Exception as e:
                    print(f"[周末活动] {day_label} {city} 查询失败：{e}")
                    city_rows.append({"city": city, "avg": "—", "max": "—", "judgment": "查询失败"})
                await asyncio.sleep(0.1)
            day_results[day_label] = {"date": date_label, "rows": city_rows}

        # 生成输出：流域范围 → 活动建议；非流域 → 天气预报
        if is_basin_scope:
            lines = ["## 周末户外活动建议\n\n"]
            overall_suitable = True
            for day_label, info in day_results.items():
                date_label = info["date"]
                valid = [r for r in info["rows"] if isinstance(r["avg"], (int, float))]
                max_avg = max(float(r["avg"]) for r in valid) if valid else 0.0
                if max_avg >= 10:
                    advice = "不建议户外活动，建议安排室内行程"
                    overall_suitable = False
                elif max_avg >= 1:
                    advice = "不太适合长时间户外活动，外出请携带雨具"
                    overall_suitable = False
                elif max_avg >= 0.1:
                    advice = "基本适合户外活动，偶有微量降雨"
                else:
                    advice = "适合户外活动"
                lines.append(f"**{day_label}（{date_label}）**：{advice}\n\n")
                lines.append("| 代表城市 | 平均雨量(mm) | 最大雨量(mm) | 降雨趋势 |\n")
                lines.append("| :--- | :--- | :--- | :--- |\n")
                for r in info["rows"]:
                    lines.append(f"| {r['city']} | {r['avg']} | {r['max']} | {r['judgment']} |\n")
                lines.append("\n")
            conclusion = "📌 本周末海河流域代表城市整体降雨较弱，比较适合户外活动。" if overall_suitable else "📌 本周末海河流域部分时段有明显降雨，请根据具体日期和区域安排活动。"
            # 数据来源以工具返回的 data_resource 为准（汛期滚动预报 / 平时 EC）
            weekend_data_resource = ""
            for info in day_results.values():
                for r in info.get("rows", []):
                    if r.get("data_resource"):
                        weekend_data_resource = str(r["data_resource"])
                        break
                if weekend_data_resource:
                    break
            weekend_data_resource = weekend_data_resource or "ECMWF AIFS"
            text = conclusion + "\n\n" + "".join(lines)
            text += f"**说明**：以上为代表城市 24 小时降雨量预报（{weekend_data_resource}），具体点位可能有差异；临近出行前请关注最新预报。"
        else:
            lines = ["## 天津周末天气预报\n\n"]
            for day_label, info in day_results.items():
                date_label = info["date"]
                lines.append(f"**{day_label}（{date_label}）**\n\n")
                lines.append("| 城市 | 平均雨量(mm) | 最大雨量(mm) | 降雨趋势 |\n")
                lines.append("| :--- | :--- | :--- | :--- |\n")
                for r in info["rows"]:
                    lines.append(f"| {r['city']} | {r['avg']} | {r['max']} | {r['judgment']} |\n")
                lines.append("\n")
            text = "".join(lines)
            text += "**说明**：以上为天津 24 小时降雨量预报（ECMWF AIFS），临近出行前请关注最新预报。"
        text = callbacks.get("append_followup_if_needed", lambda txt, u: txt)(text, user_text)
        text = _prepend_thinking_summary(text, user_text, has_chart=False)
        await _maybe_close_reasoning(reasoning)
        await callbacks["stream_text_to_message"](text)
        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content=text))
        cl.user_session.set("messages", messages)
        return True

    except asyncio.TimeoutError:
        await _maybe_close_reasoning(reasoning)
        await cl.Message(content="⏱️ 预报查询超时，请稍后重试。").send()
        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content="预报查询超时，请稍后重试。"))
        cl.user_session.set("messages", messages)
        return True
    except Exception as e:
        print(f"周末活动快速路径失败：{e}")
        return False
    finally:
        await reasoning.close()


_KNOWN_WATER_LEVEL_RIVERS = ["大清河", "子牙河", "永定河", "北三河", "漳卫南运河", "徒骇马颊河", "黑龙港", "滦河", "潮白河", "蓟运河", "海河干流"]


def _extract_river_name_for_water_level(user_text: str) -> str:
    """从"子牙河水位情况"这类问句中提取河名。"""
    if not user_text:
        return ""
    t = user_text.strip()
    for name in _KNOWN_WATER_LEVEL_RIVERS:
        if name in t:
            return name
    m = re.search(r"([一-龥]{1,8}河)", t)
    return m.group(1) if m else ""


async def _try_water_level_fast_path(user_text: str, thinking_chain, tools, messages, callbacks) -> bool:
    """水位查询快速路径：直接调用 query_water_level 并输出规范表格，避免模型自行生成畸形表格。"""
    if not user_text:
        return False
    t = user_text.strip()
    if "水位" not in t:
        return False

    river_name = _extract_river_name_for_water_level(t)
    if not river_name:
        return False

    tool = _find_tool(tools, "query_water_level")
    if not tool:
        return False

    print(f"\n=== 水位快速路径：{river_name} ===")
    reasoning = await _show_business_reasoning(
        "查询河网水位",
        ["河网水位数据"],
        "将给出关键站点水位信息",
    )
    await generate_fast_path_thinking(
        thinking_chain, user_text, "查询河网水位", ["河网水位数据"], reasoning
    )
    await reasoning.stage("📡 查询数据", f"正在查询{river_name}水位情况...")

    try:
        result = await asyncio.wait_for(
            _invoke_tool_for_fast_path(
                tool.name, tool, {"river_name": river_name, "data_type": "river"}, user_text
            ),
            timeout=30,
        )
        print(f"[水位快速路径] 原始结果类型={type(result)}, 内容={result}")

        # 统一解包 MCP 工具返回
        data = _unwrap_tool_result(result)

        print(f"[水位快速路径] 解包后类型={type(data)}, 内容={data}")

        if not isinstance(data, dict):
            await _emit_fast_path_result(
                "水位数据格式异常，无法生成表格。", messages, user_text, reasoning=reasoning
            )
            return True

        if data.get("error"):
            raw_err = str(data["error"])
            print(f"[水位快速路径] 后端返回错误（已隐藏）：{raw_err}")
            lower = raw_err.lower()
            if "no record" in lower or "无记录" in raw_err or "暂无数据" in raw_err:
                friendly = f"当前未查询到{river_name}相关站点水位数据，请确认河流名称或稍后重试。"
            elif any(k in lower for k in ["timeout", "timed out", "连接", "connect", "refused", "unreachable"]):
                friendly = "水位查询服务连接超时，请稍后重试。"
            elif any(k in lower for k in ["unauthorized", "auth", "forbidden", "permission", "鉴权", "权限", "欠费"]):
                friendly = "水位查询服务鉴权失败，请联系管理员检查服务配置。"
            else:
                friendly = "水位查询服务暂时不可用，请稍后重试。"
            await _emit_fast_path_result(friendly, messages, user_text, reasoning=reasoning)
            return True

        records = data.get("records", [])
        if not isinstance(records, list) or not records:
            await _emit_fast_path_result(
                f"当前未查询到{river_name}相关站点水位数据。", messages, user_text, reasoning=reasoning
            )
            return True
        now_str = datetime.now().strftime("%Y年%m月%d日%H:%M")

        lines = [f"## {_clean_table_cell(river_name)}水位情况\n\n"]
        lines.append(f"截至{now_str}，{_clean_table_cell(river_name)}相关监测站点水位如下：\n\n")
        lines.append("| 站点名称 | 当前水位(m) | 警戒水位(m) | 超警戒(m) | 涨率 | 更新时间 |\n")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |\n")

        for r in records:
            if not isinstance(r, dict):
                continue
            name = r.get("station_name") or "-"
            wl = r.get("water_level_m")
            if wl is None:
                wl = r.get("水位(m)")
            warn = r.get("warning_level_m")
            if warn is None:
                warn = r.get("警戒水位(m)")
            over = r.get("超警戒(m)", "-")
            rate = r.get("涨率", "-")
            if rate == "" or rate is None:
                rate = "-"
            time = r.get("time") or "-"

            def _fmt(v):
                if isinstance(v, (int, float)):
                    return f"{v:.2f}"
                return _clean_table_cell(v) if v is not None else "-"

            lines.append(
                f"| {_fmt(name)} | {_fmt(wl)} | {_fmt(warn)} | {_fmt(over)} | {_fmt(rate)} | {_fmt(time)} |\n"
            )

        source = _clean_table_cell(data.get("source") or "十四所水位接口")
        lines.append(f"\n**数据来源**：{source}")
        text = "".join(lines)
        text = _sanitize_display_text(text)
        print(f"[水位快速路径] 最终输出文本=\n{text}")

        text = _prepend_thinking_summary(text, user_text, has_chart=False)
        await _maybe_close_reasoning(reasoning)
        await callbacks["stream_text_to_message"](text)
        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content=text))
        cl.user_session.set("messages", messages)
        return True

    except asyncio.TimeoutError:
        await _emit_fast_path_result(
            "⏱️ 水位查询超时，请稍后重试。", messages, user_text, reasoning=reasoning
        )
        return True
    except Exception as e:

        print(f"[水位快速路径] 异常：{e}")
        traceback.print_exc()
        await _emit_fast_path_result(
            "水位查询遇到异常，请稍后重试。", messages, user_text, reasoning=reasoning
        )
        return True
    finally:
        if reasoning is not None:
            await reasoning.close()


async def _try_general_weather_fast_path(user_text: str, thinking_chain, tools, messages, callbacks) -> bool:
    """
    通用天气快速路径：
    - "今天/当前海河流域会下雨吗/有雨吗/降雨情况" → 走本地/后端降雨分析实况
    - "明天/后天/未来X天会下雨吗/降雨情况" → 走 get_city_rainfall_time_range 天津市预报
    避免简单降水问题进入完整 Planner/Answer 循环。
    注意：宽泛的"天气如何"（未明确问降雨）不放此路径，交给 Planner 说明能力范围。
    """
    if not user_text:
        return False
    t = user_text.strip()

    # 必须是明确降雨/降水相关；仅含“天气”的宽泛询问交给 Planner 处理，
    # 避免把“天气如何”回答成纯降雨数据。
    rain_kw = ["下雨", "有雨", "降水", "降雨", "雨量", "雨情"]
    if not any(k in t for k in rain_kw):
        return False

    # 只处理“海河流域 / 天津 / 无具体区域”的通用天气问题。
    # 一旦用户点名具体子流域、具体城市、具体站点，或涉及分析/对比/图表，
    # 应交回 LLM 规划，避免答非所问。
    sub_basins = [
        "大清河", "子牙河", "永定河", "北三河", "漳卫南运河",
        "徒骇马颊河", "黑龙港", "滦河", "潮白河", "蓟运河",
        "海河干流", "卫河", "淇河", "洹河", "滏阳河",
    ]
    if any(k in t for k in sub_basins):
        return False

    # 排除已由专用快速路径覆盖的场景，以及需要深度分析/对比/制图的问题
    excluded = [
        "河网", "水系", "拓扑", "流域图", "示意图",
        "行政区划", "下游",
        "降雨时长", "实况图",
        "未来一周", "一周天气", "7天天气", "七天天气",
        "站点", "国家站", "自动站", "观测",
        "昨日", "昨天", "过去", "历史", "近",
        "雨情汇总", "降雨汇总", "雨情分析", "降雨分析",
        "周末", "周六", "周日",
    ]
    if any(k in t for k in excluded):
        return False

    # 识别时间词（提前到这里，供下方"流域"判断使用）
    has_today = any(k in t for k in ["今天", "今日", "现在", "当前", "目前"])
    has_tomorrow = any(k in t for k in ["明天", "明日"])
    has_dayafter = "后天" in t
    future_days_match = re.search(r"未来\s*(\d+)\s*天", t)
    next_days_match = re.search(r"接下来\s*(\d+)\s*天", t)
    num_future_days = None
    if future_days_match:
        num_future_days = int(future_days_match.group(1))
    elif next_days_match:
        num_future_days = int(next_days_match.group(1))

    # 如果提到“流域”但不是“海河流域”，通常不拦截（如大清河流域）。
    # 但如果同时含有未来时间词，说明是问整体流域未来天气，予以拦截。
    if "流域" in t and "海河流域" not in t:
        if not (has_tomorrow or has_dayafter or num_future_days):
            return False

    # 如果提到具体城市/区县（非天津），也不拦截
    cities = ["北京", "石家庄", "保定", "沧州", "衡水", "邢台", "邯郸", "唐山",
              "秦皇岛", "廊坊", "承德", "张家口", "太原", "济南", "郑州",
              "滨海新区", "武清", "宝坻", "静海", "宁河", "蓟州"]
    if any(k in t for k in cities):
        return False
    now = datetime.now()

    # 今天 → 实况
    if has_today and not (has_tomorrow or has_dayafter or num_future_days):
        tool = _find_rainfall_tool(tools)
        if not tool:
            return False
        print(f"\n=== 通用天气快速路径（今天实况）===")
        reasoning = await _show_business_reasoning(
            "查询通用天气",
            ["天气预报数据"],
            "将给出天气概况与变化趋势",
        )
        await generate_fast_path_thinking(
            thinking_chain, user_text, "查询通用天气", ["天气预报数据"], reasoning
        )
        await reasoning.stage("📡 查询数据", "正在查询今日实况天气...")

        try:
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            result = await asyncio.wait_for(
                _invoke_tool_for_fast_path(
                    tool.name,
                    tool,
                    {
                        "time_str": now.strftime("%Y%m%d%H%M%S"),
                        "start_time": today_start.strftime("%Y%m%d%H%M%S"),
                        "end_time": now.strftime("%Y%m%d%H%M%S"),
                    },
                    user_text,
                ),
                timeout=30,
            )
            data = _unwrap_tool_result(result)

            if isinstance(data, dict) and data.get("total_stations", 0) > 0:
                max_r = data.get("max_rainfall", 0)
                max_level = data.get("max_level", "无")
                time_range = _clean_table_cell(data.get("time_range_readable", ""))
                text = (
                    f"## 今日海河流域天气实况\n\n"
                    f"**统计时段**：{time_range}（北京时）　**数据来源**：天擎自动站\n\n"
                    f"| 指标 | 数值 |\n| :--- | :--- |\n"
                    f"| 最大降雨量 | {max_r:.1f} mm |\n"
                    f"| 降雨等级 | {_clean_table_cell(max_level)} |\n"
                    f"| 监测站数 | {data.get('total_stations', 0)} 站 |"
                )
            else:
                text = "今日海河流域暂无有效降雨数据。"

            text = _prepend_thinking_summary(text, user_text, has_chart=False)
            await _maybe_close_reasoning(reasoning)
            await callbacks["stream_text_to_message"](text)
            messages.append(HumanMessage(content=user_text))
            messages.append(AIMessage(content=text))
            cl.user_session.set("messages", messages)
            return True
        except asyncio.TimeoutError:
            await cl.Message(content="⏱️ 查询超时，请稍后重试。").send()
            messages.append(HumanMessage(content=user_text))
            messages.append(AIMessage(content="查询超时，请稍后重试。"))
            cl.user_session.set("messages", messages)
            return True
        except Exception as e:
            print(f"通用天气快速路径（今天）失败：{e}")
            return False
        finally:
            await reasoning.close()

    # 明天/后天/未来N天 → 预报
    fc_tool = _find_tool(tools, "get_city_rainfall_time_range")
    if not fc_tool:
        return False

    # 确定要查哪些天；如果没有识别到任何未来时间词，不应误拦截
    days_to_query = []
    if has_tomorrow:
        days_to_query.append(1)
    if has_dayafter:
        days_to_query.append(2)
    if num_future_days:
        days_to_query.extend(range(1, min(num_future_days, 5) + 1))
    if not days_to_query:
        return False

    print(f"\n=== 通用天气快速路径（预报 {days_to_query} 天）===")
    reasoning = await _show_business_reasoning(
        "查询通用天气",
        ["天气预报数据"],
        "将给出天气概况与变化趋势",
    )
    await generate_fast_path_thinking(
        thinking_chain, user_text, "查询通用天气", ["天气预报数据"], reasoning
    )
    await reasoning.stage("📡 查询数据", f"正在查询未来 {len(days_to_query)} 天天气预报...")

    try:
        rows = []
        for day_off in days_to_query:
            day_dt = now.replace(hour=2, minute=0, second=0, microsecond=0) + timedelta(days=day_off)
            day_str = day_dt.strftime("%Y-%m-%d %H:%M:%S")
            try:
                r = await asyncio.wait_for(
                    _invoke_tool_for_fast_path(
                        fc_tool.name,
                        fc_tool,
                        {"city_name": "天津市", "start_time": day_str, "forecast_hours": 24},
                        user_text,
                    ),
                    timeout=15,
                )
                rd = _unwrap_tool_result(r)
                if isinstance(rd, dict):
                    avg = rd.get("average_rainfall_mm", "-")
                    mx = rd.get("max_rainfall_mm", "-")
                    mn = rd.get("min_rainfall_mm", "-")
                    rows.append((day_dt.strftime("%m月%d日"), avg, mx, mn, rd.get("data_resource", "")))
                else:
                    rows.append((day_dt.strftime("%m月%d日"), "无数据", "—", "—", ""))
            except Exception:
                rows.append((day_dt.strftime("%m月%d日"), "无数据", "—", "—"))
            await asyncio.sleep(0.1)

        if rows:
            lines = ["## 海河流域未来天气预报（天津市代表站）\n\n"]
            lines.append("以天津市为代表站，预报未来降雨情况：\n\n")
            lines.append("| 日期 | 平均雨量(mm) | 最大雨量(mm) | 最小雨量(mm) |\n")
            lines.append("| :--- | :--- | :--- | :--- |\n")
            for d, avg, mx, mn, _dr in rows:
                lines.append(f"| {d} | {avg} | {mx} | {mn} |\n")
            genweather_data_resource = next((str(dr) for _, _, _, _, dr in rows if dr), "") or "ECMWF AIFS"
            lines.append(f"\n**说明**：{genweather_data_resource} 预报；以天津市作为海河流域代表站展示整体趋势，具体子流域请咨询对应区域。")
            text = "".join(lines)
        else:
            text = "暂无未来天气预报数据。"

        text = _prepend_thinking_summary(text, user_text, has_chart=False)
        await _maybe_close_reasoning(reasoning)
        await callbacks["stream_text_to_message"](text)
        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content=text))
        cl.user_session.set("messages", messages)
        return True
    except asyncio.TimeoutError:
        await _maybe_close_reasoning(reasoning)
        await cl.Message(content="⏱️ 预报查询超时，请稍后重试。").send()
        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content="预报查询超时，请稍后重试。"))
        cl.user_session.set("messages", messages)
        return True
    except Exception as e:
        print(f"通用天气快速路径（预报）失败：{e}")
        return False
    finally:
        await reasoning.close()


# 子流域 → 代表性城市（用于未来天气预报，因流域级预报工具暂缺）
_SUBBASIN_REP_CITIES: dict[str, list[str]] = {
    "大清河": ["保定", "廊坊"],
    "子牙河": ["石家庄", "衡水"],
    "永定河": ["北京", "张家口"],
    "北三河": ["唐山", "秦皇岛"],
    "漳卫南运河": ["邯郸", "沧州"],
    "徒骇马颊河": ["德州"],
    "黑龙港": ["衡水", "沧州"],
    "滦河": ["承德", "唐山"],
    "潮白河": ["北京", "承德"],
    "蓟运河": ["天津", "唐山"],
    "海河干流": ["天津"],
}


def _weather_judgment(avg_mm) -> str:
    try:
        avg = float(avg_mm)
    except Exception:
        return "—"
    if avg < 0.1:
        return "晴好，无有效降雨"
    elif avg < 10:
        return "小雨"
    elif avg < 25:
        return "中雨"
    elif avg < 50:
        return "大雨"
    else:
        return "暴雨及以上"


async def _try_subbasin_forecast_fast_path(user_text: str, thinking_chain, tools, messages, callbacks) -> bool:
    """
    子流域未来天气预报快速路径：
    例如"大清河流域未来三天天气"，用该流域的代表城市逐日查 24h 预报，汇总成表。
    """
    if not user_text:
        return False
    t = user_text.strip()

    # 必须同时包含子流域名 + 未来时间词 + 天气/降雨意向
    subbasin = None
    for name in _SUBBASIN_REP_CITIES:
        if name in t:
            subbasin = name
            break
    if not subbasin:
        return False

    has_future = any(k in t for k in ["未来", "明天", "后天", "接下来"])
    num_days_match = re.search(r"未来\s*(\d+)\s*天", t)
    if not has_future and not num_days_match:
        return False

    rain_kw = ["下雨", "有雨", "降水", "降雨", "雨量", "雨情", "天气"]
    if not any(k in t for k in rain_kw):
        return False

    fc_tool = _find_tool(tools, "get_city_rainfall_time_range")
    if not fc_tool:
        return False

    days = 3
    if num_days_match:
        days = min(int(num_days_match.group(1)), 7)
    elif "明天" in t and "后天" in t:
        days = 3  # 今/明/后 通常含今天
    elif "明天" in t:
        days = 2
    elif "后天" in t:
        days = 3

    cities = _SUBBASIN_REP_CITIES[subbasin]
    print(f"\n=== 子流域预报快速路径：{subbasin}，代表城市 {cities}，{days} 天 ===")
    reasoning = await _show_business_reasoning(
        "查询子流域未来天气预报",
        ["子流域预报数据"],
        "将给出指定子流域未来几天天气预报",
    )
    await generate_fast_path_thinking(
        thinking_chain, user_text, "查询子流域未来天气预报", ["子流域预报数据"], reasoning
    )
    await reasoning.stage("📡 查询数据", f"正在查询{subbasin}代表城市未来{days}天降雨预报...")

    try:
        now = datetime.now()
        rows = []

        for day_off in range(days):
            day_dt = now.replace(hour=2, minute=0, second=0, microsecond=0) + timedelta(days=day_off)
            day_str = day_dt.strftime("%Y-%m-%d %H:%M:%S")
            date_label = day_dt.strftime("%m月%d日")

            for city in cities:
                try:
                    r = await asyncio.wait_for(
                        _invoke_tool_for_fast_path(
                            fc_tool.name,
                            fc_tool,
                            {"city_name": city, "start_time": day_str, "forecast_hours": 24},
                            user_text,
                        ),
                        timeout=15,
                    )
                    rd = _unwrap_tool_result(r)
                    if isinstance(rd, dict) and rd.get("average_rainfall_mm") is not None:
                        rows.append({
                            "city": city,
                            "date": date_label,
                            "avg": rd.get("average_rainfall_mm", "—"),
                            "max": rd.get("max_rainfall_mm", "—"),
                            "min": rd.get("min_rainfall_mm", "—"),
                            "judgment": _weather_judgment(rd.get("average_rainfall_mm")),
                            "data_resource": rd.get("data_resource", ""),
                        })
                    else:
                        rows.append({"city": city, "date": date_label, "avg": "—", "max": "—", "min": "—", "judgment": "无数据", "data_resource": ""})
                except Exception as e:
                    print(f"[{subbasin}] {city} {date_label} 预报查询失败：{e}")
                    rows.append({"city": city, "date": date_label, "avg": "—", "max": "—", "min": "—", "judgment": "查询失败"})
                await asyncio.sleep(0.1)

        if rows:
            subbasin_data_resource = next((str(r.get("data_resource") or "") for r in rows if r.get("data_resource")), "") or "ECMWF AIFS"
            lines = [f"## {_clean_table_cell(subbasin)}未来{days}天降雨预报\n\n"]
            lines.append(f"基于 {subbasin_data_resource} 24 小时降雨量预报，以**{'、'.join(cities)}**为代表城市，反映{_clean_table_cell(subbasin)}未来{days}天降雨趋势：\n\n")
            lines.append("| 代表城市 | 日期 | 平均雨量(mm) | 最大雨量(mm) | 天气预判 |\n")
            lines.append("| :--- | :--- | :--- | :--- | :--- |\n")
            for r in rows:
                lines.append(f"| {r['city']} | {r['date']} | {r['avg']} | {r['max']} | {r['judgment']} |\n")
            lines.append("\n**说明**：以代表城市预报近似反映子流域降雨趋势；实际流域面雨量分布可能有差异，预报具有不确定性，请以临近预报为准。")
            text = "".join(lines)
        else:
            text = f"{_clean_table_cell(subbasin)}未来{days}天预报数据暂不可用。"

        text = callbacks.get("append_followup_if_needed", lambda txt, u: txt)(text, user_text)
        text = _prepend_thinking_summary(text, user_text, has_chart=False)
        await _maybe_close_reasoning(reasoning)
        await callbacks["stream_text_to_message"](text)
        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content=text))
        cl.user_session.set("messages", messages)
        return True

    except asyncio.TimeoutError:
        await _maybe_close_reasoning(reasoning)
        await cl.Message(content="⏱️ 预报查询超时，请稍后重试。").send()
        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content="预报查询超时，请稍后重试。"))
        cl.user_session.set("messages", messages)
        return True
    except Exception as e:
        print(f"子流域预报快速路径失败：{e}")
        return False
    finally:
        await reasoning.close()


async def _try_basin_areal_rainfall_fast_path(user_text: str, thinking_chain, tools, messages, callbacks) -> bool:
    """面雨量快速路径：各子流域面雨量对比、哪个河系降雨最多/最少"""
    if not user_text:
        return False
    t = user_text.strip()

    trigger_keywords = ["面雨量", "子流域", "河系", "分区雨量", "流域对比", "流域降雨", "流域雨量", "降雨最多", "降雨最少"]
    if not any(k in t for k in trigger_keywords):
        return False

    # 避免误拦截具体城市/站点的查询
    city_keywords = ["城市", "站点", "站"]
    cities = ["北京", "天津", "石家庄", "保定", "沧州", "衡水", "邢台", "邯郸", "唐山",
              "秦皇岛", "廊坊", "承德", "张家口", "太原", "济南", "郑州"]
    if any(k in t for k in city_keywords) or any(c in t for c in cities):
        return False

    tool = _find_tool(tools, "query_basin_areal_rainfall")
    if not tool:
        return False

    def _parse_areal_rainfall_time(text: str) -> tuple[int, str | None, str, bool]:
        """
        解析面雨量查询的时间意图。
        返回 (hours, time_range, time_label, explicit_time_range)。
        time_range 为 [start,end] 格式（YYYYMMDDHHMMSS），time_label 为给业务展示的可读具体时段，
        explicit_time_range 表示用户是否明确指定了绝对日期。
        """
        now = datetime.now()

        # 1. 绝对日期：2024年7月25日 / 2024-07-25 / 20240725 / 2024/07/25
        abs_patterns = [
            (r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", lambda groups: datetime(int(groups[0]), int(groups[1]), int(groups[2]))),
            (r"(\d{4})-(\d{1,2})-(\d{1,2})", lambda groups: datetime(int(groups[0]), int(groups[1]), int(groups[2]))),
            (r"(\d{4})/(\d{1,2})/(\d{1,2})", lambda groups: datetime(int(groups[0]), int(groups[1]), int(groups[2]))),
            (r"(\d{4})(\d{2})(\d{2})", lambda groups: datetime(int(groups[0]), int(groups[1]), int(groups[2]))),
        ]
        for pat, builder in abs_patterns:
            m = re.search(pat, text)
            if m:
                try:
                    day = builder(m.groups())
                    start = day.replace(hour=0, minute=0, second=0)
                    end = day.replace(hour=23, minute=59, second=59)
                    time_range = f"[{start.strftime('%Y%m%d%H%M%S')},{end.strftime('%Y%m%d%H%M%S')}]"
                    label = f"{start.strftime('%Y年%m月%d日 %H:%M')} ~ {end.strftime('%Y年%m月%d日 %H:%M')}"
                    return 24, time_range, label, True
                except Exception:
                    continue

        # 2. 相对时长词：转换成具体起止时间
        for pattern, h in [
            (r"72\s*小时", 72),
            (r"48\s*小时", 48),
            (r"24\s*小时", 24),
            (r"过去一周", 168),
            (r"一周", 168),
            (r"过去7天", 168),
            (r"7天", 168),
        ]:
            if re.search(pattern, text):
                end = now
                start = end - timedelta(hours=h)
                time_range = f"[{start.strftime('%Y%m%d%H%M%S')},{end.strftime('%Y%m%d%H%M%S')}]"
                label = f"{start.strftime('%Y年%m月%d日 %H:%M')} ~ {end.strftime('%Y年%m月%d日 %H:%M')}"
                return h, time_range, label, False

        # 默认：未识别到具体时间词，按过去24小时查询并生成具体起止时间标签
        end = now
        start = end - timedelta(hours=24)
        time_range = f"[{start.strftime('%Y%m%d%H%M%S')},{end.strftime('%Y%m%d%H%M%S')}]"
        label = f"{start.strftime('%Y年%m月%d日 %H:%M')} ~ {end.strftime('%Y年%m月%d日 %H:%M')}"
        return 24, time_range, label, False

    hours, time_range, time_label, explicit_time_range = _parse_areal_rainfall_time(t)
    print(f"\n=== 面雨量快速路径：{time_label} ===")
    reasoning = await _show_business_reasoning(
        "查询流域面雨量",
        ["面雨量数据"],
        "将给出流域面雨量统计与对比",
    )
    await generate_fast_path_thinking(
        thinking_chain, user_text, "查询流域面雨量", ["面雨量数据"], reasoning
    )
    await reasoning.stage("📡 查询数据", "正在查询各子流域面雨量数据...")

    _HOURS_SENTINEL = object()

    async def _invoke_areal_rainfall(zone_type: str, tr: str | None = None, hrs=_HOURS_SENTINEL, timeout: int = 30):
        args = {"zone_type": zone_type, "time_range": tr or time_range}
        if hrs is not _HOURS_SENTINEL:
            if hrs is not None:
                args["hours"] = hrs
        elif hours is not None:
            args["hours"] = hours
        # 长时段查询给更宽裕的超时，避免 168h 等大数据量请求被 30s 截断
        result = await asyncio.wait_for(
            _invoke_tool_for_fast_path(tool.name, tool, args, user_text),
            timeout=timeout,
        )
        return _unwrap_tool_result(result)

    def _has_error(data) -> tuple[bool, str]:
        if isinstance(data, list) and data and isinstance(data[0], dict) and "error" in data[0]:
            return True, str(data[0]["error"])
        if isinstance(data, dict) and "error" in data:
            return True, str(data["error"])
        return False, ""

    def _friendly_areal_rainfall_error(err_msg: str, time_label: str) -> str:
        """把后端/接口原始错误转换为业务人员可理解的提示，避免暴露 URL、参数、签名等内部信息。"""
        if not err_msg:
            return "面雨量查询服务暂时不可用，请稍后重试。"
        lower = err_msg.lower()
        if any(k in lower for k in ["no record", "无记录", "暂无数据", "面雨量无数据"]):
            return f"所选时段（{time_label}）暂无各子流域面雨量数据，请确认时段是否正确，或稍后重试。"
        if any(k in lower for k in ["timeout", "timed out", "连接", "connect", "refused", "unreachable", "temporarily unavailable"]):
            return "面雨量查询服务连接超时，请稍后重试。"
        if any(k in lower for k in ["unauthorized", "auth", "forbidden", "permission", "鉴权", "权限", "欠费"]):
            return "面雨量查询服务鉴权失败，请联系管理员检查服务配置。"
        return "面雨量查询服务暂时不可用，请稍后重试。"

    def _format_areal_rainfall_table(data, used_zone_type: str, time_label: str, avg_label: str = "平均面雨量") -> str:
        if not isinstance(data, list) or not data:
            return ""
        lines = [f"## 海河流域各子流域面雨量对比（{time_label}）\n\n"]
        if used_zone_type:
            zone_label = {"9": "海河9分区", "11": "海河11分区", "77": "海河77分区", "246": "海河246分区", "32": "海河32分区"}.get(used_zone_type, f"{used_zone_type}分区")
            lines.append(f"**分区体系**：{zone_label}\n\n")
        lines.append(f"| 排名 | 分区 | {avg_label}(mm) | 最大面雨量(mm) |\n")
        lines.append("| :--- | :--- | :--- | :--- |\n")
        valid_rows = 0
        for idx, item in enumerate(data[:20], 1):
            if not isinstance(item, dict) or "error" in item:
                continue
            zone_name = _clean_table_cell(
                item.get("zone_name")
                or item.get("zone_id")
                or item.get("name")
                or item.get("分区")
                or "未知"
            )
            avg = (
                item.get("avg_rainfall_mm")
                or item.get("avg")
                or item.get("average_rainfall_mm")
                or item.get("average")
                or item.get("mean")
                or "-"
            )
            mx = (
                item.get("max_rainfall_mm")
                or item.get("max")
                or item.get("maximum_rainfall_mm")
                or item.get("maximum")
                or "-"
            )
            lines.append(f"| {idx} | {zone_name} | {avg} | {mx} |\n")
            valid_rows += 1
        if valid_rows == 0:
            return ""
        return "".join(lines)

    async def _query_areal_rainfall_split(zone_type: str, total_hours: int, timeout: int = 30) -> tuple[list, str]:
        """
        把长时段拆成多个24小时查询并聚合。
        用于后端不支持一次性查询168h等长时段的场景。
        返回 (聚合结果列表, 对齐后的时间标签)。
        """
        now = datetime.now()
        # 结束时刻对齐到最近的过去 08:00/20:00，与后端数据时次保持一致
        sync_hours = [h for h in [20, 8] if now.replace(hour=h, minute=0, second=0, microsecond=0) <= now]
        if not sync_hours:
            sync_hours = [8]
        end_hour = sync_hours[0]
        end_dt = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
        days = total_hours // 24
        aggregated: dict[str, dict] = {}
        for day in range(days):
            day_end = end_dt - timedelta(days=day)
            day_start = day_end - timedelta(hours=24)
            day_range = f"[{day_start.strftime('%Y%m%d%H%M%S')},{day_end.strftime('%Y%m%d%H%M%S')}]"
            print(f"[面雨量] 拆分查询第 {day + 1}/{days} 天：{day_range}")
            try:
                day_data = await _invoke_areal_rainfall(zone_type, day_range, 24, timeout=timeout)
            except Exception as e:
                print(f"[面雨量] 拆分查询第 {day + 1} 天失败：{e}")
                continue
            has_err, _ = _has_error(day_data)
            if has_err or not isinstance(day_data, list):
                continue
            for item in day_data:
                if not isinstance(item, dict) or "error" in item:
                    continue
                zone_name = (
                    item.get("zone_name")
                    or item.get("zone_id")
                    or item.get("name")
                    or item.get("分区")
                    or "未知"
                )
                avg = item.get("avg_rainfall_mm") or item.get("avg") or item.get("average_rainfall_mm") or item.get("average") or item.get("mean") or 0
                mx = item.get("max_rainfall_mm") or item.get("max") or item.get("maximum_rainfall_mm") or item.get("maximum") or 0
                try:
                    avg_f = float(avg)
                    mx_f = float(mx)
                except (TypeError, ValueError):
                    continue
                if zone_name not in aggregated:
                    aggregated[zone_name] = {"avg_sum": 0.0, "max_max": 0.0, "count": 0}
                aggregated[zone_name]["avg_sum"] += avg_f
                aggregated[zone_name]["max_max"] = max(aggregated[zone_name]["max_max"], mx_f)
                aggregated[zone_name]["count"] += 1

        if not aggregated:
            return [], ""
        result = []
        for zone_name, vals in aggregated.items():
            if vals["count"] == 0:
                continue
            result.append({
                "zone_name": zone_name,
                "avg_rainfall_mm": round(vals["avg_sum"], 2),
                "max_rainfall_mm": round(vals["max_max"], 2),
            })
        # 按累计面雨量降序
        result.sort(key=lambda x: float(x.get("avg_rainfall_mm", 0)), reverse=True)
        start_dt = end_dt - timedelta(hours=total_hours)
        label = f"{start_dt.strftime('%Y年%m月%d日 %H:%M')} ~ {end_dt.strftime('%Y年%m月%d日 %H:%M')}"
        return result, label

    try:
        # 统一使用 9 分区，保持业务输出一致；带一次重试，兼容 MCP 适配器偶发超时
        data = None
        areal_timeout = 90 if hours > 24 else 30
        for attempt in range(2):
            try:
                data = await _invoke_areal_rainfall("9", timeout=areal_timeout)
                break
            except asyncio.TimeoutError as e:
                print(f"[面雨量] 第 {attempt + 1} 次查询超时")
                if attempt == 0:
                    await asyncio.sleep(1)
            except Exception as e:
                err_text = str(e).lower()
                # MCP 适配器在任务取消/超时时可能抛出 UnboundLocalError: call_tool_result
                if "call_tool_result" in err_text or "timeout" in err_text or "cancel" in err_text:
                    print(f"[面雨量] 第 {attempt + 1} 次查询异常（疑似超时）：{e}")
                    if attempt == 0:
                        await asyncio.sleep(1)
                else:
                    raise

        if data is None:
            await _emit_fast_path_result(
                "⏱️ 面雨量查询超时，请稍后重试。", messages, user_text, reasoning=reasoning
            )
            return True

        used_zone_type = "9"
        has_err, err_msg = _has_error(data)
        split_data = None

        if has_err and not explicit_time_range:
            # 用户未指定绝对日期且查询失败时，尝试用 08:00/20:00 对齐的时间范围再查一次，保持原时长时间隔
            now = datetime.now()
            candidates = []
            for h in [8, 20]:
                cand = now.replace(hour=h, minute=0, second=0, microsecond=0)
                if cand <= now:
                    candidates.append(cand)
            if candidates:
                end = max(candidates)
                start = end - timedelta(hours=hours)
                fallback_range = f"[{start.strftime('%Y%m%d%H%M%S')},{end.strftime('%Y%m%d%H%M%S')}]"
                fallback_label = f"{start.strftime('%Y年%m月%d日 %H:%M')} ~ {end.strftime('%Y年%m月%d日 %H:%M')}"
                print(f"[面雨量] 尝试08:00/20:00对齐时间范围：{fallback_range}")
                fb_timeout = 90 if hours > 24 else 30
                # 长时段对齐查询尝试不传 hours，仅用 time_range，避免后端 hours 参数限制
                data = await _invoke_areal_rainfall("9", fallback_range, None, timeout=fb_timeout)
                used_zone_type = "9"
                has_err, err_msg = _has_error(data)
                if not has_err:
                    time_label = fallback_label

        if has_err and hours > 24:
            # 单次/对齐查询失败，尝试拆分为多个 24 小时查询并聚合
            print(f"[面雨量] 长时段查询失败，尝试拆分为 {hours // 24} 个24小时查询并聚合")
            split_data, split_label = await _query_areal_rainfall_split("9", hours, timeout=30)
            if split_data:
                data = split_data
                used_zone_type = "9"
                has_err = False
                err_msg = ""
                if split_label:
                    time_label = split_label

        if has_err:
            # 控制台保留原始错误用于调试，UI 只展示业务化提示
            print(f"[面雨量] 查询失败（内部错误）：{err_msg}")
            text = _friendly_areal_rainfall_error(err_msg, time_label)
        else:
            # 长时段拆分聚合时，avg 实际为累计面雨量，表头做区分
            avg_label = "累计面雨量" if hours > 24 and split_data is not None else "平均面雨量"
            text = _format_areal_rainfall_table(data, used_zone_type, time_label, avg_label)
            if not text:
                text = f"所选时段（{time_label}）暂无有效面雨量数据，请稍后重试。"

        await _emit_fast_path_result(text, messages, user_text, reasoning=reasoning)
        return True
    except asyncio.TimeoutError:
        await _emit_fast_path_result(
            "⏱️ 面雨量查询超时，请稍后重试。", messages, user_text, reasoning=reasoning
        )
        return True
    except Exception as e:
        print(f"面雨量快速路径失败：{e}")

        traceback.print_exc()
        await _emit_fast_path_result(
            "面雨量查询遇到异常，请稍后重试。", messages, user_text, reasoning=reasoning
        )
        return True
    finally:
        if reasoning is not None:
            await reasoning.close()


async def _try_emergency_response_fast_path(user_text: str, thinking_chain, tools, messages, callbacks) -> bool:
    """防汛应急响应判定快速路径：直接调用 evaluate_haihe_emergency_response。"""
    matched, times = _extract_emergency_response_time(user_text)
    if not matched:
        return False

    tool = _find_tool(tools, "evaluate_haihe_emergency_response")
    if not tool:
        return False

    print(f"\n=== 防汛应急响应快速路径：times={times} ===")
    reasoning = await _show_business_reasoning(
        "查询防汛应急响应信息",
        ["防汛应急响应数据"],
        "将给出应急响应级别与相关信息",
    )
    await generate_fast_path_thinking(
        thinking_chain, user_text, "查询防汛应急响应信息", ["防汛应急响应数据"], reasoning
    )
    await reasoning.stage("📡 查询数据", "正在查询防汛应急响应判定结果...")

    try:
        result = await asyncio.wait_for(
            _invoke_tool_for_fast_path(
                tool.name, tool, {"times": times, "basin_codes": "HHLY"}, user_text
            ),
            timeout=60,
        )

        data = _unwrap_tool_result(result)

        if not isinstance(data, dict):
            await _emit_fast_path_result(
                "应急响应判定结果格式异常，无法生成回答。", messages, user_text, reasoning=reasoning
            )
            return True

        if data.get("error"):
            raw_err = str(data["error"])
            print(f"[应急响应快速路径] 后端错误：{raw_err}")
            friendly = "当前无法获取应急响应判定数据，请稍后重试。"
            if "no record" in raw_err.lower() or "无记录" in raw_err or "暂无数据" in raw_err:
                friendly = f"未查询到 {times[:4]}年{times[4:6]}月{times[6:8]}日 {times[8:10]}:{times[10:12]} 的应急响应判定数据，可能该时刻无有效分钟降水资料。"
            await _emit_fast_path_result(friendly, messages, user_text, reasoning=reasoning)
            return True

        triggered = data.get("triggered") or data.get("reached")
        level = data.get("level")
        msg = data.get("message", "")
        evidence = data.get("evidence", {}) if isinstance(data.get("evidence"), dict) else {}
        dt_obj = datetime.strptime(times, "%Y%m%d%H%M%S")
        time_label = dt_obj.strftime("%Y年%m月%d日%H时%M分")

        if triggered:
            lines = [f"## 防汛应急响应判定：已触发 {level} 级响应\n"]
            lines.append(f"截至 **{time_label}**，海河流域实况雨量已达到 **{level} 级**防汛应急响应启动条件。\n")
        else:
            lines = [f"## 防汛应急响应判定：未触发\n"]
            lines.append(f"截至 **{time_label}**，海河流域实况雨量**未达到**防汛应急响应启动条件。\n")

        if msg:
            lines.append(f"**判定结论**：{msg}\n")

        # 关键证据
        total = evidence.get("total_station_count")
        qualified = evidence.get("qualified_station_count") or evidence.get("qualified_adjacent_station_count")
        ratio = evidence.get("ratio")
        threshold = evidence.get("threshold_mm")
        window = evidence.get("window_hours")

        if total is not None:
            lines.append("### 判定依据\n")
            lines.append(f"- 参与判定国家站数：{total}")
            if qualified is not None:
                lines.append(f"- 达标站点数：{qualified}")
            if ratio is not None:
                lines.append(f"- 达标站点占比：{ratio:.1%}")
            if threshold is not None and window is not None:
                lines.append(f"- 触发阈值：最近 {window} 小时累计降水 ≥ {threshold} mm")
            lines.append("")

        lines.append("\n数据来源：天擎分钟降水实况")
        await _emit_fast_path_result("\n".join(lines), messages, user_text, reasoning=reasoning)
        return True
    except asyncio.TimeoutError:
        await _emit_fast_path_result(
            "⏱️ 应急响应判定查询超时，请稍后重试。", messages, user_text, reasoning=reasoning
        )
        return True
    except Exception as e:
        print(f"防汛应急响应快速路径失败：{e}")

        traceback.print_exc()
        await _emit_fast_path_result(
            "应急响应判定查询遇到异常，请稍后重试。", messages, user_text, reasoning=reasoning
        )
        return True
    finally:
        if reasoning is not None:
            await reasoning.close()


async def process_message(message: cl.Message, planner_chain, answer_chain, thinking_chain, tools, messages, callbacks):
    query_start_time = time.time()
    cl.user_session.set("query_start_time", query_start_time)
    session_id = cl.user_session.get("id") or ""
    query_summary = message.content
    cl.user_session.set("query_timing_logged", False)
    cl.user_session.set("has_chart_generated", False)

    if ENABLE_FAST_PATHS:
        # 降雨分布图快速路径（优先判断，避免误入河网路径）
        if await _try_rainfall_img_fast_path(message.content, thinking_chain, tools, messages, callbacks):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return

        # 防汛应急响应判定快速路径
        if await _try_emergency_response_fast_path(message.content, thinking_chain, tools, messages, callbacks):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return

        # 暴雨影响河系专题图快速路径（比通用河网路径更具体，优先判断）
        if await _try_affected_river_network_by_rainfall_fast_path(
            message.content, thinking_chain, tools, messages, callbacks
        ):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return

        # 河网图快速路径
        if await _try_river_plot_fast_path(message.content, thinking_chain, tools, messages, callbacks):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return

        # 降雨分析快速路径
        if await _try_rainfall_analysis_fast_path(message.content, thinking_chain, tools, messages, callbacks):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return

        # 城市平均降雨量快速路径
        if await _try_city_avg_rainfall_fast_path(message.content, thinking_chain, tools, messages, callbacks):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return

        # 预警事实查询快速路径（包含”预警”时先判断接口，再调用工具并混合生成回答）
        if await _try_warning_fact_fast_path(message.content, thinking_chain, answer_chain, tools, messages, callbacks):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return

        # 今日累计降雨时长快速路径（比”今天降雨”更具体，优先判断）
        if await _try_today_rain_duration_fast_path(message.content, thinking_chain, tools, messages, callbacks):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return

        # 今天降雨快速路径（分两段：今天0点~现在用实况，现在~明天0点用预报）
        if await _try_today_rainfall_fast_path(message.content, thinking_chain, tools, messages, callbacks):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return

        # 未来大暴雨预报快速路径（无大暴雨时短答收口）
        if await _try_big_rain_forecast_fast_path(message.content, thinking_chain, tools, messages, callbacks):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return

        # 未来一周预报快速路径
        if await _try_weekly_forecast_fast_path(message.content, thinking_chain, tools, messages, callbacks):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return

        # 强降雨/暴雨检查快速路径
        if await _try_heavy_rain_check_fast_path(message.content, thinking_chain, tools, messages, callbacks):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return

        # 子流域未来天气预报快速路径（大清河/子牙河等未来N天天气）
        if await _try_subbasin_forecast_fast_path(message.content, thinking_chain, tools, messages, callbacks):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return

        # 面雨量快速路径（子流域对比、排名）
        if await _try_basin_areal_rainfall_fast_path(message.content, thinking_chain, tools, messages, callbacks):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return

        # 周末户外活动建议快速路径
        if await _try_weekend_activity_fast_path(message.content, thinking_chain, tools, messages, callbacks):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return

        # 海河流域整体天气快速路径（今天/明天/后天海河流域/天津天气如何）
        if await _try_basin_weather_fast_path(message.content, thinking_chain, tools, messages, callbacks):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return

        # 水位查询快速路径（避免模型生成畸形表格）
        if await _try_water_level_fast_path(message.content, thinking_chain, tools, messages, callbacks):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return

        # 通用天气快速路径（今天/明天/后天/未来N天天气）
        if await _try_general_weather_fast_path(message.content, thinking_chain, tools, messages, callbacks):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return

        # 点位决策天气快速路径（具体学校/场馆/单位/设施，需先被以上路径排除后才做 POI 定位）
        if await _try_decision_weather_fast_path(message.content, thinking_chain, answer_chain, tools, messages, callbacks):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return

    else:
        print(f"[process_message] fast paths disabled; routing to planner LLM: {message.content[:80]!r}")

    messages.append(HumanMessage(content=message.content))
    cl.user_session.set("last_query", message.content)

    stream_msg = cl.Message(content="")
    await stream_msg.send()

    reasoning = ReasoningStep("🤔 思考过程")
    await reasoning.__aenter__()

    # 生成并展示深度思考
    try:
        print("[THINKING_PLANNER] starting thinking generation")
        await callbacks["astream_thinking_to_reasoning"](
            thinking_chain,
            {
                "messages": [HumanMessage(content=message.content)],
                "system_message": THINKING_PROMPT.format(
                    current_time=datetime.now().strftime("%Y-%m-%d %H:%M"),
                    user_query=message.content,
                ),
            },
            reasoning,
        )
        print("[THINKING_PLANNER] thinking generation done")
    except Exception:
        pass

    await reasoning.stage("🔍 理解问题", "正在规划数据查询方案...")

    try:
        _compress_messages(messages)
        planner_msg = await callbacks["astream_planner_think"](
            planner_chain, {"messages": messages}, reasoning
        )
        planner_msg = _ensure_tool_calls_from_content(planner_msg)
    except Exception as e:
        await reasoning.line(f"❌ 规划失败：{str(e)[:200]}")
        await reasoning.__aexit__(None, None, None)
        await cl.Message(content=_friendly_llm_error_text(e)).send()
        print(f"Planner 首轮调用失败：{e}")

        traceback.print_exc()
        cl.user_session.set("messages", messages)
        _log_query_exit(query_start_time, session_id, query_summary, "fail")
        return

    if planner_msg.tool_calls:
        tool_count = len(planner_msg.tool_calls)
        tool_names_display = "、".join(
            TOOL_DISPLAY_NAMES.get(tc["name"], tc["name"]) for tc in planner_msg.tool_calls
        )
        await reasoning.stage("📡 查询数据", f"需要查询以下数据：{tool_names_display}（共 {tool_count} 项）")
        await reasoning.stage("📡 查询数据", f"正在查询 {tool_count} 项数据，请稍候...")
    else:
        await reasoning.stage("✍️ 生成结论", "已掌握足够信息，直接为您整理回答。")
        await reasoning.stage("✍️ 生成结论", "正在整理回答...")

    print(f"\n=== 第一次 Planner 调用结果 ===")
    print(f"Planner Message: {planner_msg}")
    print(f"Tool Calls: {planner_msg.tool_calls}")
    print(f"Content: {planner_msg.content}")
    print(f"========================\n")

    used_manual_plot_fallback = False

    if (not planner_msg.tool_calls) and callbacks["need_river_plot"](message.content):
        used_manual_plot_fallback = await _try_manual_plot_fallback(message.content, tools, stream_msg, callbacks, reasoning=reasoning)

    if not planner_msg.tool_calls:
        if used_manual_plot_fallback:
            await reasoning.line("**已生成河网图，无需进一步回答。**")
            await _maybe_close_reasoning(reasoning)
            messages.append(AIMessage(content=stream_msg.content))
            cl.user_session.set("messages", messages)
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return

        # 若 planner 已经生成完整业务化回答，直接复用，避免 answer_chain 二次生成导致格式异常
        cleaned_planner_content = _sanitize_display_text(planner_msg.content or "")
        if cleaned_planner_content.strip():
            await reasoning.stage("✍️ 生成结论", "正在为您整理分析结论...")
            await reasoning.close()
            text = callbacks["append_followup_if_needed"](cleaned_planner_content, message.content)
            has_chart = cl.user_session.get("has_chart_generated", False) or False
            text = _prepend_thinking_summary(text, message.content, has_chart=has_chart)
            await callbacks["stream_text_to_message"](text, stream_msg=stream_msg)
            messages.append(AIMessage(content=text))
            cl.user_session.set("messages", messages)
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return

        # 首轮 answer_chain（真实流式输出）
        try:
            _compress_messages(messages)
            await reasoning.stage("✍️ 生成结论", "正在为您生成分析结论...")
            has_chart = cl.user_session.get("has_chart_generated", False) or False
            stream_msg.content = _prepend_thinking_summary(stream_msg.content, message.content, has_chart=has_chart)
            if stream_msg.content:
                await stream_msg.update()
            await _maybe_close_reasoning(reasoning)
            text = await asyncio.wait_for(
                callbacks["astream_answer_chain_to_message"](answer_chain, {"messages": messages}, stream_msg),
                timeout=60,
            )
        except Exception as e:
            await reasoning.line(f"❌ 生成回答失败：{str(e)[:200]}")
            await _maybe_close_reasoning(reasoning)
            await cl.Message(content=_friendly_llm_error_text(e)).send()
            print(f"Answer 首轮调用失败：{e}")

            traceback.print_exc()
            cl.user_session.set("messages", messages)
            _log_query_exit(query_start_time, session_id, query_summary, "fail")
            return
        text = _prepend_thinking_summary(
            _sanitize_display_text(callbacks["append_followup_if_needed"](text or "", message.content)),
            message.content,
            has_chart=has_chart,
        )
        if text:
            stream_msg.content = text
            await stream_msg.update()
            messages.append(AIMessage(content=text))
        cl.user_session.set("messages", messages)
        _log_query_exit(query_start_time, session_id, query_summary, "ok")
        return

    messages.append(planner_msg)

    max_iterations = 5
    iteration = 0
    forced_final_text = None
    answer_generated = False  # 标记是否已在循环内成功生成最终回答

    while planner_msg.tool_calls and iteration < max_iterations:
        iteration += 1
        tool_count = len(planner_msg.tool_calls)
        tool_names_display = "、".join(
            TOOL_DISPLAY_NAMES.get(tc["name"], tc["name"]) for tc in planner_msg.tool_calls
        )
        await reasoning.stage("📡 查询数据", f"补充查询更多数据：{tool_names_display}")
        await reasoning.stage("📡 查询数据", f"第 {iteration} 轮补充查询中...")

        forced_final_text, ree, warning_bundles = await _run_tool_round(
            planner_msg, tools, messages, message.content, iteration, callbacks
        )
        if ree:
            await cl.send_window_message(ree)

        # 若本轮同时调用了应急响应工具，优先让 planner 综合应急判定与预警信息生成回答，
        # 而不是直接走强制收口或预警专用组装答案。
        has_emergency_response_tool = not _tool_call_names(planner_msg).isdisjoint(EMERGENCY_RESPONSE_TOOL_NAMES)
        if forced_final_text and has_emergency_response_tool:
            print("[process_message] 本轮同时调用应急响应工具，忽略强制收口文本，由 planner 综合生成回答。")
            forced_final_text = None

        if forced_final_text:
            await reasoning.stage("✅ 评估结果", "已获取足够数据，正在为您整理定制化结论...")
            has_chart = cl.user_session.get("has_chart_generated", False) or False
            forced_final_text = _prepend_thinking_summary(forced_final_text, message.content, has_chart=has_chart)
            await _maybe_close_reasoning(reasoning)
            await callbacks["stream_text_to_message"](forced_final_text, stream_msg=stream_msg)
            messages.append(AIMessage(content=forced_final_text))
            print("\n=== 使用定制化收口答案，退出循环 ===\n")
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            answer_generated = True
            break

        if warning_bundles and not has_emergency_response_tool:
            await reasoning.stage("✅ 评估结果", "预警数据已获取完整，正在整理预警清单并生成防范建议...")
            await reasoning.stage("✍️ 生成结论", "正在生成回答...")
            try:
                final_text = await _generate_warning_hybrid_answer(
                    answer_chain, warning_bundles, message.content, callbacks
                )
                final_text = _sanitize_display_text(
                    callbacks["append_followup_if_needed"](final_text or "", message.content)
                )
                await reasoning.close()
            except Exception as e:
                await reasoning.line(f"❌ 预警专用回答生成失败：{str(e)[:200]}")
                await reasoning.close()
                print(f"预警专用回答生成失败：{e}")
                merged = _merge_warning_bundles(warning_bundles)
                records = _filter_warning_records_for_user(merged["records"], message.content)
                table_text = _build_warning_table_markdown(records, merged["title"]) if records else ""
                content_lines = [
                    f"{idx}. {_sanitize_display_text(str(record.get('content') or '').strip())}"
                    for idx, record in enumerate(records, 1)
                    if str(record.get("content") or "").strip()
                ]
                if records:
                    final_text = (
                        "【核心结论】\n"
                        "智能摘要生成超时，以下先提供代码生成的预警清单和原始预警内容。"
                        f"\n\n{table_text}"
                    )
                    if content_lines:
                        final_text += "\n\n【预警内容】\n" + "\n".join(content_lines)
                else:
                    final_text = "【核心结论】\n未检索到符合条件的预警记录。"

            has_chart = cl.user_session.get("has_chart_generated", False) or False
            final_text = _prepend_thinking_summary(final_text, message.content, has_chart=has_chart)
            await callbacks["stream_text_to_message"](final_text, stream_msg=stream_msg)
            messages.append(AIMessage(content=final_text))
            cl.user_session.set("messages", messages)
            print("\n=== 使用预警专用组装答案，退出循环 ===\n")
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            answer_generated = True
            break

        await reasoning.stage("✅ 评估结果", "已获取数据，正在判断能否完整回答您的问题...")
        await reasoning.stage("✅ 评估结果", "正在评估是否需要补充查询...")

        print(f"\n=== 第 {iteration} 轮 Planner 调用前 ===")
        print(f"Messages 数量：{len(messages)}")
        for i, msg in enumerate(messages):
            print(f"Message {i}: {type(msg).__name__} - {str(msg)[:100]}")
        print("======================\n")

        try:
            _compress_messages(messages)
            planner_msg = await callbacks["astream_planner_think"](
                planner_chain, {"messages": messages}, reasoning
            )
            planner_msg = _ensure_tool_calls_from_content(planner_msg)

            print(f"\n=== 第 {iteration} 轮 Planner 调用结果 ===")
            print(f"Planner Message: {planner_msg}")
            print(f"Tool Calls: {planner_msg.tool_calls if hasattr(planner_msg, 'tool_calls') else 'N/A'}")
            print(f"Content: {planner_msg.content}")
            print(f"========================\n")

            if not planner_msg.tool_calls:
                cleaned_planner_content = _sanitize_display_text(planner_msg.content or "")
                if cleaned_planner_content.strip():
                    # 二轮 planner 已生成完整回答，直接复用，避免 answer_chain 超时/格式异常
                    await reasoning.stage("✍️ 生成结论", "正在整理回答...")
                    await reasoning.close()
                    text = callbacks["append_followup_if_needed"](cleaned_planner_content, message.content)
                    has_chart = cl.user_session.get("has_chart_generated", False) or False
                    text = _prepend_thinking_summary(text, message.content, has_chart=has_chart)
                    await callbacks["stream_text_to_message"](text, stream_msg=stream_msg)
                    messages.append(AIMessage(content=text))
                    print("\n=== 复用 Planner 回答，退出循环 ===\n")
                    answer_generated = True
                    break

                try:
                    _compress_messages(messages)
                    await reasoning.stage("✍️ 生成结论", "正在为您生成分析结论...")
                    has_chart = cl.user_session.get("has_chart_generated", False) or False
                    stream_msg.content = _prepend_thinking_summary(stream_msg.content, message.content, has_chart=has_chart)
                    if stream_msg.content:
                        await stream_msg.update()
                    await _maybe_close_reasoning(reasoning)
                    text = await asyncio.wait_for(
                        callbacks["astream_answer_chain_to_message"](answer_chain, {"messages": messages}, stream_msg),
                        timeout=60,
                    )
                except Exception as e:
                    await reasoning.line(f"❌ 循环生成回答失败：{str(e)[:200]}")
                    # 不 close，让循环外兜底继续复用 reasoning
                    print(f"Answer 循环调用失败：{e}")
                    # 不 break，继续走循环外兜底
                else:
                    text = _prepend_thinking_summary(
                        _sanitize_display_text(callbacks["append_followup_if_needed"](text or "", message.content)),
                        message.content,
                        has_chart=has_chart,
                    )
                    if text:
                        stream_msg.content = text
                        await stream_msg.update()
                        messages.append(AIMessage(content=text))
                    print("\n=== 回答器已生成最终回答，退出循环 ===\n")
                    answer_generated = True
                    break

            messages.append(planner_msg)

            if planner_msg.tool_calls and stream_msg.content.strip():
                stream_msg.content = ""
                await stream_msg.update()

        except Exception as e:
            error_msg = _friendly_llm_error_text(e)
            await reasoning.line(f"❌ 调用失败：{str(e)[:200]}")
            await _maybe_close_reasoning(reasoning)
            await cl.Message(content=error_msg).send()
            print(f"LLM 调用失败：{e}")

            traceback.print_exc()
            print(f"Messages 内容：{messages}")
            _log_query_exit(query_start_time, session_id, query_summary, "fail")
            break  # 中断循环，避免同一异常重复报错，后续走循环外兜底

    cl.user_session.set("messages", messages)

    # 循环结束后若无确定性最终回答且循环内未成功生成回答，才走兜底
    if not forced_final_text and not answer_generated:
        if not stream_msg.content.strip():
            await reasoning.stage("✍️ 生成结论", "正在整理回答...")
        try:
            _compress_messages(messages)
            if not reasoning._closed:
                await reasoning.stage("✍️ 生成结论", "正在为您生成分析结论...")
            has_chart = cl.user_session.get("has_chart_generated", False) or False
            stream_msg.content = _prepend_thinking_summary(stream_msg.content, message.content, has_chart=has_chart)
            if stream_msg.content:
                await stream_msg.update()
            await _maybe_close_reasoning(reasoning)
            text = await asyncio.wait_for(
                callbacks["astream_answer_chain_to_message"](answer_chain, {"messages": messages}, stream_msg),
                timeout=60,
            )
        except Exception as e:
            await reasoning.line(f"❌ 兜底回答失败：{str(e)[:200]}")
            await _maybe_close_reasoning(reasoning)
            await cl.Message(content="当前查询未能获得有效结果，请换个问法或稍后重试。").send()
            print(f"兜底回答调用失败：{e}")
            _log_query_exit(query_start_time, session_id, query_summary, "fail")
            return
        if text:
            text = callbacks["append_followup_if_needed"](text, message.content)
            text = _prepend_thinking_summary(text, message.content, has_chart=has_chart)
            stream_msg.content = text
            await stream_msg.update()
            messages.append(AIMessage(content=text))
            cl.user_session.set("messages", messages)

    _log_query_exit(query_start_time, session_id, query_summary, "ok")
    await reasoning.close()
    return
