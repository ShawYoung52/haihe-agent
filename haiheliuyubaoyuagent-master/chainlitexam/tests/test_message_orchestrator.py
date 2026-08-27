"""Tests for message_orchestrator feature-flag behavior."""

import asyncio
import importlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chainlitexam.tests.stubs import ensure_stubs

ensure_stubs()

import chainlit
import langchain_core.messages  # noqa: F401
import chainlitexam.message_orchestrator as mo
import external_skill_tools as est


@pytest.mark.asyncio
async def test_weekend_activity_fast_path_does_not_intercept_specific_poi(monkeypatch):
    """具体景点周末游玩必须交给点位天气链路，不能降级成天津市降雨概览。"""
    class ForecastTool:
        name = "get_city_rainfall_time_range"

    async def forbidden_reasoning(*args, **kwargs):
        raise AssertionError("POI 问题不应进入周末区域快速路径")

    monkeypatch.setattr(mo, "_find_tool", lambda *args, **kwargs: ForecastTool())
    monkeypatch.setattr(mo, "_show_business_reasoning", forbidden_reasoning)

    handled = await mo._try_weekend_activity_fast_path(
        "本周末适合去泰达航母主题公园游玩吗？", None, [ForecastTool()], [], {}
    )

    assert handled is False


def test_enable_fast_paths_is_permanently_disabled(monkeypatch):
    """快速路径属于禁用业务边界，环境变量不得重新开启。"""
    monkeypatch.setenv("ENABLE_FAST_PATHS", "false")
    importlib.reload(mo)
    assert mo.ENABLE_FAST_PATHS is False
    monkeypatch.setenv("ENABLE_FAST_PATHS", "true")
    importlib.reload(mo)
    assert mo.ENABLE_FAST_PATHS is False


def test_simple_weather_route_rejects_generic_river_names():
    assert mo._route_simple_weather_query("明天泃河有雨吗？") is None


def test_river_forecast_boundary_replaces_later_planner_overreach():
    """补充 Planner 轮也只能执行统一河流预报工具。"""
    planner_msg = type("PlannerMessage", (), {
        "tool_calls": [
            {"id": "late-1", "name": "query_rolling_forecast", "args": {"user_query": "改写问题"}},
            {"id": "late-2", "name": "query_tianhe_fixed_qa", "args": {"query": "改写问题"}},
        ],
        "content": "",
    })()

    guarded = mo._enforce_river_forecast_tool_boundary(planner_msg, "明天泃河有雨吗？")

    assert guarded.tool_calls == [{
        "id": "river_forecast_boundary",
        "name": "query_river_rainfall_forecast",
        "args": {"user_query": "明天泃河有雨吗？"},
        "type": "tool_call",
    }]

def test_db_bootstrap_cannot_enable_fast_paths_from_environment():
    """数据库初始化模块也不得根据环境变量安装快速路径。"""
    db_source = (
        Path(__file__).resolve().parents[1] / "utils" / "db.py"
    ).read_text(encoding="utf-8")

    assert "ENABLE_FAST_PATHS = False" in db_source
    assert 'os.environ.get("ENABLE_FAST_PATHS"' not in db_source


@pytest.mark.asyncio
async def test_process_message_skips_fast_paths_when_disabled(monkeypatch):
    """When ENABLE_FAST_PATHS is False, no _try_*_fast_path function is awaited and process_message returns normally."""
    monkeypatch.setattr(mo, "ENABLE_FAST_PATHS", False)

    called = []

    async def fake_fast_path(*args, **kwargs):
        called.append("fast_path")
        return False

    # Patch every fast-path function we can find on the module.
    for name in dir(mo):
        if name.startswith("_try_") and name.endswith("_fast_path") and callable(getattr(mo, name)):
            monkeypatch.setattr(mo, name, fake_fast_path)

    class FakeMessage:
        content = "测试查询"

    class FakePlannerMsg:
        content = "这是一个测试回答。"
        tool_calls = []

    async def fake_astream_planner_think(*args, **kwargs):
        return FakePlannerMsg()

    async def noop_async(*args, **kwargs):
        return None

    class FakeMessageObj:
        content = ""
        send = noop_async
        remove = noop_async
        update = noop_async

    callbacks = {
        "astream_planner_think": fake_astream_planner_think,
        "need_river_plot": lambda message: False,
        "astream_thinking_to_reasoning": noop_async,
        "append_followup_if_needed": lambda text, query: text,
        "stream_text_to_message": noop_async,
        "astream_answer_chain_to_message": lambda *a, **k: "",
    }

    monkeypatch.setattr(mo.cl, "Message", lambda **kwargs: FakeMessageObj())

    result = await mo.process_message(
        FakeMessage(),
        planner_chain=None,
        answer_chain=None,
        thinking_chain=None,
        tools=[],
        messages=[],
        callbacks=callbacks,
    )

    assert called == [], f"Expected no fast-path calls when disabled, got {called}"
    assert result is None


@pytest.mark.asyncio
async def test_run_tool_round_failure_records_tool_message_without_generic_error(monkeypatch):
    """工具在 _run_tool_round 中失败时，不应再向用户发送固定的通用错误消息，
    而应把失败信息以 ToolMessage 形式交给 planner 自行组织回答。"""

    class FakeTool:
        name = "evaluate_haihe_forecast_emergency_response"

    class FakePlannerMsg:
        tool_calls = [
            {
                "name": FakeTool.name,
                "args": {"start_time": "2026-07-13 02:00:00"},
                "id": "call-1",
            }
        ]

    async def fake_invoke_tool_with_tolerance(*args, **kwargs):
        raise Exception("未找到起报 2026071302 的 12h/24h 预报文件。")

    monkeypatch.setattr(mo, "_invoke_tool_with_tolerance", fake_invoke_tool_with_tolerance)
    monkeypatch.setattr(mo.cl, "Step", chainlit.Step)

    sent_messages: list[dict] = []
    monkeypatch.setattr(mo.cl, "Message", lambda **kwargs: type("CapturingMessage", (), {
        "send": lambda self: sent_messages.append(kwargs),
        "remove": lambda self: None,
        "update": lambda self: None,
    })())

    callbacks = {"tool_observation_to_text": lambda obs: str(obs)}
    messages = []

    forced, ree, bundles, rolling_bundles, _ = await mo._run_tool_round(
        FakePlannerMsg(), [FakeTool()], messages, "测试", 1, callbacks
    )

    assert sent_messages == [], f"Expected no generic cl.Message, got {sent_messages}"
    assert forced is None
    assert ree is None
    assert bundles == []
    assert rolling_bundles == []
    assert len(messages) == 1
    tool_msg = messages[0]
    assert tool_msg.content.startswith(f"工具 {FakeTool.name} 执行失败")
    assert "该数据暂不可用" in tool_msg.content
    assert "2026071302" in tool_msg.content


