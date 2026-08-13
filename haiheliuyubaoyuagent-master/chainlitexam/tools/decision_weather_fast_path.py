"""点位决策天气快捷路径。

本模块只保留业务处理和最终回答发送，不创建状态消息、思考步骤或工具进度卡片。
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import Any, Callable

import chainlit as cl
from langchain_core.messages import AIMessage, HumanMessage

from tools.decision_weather_core import (
    _compact_decision_forecast_facts,
    _decision_fetch_hazard_context,
    _decision_historical_window_args,
    _decision_pick_first_poi,
    _decision_weather_prefilter,
    _extract_decision_weather_slots,
    _generate_decision_historical_answer_from_raw,
    _generate_decision_weather_answer,
    _is_past_date_forecast_payload,
    _nearest_decision_station,
    _normalize_decision_weather_slots,
    classify_poi_category,
)
from utils.tool_result import _unwrap_tool_result


@dataclass(frozen=True)
class DecisionWeatherRuntime:
    """由编排器注入的通用能力，避免反向导入 message_orchestrator。"""

    find_tool: Callable[..., Any]
    invoke_fast_tool: Callable[..., Any]
    clean_table_cell: Callable[[Any], str]
    sanitize_display_text: Callable[[str], str]
    prepend_thinking_summary: Callable[..., str]


class DecisionWeatherQAService:
    """LLM 抽槽、代码定位与滚动预报查询的无中间前端展示版本。"""

    def __init__(self, answer_chain: Any, tools: list[Any], callbacks: dict[str, Any], runtime: DecisionWeatherRuntime):
        self.answer_chain = answer_chain
        self.tools = tools
        self.callbacks = callbacks
        self.runtime = runtime

    async def try_handle(self, user_text: str, messages: list[Any]) -> bool:
        if not user_text or not _decision_weather_prefilter(user_text):
            return False
        poi_tool = self.runtime.find_tool(self.tools, "search_poi")
        forecast_tool = self.runtime.find_tool(self.tools, "query_rolling_forecast")
        if not poi_tool or not forecast_tool:
            return False

        try:
            slots = await _extract_decision_weather_slots(user_text, self.answer_chain, self.callbacks)
        except Exception as exc:
            print(f"[DecisionWeather] LLM 抽取失败：{exc}")
            return False
        if not bool(slots.get("is_decision_weather")):
            return False

        if bool(slots.get("need_clarification")):
            question = str(slots.get("clarification_question") or "请补充具体位置。").strip()
            await self._emit(question, user_text, messages)
            return True

        normalized = _normalize_decision_weather_slots(slots)
        if normalized.get("error"):
            await self._emit(str(normalized["error"]), user_text, messages)
            return True

        location_name = normalized["location_name"]

        poi_raw = await self.runtime.invoke_fast_tool(
            poi_tool.name, poi_tool, {"keyword": location_name, "size": 5}, user_text
        )
        poi_payload = _unwrap_tool_result(poi_raw)
        poi = _decision_pick_first_poi(poi_payload, location_name)
        if not poi:
            text = f"未检索到“{self.runtime.clean_table_cell(location_name)}”的可用经纬度信息，请换一个更明确的位置名称。"
            await self._emit(text, user_text, messages)
            return True

        poi_lon = float(poi["longitude"])
        poi_lat = float(poi["latitude"])
        nearest = _nearest_decision_station(poi_lon, poi_lat)
        point_name = str(poi.get("name") or location_name)
        poi_address = str(poi.get("address") or "")
        print(
            "[DecisionWeather] POI定位: "
            f"name={point_name}, address={poi_address}, lon={poi_lon}, lat={poi_lat}; "
            f"nearest_region={nearest['region']}, distance_km={nearest['distance_km']:.2f}"
        )

        # POI 地理类型分类 + 周边灾害隐患点（用于回答后追加“注意事项”），预报/历史两分支共用。
        # 无类别不调隐患工具、工具缺失/失败均静默跳过（见 _decision_fetch_hazard_context）。
        category = classify_poi_category(
            point_name, poi_address, poi.get("category_1"), poi.get("category_2")
        )
        hazard_tool = self.runtime.find_tool(self.tools, "query_poi_hazard_reminders")
        hazard_points = await _decision_fetch_hazard_context(
            category, poi_lon, poi_lat, hazard_tool,
            lambda tool, args: self.runtime.invoke_fast_tool(tool.name, tool, args, user_text),
            "DecisionWeather",
        )

        forecast_args = {
            "user_query": user_text,
            "regions": "",
            "lon": poi_lon,
            "lat": poi_lat,
            "point_name": point_name,
        }
        forecast_raw = await self.runtime.invoke_fast_tool(
            forecast_tool.name, forecast_tool, forecast_args, user_text
        )
        forecast_payload = _unwrap_tool_result(forecast_raw)
        if not isinstance(forecast_payload, dict) or forecast_payload.get("api_code") not in (None, "200", 200):
            print(f"[DecisionWeather] forecast raw payload: {forecast_payload}")

        # 历史日期：滚动预报返回 past_date 标记 → 转历史实况查询（回答仍走同一组装函数）
        if _is_past_date_forecast_payload(forecast_payload):
            historical_tool = self.runtime.find_tool(self.tools, "query_poi_historical_weather")
            if historical_tool is None:
                await self._emit("该日期已属历史日期，历史实况查询工具暂不可用，请稍后重试或换用未来日期查询。", user_text, messages)
                return True
            try:
                hist_raw = await self.runtime.invoke_fast_tool(
                    historical_tool.name, historical_tool,
                    _decision_historical_window_args(forecast_payload, poi_lon, poi_lat, point_name),
                    user_text,
                )
                hist_text = await _generate_decision_historical_answer_from_raw(
                    hist_raw, user_text, poi, point_name, normalized["question_type"], self.answer_chain, self.callbacks,
                    poi_category=category, hazard_points=hazard_points,
                )
                final_text = self.runtime.sanitize_display_text(
                    self.callbacks["append_followup_if_needed"](hist_text or "", user_text)
                )
                await self._emit(final_text, user_text, messages, add_summary=True)
                return True
            except Exception as exc:
                print(f"[DecisionWeather] 历史实况查询失败：{exc}")
                await self._emit("历史实况查询遇到异常，请稍后重试或换用未来日期查询。", user_text, messages)
                return True

        facts = _compact_decision_forecast_facts(
            forecast_payload if isinstance(forecast_payload, dict) else {}
        )
        facts["poi"] = {"name": point_name, "address": poi_address, "lon": poi_lon, "lat": poi_lat}
        facts["matched_station"] = nearest
        facts["question_type"] = normalized["question_type"]
        facts["poi_category"] = category
        facts["hazard_points"] = hazard_points

        final_text = await _generate_decision_weather_answer(user_text, facts, self.answer_chain, self.callbacks)
        final_text = self.runtime.sanitize_display_text(
            self.callbacks["append_followup_if_needed"](final_text or "", user_text)
        )
        await self._emit(final_text, user_text, messages, add_summary=True)
        return True

    async def _emit(self, text: str, user_text: str, messages: list[Any], add_summary: bool = False) -> None:
        final_text = self.runtime.prepend_thinking_summary(text, user_text, has_chart=False) if add_summary else text
        await self.callbacks["stream_text_to_message"](final_text)
        messages.extend([HumanMessage(content=user_text), AIMessage(content=final_text)])
        cl.user_session.set("messages", messages)


async def try_decision_weather_fast_path(
    user_text: str,
    answer_chain: Any,
    tools: list[Any],
    messages: list[Any],
    callbacks: dict[str, Any],
    runtime: DecisionWeatherRuntime,
) -> bool:
    """执行点位天气快捷路径；中间过程只写入后台日志。"""
    if not _decision_weather_prefilter(user_text):
        return False
    service = DecisionWeatherQAService(answer_chain=answer_chain, tools=tools, callbacks=callbacks, runtime=runtime)
    try:
        return await service.try_handle(user_text, messages)
    except Exception as exc:
        print(f"[DecisionWeather] fast path 失败，回退通用流程：{exc}")
        traceback.print_exc()
        return False
