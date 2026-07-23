"""Tests for the decision weather POI tool."""

import json
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chainlitexam.tests.stubs import ensure_stubs

ensure_stubs()

# decision_weather.py imports langchain_core.tools.tool, which is not covered by the shared stubs.
_lc_tools = types.ModuleType("langchain_core.tools")


def _tool_stub(func):
    class _ToolWrapper:
        def __init__(self, fn):
            self._fn = fn
            self.name = fn.__name__
            self.description = fn.__doc__

        async def ainvoke(self, args):
            if isinstance(args, dict):
                return await self._fn(**args)
            return await self._fn(args)

    return _ToolWrapper(func)


_lc_tools.tool = _tool_stub
sys.modules["langchain_core.tools"] = _lc_tools

import chainlitexam.tools.decision_weather as dw


def test_build_decision_weather_tools_returns_one_tool():
    tools = dw.build_decision_weather_tools(None, [], {})
    assert len(tools) == 1
    assert tools[0].name == "query_decision_weather_for_poi"


def test_prefilter_allows_location_with_time_and_rejects_time_only():
    assert dw._decision_weather_prefilter("梅江会展中心明天天气怎么样") is True
    assert dw._decision_weather_prefilter("XX公园适合周末露营吗") is True
    assert dw._decision_weather_prefilter("今天天气怎么样") is False
    assert dw._decision_weather_prefilter("未来24小时会下雨吗") is False


@pytest.mark.asyncio
async def test_query_decision_weather_for_poi_rejects_non_poi_question():
    answer_chain = None
    callbacks = {"ainvoke_chain": lambda chain, inputs: None}
    tool = dw.build_decision_weather_tools(answer_chain, [], callbacks)[0]
    result = await tool.ainvoke({"user_text": "今天天气怎么样"})
    assert isinstance(result, str)
    assert "不属于" in result or "范围" in result


@pytest.mark.asyncio
async def test_query_decision_weather_for_poi_missing_tools():
    answer_chain = None
    callbacks = {"ainvoke_chain": lambda chain, inputs: None}
    tool = dw.build_decision_weather_tools(answer_chain, [], callbacks)[0]
    result = await tool.ainvoke({"user_text": "天津大学未来24小时天气怎么样"})
    assert isinstance(result, str)
    assert "暂时不可用" in result or "缺少" in result


class FakeChain:
    def __init__(self, overrides=None):
        self._overrides = overrides or {}

    async def ainvoke(self, *args, **kwargs):
        now = datetime.now()
        payload = {
            "is_decision_weather": True,
            "location_name": "天津大学",
            "target_start_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "target_end_time": (
                now.replace(hour=0, minute=0, second=0, microsecond=0)
                + timedelta(days=2)
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "interval_hours": 24,
            "question_type": "general_weather",
            "need_clarification": False,
            "clarification_question": "",
        }
        payload.update(self._overrides)

        class Result:
            content = json.dumps(payload, ensure_ascii=False)

        return Result()


class FakePoiTool:
    name = "search_poi"

    async def ainvoke(self, args):
        return [
            {
                "text": json.dumps(
                    {
                        "pois": [
                            {
                                "name": "天津大学",
                                "address": "天津市南开区卫津路92号",
                                "longitude": 117.16,
                                "latitude": 39.11,
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            }
        ]


class FakeForecastTool:
    name = "query_rolling_forecast"

    async def ainvoke(self, args):
        now = datetime.now()
        return [
            {
                "text": json.dumps(
                    {
                        "periods": [
                            {
                                "start_time": now.strftime("%Y-%m-%d %H:%M:%S"),
                                "end_time": (
                                    now + timedelta(hours=24)
                                ).strftime("%Y-%m-%d %H:%M:%S"),
                                "region": "南开区",
                                "WEA": "晴",
                                "TMAX": 32,
                                "TMIN": 24,
                                "EDA": "东南风3级",
                                "TP1H": 0,
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            }
        ]


@pytest.mark.asyncio
async def test_query_decision_weather_for_poi_happy_path():
    answer_chain = FakeChain()
    tools = [FakePoiTool(), FakeForecastTool()]
    callbacks = {"ainvoke_chain": lambda chain, inputs: answer_chain.ainvoke()}

    poi_tools = dw.build_decision_weather_tools(answer_chain, tools, callbacks)
    tool = poi_tools[0]
    result = await tool.ainvoke({"user_text": "天津大学未来24小时天气怎么样"})

    assert isinstance(result, str)
    assert "天津大学" in result or "核心结论" in result


@pytest.mark.asyncio
async def test_query_decision_weather_for_poi_need_clarification():
    answer_chain = FakeChain(overrides={"need_clarification": True, "clarification_question": "请补充具体地点。"})
    tools = [FakePoiTool(), FakeForecastTool()]
    callbacks = {"ainvoke_chain": lambda chain, inputs: answer_chain.ainvoke()}

    tool = dw.build_decision_weather_tools(answer_chain, tools, callbacks)[0]
    result = await tool.ainvoke({"user_text": "学校明天天气怎么样"})

    assert isinstance(result, str)
    assert "请补充具体地点" in result


@pytest.mark.asyncio
async def test_query_decision_weather_for_poi_not_decision_weather():
    answer_chain = FakeChain(overrides={"is_decision_weather": False})
    tools = [FakePoiTool(), FakeForecastTool()]
    callbacks = {"ainvoke_chain": lambda chain, inputs: answer_chain.ainvoke()}

    tool = dw.build_decision_weather_tools(answer_chain, tools, callbacks)[0]
    result = await tool.ainvoke({"user_text": "天津降雨量"})

    assert isinstance(result, str)
    assert "不是" in result or "不属于" in result