def _impact_result_with_propagation():
    return {
        "time_range_readable": "2026-07-22 08:00 ~ 2026-07-23 08:00",
        "rainfall_threshold_mm": 50.0,
        "affected_rivers": ["滦河"],
        "affected_zone_77_regions": ["滦河山区"],
        "affected_admin_divisions": ["承德市"],
        "total_segments": 3,
        "affected_segments": 3,
        "river_propagation": {
            "flow_velocity_mps": 2.0,
            "rivers": [
                {
                    "river_name": "滦河",
                    "propagation_distance_km": 48.2,
                    "propagation_time_hours": 6.7,
                    "arrival_estimate_readable": "约6.7小时",
                    "has_downstream": True,
                }
            ],
        },
    }


def test_brief_includes_propagation_summary():
    brief = mo._build_affected_river_network_brief(_impact_result_with_propagation(), "暴雨影响哪些河系")
    assert "按经验流速 2.0 m/s 估算" in brief
    assert "约6.7小时" in brief
    assert "48.2" in brief
    assert "滦河" in brief
    assert "传播至下游最远约" in brief


def test_brief_direct_only_propagation_uses_pass_through_wording():
    result = _impact_result_with_propagation()
    result["river_propagation"]["rivers"][0]["has_downstream"] = False
    brief = mo._build_affected_river_network_brief(result, "暴雨影响哪些河系")
    assert "洪水通过需约6.7小时" in brief
    assert "传播至下游" not in brief


def test_brief_skips_propagation_line_when_keys_missing():
    result = _impact_result_with_propagation()
    result["river_propagation"]["rivers"] = [{"propagation_time_hours": 1.0}]
    brief = mo._build_affected_river_network_brief(result, "暴雨影响哪些河系")
    assert "经验流速" not in brief
    assert "None" not in brief


def test_brief_without_propagation_block_stays_compatible():
    result = _impact_result_with_propagation()
    del result["river_propagation"]
    brief = mo._build_affected_river_network_brief(result, "暴雨影响哪些河系")
    assert "经验流速" not in brief
    assert "滦河" in brief  # 既有河系列表不受影响


@pytest.mark.asyncio
async def test_process_message_creates_reasoning_before_stream_msg(monkeypatch):
    """思考过程必须比回答消息先创建/发送（网页端思考在上、回答在下）。"""
    monkeypatch.setattr(mo, "ENABLE_FAST_PATHS", False)

    order: list[str] = []

    class FakeMessage:
        content = "测试查询"

    class FakePlannerMsg:
        content = ""
        tool_calls = []

    async def fake_astream_planner_think(*args, **kwargs):
        return FakePlannerMsg()

    async def noop_async(*args, **kwargs):
        return None

    # 捕获 ReasoningStep 创建顺序：monkeypatch 构造器
    def fake_reasoning_step(name="🤔 思考过程"):
        order.append("reasoning")
        return _FakeReasoning()

    class _FakeReasoning:
        _closed = False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def stage(self, *a, **k):
            return None
        async def line(self, *a, **k):
            return None
        async def append(self, *a, **k):
            return None
        async def close(self):
            return None

    class FakeStreamMsg:
        def __init__(self, **kw):
            order.append("stream_msg")
            self.content = ""
        async def send(self):
            return None
        async def update(self):
            return None
        async def remove(self):
            return None

    callbacks = {
        "astream_planner_think": fake_astream_planner_think,
        "need_river_plot": lambda message: False,
        "astream_thinking_to_reasoning": noop_async,
        "append_followup_if_needed": lambda text, query: text,
        "stream_text_to_message": noop_async,
        "astream_answer_chain_to_message": noop_async,
    }

    monkeypatch.setattr(mo, "ReasoningStep", fake_reasoning_step)
    monkeypatch.setattr(mo.cl, "Message", FakeStreamMsg)

    # 全量测试中 process_message 内 cl.user_session.set 需要 Chainlit 上下文；
    # 单独跑本文件时上下文偶然存在，这里统一补一个 http 上下文保证全量可跑。
    # 裸环境（tests/stubs.py 假 chainlit）下无 context 模块，靠假 user_session 兜底。
    try:
        from chainlit.context import context_var, init_http_context
        context_var.set(init_http_context(thread_id="test-reasoning-order"))
    except Exception:
        pass

    await mo.process_message(
        FakeMessage(), planner_chain=None, answer_chain=None,
        thinking_chain=None, tools=[], messages=[], callbacks=callbacks,
    )

    assert order.index("reasoning") < order.index("stream_msg"), \
        f"思考过程应在上、回答在下，实际顺序: {order}"


@pytest.mark.asyncio
async def test_run_tool_round_parallelizes_pure_data_tools(monkeypatch):
    """相互独立的纯数据工具应并行调用（总耗时≈最慢工具，而非各工具之和）。"""

    class FakeTool:
        def __init__(self, name):
            self.name = name

    async def slow_invoke(tool_name, tool, tool_args, step, user_text=""):
        await asyncio.sleep(0.05)
        return f"{tool_name}-result", 0.05

    monkeypatch.setattr(mo, "_invoke_tool_with_tolerance", slow_invoke)
    monkeypatch.setattr(mo.cl, "Step", chainlit.Step)

    class FakePlannerMsg:
        tool_calls = [
            {"name": "get_city_rainfall_time_range", "args": {"city": "天津"}, "id": "c1"},
            {"name": "get_river_system_rainfall_forecast", "args": {"river_system": "大清河"}, "id": "c2"},
            {"name": "get_city_rainfall_time_range", "args": {"city": "北京"}, "id": "c3"},
        ]

    tools = [FakeTool(c["name"]) for c in FakePlannerMsg.tool_calls]
    callbacks = {"tool_observation_to_text": lambda obs: str(obs)}
    messages = []

    start = time.time()
    forced, ree, bundles, rolling_bundles, _ = await mo._run_tool_round(
        FakePlannerMsg(), tools, messages, "测试", 1, callbacks
    )
    elapsed = time.time() - start

    # 3 个 0.05s 工具串行应 ~0.15s，并行应 < 0.12s
    assert elapsed < 0.12, f"工具应并行执行，实际耗时 {elapsed:.3f}s"
    assert len(messages) == 3
    contents = [m.content for m in messages]
    assert contents[0] == "get_city_rainfall_time_range-result"
    assert contents[1] == "get_river_system_rainfall_forecast-result"
    assert contents[2] == "get_city_rainfall_time_range-result"
    # ToolMessage 顺序与 tool_call 顺序一致
    assert [m.tool_call_id for m in messages] == ["c1", "c2", "c3"]


