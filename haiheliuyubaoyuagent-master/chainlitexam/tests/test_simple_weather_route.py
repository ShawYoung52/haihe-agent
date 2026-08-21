"""简单天气规则路由测试：高置信度问题跳过 planner，误判问题交回 planner。"""

from __future__ import annotations

import sys
import ast
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import message_orchestrator as mo  # noqa: E402
from tools.request_intent_policy import SUPPORTED_ROLLING_FORECAST_REGIONS  # noqa: E402


class TestRouteSimpleWeatherQuery:
    @pytest.mark.parametrize(
        "question, expected_tool",
        [
            ("明天天气", "query_rolling_forecast"),
            ("明天天气怎么样", "query_rolling_forecast"),
            ("明天天气如何", "query_rolling_forecast"),
            ("今天会下雨吗", "query_rolling_forecast"),
            ("梅江会展中心明日天气", "query_decision_weather_for_poi"),
            ("天津二十中学明天天气", "query_decision_weather_for_poi"),
            ("天津三中明天天气如何", "query_decision_weather_for_poi"),
            ("天津站明天天气", "query_decision_weather_for_poi"),
            ("天津滨海国际机场明天天气", "query_decision_weather_for_poi"),
            ("盘山风景名胜区明天天气", "query_decision_weather_for_poi"),
            ("后天天津气温", "query_rolling_forecast"),
            ("明天下雨吗", "query_rolling_forecast"),
            ("周末天气预报", "query_rolling_forecast"),
            ("天津在未来24小时会下雨吗", "query_rolling_forecast"),
            ("天津到明天会下雨吗", "query_rolling_forecast"),
            ("明天适合去蓟州游玩吗？", "query_rolling_forecast"),
            ("天津滨海新区明天适合游玩吗", "query_rolling_forecast"),
            ("天津蓟州区明天适合游玩吗", "query_rolling_forecast"),
            ("明天适合去天津之眼游玩吗", "query_decision_weather_for_poi"),
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
            "明天去郊游天气怎么样",      # 决策类（去）
            "梅江会展中心周边适合跑步吗",  # 决策类
            "你好",                  # 无天气词
            "什么是海河流域",          # 无天气词
            "今天",                  # 无天气词
            "暴雨预警",               # 预警类：不走滚动预报
            "对比天津现在和明天的天气",  # 当前+未来混合：完整 Planner
            "天津当前气温和未来降雨如何",
            "现在与后天的气温差多少",
            "中心城区和梅江会展中心明天气温对比",
            "全市各站和天津站明天气温对比",
            "上海市区明天适合游玩吗",
            "雄安新区明天适合游玩吗",
            "明天去蓟州旅游局办事需要什么材料？",
            "明天到武清跑步比赛几点开始？",
            "明天有暴雨预警，适合去蓟州游玩吗？",
            "明天适合去蓟州溶洞游玩吗？",
            "明天去蓟州骑行路线怎么走？",
            "明天蓟州旅游景点有哪些？",
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


def test_supported_rolling_regions_stay_in_sync_with_mcp_service():
    service_path = (
        Path(__file__).resolve().parents[2]
        / "haihe-weather-analyzer-mcp"
        / "rolling_forecast_service.py"
    )
    tree = ast.parse(service_path.read_text(encoding="utf-8"))
    coords = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "ROLLING_FORECAST_COORDS"
            for target in node.targets
        ):
            coords = ast.literal_eval(node.value)
            break

    assert coords is not None
    assert set(SUPPORTED_ROLLING_FORECAST_REGIONS) == set(coords)
