"""点位决策天气快捷路径。

本模块只保留业务处理和最终回答发送，不创建状态消息、思考步骤或工具进度卡片。
"""
from __future__ import annotations

import json
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import chainlit as cl
from langchain_core.messages import AIMessage, HumanMessage

from tools.decision_weather_core import (
    _compact_decision_forecast_facts,
    _decision_hourly_window,
    _decision_period_args,
    _decision_pick_first_poi,
    _decision_weather_prefilter,
    _extract_decision_weather_slots,
    _generate_decision_weather_answer,
    _nearest_decision_station,
    _normalize_decision_weather_slots,
    _select_decision_fcst_time,
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
            question = str(slots.get("clarification_question") or "请补充具体位置和查询时段。").strip()
            await self._emit(question, user_text, messages)
            return True

        hourly_request = _decision_hourly_window(user_text, slots.get("question_type"), datetime.now())
        normalized = _normalize_decision_weather_slots(slots, hourly_request)
        if normalized.get("error"):
            await self._emit(str(normalized["error"]), user_text, messages)
            return True

        location_name = normalized["location_name"]
        target_start = normalized["target_start"]
        target_end = normalized["target_end"]
        interval = normalized["interval"]
        fcst_time = _select_decision_fcst_time()
        start_period, end_period = _decision_period_args(fcst_time, target_start, target_end)
        print(
            "[DecisionWeather] normalized time: "
            f"target_start={target_start}, target_end={target_end}, interval={interval}, "
            f"fcst_time={fcst_time}, startPeriod={start_period}, endPeriod={end_period}"
        )

        poi_raw = await self.runtime.invoke_fast_tool(
            poi_tool.name, poi_tool, {"keyword": location_name, "size": 5}, user_text
        )
        poi_payload = _unwrap_tool_result(poi_raw)
        poi = _decision_pick_first_poi(poi_payload if isinstance(poi_payload, dict) else {})
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
        forecast_raw = await self.runtime.invoke_fast_tool(
            forecast_tool.name, forecast_tool, forecast_args, user_text
        )
        forecast_payload = _unwrap_tool_result(forecast_raw)
        if not isinstance(forecast_payload, dict) or forecast_payload.get("api_code") not in (None, "200", 200):
            print(f"[DecisionWeather] forecast raw payload: {forecast_payload}")

        facts = _compact_decision_forecast_facts(
            forecast_payload if isinstance(forecast_payload, dict) else {}, target_start, target_end, hourly_request
        )
        facts["poi"] = {"name": point_name, "address": poi_address, "lon": poi_lon, "lat": poi_lat}
        facts["matched_station"] = nearest
        facts["question_type"] = hourly_request["mode"] if hourly_request else (slots.get("question_type") or "general_weather")

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