def _tianhe_round_setup(monkeypatch, answer: str):
    """构造一次 query_tianhe_fixed_qa 的 _run_tool_round 调用所需 mock，返回 (messages, forced...)。"""

    class FakeTool:
        name = "query_tianhe_fixed_qa"

    class FakePlannerMsg:
        tool_calls = [
            {"name": "query_tianhe_fixed_qa", "args": {"query": "全市现在下了多少雨"}, "id": "call-t1"}
        ]

    class _CtxFreeStep:
        """无 Chainlit context 的假 Step。

        串行 _run_tool_round 用 `async with cl.Step(...)`（需 id/show_input/output/update）。
        真实 chainlit.Step 在无 context 时抛 ChainlitContextException，会被当成工具失败，
        导致 forced_final_text 不被设置。这里与 tests/stubs.py 的假 Step 同接口。
        """

        def __init__(self, **kwargs):
            self.name = kwargs.get("name", "")
            self.id = kwargs.get("id") or "fake-step"
            self.show_input = False
            self.input = ""
            self.output = ""

        async def send(self):
            pass

        async def update(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    async def fake_invoke(tool_name, tool, tool_args, step, user_text=""):
        return answer, 1.0

    monkeypatch.setattr(mo, "_invoke_tool_with_tolerance", fake_invoke)
    monkeypatch.setattr(mo.cl, "Step", _CtxFreeStep)
    # 天河透传已改为值绑定（_run_tool_round 返回天河原文），不再写 cl.user_session 会话标志，
    # 因此无需 stub user_session。
    callbacks = {"tool_observation_to_text": lambda obs: str(obs)}
    messages: list = []
    return FakePlannerMsg(), [FakeTool()], messages, callbacks


@pytest.mark.asyncio
async def test_tianhe_answer_passthrough_sets_forced_final_text(monkeypatch):
    """天河返回完整 answer 时，应直接作为 forced_final_text 收口（跳过 answer LLM 原样透传）。"""
    answer = "【核心结论】\n全市近24小时平均降雨量：2.5毫米\n最大降雨量：79.8毫米"
    planner_msg, tools, messages, callbacks = _tianhe_round_setup(monkeypatch, answer)

    forced, ree, bundles, rolling_bundles, tianhe_text = await mo._run_tool_round(
        planner_msg, tools, messages, "全市现在下了多少雨", 1, callbacks
    )

    assert forced == answer, "天河 answer 应原样作为 forced_final_text 透传"
    assert tianhe_text == answer, "透传标记应记录天河原文，供 process_message 收口做值绑定判定"
    assert rolling_bundles == [], "天河不是滚动预报，不应产生 bundle"
    assert len(messages) == 1
    assert messages[0].content == answer, "ToolMessage 也应保留原文供历史记录"


@pytest.mark.asyncio
async def test_tianhe_tool_round_rejects_non_catalog_forecast(monkeypatch):
    """即使后续 Planner 误选天河，普通天气预报也不得实际调用供应方接口。"""
    planner_msg, tools, messages, callbacks = _tianhe_round_setup(monkeypatch, "不应被调用")
    invoked = False

    async def fail_if_invoked(*args, **kwargs):
        nonlocal invoked
        invoked = True
        return "不应被调用", 0.0

    monkeypatch.setattr(mo, "_invoke_tool_with_tolerance", fail_if_invoked)

    forced, ree, bundles, rolling_bundles, tianhe_text = await mo._run_tool_round(
        planner_msg, tools, messages, "未来三天的天气怎么样？", 2, callbacks
    )

    assert invoked is False
    assert planner_msg.tool_calls == []
    assert messages == []
    assert forced is None
    assert tianhe_text is None


@pytest.mark.asyncio
async def test_tianhe_degraded_answer_also_passthrough(monkeypatch):
    """天河 HTTP 200 但返回降级文案时，按对接文档 9.4 原样展示，不走本地兜底也不再过 LLM。"""
    degraded = "智能体服务暂时不可用，请稍后重试。"
    planner_msg, tools, messages, callbacks = _tianhe_round_setup(monkeypatch, degraded)

    forced, ree, bundles, rolling_bundles, tianhe_text = await mo._run_tool_round(
        planner_msg, tools, messages, "今天雨下了多长时间", 1, callbacks
    )

    assert forced == degraded, "降级文案同样原样透传（文档 9.4），不当失败、不过 LLM"
    assert tianhe_text == degraded, "降级文案也记录进透传标记"


@pytest.mark.asyncio
async def test_tianhe_json_like_text_and_whitespace_are_preserved_verbatim(monkeypatch):
    """天河 answer 即使长得像 JSON，也不能被解包成 dict/repr 或裁掉首尾空白。"""
    answer = '  {"level": "degraded", "answer": "keep me"} \n'
    planner_msg, tools, messages, callbacks = _tianhe_round_setup(monkeypatch, answer)

    forced, ree, bundles, rolling_bundles, tianhe_text = await mo._run_tool_round(
        planner_msg, tools, messages, "今天雨下了多长时间", 1, callbacks
    )

    assert forced == answer
    assert tianhe_text == answer
    assert messages[0].content == answer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "err_text",
    [
        est._TIANHE_ERR_EMPTY,
        est._TIANHE_ERR_CONNECT,
        est._TIANHE_ERR_UNAVAILABLE,
        est._TIANHE_ERR_FORMAT,
    ],
)
async def test_tianhe_tool_level_failure_still_stops_local_agent(monkeypatch, err_text):
    """天河专属问题即使接口失败也直接展示其失败说明，不交给本地智能体代答。"""
    planner_msg, tools, messages, callbacks = _tianhe_round_setup(monkeypatch, err_text)

    forced, ree, bundles, rolling_bundles, tianhe_text = await mo._run_tool_round(
        planner_msg, tools, messages, "今天雨下了多长时间", 1, callbacks
    )

    assert forced == err_text, "天河专属问题失败后也应强制收口，禁止本地智能体代答"
    assert tianhe_text == err_text, "失败说明也应绑定为天河透传文本"
    assert len(messages) == 1
    assert messages[0].content == err_text


