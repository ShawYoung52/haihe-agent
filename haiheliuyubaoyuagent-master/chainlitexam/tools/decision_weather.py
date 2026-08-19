"""点位决策天气 Planner 工具。

供 LLM Agent 在需要回答“某个具体点位/场馆/单位附近未来天气如何”时调用；
内部自动完成 POI 检索、代表站匹配、滚动预报查询与格式化回答生成。
快捷路径实现位于 ``tools.decision_weather_fast_path``，两者共享核心逻辑。
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from tools.decision_weather_core import (
    _build_ec_rain_answer_text,
    _compact_decision_forecast_facts,
    _decision_fetch_hazard_context,
    _decision_fetch_water_level,
    _decision_historical_window_args,
    _decision_pick_first_poi,
    _decision_point_display_name,
    _decision_weather_prefilter,
    _extract_decision_weather_slots,
    _generate_decision_historical_answer_from_raw,
    _generate_decision_weather_answer,
    _is_ec_rain_fallback_payload,
    _is_past_date_forecast_payload,
    _nearest_decision_station,
    _normalize_decision_weather_slots,
    classify_poi_category,
)

from message_orchestrator import _clean_table_cell, _find_tool, _sanitize_display_text
from utils.tool_result import _unwrap_tool_result


def build_decision_weather_tools(answer_chain: Any, tools: list, callbacks: dict) -> list:
    """构造点位决策天气工具列表，供 LangChain Agent 绑定。"""

    @tool
    async def query_decision_weather_for_poi(user_text: str) -> str:
        """回答具体点位（POI）附近的决策天气问题。

        适用于用户询问某个具体地点、场馆、单位、学校、医院或设施附近的
        当前或未来天气（如是否下雨、气温、风力、是否适合活动等）。

        调用本工具时，只需传入用户原始问题文本；工具内部会自动完成：
        1. 意图前置过滤；
        2. 使用 LLM 只抽取位置名称和问题类型；
        3. POI 检索与定位；
        4. 匹配最近的滚动预报代表区域；
        5. 将用户原问和 POI 经纬度交给滚动预报服务，由服务统一计算时区、起报时次和查询窗口；
        6. 格式化并生成面向用户的 Markdown 回答。

        本工具已完整封装上述流程。Planner 选择本工具后，不得同轮并列调用
        search_poi、query_rolling_forecast、get_server_time 或 analyze_rainfall_by_time。

        如果问题不属于点位决策天气范围，或缺少必要的位置信息，
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
                slots = await _extract_decision_weather_slots(user_text, answer_chain, callbacks)
            except Exception as exc:
                print(f"[DecisionWeatherTool] LLM 抽取失败：{exc}")
                return "问题理解失败，请尝试换一种更明确的说法。"

            if not slots.get("is_decision_weather"):
                return "该问题看起来不是具体点位的天气问题，请补充具体地点。"

            if slots.get("need_clarification"):
                return str(slots.get("clarification_question") or "请补充具体位置。").strip()

            normalized = _normalize_decision_weather_slots(slots)
            if normalized.get("error"):
                return normalized["error"]

            location_name = normalized["location_name"]

            poi_raw = await poi_tool.ainvoke({"keyword": location_name, "size": 5})
            poi_payload = _unwrap_tool_result(poi_raw)
            poi = _decision_pick_first_poi(poi_payload, location_name)
            if not poi:
                return f"未检索到“{_clean_table_cell(location_name)}”的可用经纬度信息，请换一个更明确的位置名称。"

            poi_lon = float(poi["longitude"])
            poi_lat = float(poi["latitude"])
            nearest = _nearest_decision_station(poi_lon, poi_lat)
            point_name = _decision_point_display_name(poi.get("name"), location_name, poi_payload.get("match_type"))
            poi_address = str(poi.get("address") or "")

            print(
                "[DecisionWeatherTool] POI定位: "
                f"name={point_name}, address={poi_address}, lon={poi_lon}, lat={poi_lat}; "
                f"nearest_region={nearest['region']}, nearest_lon={nearest['lon']}, "
                f"nearest_lat={nearest['lat']}, distance_km={nearest['distance_km']:.2f}"
            )

            # POI 地理类型分类 + 周边灾害隐患点（用于回答后追加“注意事项”），预报/历史两分支共用。
            # 无类别不调隐患工具、工具缺失/失败均静默跳过（见 _decision_fetch_hazard_context）。
            category = classify_poi_category(
                point_name, poi_address, poi.get("category_1"), poi.get("category_2")
            )
            hazard_tool = _find_tool(tools, "query_poi_hazard_reminders")
            hazard_points = await _decision_fetch_hazard_context(
                category, poi_lon, poi_lat, hazard_tool,
                lambda tool, args: tool.ainvoke(args), "DecisionWeatherTool",
            )
            # 水库类别：查 14所 水库水位（data_type=reservoir），数值来自接口、不编造
            water_level_tool = _find_tool(tools, "query_water_level")
            water_level_info = (
                await _decision_fetch_water_level(
                    point_name, water_level_tool,
                    lambda tool, args: tool.ainvoke(args), "DecisionWeatherTool",
                )
                if category == "reservoir" else None
            )

            forecast_args = {
                "user_query": user_text,
                "regions": "",
                "lon": poi_lon,
                "lat": poi_lat,
                "point_name": point_name,
            }
            print(f"[DecisionWeatherTool] query_rolling_forecast args: {json.dumps(forecast_args, ensure_ascii=False)}")

            forecast_raw = await forecast_tool.ainvoke(forecast_args)
            forecast_payload = _unwrap_tool_result(forecast_raw)
            if not isinstance(forecast_payload, dict) or forecast_payload.get("api_code") not in (None, "200", 200):
                print(f"[DecisionWeatherTool] forecast raw payload: {forecast_payload}")

            # 历史日期：滚动预报返回 past_date 标记 → 转历史实况查询（回答仍走同一组装函数）
            if _is_past_date_forecast_payload(forecast_payload):
                historical_tool = _find_tool(tools, "query_poi_historical_weather")
                if historical_tool is None:
                    return "该日期已属历史日期，历史实况查询工具暂不可用，请稍后重试或换用未来日期查询。"
                try:
                    hist_raw = await historical_tool.ainvoke(
                        _decision_historical_window_args(forecast_payload, poi_lon, poi_lat, point_name)
                    )
                    hist_text = await _generate_decision_historical_answer_from_raw(
                        hist_raw, user_text, poi, point_name, normalized["question_type"], answer_chain, callbacks,
                        poi_category=category, hazard_points=hazard_points, water_level_info=water_level_info,
                    )
                    append_followup = callbacks.get("append_followup_if_needed", lambda t, u: t)
                    return _sanitize_display_text(append_followup(hist_text or "", user_text))
                except Exception as exc:
                    print(f"[DecisionWeatherTool] 历史实况查询失败：{exc}")
                    import traceback
                    traceback.print_exc()
                    return "历史实况查询遇到异常，请稍后重试或换用未来日期查询。"

            # 超滚动预报 240h 的点位日期：MCP 已用 EC 降水回退 → 只讲降雨的确定性回答
            if _is_ec_rain_fallback_payload(forecast_payload):
                ec_text = _build_ec_rain_answer_text(forecast_payload, point_name)
                append_followup = callbacks.get("append_followup_if_needed", lambda t, u: t)
                return _sanitize_display_text(append_followup(ec_text, user_text))

            facts = _compact_decision_forecast_facts(
                forecast_payload if isinstance(forecast_payload, dict) else {}
            )
            facts["poi"] = {
                "name": point_name,
                "address": poi_address,
                "lon": poi_lon,
                "lat": poi_lat,
            }
            facts["matched_station"] = nearest
            facts["question_type"] = normalized["question_type"]
            facts["poi_category"] = category
            facts["hazard_points"] = hazard_points
            facts["water_level_info"] = water_level_info

            final_text = await _generate_decision_weather_answer(user_text, facts, answer_chain, callbacks)
            append_followup = callbacks.get("append_followup_if_needed", lambda t, u: t)
            return _sanitize_display_text(append_followup(final_text or "", user_text))

        except Exception as exc:
            print(f"[DecisionWeatherTool] 执行异常：{exc}")
            import traceback
            traceback.print_exc()
            return "点位天气查询遇到异常，请稍后重试或换一种问法。"

    return [query_decision_weather_for_poi]
