"""简单天气规则路由测试：高置信度问题跳过 planner，误判问题交回 planner。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import message_orchestrator as mo  # noqa: E402


class TestRouteSimpleWeatherQuery:
    @pytest.mark.parametrize(
        "question, expected_tool",
        [
            ("明天天气", "query_rolling_forecast"),
            ("今天会下雨吗", "query_rolling_forecast"),
            ("梅江会展中心明日天气", "query_decision_weather_for_poi"),
            ("后天天津气温", "query_rolling_forecast"),
            ("明天下雨吗", "query_rolling_forecast"),
            ("周末天气预报", "query_rolling_forecast"),
        ],
    )
    def test_routes_simple_weather(self, question, expected_tool):
        route = mo._route_simple_weather_query(question)
        assert route is not None, f"应命中规则路由：{question}"
        assert route[0] == expected_tool

    @pytest.mark.parametrize(
        "question",
        [
            "海河流域明天天气",      # 流域：走专用河系工具
            "大清河明天有雨吗",       # 裸河名：流域
            "明天适合跑步吗",          # 决策类
            "梅江会展中心周边适合跑步吗",  # 决策类
            "你好",                  # 无天气词
            "什么是海河流域",          # 无天气词
            "今天",                  # 无天气词
        ],
    )
    def test_does_not_route_ambiguous(self, question):
        assert mo._route_simple_weather_query(question) is None, f"不应误路由：{question}"


class TestEnforceSimpleWeatherRoute:
    def test_sets_tool_call_and_clears_content(self):
        from langchain_core.messages import AIMessage

        msg = AIMessage(content="原内容")
        route = ("query_rolling_forecast", {"user_query": "明天天气", "regions": ""})
        out = mo._enforce_simple_weather_route(msg, "明天天气", route)
        assert out.tool_calls, "应设置 tool_calls"
        assert out.tool_calls[0]["name"] == "query_rolling_forecast"
        assert out.content == ""


class TestIsBasinOrRiverQuery:
    @pytest.mark.parametrize(
        "question",
        ["海河流域明天天气", "大清河明天有雨吗", "流域未来三天", "子牙河天气"],
    )
    def test_basin_and_river_queries(self, question):
        assert mo._is_basin_or_river_query(question) is True

    @pytest.mark.parametrize(
        "question",
        ["明天天气", "梅江会展中心天气", "天津天气"],
    )
    def test_non_basin_queries(self, question):
        assert mo._is_basin_or_river_query(question) is False