@pytest.mark.asyncio
async def test_tianhe_unexpected_tool_exception_still_stops_local_agent(monkeypatch):
    """天河工具发生未预期异常时也必须由供应方失败说明收口，不能落回本地 planner。"""
    planner_msg, tools, messages, callbacks = _tianhe_round_setup(monkeypatch, "unused")

    async def _raise(*args, **kwargs):
        raise RuntimeError("unexpected client failure")

    monkeypatch.setattr(mo, "_invoke_tool_with_tolerance", _raise)

    forced, ree, bundles, rolling_bundles, tianhe_text = await mo._run_tool_round(
        planner_msg, tools, messages, "今天雨下了多长时间", 1, callbacks
    )

    assert forced == est._TIANHE_ERR_UNAVAILABLE
    assert tianhe_text == est._TIANHE_ERR_UNAVAILABLE
    assert messages[0].content == est._TIANHE_ERR_UNAVAILABLE


@pytest.mark.asyncio
async def test_tianhe_missing_tool_still_stops_local_agent(monkeypatch):
    """天河工具装配缺失时也必须统一失败收口，不能把专属问题交给本地 planner。"""
    planner_msg, _tools, messages, callbacks = _tianhe_round_setup(monkeypatch, "unused")

    forced, ree, bundles, rolling_bundles, tianhe_text = await mo._run_tool_round(
        planner_msg, [], messages, "今天雨下了多长时间", 1, callbacks
    )

    assert forced == est._TIANHE_ERR_UNAVAILABLE
    assert tianhe_text == est._TIANHE_ERR_UNAVAILABLE
    assert messages[0].content == est._TIANHE_ERR_UNAVAILABLE


def test_is_tianhe_passthrough_value_binding():
    """透传判定是值绑定的：仅当 forced_final_text 仍等于本轮天河原文时为真。

    覆盖修复的泄漏路径——天河被后续工具覆盖、被应急响应清空、或非天河强收口时
    都不应误判为透传；且不依赖会话级标志，天然不会跨消息残留。
    """
    answer = "【核心结论】全市近24小时平均降雨量：2.5毫米"
    assert mo._is_tianhe_passthrough(answer, answer) is True, "未覆盖时透传"
    assert mo._is_tianhe_passthrough(answer, "滚动预报核心结论") is False, "被其他工具覆盖后不透传"
    assert mo._is_tianhe_passthrough(answer, None) is False, "应急响应清空 forced_final_text 后不透传"
    assert mo._is_tianhe_passthrough(None, answer) is False, "非天河强收口不透传"
    assert mo._is_tianhe_passthrough(None, None) is False


def _qa_round(q: str, a: str):
    return [mo.HumanMessage(content=q), mo.AIMessage(content=a)]


def test_trim_history_rounds_keeps_recent_n():
    """历史超过 max_rounds 时只保留最近 N 轮，当前轮完整保留，返回新列表不改原列表。"""
    messages = []
    for i in range(10):
        messages += _qa_round(f"问题{i}", f"回答{i}")
    trimmed = mo._trim_history_rounds(messages, max_rounds=6)
    # 保留最近 6 个 HumanMessage 起的内容 = 6 轮 × 2 条 = 12 条
    assert len(trimmed) == 12
    assert trimmed[0].content == "问题4", "应从第 4 个问题开始保留"
    assert trimmed[-1].content == "回答9", "当前轮完整保留"
    assert len(messages) == 20, "原列表不被修改"


def test_trim_history_rounds_under_limit_unchanged():
    """历史未超限时原样返回。"""
    messages = _qa_round("问题0", "回答0") + _qa_round("问题1", "回答1")
    trimmed = mo._trim_history_rounds(messages, max_rounds=6)
    assert trimmed is messages


@pytest.mark.asyncio
async def test_process_message_skips_thinking_when_disabled(monkeypatch):
    """ENABLE_FAST_PATHS=false 且 ENABLE_LLM_THINKING=false 时，thinking_chain 调用次数严格为 0。"""
    monkeypatch.setattr(mo, "ENABLE_FAST_PATHS", False)
    monkeypatch.setattr(mo, "ENABLE_LLM_THINKING", False)

    thinking_calls = []

    async def fake_thinking(*args, **kwargs):
        thinking_calls.append("thinking")
        return None

    async def fake_astream_planner_think(*args, **kwargs):
        class FakePlannerMsg:
            content = "这是一个测试回答。"
            tool_calls = []
        return FakePlannerMsg()

    async def noop_async(*args, **kwargs):
        return None

    class FakeMessage:
        content = "测试查询"

    class FakeStreamMsg:
        def __init__(self, **kw):
            self.content = ""
        async def send(self):
            return None
        async def update(self):
            return None
        async def remove(self):
            return None

    callbacks = {
        "astream_planner_think": fake_astream_planner_think,
        "need_river_plot": lambda message: False,
        "astream_thinking_to_reasoning": fake_thinking,
        "append_followup_if_needed": lambda text, query: text,
        "stream_text_to_message": noop_async,
        "astream_answer_chain_to_message": lambda *a, **k: "",
    }

    monkeypatch.setattr(mo.cl, "Message", FakeStreamMsg)
    # ReasoningStep 保留真实实现需要的 stub 环境；若环境无 Chainlit context，
    # 单独 patch 掉 ReasoningStep 构造与 __aenter__
    class FakeReasoning:
        _closed = False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def stage(self, *a, **k):
            return None
        async def line(self, *a, **k):
            return None
        async def append(self, *a, **k):
            return None
        async def close(self):
            self._closed = True
            return None
    monkeypatch.setattr(mo, "ReasoningStep", lambda name="": FakeReasoning())

    # 全量测试中 process_message 内 cl.user_session.set 需要 Chainlit 上下文；
    # 单独跑本文件时上下文偶然存在，这里统一补一个 http 上下文保证全量可跑。
    # 裸环境（tests/stubs.py 假 chainlit）下无 context 模块，靠假 user_session 兜底。
    try:
        from chainlit.context import context_var, init_http_context
        context_var.set(init_http_context(thread_id="test-skips-thinking"))
    except Exception:
        pass

    await mo.process_message(
        FakeMessage(), planner_chain=None, answer_chain=None,
        thinking_chain=None, tools=[], messages=[], callbacks=callbacks,
    )

    assert thinking_calls == [], f"ENABLE_LLM_THINKING=false 时不应调用 thinking_chain，实际 {thinking_calls}"


