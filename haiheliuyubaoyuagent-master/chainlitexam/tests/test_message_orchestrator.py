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
