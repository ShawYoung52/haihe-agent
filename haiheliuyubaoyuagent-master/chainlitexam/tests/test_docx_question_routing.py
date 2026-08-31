# -*- coding: utf-8 -*-
"""docx《问题分类列表20260826》43 条问题的路由回归测试。

口径（2026-08-31 用户确认）：
- 天河已做好的问题（用户列的清单）一律确定性走 query_tianhe_fixed_qa；
- **冲突问题先走我们智能体**："今日雨情"两边清单都有，但天河纯文本答案拿不到图，
  按"冲突先走我们"原则路由到本地组合长图 generate_haihe_composite_longimg；
- 其余业务问题（标黄 5 条 + 区域/点位/预警等）走本地对应工具或领域分类器，不被天河截走。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chainlitexam.tests.stubs import ensure_stubs  # noqa: E402

ensure_stubs()

import chainlitexam.message_orchestrator as mo  # noqa: E402
from chainlitexam.tools.active_tool_router import (  # noqa: E402
    is_conservative_region_risk_query,
    is_conservative_river_forecast_query,
)
from chainlitexam.tools.decision_weather_core import _decision_weather_prefilter  # noqa: E402


# —— 天河问题：确定性走 query_tianhe_fixed_qa，原样透传原文 ——
TIANHE_QUESTIONS = [
    "今天适合洗车吗？",
    "今天穿衣有什么建议？",
    "今天适不适合晾晒？",
    "暴雨天气的防范建议？",
    "什么是短时强降水？",
    "雷电怎么防御？",
    "自动气象站如何观测？",
    "气象卫星有什么作用？",
    "雾和霾有什么区别？",
    "双偏振雷达产品怎么看？",
    "MICAPS产品怎么分析？",
    "面雨量如何计算？",
    "预警发布流程是什么？",
    "天气会商包含哪些内容？",
    "降雨对道路交通会带来什么影响？",
    "我该怎么向你提问？",
    "今年7月蓟州区有多少天超过35℃",
    "今年以来我市40℃以上高温出现过几次？",
    "你可以回答哪些问题？",
    "哪些问题你无法解答？",
    "你的气象数据来源是什么？",
    "预报可以支持多长时效？",
]


@pytest.mark.parametrize("question", TIANHE_QUESTIONS)
def test_tianhe_questions_route_to_tianhe(question):
    route, label = mo._select_pre_planner_route(question)
    assert route == ("query_tianhe_fixed_qa", {"query": question})
    assert label == "天河固定目录路由"


def test_baoyu_formation_routes_to_tianhe_via_compat_catalog():
    # "暴雨如何形成？"非整句目录，走天河兼容目录路由（形成/成因家族），仍收口天河。
    route, label = mo._select_pre_planner_route("暴雨如何形成？")
    assert route == ("query_tianhe_fixed_qa", {"query": "暴雨如何形成？"})
    assert label == "天河兼容目录路由"


# —— 冲突问题：先走我们智能体（本地组合长图），不走天河 ——
@pytest.mark.parametrize("question", ["今日雨情", "今日雨情？", "今天雨情"])
def test_conflicted_today_rain_goes_to_local_longimg(question):
    route, label = mo._select_pre_planner_route(question)
    assert route == ("generate_haihe_composite_longimg", {})
    assert label == "本地组合长图路由"


# —— 本地业务问题：前置路由到本地工具（简单天气/决策天气） ——
@pytest.mark.parametrize(
    ("question", "expected_tool"),
    [
        ("天津未来三天天气？", "query_rolling_forecast"),
        ("未来一周我市天气怎么样？", "query_rolling_forecast"),
        ("今天晚上蓟州的天气怎么样？", "query_rolling_forecast"),
        ("未来三天于桥水库降雨预报？", "query_decision_weather_for_poi"),
        ("本周末适合去泰达航母主题公园游玩吗？", "query_decision_weather_for_poi"),
        ("盘山景区未来两天天气？", "query_decision_weather_for_poi"),
        ("天津港明日风力多大？", "query_decision_weather_for_poi"),
    ],
)
def test_local_business_pre_planner_routes(question, expected_tool):
    route, _ = mo._select_pre_planner_route(question)
    assert route is not None
    assert route[0] == expected_tool


# —— 本地业务问题：走 planner 但命中正确领域分类器，且绝不被天河截走 ——
@pytest.mark.parametrize(
    "question",
    [
        "天津当前天气实况",
        "天津当前的天气情况",  # 天河目录问法，按"冲突先走我们"收归本地实况（不走天河）
        "当前有哪些预警？",
        "明天泃河有雨吗？",
        "今天晚上滦河有雨吗？",
        "今天蓟州可能有哪些风险？",
        "今天下午天津港附近有雨吗？",
        "近期适合农事播种吗？",
        "有什么使用小技巧？",
    ],
)
def test_local_business_questions_not_hijacked_by_tianhe(question):
    route, _ = mo._select_pre_planner_route(question)
    assert route is None or route[0] != "query_tianhe_fixed_qa"


@pytest.mark.parametrize("question", ["明天泃河有雨吗？", "今天晚上滦河有雨吗？"])
def test_tributary_river_questions_match_river_forecast(question):
    assert is_conservative_river_forecast_query(question)


def test_jizhou_risk_matches_region_risk():
    assert is_conservative_region_risk_query("今天蓟州可能有哪些风险？")


@pytest.mark.parametrize(
    "question",
    ["今天下午天津港附近有雨吗？", "未来三天于桥水库降雨预报？", "盘山景区未来两天天气？"],
)
def test_poi_questions_match_decision_weather_prefilter(question):
    assert _decision_weather_prefilter(question)
