from __future__ import annotations

import json
from pathlib import Path

from chainlitexam.tests.stubs import ensure_stubs

ensure_stubs()

from tools.active_tool_router import ActiveToolRouter, ToolRouteDecision
from tools.tool_candidate_index import ToolCandidateIndex


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "meteo_qa_cases.json"


class _FakeTool:
    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description or name


def _fixture_tool_names() -> list[str]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    names = {"rag_search"}
    for case in payload["cases"]:
        expected = case["expected_tools"]
        names.update(expected["allowed"])
        names.update(expected["required"])
        names.update(expected["forbidden"])
    return sorted(name for name in names if name)


def _router(*, cache_size: int = 64):
    tools = [_FakeTool(name) for name in _fixture_tool_names()]
    builds: list[tuple[str, ...]] = []

    def build_chain(selected):
        names = tuple(tool.name for tool in selected)
        builds.append(names)
        return {"tools": names, "build": len(builds)}

    full_chain = object()
    router = ActiveToolRouter(
        tools=tools,
        full_chain=full_chain,
        build_chain=build_chain,
        candidate_index=ToolCandidateIndex(tools),
        chain_cache_max_size=cache_size,
    )
    return router, builds


def test_water_level_uses_filtered_domain_without_forecast_tool():
    router, _ = _router()
    decision = router.select("子牙河现在水位多高")
    assert decision.mode == "filtered"
    assert decision.query_type == "water_level"
    assert "query_water_level" in decision.tool_names
    assert "query_rolling_forecast" not in decision.tool_names


def test_current_poi_uses_decision_tool_not_regional_observation():
    """防止把点位当前天气退化成天津/区域聚合实况。"""
    router, _ = _router()
    for question in (
        "梅江会展中心现在天气怎么样",
        "密云水库目前气温多少",
        "天津站现在天气怎么样",
        "海河假日酒店当前气温多少",
        "海河教育园区目前天气如何",
    ):
        decision = router.select(question)
        assert decision.mode == "filtered"
        assert decision.query_type == "decision_poi"
        assert decision.tool_names == ("query_decision_weather_for_poi",)

    river_named_poi = router.select("海河教育园区明天天气怎么样")
    assert river_named_poi.query_type == "decision_poi"
    assert river_named_poi.tool_names == ("query_decision_weather_for_poi",)


def test_weather_intensity_word_is_not_misclassified_as_school_poi():
    router, _ = _router()
    regional = router.select("天津当前有中雨吗")
    assert regional.mode == "filtered"
    assert regional.query_type == "current"
    assert regional.tool_names == ("query_current_weather_observation",)

    school = router.select("天津一中现在天气怎么样")
    assert school.query_type == "decision_poi"
    assert school.tool_names == ("query_decision_weather_for_poi",)


def test_regional_collection_words_are_not_misclassified_as_poi():
    router, _ = _router()
    for question in (
        "中心城区现在气温和风的实况",
        "全市各站现在气温如何",
        "全市站点当前风力怎么样",
    ):
        decision = router.select(question)
        assert decision.mode == "filtered"
        assert decision.query_type == "current"
        assert decision.tool_names == ("query_current_weather_observation",)


def test_bare_river_forecast_never_uses_tianjin_rolling_forecast():
    """裸河名仍是子流域问题，不能套用天津滚动预报。"""
    router, _ = _router()
    for question in ("大清河明天天气如何", "卫河未来24小时降雨如何"):
        decision = router.select(question)
        assert decision.mode in {"filtered", "full"}
        assert "query_rolling_forecast" not in decision.tool_names
        if decision.mode == "filtered":
            assert decision.tool_names == ("query_river_rainfall_forecast",)


def test_highlighted_river_questions_use_unified_river_tool():
    router, _ = _router()
    for question in ("明天泃河有雨吗？", "今天晚上滦河有雨吗？"):
        decision = router.select(question)
        assert decision.mode == "filtered"
        assert decision.query_type == "river_forecast"
        assert decision.tool_names == ("query_river_rainfall_forecast",)
        assert "query_rolling_forecast" not in decision.tool_names


def test_river_forecast_filter_keeps_poi_and_mixed_questions_for_existing_routes():
    router, _ = _router()
    assert router.select("海河教育园区明天天气怎么样").query_type == "decision_poi"
    along_river = router.select("泃河沿线明天降雨如何")
    assert along_river.query_type == "river_forecast"
    assert along_river.tool_names == ("query_river_rainfall_forecast",)
    for question in (
        "泃河明天水位多少",
        "泃河下游明天有雨吗",
        "泃河历史降雨多少",
        "泃河与滦河明天降雨对比",
        "泃河暴雨会影响哪些河流",
    ):
        assert router.select(question).query_type != "river_forecast", question


def test_river_forecast_predicate_rejects_unsafe_mixed_observation_and_non_rain_queries():
    router, _ = _router()
    for question in (
        "海河明天有雨吗，是否启动应急响应",
        "明天泃河有雨吗，天津气温多少",
        "海河今天下了多少雨",
        "海河明天风力多大",
    ):
        decision = router.select(question)
        assert decision.query_type != "river_forecast", question
        assert "query_river_rainfall_forecast" not in decision.tool_names, question


def test_river_forecast_filter_rejects_retrospective_rain_observation_forms():
    """当日已发生的降雨问法不能被未来河流预报工具抢占。"""
    router, _ = _router()
    for question in (
        "海河今天已经下雨了吗",
        "海河今天下雨了吗",
        "海河今日下雨情况",
    ):
        decision = router.select(question)
        assert decision.query_type != "river_forecast", question
        assert "query_river_rainfall_forecast" not in decision.tool_names, question