@pytest.mark.asyncio
async def test_run_tool_round_uses_parent_step_id(monkeypatch):
    """_run_tool_round 传入 parent_step_id 时，工具 step 应挂到该父 step 下。"""
    created = []

    class FakeStep:
        _id_counter = 0

        def __init__(self, **kwargs):
            type(self)._id_counter += 1
            self.id = f"step-{type(self)._id_counter}"
            self.name = kwargs.get("name", "")
            self.parent_id = kwargs.get("parent_id")
            self.type = kwargs.get("type", "tool")
            self.show_input = True
            self.output = ""
            self.input = ""
            self.default_open = None
            created.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def update(self):
            return None

    class FakePlannerMsg:
        tool_calls = []

    monkeypatch.setattr(mo.cl, "Step", FakeStep)

    callbacks = {"tool_observation_to_text": lambda obs: str(obs)}
    messages = []
    await mo._run_tool_round(
        FakePlannerMsg(), [], messages, "测试", 1, callbacks, parent_step_id="reasoning-step-1"
    )

    round_step = next((s for s in created if s.name.startswith("第 1 轮")), None)
    assert round_step is not None, f"应创建第 1 轮 step，实际 created={[s.name for s in created]}"
    assert round_step.parent_id == "reasoning-step-1", (
        f"第 1 轮 step 应挂到 reasoning-step-1，实际 {round_step.parent_id}"
    )


def test_has_complete_rolling_forecast():
    """最后一个滚动预报 bundle 有 code_section 时视为数据完整。"""
    assert mo._has_complete_rolling_forecast([{"code_section": "| 表格 |"}]) is True
    assert mo._has_complete_rolling_forecast([{"code_section": ""}]) is False
    assert mo._has_complete_rolling_forecast([]) is False
    assert mo._has_complete_rolling_forecast([{"category": "activity"}]) is False


def test_fallback_on_planner_timeout_with_complete_data():
    """Planner 超时且已有完整滚动预报数据时，应回退生成回答而非抛错。"""
    # 直接验证 Fix C 的核心条件：数据完整时允许回退
    assert mo._has_complete_rolling_forecast([{"code_section": "表格"}]) is True


def test_is_failed_tool_observation():
    """失败/占位/天河哨兵观测应被识别为失败，不应当作有效数据输出。"""
    assert mo._is_failed_tool_observation("") is True
    assert mo._is_failed_tool_observation("   ") is True
    assert mo._is_failed_tool_observation("工具未找到：query_xxx") is True
    assert mo._is_failed_tool_observation(
        "工具 query_x 执行失败（TimeoutError），该数据暂不可用。错误摘要：boom"
    ) is True
    for err in est.TIANHE_ERROR_TEXTS:
        assert mo._is_failed_tool_observation(err) is True, f"天河哨兵应判失败: {err}"
    assert mo._is_failed_tool_observation("天河问答服务返回为空。") is True
    assert mo._is_failed_tool_observation("过去24小时全市平均降雨2.5毫米") is False
    assert mo._is_failed_tool_observation("| 时段 | 雨量 |\n| 今夜 | 中雨 |") is False


def test_assemble_tool_observations_fallback_current_round_only():
    """只拼接本轮（最后 HumanMessage 起）的有效工具观测，过滤失败/占位/天河哨兵，不纳入历史轮。"""
    messages = [
        mo.HumanMessage(content="历史问题"),
        mo.AIMessage(content="历史回答"),
        mo.ToolMessage(content="历史工具数据", tool_call_id="h1"),
        mo.HumanMessage(content="今天雨下了多长时间"),
        mo.AIMessage(content=""),
        mo.ToolMessage(content="区域最大雨量：79.8毫米", tool_call_id="c1"),
        mo.ToolMessage(content="工具 query_x 执行失败（TimeoutError），该数据暂不可用。", tool_call_id="c2"),
        mo.ToolMessage(content=est._TIANHE_ERR_UNAVAILABLE, tool_call_id="c3"),
    ]
    result = mo._assemble_tool_observations_fallback(messages)
    assert "区域最大雨量：79.8毫米" in result
    assert "执行失败" not in result, "失败观测应被过滤"
    assert "历史工具数据" not in result, "历史轮次不应纳入"
    assert est._TIANHE_ERR_UNAVAILABLE not in result, "天河哨兵不应输出"


def test_assemble_tool_observations_fallback_all_failed_returns_empty():
    """本轮观测全部失败/占位时返回空串，交由调用方走原错误路径。"""
    messages = [
        mo.HumanMessage(content="今天雨下了多长时间"),
        mo.ToolMessage(content="工具 query_x 执行失败（TimeoutError），该数据暂不可用。", tool_call_id="c1"),
        mo.ToolMessage(content=est._TIANHE_ERR_CONNECT, tool_call_id="c2"),
    ]
    assert mo._assemble_tool_observations_fallback(messages) == ""


@pytest.mark.asyncio
async def test_astream_planner_think_retry_once_succeeds_on_retry():
    """planner 超时后重试一次，第二次成功则返回结果。"""
    calls = {"n": 0}

    async def fake(chain, input_dict, reasoning):
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncio.TimeoutError("planner inference timeout")
        return "ok"

    result = await mo._astream_planner_think_retry_once(
        {"astream_planner_think": fake}, None, [], None
    )
    assert result == "ok"
    assert calls["n"] == 2, "应在超时后重试一次"


@pytest.mark.asyncio
async def test_astream_planner_think_retry_once_reraises_when_still_timeout():
    """重试仍超时应把 TimeoutError 上抛给外层数据兜底。"""
    async def fake(chain, input_dict, reasoning):
        raise asyncio.TimeoutError("planner inference timeout")

    with pytest.raises(asyncio.TimeoutError):
        await mo._astream_planner_think_retry_once(
            {"astream_planner_think": fake}, None, [], None
        )


