"""天河知识类问题规则路由测试：预警等级/防范建议两个家族确定性走 query_tianhe_fixed_qa。

背景（2026-08-24 用户口径）：「暴雨预警四个等级是什么」「暴雨天气的防范建议有哪些」
这类纯知识问题需要用天河问答接口回答，不再靠 planner LLM 自觉（prompt 引导可能漏接），
也不走本地 rag_search。规则路由命中后强制调 query_tianhe_fixed_qa、跳过 planner，
天河工具级失败时作为普通 ToolMessage 交回 planner 回退本地工具。

命中收紧防误伤：
- 查当前生效预警（"现在有哪些暴雨预警"）仍走本地预警工具；
- 带时间词/POI 点位的决策天气问法（"明天去盘山暴雨注意事项"）不受影响；
- 天河目录另外 3 条实况类问法（"今天雨下了多长时间"等）不在本路由范围，
  仍走原 prompt 引导/本地工具。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import message_orchestrator as mo  # noqa: E402


class TestTianheKnowledgeRouteHit:
    @pytest.mark.parametrize(
        "question",
        [
            # 预警等级家族
            "暴雨预警四个等级是什么",
            "暴雨预警四个等级是什么？",
            "暴雨预警等级怎么划分",
            "暴雨预警分为几级",
            "暴雨预警信号的颜色等级",
            "气象预警等级划分标准",
            "洪水预警等级定义",
            # 防范建议家族
            "暴雨天气的防范建议",
            "暴雨天气的防范建议有哪些？",
            "暴雨防范建议",
            "暴雨防范措施有哪些",
            "强降雨防御指南",
            "暴雨天注意事项",
            "暴雨天气如何应对",
            "大暴雨怎么防范",
        ],
    )
    def test_hit_returns_tianhe_tool(self, question):
        route = mo._route_tianhe_knowledge_query(question)
        assert route is not None, f"应命中天河知识路由：{question}"
        tool_name, tool_args = route
        assert tool_name == "query_tianhe_fixed_qa"
        # query 原样透传用户问题（天河侧自行规范化，不改写不提炼）
        assert tool_args == {"query": question}

    def test_query_verbatim_with_punctuation(self):
        """带句末标点也原样传（天河 Fixed QA 规范化自己会去句末标点）。"""
        route = mo._route_tianhe_knowledge_query("暴雨天气的防范建议有哪些？")
        assert route == ("query_tianhe_fixed_qa", {"query": "暴雨天气的防范建议有哪些？"})


class TestTianheKnowledgeRouteMiss:
    @pytest.mark.parametrize(
        "question",
        [
            # 查当前生效预警 → 本地预警工具，绝不能去天河
            "现在有哪些暴雨预警",
            "天津现在生效的暴雨预警",
            "当前暴雨预警",
            "最新暴雨预警信息",
            "天津发布暴雨预警了吗",
            "今天有什么预警",
            # 带时间词/POI 的决策天气 → 原路由不变
            "明天去盘山暴雨注意事项",
            "下周一津泰达实验学校附近天气怎么样",
            "明天天气怎么样",
            "天津大学暴雨防范建议",
            # 天河目录实况类（不在本路由范围，仍走原 prompt 引导）
            "今天雨下了多长时间",
            "全市现在下了多少雨",
            "市区现在气温和风的实况",
            # 流域/河系与无关问题
            "海河流域明天天气",
            "暴雨影响哪些河流",
            "今天天气怎么样",
            "密云水库水位多少",
        ],
    )
    def test_miss_returns_none(self, question):
        assert mo._route_tianhe_knowledge_query(question) is None, f"不应命中：{question}"


class TestTianheKnowledgeRouteWiring:
    """process_message 接线：命中时作为 simple_route 跳过 thinking/planner。"""

    def test_wired_into_process_message(self):
        """process_message 源码里必须在 simple_route 为空时补查天河知识路由。"""
        import inspect

        src = inspect.getsource(mo.process_message)
        assert "_route_tianhe_knowledge_query" in src

    def test_enforce_route_label(self):
        """强制路由复用 _enforce_simple_weather_route，且日志标签可区分天河知识路由。"""
        from langchain_core.messages import AIMessage

        msg = mo._enforce_simple_weather_route(
            AIMessage(content=""),
            "暴雨预警四个等级是什么",
            ("query_tianhe_fixed_qa", {"query": "暴雨预警四个等级是什么"}),
            label="天河知识路由",
        )
        assert msg.tool_calls[0]["name"] == "query_tianhe_fixed_qa"
        assert msg.tool_calls[0]["args"] == {"query": "暴雨预警四个等级是什么"}
