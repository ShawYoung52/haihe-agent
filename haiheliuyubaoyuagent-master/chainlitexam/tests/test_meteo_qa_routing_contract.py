"""黄金问法的确定性路由兼容契约。

只校验代码能够确定的简单路由与主动工具过滤；完整 Planner 路径仍由 Prompt
规则和端到端测试覆盖，不在这里伪造 LLM 选择。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import message_orchestrator as mo  # noqa: E402
from tools.active_tool_router import ActiveToolRouter  # noqa: E402


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "meteo_qa_cases.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
FULL_PLANNER_CASE_IDS = {
    "accum_rain",
    "rainstorm_impact",
    "rainstorm_impact_arrival",
    "admin_division",
    "emergency_response",
    "river_network",
    "downstream_river",
    "region_rainfall",
    "station_img",
    "gis_layer",
    "missing_station_data",
    "follow_up_question",
}
FUTURE_HOUR_CASE_IDS = {"hourly_forecast", "decision_poi_rain_hours"}
SIMPLE_ROUTE_CASE_IDS = {
    "multi_day",
    "precip_start_end",
    "temp",
    "wind",
    "visibility",
    "decision_poi",
    "tool_timeout",
    "same_session",
    "cross_session",
}
ACTIVE_FILTER_CASE_IDS = {
    "current_obs",
    "station_real_time_obs",
    "precip_end_time",
    "effective_warning",
    "history_warning",
    "national_warning",
    "basin_areal",
    "basin_future_weather",
    "water_level",
}


def _build_router() -> ActiveToolRouter:
    tool_names = {
        name
        for case in CASES
        for key in ("allowed", "required", "forbidden")
        for name in case["expected_tools"][key]
    }
    tool_names.update(mo.TOOL_DISPLAY_NAMES)
    tools = [SimpleNamespace(name=name) for name in sorted(tool_names)]
    return ActiveToolRouter(
        tools=tools,
        full_chain=object(),
        build_chain=lambda selected: object(),
        candidate_index=None,
    )


ROUTER = _build_router()


def test_every_golden_question_has_an_explicit_route_mode_contract():
    groups = (
        FUTURE_HOUR_CASE_IDS,
        SIMPLE_ROUTE_CASE_IDS,
        ACTIVE_FILTER_CASE_IDS,
        FULL_PLANNER_CASE_IDS,
    )
    expected_ids = {case["id"] for case in CASES}
    assert set().union(*groups) == expected_ids
    assert sum(len(group) for group in groups) == len(expected_ids)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_golden_question_deterministic_route_respects_tool_contract(case):
    """错误工具、禁用工具或漏掉必需工具时必须失败。"""
    case_id = case["id"]
    question = case["question"]
    decision = None
    if mo._is_future_hour_weather_query(question):
        planner_msg = mo._enforce_initial_future_hour_weather_route(
            AIMessage(content=""), question
        )
        route_source = "future_hour"
        selected = {call["name"] for call in planner_msg.tool_calls}
    elif (simple_route := mo._route_simple_weather_query(question)):
        route_source = "simple"
        selected = {simple_route[0]}
    else:
        decision = ROUTER.select(question)
        route_source = f"active:{decision.mode}:{decision.query_type}:{decision.reason}"
        selected = set(decision.tool_names)

    expected = case["expected_tools"]
    allowed = set(expected["allowed"])
    required = set(expected["required"])
    forbidden = set(expected["forbidden"])

    if case_id in FULL_PLANNER_CASE_IDS:
        assert decision is not None and decision.mode == "full"
        assert decision.requires_tool is False
        assert not selected, f"{case_id} 必须保留完整 Planner，实际经 {route_source} 选中 {sorted(selected)}"
        return

    expected_mode = (
        "future_hour"
        if case_id in FUTURE_HOUR_CASE_IDS
        else "simple"
        if case_id in SIMPLE_ROUTE_CASE_IDS
        else "active"
    )
    assert route_source == expected_mode or route_source.startswith(f"{expected_mode}:"), (
        f"{case_id} 路由模式改变：期望 {expected_mode}，实际 {route_source}"
    )
    if case_id in ACTIVE_FILTER_CASE_IDS:
        assert decision is not None and decision.mode == "filtered"
        assert decision.requires_tool is True
    assert selected, f"{case_id} 不得退化为完整 Planner：{route_source}"
    assert selected.isdisjoint(forbidden), (
        f"{case['id']} 经 {route_source} 选中了禁用工具：{sorted(selected & forbidden)}"
    )
    assert selected <= allowed, (
        f"{case['id']} 经 {route_source} 选中未授权工具：{sorted(selected - allowed)}"
    )
    assert required <= selected, (
        f"{case['id']} 经 {route_source} 漏掉必需工具：{sorted(required - selected)}"
    )