@pytest.mark.asyncio
async def test_second_planner_timeout_with_complete_data_falls_back_to_answer(monkeypatch):
    """Fix C：第 2 次 Planner 超时且滚动预报数据完整（含应急响应工具综合场景）时，
    应回退调用 Answer LLM 组装回答，而非把超时异常抛给外层兜底返回错误。"""
    monkeypatch.setattr(mo, "ENABLE_FAST_PATHS", False)
    monkeypatch.setattr(mo, "ENABLE_LLM_THINKING", False)

    user_query = "查询海河流域的应急响应，并给出滚动预报"
    answer_text = "应急响应已启动，滚动预报如下。"
    stream_contents: list[str] = []
    final_messages: list = []

    # 第 1 次 planner 返回应急响应 + 滚动预报工具调用；第 2 次 planner 抛超时。
    round_no = {"n": 0}

    def make_planner_msg(tool_calls):
        msg = type("FakePlannerMsg", (), {})()
        msg.content = ""
        msg.tool_calls = tool_calls
        return msg

    async def fake_astream_planner_think(*args, **kwargs):
        round_no["n"] += 1
        if round_no["n"] == 1:
            return make_planner_msg([
                {"id": "c-emergency", "name": "safe_evaluate_haihe_emergency_response", "args": {}},
                {"id": "c-rolling", "name": "query_rolling_forecast", "args": {"user_query": user_query}},
            ])
        raise asyncio.TimeoutError("planner inference timeout")

    async def fake_answer_chain(*args, **kwargs):
        return answer_text

    async def noop_async(*args, **kwargs):
        return None

    class FakeStreamMsg:
        def __init__(self, **kw):
            self.content = ""

        async def send(self):
            return None

        async def update(self):
            stream_contents.append(self.content)
            return None

        async def remove(self):
            return None

    class FakeReasoning:
        _closed = False
        step = type("FakeStep", (), {"id": "reasoning-step"})()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def stage(self, *a, **k):
            return None

        async def line(self, *a, **k):
            return None

        async def append(self, *a, **k):
            return None

        async def close(self):
            self._closed = True
            return None

    class FakeTool:
        def __init__(self, name):
            self.name = name

        async def ainvoke(self, args):
            if self.name == "safe_evaluate_haihe_emergency_response":
                return {"status": "ok", "level": 2, "summary": "Ⅲ级应急响应"}
            return {
                "status": "ok",
                "code_section": "| 时段 | 雨量 |\n| --- | --- |\n| 今夜 | 中雨 |",
                "data_source": "滚动预报测试源",
                "forced_core_conclusion": "今夜至明天有中雨。",
            }

    # 工具分支需要用的 callbacks：_run_tool_round 仅用到 tool_observation_to_text。
    tools = [FakeTool("safe_evaluate_haihe_emergency_response"), FakeTool("query_rolling_forecast")]

    callbacks = {
        "astream_planner_think": fake_astream_planner_think,
        "need_river_plot": lambda message: False,
        "astream_thinking_to_reasoning": noop_async,
        "append_followup_if_needed": lambda text, query: text,
        "stream_text_to_message": noop_async,
        "astream_answer_chain_to_message": fake_answer_chain,
        "tool_observation_to_text": lambda obs: str(obs),
        "enrich_with_impact_time_tool": lambda **k: k.get("observation"),
        "should_force_admin_units_reply": lambda text: False,
        "build_admin_units_only_reply": lambda obs: obs,
        "should_force_partition_table_reply": lambda text: False,
        "build_partition_only_reply": lambda obs: obs,
        "should_force_structured_impact_reply": lambda text: False,
        "build_structured_impact_reply": lambda obs: obs,
    }

    monkeypatch.setattr(mo, "ReasoningStep", lambda name="": FakeReasoning())
    monkeypatch.setattr(mo.cl, "Message", FakeStreamMsg)
    # 固定滚动预报 bundle 构造：确保 code_section 非空（数据完整）且带数据来源。
    monkeypatch.setattr(
        mo,
        "build_rolling_forecast_bundle",
        lambda user_text, payload: {
            "category": "rain",
            "code_section": "| 时段 | 雨量 |\n| --- | --- |\n| 今夜 | 中雨 |",
            "data_source": "滚动预报测试源",
            "forced_core_conclusion": "",
        },
    )

    try:
        from chainlit.context import context_var, init_http_context
        context_var.set(init_http_context(thread_id="test-timeout-fallback"))
    except Exception:
        pass

    await mo.process_message(
        type("FakeMessage", (), {"content": user_query})(),
        planner_chain=None, answer_chain=None,
        thinking_chain=None, tools=tools, messages=final_messages, callbacks=callbacks,
    )

    # 期望：Fix C 回退后 Answer 文本经代码收口组装（含数据来源行）。
    joined = "\n".join(stream_contents)
    assert "应急响应已启动" in joined, f"应回退生成 Answer 回答，实际 stream_msg: {joined!r}"
    assert "滚动预报测试源" in joined, f"应包含滚动预报数据来源，实际: {joined!r}"
    # 最终 messages 末尾应含生成的 AIMessage 回答（前置思考摘要不影响正文包含）。
    assert any(
        "应急响应已启动" in getattr(m, "content", "")
        for m in final_messages
    ), f"messages 末尾应追加回退生成的回答，实际: {[type(m).__name__ for m in final_messages]}"