def test_river_forecast_filter_keeps_observation_and_future_followup_mixed():
    """实况加明天追问是混合问题，不能被单一未来河流工具抢占。"""
    decision = _router()[0].select("海河今天已经下雨了吗，明天还会下吗")

    assert decision.mode == "full"
    assert decision.query_type != "river_forecast"
    assert "query_river_rainfall_forecast" not in decision.tool_names


def test_unsafe_and_mixed_questions_always_use_full_planner():
    router, _ = _router()
    assert router.select("暴雨洪水大概多久到达下游").mode == "full"
    assert router.select("海河流域当前防汛应急响应级别是多少").mode == "full"
    mixed = router.select("查子牙河水位并分析暴雨影响哪些河流")
    assert mixed.mode == "full"


def test_all_emergency_response_phrasings_use_full_planner():
    router, _ = _router()
    for question in (
        "根据当前雨量是否启动防汛响应",
        "当前降雨是否启动响应",
        "目前降水达到几级响应",
        "根据当前水位是否启动响应",
        "当前雨情的响应级别是什么",
    ):
        assert router.select(question).mode == "full", question


def test_river_relation_and_rainfall_impact_questions_use_full_planner():
    router, _ = _router()
    for question in (
        "查询子牙河当前水位并说明它汇入哪条河",
        "子牙河当前水位和流向是什么",
        "子牙河当前水位及直接连接河流",
        "当前降雨会影响哪些河道",
        "当前降水的影响范围有哪些",
        "现在这场雨会波及哪些河流",
        "目前降雨对河流有什么影响",
    ):
        assert router.select(question).mode == "full", question


def test_areal_rainfall_collection_questions_use_dedicated_tool():
    router, _ = _router()
    for question in (
        "当前九分区平均降水量是多少",
        "各流域当前平均降雨多少",
        "海河11分区累计雨量是多少",
    ):
        decision = router.select(question)
        assert decision.mode == "filtered", question
        assert decision.query_type == "rain"
        assert decision.tool_names == ("query_basin_areal_rainfall",)


def test_mixed_current_and_future_weather_uses_full_planner():
    router, _ = _router()
    for question in (
        "对比天津现在和明天的天气",
        "天津当前气温和未来降雨如何",
        "现在与后天的气温差多少",
    ):
        assert router.select(question).mode == "full", question


def test_unsupported_or_overly_specific_current_scope_uses_full_planner():
    router, _ = _router()
    for question in (
        "上海现在天气怎么样",
        "北京市朝阳区现在天气",
        "雄安新区现在天气",
    ):
        assert router.select(question).mode == "full", question

    for question in ("现在天气怎么样", "天津现在天气", "中心城区当前气温"):
        assert router.select(question).query_type == "current", question


def test_future_water_level_never_early_routes_to_observation_tool():
    router, _ = _router()
    for question in ("子牙河明天水位多少", "未来水位趋势如何"):
        assert router.select(question).mode == "full", question


def test_future_areal_rainfall_and_warning_queries_use_full_planner():
    router, _ = _router()
    for question in (
        "九分区降雨预报",
        "各流域降雨预报",
        "大清河流域降雨预报",
        "明天有什么暴雨预警",
        "未来会发布预警吗",
    ):
        assert router.select(question).mode == "full", question


def test_regional_and_specific_poi_comparison_uses_full_planner():
    router, _ = _router()
    for question in (
        "中心城区和梅江会展中心现在气温对比",
        "全市各站和天津站现在气温对比",
    ):
        assert router.select(question).mode == "full", question


def test_filtered_golden_cases_keep_required_and_exclude_forbidden_tools():
    router, _ = _router()
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    filtered_count = 0
    for case in payload["cases"]:
        decision = router.select(case["question"])
        if decision.mode != "filtered":
            continue
        filtered_count += 1
        candidates = set(decision.tool_names)
        required = set(case["expected_tools"]["required"])
        forbidden = set(case["expected_tools"]["forbidden"])
        assert required <= candidates, case["id"]
        assert not (forbidden & candidates), case["id"]
    assert filtered_count >= 12, "主动过滤应覆盖主要单域高频问题"


def test_filtered_chain_is_cached_and_cache_capacity_is_bounded():
    router, builds = _router(cache_size=2)
    water = router.select("子牙河现在水位多高")
    assert router.chain_for(water) is router.chain_for(water)
    assert len(builds) == 1

    router.chain_for(router.select("天津明天最高气温多少"))
    router.chain_for(router.select("现在有什么暴雨预警"))
    assert router.chain_cache_size == 2

    router.chain_for(water)
    assert len(builds) == 4, "最旧 water chain 应被 LRU 淘汰后重建"


def test_unknown_question_and_missing_required_tool_use_full_chain():
    router, _ = _router()
    unknown = router.select("帮我解释一下这份材料")
    assert unknown.mode == "full"
    assert router.chain_for(unknown) is router.full_chain

    tools = [_FakeTool("rag_search")]
    missing = ActiveToolRouter(
        tools=tools,
        full_chain="full",
        build_chain=lambda selected: selected,
        candidate_index=ToolCandidateIndex(tools),
    )
    assert missing.select("子牙河现在水位多高").mode == "full"


def test_forged_filtered_decision_with_missing_tool_fails_closed():
    router, _ = _router()
    forged = ToolRouteDecision(
        "filtered", "water_level", ("tool_removed_after_selection",), True, "test"
    )
    import pytest
    with pytest.raises(RuntimeError, match="filtered tool unavailable"):
        router.chain_for(forged)


def test_invalid_limit_falls_back_to_twelve():
    router, _ = _router()
    decision = router.select("天津明天天气怎么样", limit=0)
    assert decision.mode == "filtered"
    assert 1 <= len(decision.tool_names) <= 12
