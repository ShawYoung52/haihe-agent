"""天河目录问题规则路由测试：03 实况监测 / 04 防灾减灾 / 05 气候统计 三类确定性走 query_tianhe_fixed_qa。

背景（2026-08-24 用户口径）：甲方提供三份天河目录文档，要求"这些问题都要接入天河问答接口"。
纯知识问题（04）不再靠 planner LLM 自觉（prompt 引导可能漏接），也不走本地 rag_search；
数据类目录（03 实况 / 05 气候统计）同样确定性路由到天河。规则路由命中后强制调
query_tianhe_fixed_qa、跳过 planner；天河工具级失败也直接展示失败说明，不回退本地工具，
确保天河目录问题始终由供应方边界收口。

命中收紧防误伤：
- 查当前生效预警（"现在有哪些暴雨预警"）仍走本地预警工具；
- 带时间词/POI 点位的决策天气问法（"明天去盘山暴雨注意事项"）不受影响；
- 未来/预报/出行决策词（"明天风大吗""周末适合去哪"）不路由到天河；
- 今日实况/预报（"今天大风怎么样""今天气温多少度"）不命中 05 气候统计（缺时段词）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chainlitexam.tests.stubs import ensure_stubs  # noqa: E402

ensure_stubs()

import chainlitexam.message_orchestrator as mo  # noqa: E402


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
            # 防范建议家族（暴雨）
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

class TestTianheDisasterPreventionCatalog:
    """04 防灾减灾类目录（知识）全覆盖。"""

    @pytest.mark.parametrize(
        "question",
        [
            "暴雨天气的防范建议",
            "大风天气的防范建议",
            "高温天气的防范建议",
            "强对流天气怎么应对",
            "暴雨预警四个等级是什么",
            "高温怎么定义",
            "气温多高算是高温",
            "高温来了公众应该怎么办",
            "高温预警信号及应对措施",
            "降雨量怎么分等级",
            "台风等级",
            "暴雨预警发出后公众该怎么办",
            "暴雨是如何形成的",
            "暴雨等级是如何划分的",
            "暴雨的主要危害有哪些",
        ],
    )
    def test_catalog_hit(self, question):
        route = mo._route_tianhe_knowledge_query(question)
        assert route == ("query_tianhe_fixed_qa", {"query": question}), f"04 目录未命中：{question}"


class TestTianheLiveCatalog:
    """03 实况监测类目录（数据，时间词是问法一部分）全覆盖。"""

    @pytest.mark.parametrize(
        "question",
        [
            "全市现在下了多少雨",
            "市区现在气温和风的实况",
            "今天雨都下在哪儿了",
            "今天雨下了多长时间",
            "现在市区风大吗",
            "昨天雨下得怎么样",
            "现在能见度好不好",
        ],
    )
    def test_catalog_hit(self, question):
        route = mo._route_tianhe_knowledge_query(question)
        assert route == ("query_tianhe_fixed_qa", {"query": question}), f"03 目录未命中：{question}"


class TestTianheClimateCatalog:
    """05 气候统计类目录（数据，含区县/时段，不按 POI 排除）全覆盖。"""

    @pytest.mark.parametrize(
        "question",
        [
            "去年夏天全市最高气温达到多少度",
            "去年7月蓟州区有多少天超过35℃",
            "近5年我市40℃以上高温出现过几次",
            "滨海新区今年6月的高温情况怎么样",
            "今年夏天哪个区最热",
            "近5年我市最冷的一天是哪一天",
            "宝坻区今年1月有多少天低于-10℃",
            "滨海新区冬天会出现-10℃以下的低温吗",
            "去年冬天哪个区最冷",
            "今年以来我市出现过几次寒潮天气",
            "去年11月的寒潮降温幅度有多大",
            "今年3月的寒潮影响了哪些区",
            "今年以来我市出现过几次8级以上大风",
            "去年4月滨海新区最大风力达到多少级",
            "近5年我市最强的大风是哪一次",
            "蓟州区今年春天的大风情况怎么样",
            "今年以来我市出现过几次大雾天气",
            "去年12月中心城区有多少天大雾",
            "哪个区的大雾天气最多",
        ],
    )
    def test_catalog_hit(self, question):
        route = mo._route_tianhe_knowledge_query(question)
        assert route == ("query_tianhe_fixed_qa", {"query": question}), f"05 目录未命中：{question}"


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
            # 流域/河系与无关问题
            "海河流域明天天气",
            "暴雨影响哪些河流",
            "今天天气怎么样",
            "密云水库水位多少",
            # 未来/预报/出行决策词不路由到天河
            "明天风大吗",
            "未来三天的天气怎么样？",
            "这个周末适合去哪玩",
            # 今日实况/预报缺 05 时段词，不命中气候统计
            "今天大风怎么样",
            "今天气温多少度",
            "现在有大雾吗",
            "今天哪个区最热",
            # 去年泛天气问法不在目录（05 是具体统计，非通用历史天气）
            "去年天气怎么样",
            # 盘山能见度是点位实况，非目录全市/市区能见度问法
            "盘山能见度怎么样",
            # 三份目录以外的灾种/泛预警知识不得借天河普通问答扩展
            "气象预警等级划分标准",
            "洪水预警等级定义",
            "寒潮天气怎么应对",
            "雷电预警分为几级",
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
        assert src.count("_enforce_tianhe_catalog_boundary") >= 2

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


class TestTianheCatalogBoundaryGuard:
    """Planner 无权扩大天河目录；执行前必须按用户原问题重新校验。"""

    def test_removes_tianhe_call_for_non_catalog_forecast(self):
        from langchain_core.messages import AIMessage

        msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "query_rolling_forecast",
                    "args": {"user_query": "未来三天的天气怎么样？", "regions": ""},
                    "id": "rolling-1",
                },
                {
                    "name": "query_tianhe_fixed_qa",
                    "args": {"query": "未来三天的天气怎么样？"},
                    "id": "tianhe-1",
                },
            ],
        )

        guarded = mo._enforce_tianhe_catalog_boundary(msg, "未来三天的天气怎么样？")

        assert [call["name"] for call in guarded.tool_calls] == ["query_rolling_forecast"]

    def test_keeps_catalog_call_and_forces_original_user_query(self):
        from langchain_core.messages import AIMessage

        user_text = "今天雨下了多长时间？"
        msg = AIMessage(
            content="",
            tool_calls=[{
                "name": "query_tianhe_fixed_qa",
                "args": {"query": "被 Planner 改写过的问题"},
                "id": "tianhe-1",
            }],
        )

        guarded = mo._enforce_tianhe_catalog_boundary(msg, user_text)

        assert guarded.tool_calls[0]["args"] == {"query": user_text}