def _fixb_non_rolling_setup(monkeypatch, planner_behavior, observation):
    """构造一次"非滚动工具 + planner 第 2 次调用超时"的 process_message 全链路 mock。

    planner_behavior(round_no) 决定第 1 次之后的 planner 行为（超时/成功），
    返回 (stream_contents, final_messages, counters, callbacks, tools, user_query)。
    """
    monkeypatch.setattr(mo, "ENABLE_FAST_PATHS", False)
    monkeypatch.setattr(mo, "ENABLE_LLM_THINKING", False)

    # 使用非天河目录问法，专注验证通用 planner 超时重试；天河目录问题现在按业务边界
    # 强制由 query_tianhe_fixed_qa 收口，不再进入本地 planner。
    user_query = "请分析当前综合气象观测数据"
    stream_contents: list[str] = []
    final_messages: list = []
    counters = {"planner": 0, "answer": 0}

    def make_planner_msg(tool_calls, content=""):
        msg = type("FakePlannerMsg", (), {})()
        msg.content = content
        msg.tool_calls = tool_calls
        return msg

    async def fake_astream_planner_think(*args, **kwargs):
        counters["planner"] += 1
        if counters["planner"] == 1:
            return make_planner_msg([
                {"id": "c-cur", "name": "query_current_weather_observation", "args": {}},
            ])
        return planner_behavior(counters["planner"], make_planner_msg)

    async def fake_answer_chain(*args, **kwargs):
        counters["answer"] += 1
        return "LLM生成的答案（不应出现：Fix B 应走代码组装兜底）"

    async def noop_async(*args, **kwargs):
        return None

    async def fake_stream_text(text, stream_msg=None, **kwargs):
        # 复用 planner 内容的路径经 stream_text_to_message 输出（不经 stream_msg.update），
        # 与 update 一样记录到 stream_contents，便于断言最终输出文本。
        stream_contents.append(text)
        return None

    class FakeStreamMsg:
        def __init__(self, **kw):
            self.content = ""

        async def send(self):
            return None

        async def update(self):
            stream_contents.append(self.content)
            return None

        async def remove(self):
            return None

    class FakeReasoning:
        _closed = False
        step = type("FakeStep", (), {"id": "reasoning-step"})()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def stage(self, *a, **k):
            return None

        async def line(self, *a, **k):
            return None

        async def append(self, *a, **k):
            return None

        async def close(self):
            self._closed = True
            return None

    class FakeTool:
        def __init__(self, name):
            self.name = name

        async def ainvoke(self, args):
            return observation

    tools = [FakeTool("query_current_weather_observation")]
    callbacks = {
        "astream_planner_think": fake_astream_planner_think,
        "need_river_plot": lambda message: False,
        "astream_thinking_to_reasoning": noop_async,
        "append_followup_if_needed": lambda text, query: text,
        "stream_text_to_message": fake_stream_text,
        "astream_answer_chain_to_message": fake_answer_chain,
        "tool_observation_to_text": lambda obs: str(obs),
        "enrich_with_impact_time_tool": lambda **k: k.get("observation"),
        "should_force_admin_units_reply": lambda text: False,
        "build_admin_units_only_reply": lambda obs: obs,
        "should_force_partition_table_reply": lambda text: False,
        "build_partition_only_reply": lambda obs: obs,
        "should_force_structured_impact_reply": lambda text: False,
        "build_structured_impact_reply": lambda obs: obs,
    }

    monkeypatch.setattr(mo, "ReasoningStep", lambda name="": FakeReasoning())
    monkeypatch.setattr(mo.cl, "Message", FakeStreamMsg)
    # 非滚动工具不应产生完整滚动 bundle，保证 _has_complete_rolling_forecast 为 False。
    monkeypatch.setattr(mo, "build_rolling_forecast_bundle", lambda user_text, payload: {})

    try:
        from chainlit.context import context_var, init_http_context
        context_var.set(init_http_context(thread_id="test-fixb"))
    except Exception:
        pass

    return stream_contents, final_messages, counters, callbacks, tools, user_query


@pytest.mark.asyncio
async def test_second_planner_retry_still_timeout_assembles_tool_data(monkeypatch):
    """Fix B 核心：非滚动工具已取回数据，planner 第 2 次调用重试仍超时时，
    应用代码把工具观测组装成回答输出（拿到数据必有输出），不再撞慢 LLM、不发 ❌ 错误。"""
    def behavior(round_no, make_planner_msg):
        raise asyncio.TimeoutError("planner inference timeout")

    observation = "过去24小时全市平均降雨2.5毫米，区域最大雨量79.8毫米。"
    stream_contents, final_messages, counters, callbacks, tools, user_query = \
        _fixb_non_rolling_setup(monkeypatch, behavior, observation)

    await mo.process_message(
        type("FakeMessage", (), {"content": user_query})(),
        planner_chain=None, answer_chain=None,
        thinking_chain=None, tools=tools, messages=final_messages, callbacks=callbacks,
    )

    joined = "\n".join(stream_contents)
    assert counters["planner"] == 3, f"planner 应调 3 次（第1次+超时+重试），实际 {counters['planner']}"
    assert counters["answer"] == 0, "Fix B 代码组装兜底不应再调 answer LLM（避免再撞慢服务）"
    assert "区域最大雨量79.8毫米" in joined, f"已取回的工具数据应被组装输出，实际: {joined!r}"
    assert any(
        "区域最大雨量79.8毫米" in getattr(m, "content", "") for m in final_messages
    ), "messages 末尾应追加代码组装的 AIMessage 回答"


@pytest.mark.asyncio
async def test_second_planner_timeout_retry_success(monkeypatch):
    """Fix B：planner 第 2 次调用首次超时、重试成功时，直接用重试结果，不走数据组装。"""
    def behavior(round_no, make_planner_msg):
        if round_no == 2:
            raise asyncio.TimeoutError("planner inference timeout")
        # 重试（第 3 次）成功：planner 直接给出最终回答（无 tool_calls）。
        return make_planner_msg([], content="整理后的答案：今天降雨持续了3小时。")

    observation = "过去24小时全市平均降雨2.5毫米。"
    stream_contents, final_messages, counters, callbacks, tools, user_query = \
        _fixb_non_rolling_setup(monkeypatch, behavior, observation)

    await mo.process_message(
        type("FakeMessage", (), {"content": user_query})(),
        planner_chain=None, answer_chain=None,
        thinking_chain=None, tools=tools, messages=final_messages, callbacks=callbacks,
    )

    joined = "\n".join(stream_contents)
    assert counters["planner"] == 3, f"planner 应调 3 次（第1次+超时+重试成功），实际 {counters['planner']}"
    assert "整理后的答案：今天降雨持续了3小时。" in joined, f"重试成功的回答应输出，实际: {joined!r}"


@pytest.mark.asyncio
async def test_second_planner_timeout_no_usable_data_falls_to_error(monkeypatch):
    """Fix B 边界：planner 重试仍超时且本轮工具全部失败（无可组装数据）时，
    维持原错误路径（loop 外兜底仍可能再调 answer LLM 争取最后一次机会）。"""
    def behavior(round_no, make_planner_msg):
        raise asyncio.TimeoutError("planner inference timeout")

    # 工具抛异常 → 观测为失败文本，_assemble_tool_observations_fallback 返回空。
    monkeypatch.setattr(mo, "ENABLE_FAST_PATHS", False)
    monkeypatch.setattr(mo, "ENABLE_LLM_THINKING", False)
    observation = "工具 query_current_weather_observation 执行失败（TimeoutError），该数据暂不可用。"
    stream_contents, final_messages, counters, callbacks, tools, user_query = \
        _fixb_non_rolling_setup(monkeypatch, behavior, observation)

    await mo.process_message(
        type("FakeMessage", (), {"content": user_query})(),
        planner_chain=None, answer_chain=None,
        thinking_chain=None, tools=tools, messages=final_messages, callbacks=callbacks,
    )

    joined = "\n".join(stream_contents)
    assert counters["planner"] == 3, f"planner 应调 3 次，实际 {counters['planner']}"
    # 负向护栏（终态断言）：无可组装数据时 Fix B 不得误置 answer_generated 短路——
    # 循环外兜底应照常再调 answer LLM 争取最后一次机会。若 Fix B 在 fallback_text 为空时
    # 错误短路，answer 计数会是 0，此断言即红。
    assert counters["answer"] >= 1, (
        f"无可用数据时应走原错误路径+循环外兜底再调 answer LLM，实际 answer 调 {counters['answer']} 次"
    )
    # 失败占位文本不得被组装成 AIMessage 答案输出（Fix B 只在有有效观测时才组装）。
    assert not any(
        isinstance(m, mo.AIMessage) and "执行失败" in getattr(m, "content", "")
        for m in final_messages
    ), "不应把'执行失败'占位文本当答案追加"


