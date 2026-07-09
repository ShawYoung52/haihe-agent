"""
点位决策天气工具：将原 DecisionWeatherQAService 中的纯逻辑抽取为可复用的 LangChain Tool。

供 LLM Agent 在需要回答“某个具体点位/场馆/单位附近未来天气如何”时调用；
内部自动完成 POI 检索、代表站匹配、滚动预报查询与格式化回答生成，
不依赖 Chainlit UI，仅返回 Markdown 文本。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

from message_orchestrator import (
    _clean_table_cell,
    _compact_decision_forecast_facts,
    _decision_period_args,
    _decision_pick_first_poi,
    _decision_weather_prefilter,
    _extract_first_json_object,
    _find_tool,
    _nearest_decision_station,
    _normalize_decision_interval,
    _parse_decision_dt,
    _sanitize_display_text,
    _select_decision_fcst_time,
    _unwrap_tool_observation,
)


async def _extract_slots(user_text: str, answer_chain, callbacks: dict) -> dict:
    """使用 LLM 从用户问题中抽取点位决策天气所需槽位。"""
    now = datetime.now()
    prompt = (
        "你是天津气象决策服务问答的结构化抽取器。请判断用户问题是否属于"
        "“具体地点/单位/场馆/学校/医院/设施附近的未来或当前天气决策服务”。\n"
        "普通区域预报（如天津、全市、西青、滨海新区、未来一周天气）不属于本类，返回 is_decision_weather=false。\n"
        "如果属于本类，请抽取位置名称、目标开始时间、目标结束时间、时间步长和问题类型。\n"
        "当前时间为："
        f"{now.strftime('%Y-%m-%d %H:%M:%S')}。\n"
        "时间规则：没有明确时间时，target_start_time 默认为当前时间，target_end_time 默认为当前时间后24小时，interval_hours=12；"
        "明日/明天为明天00:00到后天00:00，interval_hours=24；"
        "未来N小时为当前时间到N小时后，interval_hours优先取N，若N不在1/3/6/12/24中则选最接近值；"
        "高考期间、国庆期间等公共事件可给出具体时间，但必须标明 time_basis=公共常识、time_confidence=medium；"
        "展会期间、会议期间、考试期间等没有明确日期的事件必须 need_clarification=true。\n"
        "滚动预报只适合未来时段；若目标时段已经过去或无法确定，need_clarification=true。\n"
        "只返回 JSON，不要输出解释。格式：\n"
        "{\n"
        '  "is_decision_weather": true,\n'
        '  "location_name": "梅江会展中心",\n'
        '  "target_start_time": "YYYY-MM-DD HH:MM:SS",\n'
        '  "target_end_time": "YYYY-MM-DD HH:MM:SS",\n'
        '  "interval_hours": 12,\n'
        '  "question_type": "general_weather|rain_now|rain_next_hours|event_weather|visibility|temperature|wind|activity",\n'
        '  "time_basis": "用户明示|相对时间|公共常识|无法确定",\n'
        '  "time_confidence": "high|medium|low",\n'
        '  "need_clarification": false,\n'
        '  "clarification_question": ""\n'
        "}\n\n"
        f"用户问题：{user_text}"
    )
    ainvoke_chain = callbacks.get("ainvoke_chain")
    if not ainvoke_chain:
        raise RuntimeError("callbacks 中缺少 ainvoke_chain")
    result = await ainvoke_chain(answer_chain, {"messages": [HumanMessage(content=prompt)]})
    content = getattr(result, "content", None) or str(result)
    return _extract_first_json_object(content)


def _normalize_slots(slots: dict) -> dict:
    """校验并规范化 LLM 抽取的槽位。"""
    location_name = str(slots.get("location_name") or "").strip()
    if not location_name:
        return {"error": "请补充要查询天气的位置名称，例如学校、场馆、医院或具体单位。"}

    now = datetime.now()
    target_start = _parse_decision_dt(slots.get("target_start_time")) or now
    target_end = _parse_decision_dt(slots.get("target_end_time")) or (target_start + timedelta(hours=24))
    if target_end <= target_start:
        return {"error": "请确认查询的结束时间需要晚于开始时间。"}

    if target_end <= now:
        return {"error": "该时段已经过去，滚动预报仅支持未来天气查询；如需历史天气，需要改用历史实况或历史预报数据。"}

    max_end = now + timedelta(hours=240)
    if target_start > max_end:
        return {"error": "当前滚动预报最多支持未来约10天，请缩短或调整查询时段。"}
    if target_end > max_end:
        target_end = max_end

    return {
        "location_name": location_name,
        "target_start": target_start,
        "target_end": target_end,
        "interval": _normalize_decision_interval(slots.get("interval_hours")),
    }


async def _generate_answer(user_text: str, facts: dict, answer_chain, callbacks: dict) -> str:
    """基于业务天气事实生成面向用户的自然语言回答。"""
    business_facts = {
        "位置名称": (facts.get("poi") or {}).get("name") or "该位置",
        "位置地址": (facts.get("poi") or {}).get("address") or "",
        "查询开始时间": facts.get("target_start_time"),
        "查询结束时间": facts.get("target_end_time"),
        "问题类型": facts.get("question_type"),
        "是否有降雨信号": facts.get("has_rain_signal"),
        "累计降水量毫米": facts.get("total_rain_mm"),
        "预报时段": facts.get("periods") or [],
        "数据来源": facts.get("data_source") or "天津市气象台滚动预报",
    }
    prompt = (
        "请仅依据下面 JSON 中的业务天气事实回答用户问题。不要编造未返回的天气、雨量、温度、风力或能见度。\n"
        "严禁输出点位定位过程、经纬度、代表点、工具名、接口名、URL、参数名、query_mode、fcst_time、startPeriod、endPeriod、interval 等技术信息。\n"
        "回答统一采用业务口径：\n"
        "1. 必须先输出【核心结论】，用一句话直接回答天气是否良好、是否有降雨、是否有灾害性天气或是否适合活动。\n"
        "2. 综合天气/活动/考试/会展/节假日类：第二模块用【XX逐日预报】或【XX明日预报】，表格列为：日期｜天气现象｜气温(℃)｜风力（级）｜风向。\n"
        "3. 未来N小时是否下雨类：第二模块用【XX逐小时预报】，表格列为：时段｜天气现象｜气温(℃)｜风力（级）｜风向。\n"
        "4. 当前是否下雨类：核心结论写“当前无降雨/当前正在降雨”；第二模块用【降雨情况】，列出已返回的累计雨量或时段降水，缺失的1小时/3小时/6小时雨量不要编造。\n"
        "5. 风况字段中若同时包含风向和风力，请拆成“风力（级）”和“风向”；无法拆分时可在对应列写原始风况中的可识别部分。\n"
        "6. 末尾只写：数据来源：天津市气象台滚动预报。\n\n"
        f"用户问题：{user_text}\n\n"
        f"业务天气事实 JSON：{json.dumps(business_facts, ensure_ascii=False, default=str)}"
    )
    ainvoke_chain = callbacks.get("ainvoke_chain")
    if not ainvoke_chain:
        raise RuntimeError("callbacks 中缺少 ainvoke_chain")
    result = await ainvoke_chain(answer_chain, {"messages": [HumanMessage(content=prompt)]})
    return getattr(result, "content", None) or str(result)


def build_decision_weather_tools(answer_chain, tools: list, callbacks: dict) -> list:
    """构造点位决策天气工具列表，供 LangChain Agent 绑定。"""

    @tool
    async def query_decision_weather_for_poi(user_text: str) -> str:
        """回答具体点位（POI）附近的决策天气问题。

        适用于用户询问某个具体地点、场馆、单位、学校、医院或设施附近的
        当前或未来天气（如是否下雨、气温、风力、是否适合活动等）。

        调用本工具时，只需传入用户原始问题文本；工具内部会自动完成：
        1. 意图前置过滤；
        2. 使用 LLM 抽取位置名称、目标时段、问题类型等槽位；
        3. POI 检索与定位；
        4. 匹配最近的滚动预报代表区域；
        5. 调用滚动预报数据；
        6. 格式化并生成面向用户的 Markdown 回答。

        如果问题不属于点位决策天气范围，或缺少必要的位置/时间信息，
        工具会返回中文提示说明。

        Args:
            user_text: 用户的原始中文问题，例如“梅江会展中心明天天气怎么样”。

        Returns:
            面向用户的 Markdown 格式天气回答字符串。
        """
        try:
            if not user_text or not _decision_weather_prefilter(user_text):
                return (
                    "该问题暂不属于具体点位决策天气查询范围。"
                    "请提供类似“XX会展中心明天天气如何”这样包含具体地点和天气意图的问题。"
                )

            poi_tool = _find_tool(tools, "search_poi")
            forecast_tool = _find_tool(tools, "query_rolling_forecast")
            if not poi_tool or not forecast_tool:
                return "点位天气查询所需工具暂时不可用，请稍后重试。"

            try:
                slots = _extract_slots(user_text, answer_chain, callbacks)
            except Exception as exc:
                print(f"[DecisionWeatherTool] LLM 抽取失败：{exc}")
                return "问题理解失败，请尝试换一种更明确的说法。"

            if not bool(slots.get("is_decision_weather")):
                return "该问题看起来不是具体点位的天气问题，请补充具体地点或天气时段。"

            if bool(slots.get("need_clarification")):
                question = str(slots.get("clarification_question") or "请补充具体位置和查询时段。").strip()
                return question

            normalized = _normalize_slots(slots)
            if normalized.get("error"):
                return normalized["error"]

            location_name = normalized["location_name"]
            target_start = normalized["target_start"]
            target_end = normalized["target_end"]
            interval = normalized["interval"]
            fcst_time = _select_decision_fcst_time()
            start_period, end_period = _decision_period_args(fcst_time, target_start, target_end)

            print(
                "[DecisionWeatherTool] normalized time: "
                f"target_start={target_start}, target_end={target_end}, "
                f"interval={interval}, fcst_time={fcst_time}, "
                f"startPeriod={start_period}, endPeriod={end_period}"
            )

            poi_raw = await poi_tool.ainvoke({"keyword": location_name, "size": 5})
            poi_payload = _unwrap_tool_observation(poi_raw)
            poi = _decision_pick_first_poi(poi_payload if isinstance(poi_payload, dict) else {})
            if not poi:
                return f"未检索到“{_clean_table_cell(location_name)}”的可用经纬度信息，请换一个更明确的位置名称。"

            poi_lon = float(poi["longitude"])
            poi_lat = float(poi["latitude"])
            nearest = _nearest_decision_station(poi_lon, poi_lat)
            point_name = str(poi.get("name") or location_name)
            poi_address = str(poi.get("address") or "")

            print(
                "[DecisionWeatherTool] POI定位: "
                f"name={point_name}, address={poi_address}, lon={poi_lon}, lat={poi_lat}; "
                f"nearest_region={nearest['region']}, nearest_lon={nearest['lon']}, "
                f"nearest_lat={nearest['lat']}, distance_km={nearest['distance_km']:.2f}"
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
            print(f"[DecisionWeatherTool] query_rolling_forecast args: {json.dumps(forecast_args, ensure_ascii=False)}")

            forecast_raw = await forecast_tool.ainvoke(forecast_args)
            forecast_payload = _unwrap_tool_observation(forecast_raw)
            if not isinstance(forecast_payload, dict) or forecast_payload.get("api_code") not in (None, "200", 200):
                print(f"[DecisionWeatherTool] forecast raw payload: {forecast_payload}")

            facts = _compact_decision_forecast_facts(
                forecast_payload if isinstance(forecast_payload, dict) else {},
                target_start,
                target_end,
            )
            facts["poi"] = {
                "name": point_name,
                "address": poi_address,
                "lon": poi_lon,
                "lat": poi_lat,
            }
            facts["matched_station"] = nearest
            facts["question_type"] = slots.get("question_type") or "general_weather"

            final_text = await _generate_answer(user_text, facts, answer_chain, callbacks)
            append_followup = callbacks.get("append_followup_if_needed", lambda t, u: t)
            final_text = _sanitize_display_text(append_followup(final_text or "", user_text))
            return final_text

        except Exception as exc:
            print(f"[DecisionWeatherTool] 执行异常：{exc}")
            import traceback
            traceback.print_exc()
            return "点位天气查询遇到异常，请稍后重试或换一种问法。"

    return [query_decision_weather_for_poi]
