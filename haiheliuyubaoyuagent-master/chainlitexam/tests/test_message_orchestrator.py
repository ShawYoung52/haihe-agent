"""Tests for message_orchestrator feature-flag behavior."""

import asyncio
import importlib
import sys
import time
from pathlib import Path

import langchain_core.messages  # noqa: F401  在 ensure_stubs() 之前导入，避免安装 langchain stub（stub 会丢弃 tool_call_id）
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chainlitexam.tests.stubs import ensure_stubs

ensure_stubs()

import chainlit
import chainlitexam.message_orchestrator as mo


def test_enable_fast_paths_defaults_to_false(monkeypatch):
    """ENABLE_FAST_PATHS reflects the ENABLE_FAST_PATHS environment variable at import time."""
    monkeypatch.setenv("ENABLE_FAST_PATHS", "false")
    importlib.reload(mo)
    assert mo.ENABLE_FAST_PATHS is False

    monkeypatch.setenv("ENABLE_FAST_PATHS", "true")
    importlib.reload(mo)
    assert mo.ENABLE_FAST_PATHS is True


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

    forced, ree, bundles, rolling_bundles = await mo._run_tool_round(
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
    forced, ree, bundles, rolling_bundles = await mo._run_tool_round(
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


class _UserSessionStub:
    """no-op 的 cl.user_session 替身（真实 chainlit 的需 Chainlit context）。"""

    def get(self, *args, **kwargs):
        return None

    def set(self, *args, **kwargs):
        return None


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
    # 真实 chainlit 的 user_session 需 Chainlit context；tianhe 收口会 set("tianhe_passthrough")，
    # 无 context 时抛 ChainlitContextException 被记成工具失败。stub 成 no-op 命名空间。
    monkeypatch.setattr(
        mo.cl,
        "user_session",
        _UserSessionStub(),
    )
    callbacks = {"tool_observation_to_text": lambda obs: str(obs)}
    messages: list = []
    return FakePlannerMsg(), [FakeTool()], messages, callbacks


@pytest.mark.asyncio
async def test_tianhe_answer_passthrough_sets_forced_final_text(monkeypatch):
    """天河返回完整 answer 时，应直接作为 forced_final_text 收口（跳过 answer LLM 原样透传）。"""
    answer = "【核心结论】\n全市近24小时平均降雨量：2.5毫米\n最大降雨量：79.8毫米"
    planner_msg, tools, messages, callbacks = _tianhe_round_setup(monkeypatch, answer)

    forced, ree, bundles, rolling_bundles = await mo._run_tool_round(
        planner_msg, tools, messages, "全市现在下了多少雨", 1, callbacks
    )

    assert forced == answer, "天河 answer 应原样作为 forced_final_text 透传"
    assert rolling_bundles == [], "天河不是滚动预报，不应产生 bundle"
    assert len(messages) == 1
    assert messages[0].content == answer, "ToolMessage 也应保留原文供历史记录"


@pytest.mark.asyncio
async def test_tianhe_degraded_answer_also_passthrough(monkeypatch):
    """天河 HTTP 200 但返回降级文案时，按对接文档 9.4 原样展示，不走本地兜底也不再过 LLM。"""
    degraded = "智能体服务暂时不可用，请稍后重试。"
    planner_msg, tools, messages, callbacks = _tianhe_round_setup(monkeypatch, degraded)

    forced, ree, bundles, rolling_bundles = await mo._run_tool_round(
        planner_msg, tools, messages, "今天雨下了多长时间", 1, callbacks
    )

    assert forced == degraded, "降级文案同样原样透传（文档 9.4），不当失败、不过 LLM"


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