@pytest.mark.asyncio
async def test_run_tool_round_direct_historical_assembles_with_hazard(monkeypatch):
    """planner 直调 query_poi_historical_weather → orchestrator 用历史格式化器组装回答并追加隐患点注意事项。"""
    import json as _json

    hist_payload = {
        "status": "ok",
        "query_type": "historical_observation",
        "query_mode": "historical_obs",
        "data_source": "自动站历史实况",
        "lon": 117.2,
        "lat": 39.1,
        "point_name": "同乐小学",
        "forecast_start_time": "2026-07-11 00:00",
        "forecast_end_time": "2026-07-12 00:00",
        "periods": [
            {
                "start_time": "2026-07-11 00:00",
                "end_time": "2026-07-12 00:00",
                "period_label": "7月11日",
                "weather": "暴雨",
                "tmax": 26.0,
                "tmin": 24.0,
                "EDA": "东南风2级",
                "rainfall_mm": 52.5,
                "rain_1h": 52.5,
                "TP1H": 52.5,
                "visibility_min_km": 5.0,
            }
        ],
        "nearest_station": {"station_name": "同乐小学站", "distance_km": 0.5},
    }

    class FakeHistoricalTool:
        name = "query_poi_historical_weather"

        async def ainvoke(self, args):
            return [{"text": _json.dumps(hist_payload, ensure_ascii=False)}]

    class FakeHazardTool:
        name = "query_poi_hazard_reminders"

        async def ainvoke(self, args):
            return [{"text": _json.dumps({
                "status": "ok",
                "total_found": 1,
                "categories": [{"key": "dzzh", "label": "地质灾害", "count": 1}],
            }, ensure_ascii=False)}]

    class FakePlannerMsg:
        tool_calls = [{"name": "query_poi_historical_weather", "args": {}, "id": "call-h1"}]

    class FakeAnswer:
        def __init__(self, text):
            self.content = text

    async def _fake_answer(chain, inputs):
        return FakeAnswer("【核心结论】7月11日同乐小学实际出现暴雨天气，当日累计降水量达52.5毫米。")

    callbacks = {
        "ainvoke_chain": _fake_answer,
        "tool_observation_to_text": lambda obs: str(obs),
    }
    tools = [FakeHistoricalTool(), FakeHazardTool()]
    messages = []
    monkeypatch.setattr(mo.cl, "Step", chainlit.Step)
    # 真实 Chainlit Step 需要上下文；裸环境使用 tests/stubs.py 的无上下文 Step。
    if hasattr(chainlit, "__path__"):
        from chainlit.context import context_var, init_http_context

        context_var.set(init_http_context(thread_id="test-direct-historical"))

    forced, ree, bundles, rolling_bundles, _ = await mo._run_tool_round(
        FakePlannerMsg(), tools, messages, "同乐小学7月11日天气怎么样", 1, callbacks,
        answer_chain=object(),
    )

    assert forced is not None
    assert "【核心结论】" in forced
    assert "【同乐小学历史实况】" in forced
    assert "【注意事项】" in forced
    assert "风险研判" in forced
    assert "当日实际" in forced
    assert "数据来源：自动站历史实况。" in forced
    assert not any("执行失败" in getattr(m, "content", "") for m in messages)


@pytest.mark.parametrize(
    "hist_payload, expected_fragment, expect_forced",
    [
        (
            {"status": "no_data", "start_time": "2026-07-10 00:00", "end_time": "2026-07-11 00:00",
             "message": "未查询到 2026-07-10 00:00 至 2026-07-11 00:00 的历史实况数据，该时段内无可用自动站观测。"},
            "暂无可用历史实况数据",
            False,
        ),
        (
            {"status": "error", "message": "历史实况服务不可用，请稍后重试。"},
            "历史实况查询暂不可用",
            False,
        ),
    ],
)
@pytest.mark.asyncio
async def test_run_tool_round_direct_historical_non_ok_no_crash(monkeypatch, hist_payload, expected_fragment, expect_forced):
    """planner 直调 query_poi_historical_weather 返回 no_data/error → 不抛 UnboundLocalError。

    回归：修复前该分支只在 status=="ok" 时赋值 observation_text，非 ok 结果到达
    messages.append(ToolMessage(content=observation_text)) 处抛
    "local variable 'observation_text' referenced before assignment"
    （生产问题“7月10号天津市天气怎么样”）。
    """
    import json as _json

    class FakeHistoricalTool:
        name = "query_poi_historical_weather"

        async def ainvoke(self, args):
            return [{"text": _json.dumps(hist_payload, ensure_ascii=False)}]

    class FakePlannerMsg:
        tool_calls = [{"name": "query_poi_historical_weather", "args": {}, "id": "call-h2"}]

    callbacks = {
        "tool_observation_to_text": lambda obs: str(obs),
    }
    tools = [FakeHistoricalTool()]
    messages = []
    monkeypatch.setattr(mo.cl, "Step", chainlit.Step)
    if hasattr(chainlit, "__path__"):
        from chainlit.context import context_var, init_http_context

        context_var.set(init_http_context(thread_id="test-direct-historical-nonok"))

    # 修复前此调用抛 UnboundLocalError；修复后正常返回、非 ok 不强制收口
    forced, ree, bundles, rolling_bundles, _ = await mo._run_tool_round(
        FakePlannerMsg(), tools, messages, "7月10号天津市天气怎么样", 1, callbacks,
        answer_chain=object(),
    )

    assert len(messages) == 1
    tool_msg = messages[0]
    assert isinstance(tool_msg, mo.ToolMessage)
    assert expected_fragment in str(getattr(tool_msg, "content", ""))
    if expect_forced:
        assert forced is not None
    else:
        assert forced is None

