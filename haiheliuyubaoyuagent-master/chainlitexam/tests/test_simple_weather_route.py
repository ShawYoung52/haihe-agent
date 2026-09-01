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
            ("未来三天的天气怎么样？", "query_rolling_forecast"),
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
            # 裸"天津港"（无"港口/港区"后缀）走点位决策天气——领导问题清单（2026-08-26
            # 行业服务）："天津港明日风力多大"不得落到天津市区代表点。
            ("天津港明日风力多大？", "query_decision_weather_for_poi"),
            ("明天天津港有雨吗", "query_decision_weather_for_poi"),
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


class TestRouteObservationQuery:
    """含"实况/实时/实测/现在/当前"观测词的问法：绝不路由到预报工具（query_rolling_forecast）。

    测试人员反馈"问实况可能出预报"：`_route_simple_weather_query` 原先不检查观测词，
    "今天/今日 + 天气词 + 实况"会被确定性强制到预报工具；"现在/当前"类因不是时间词
    落回 planner 通常答对——表现为"有时/可能"出预报。修复后纯实况问法路由到实况工具。
    """

    @pytest.mark.parametrize(
        "question",
        [
            "今天天气实况",
            "今天降雨实况",
            "今日天气实况",
            "今天气温实况",
            "天津今天天气实况",
            "今天天气实时",
            "今日降水实测",
            "今天当前天气",
        ],
    )
    def test_observation_query_routes_to_observation_tool(self, question):
        route = mo._route_simple_weather_query(question)
        assert route is not None, f"实况问法应命中确定性路由：{question}"
        assert route[0] == "query_current_weather_observation", (
            f"实况问法不得路由到预报工具：{question} -> {route[0]}"
        )

    @pytest.mark.parametrize(
        "question",
        [
            "今天盘山风景名胜区天气实况",   # 点位实况（触发 prefilter）：区域实况工具粒度不对，交回 planner
            "今天天津大学天气实时",         # 点位实况（大学后缀触发 prefilter）
        ],
    )
    def test_poi_observation_query_falls_back_to_planner(self, question):
        assert mo._route_simple_weather_query(question) is None, (
            f"点位实况应交回 planner 用点位实况工具：{question}"
        )

    @pytest.mark.parametrize(
        "question, expected_tool",
        [
            ("今天天气", "query_rolling_forecast"),          # 无观测词：仍是预报
            ("今天天气预报", "query_rolling_forecast"),       # 预报词：仍是预报
            ("今天会下雨吗", "query_rolling_forecast"),       # 既有契约不变
            ("明天天气实况", None),                          # 实况+明天=混合：交回 planner
        ],
    )
    def test_forecast_and_mixed_unchanged(self, question, expected_tool):
        route = mo._route_simple_weather_query(question)
        if expected_tool is None:
            assert route is None, f"实况+未来词混合问法应交回 planner：{question} -> {route}"
        else:
            assert route is not None and route[0] == expected_tool, (
                f"{question} 路由不应被观测词守卫改变 -> {route}"
            )


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
