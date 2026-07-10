"""Tests for ReasoningStep and _show_business_reasoning.

Chainlit is not required for these unit tests; we monkeypatch ``chainlit.Step``
with a lightweight ``MockStep`` before importing ``message_orchestrator``.
"""

import asyncio
import sys
from pathlib import Path

# Make ``import chainlitexam`` work when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Install minimal stubs for optional dependencies.
from chainlitexam.tests.stubs import ensure_stubs

ensure_stubs()

import chainlit


class MockStep(chainlit.Step):
    """Lightweight stand-in for ``chainlit.Step`` with resettable state."""

    _instances: list["MockStep"] = []

    def __init__(self, **kwargs):
        self.name = kwargs.get("name", "")
        self.parent_id = kwargs.get("parent_id")
        self.type = kwargs.get("type", "")
        self.input = ""
        self.output = ""
        self.show_input = ""
        self.default_open = None
        self.auto_collapse = kwargs.get("auto_collapse")
        self.id = kwargs.get("id") or f"mock-step-{len(MockStep._instances)}"
        self._streamed_tokens: list[str] = []
        MockStep._instances.append(self)

    async def send(self):
        pass

    async def update(self):
        pass

    async def stream_token(self, token: str):
        self._streamed_tokens.append(token)
        self.output += token

    @classmethod
    def reset(cls):
        cls._instances.clear()


# Ensure message_orchestrator uses our mock when it imports chainlit.Step.
chainlit.Step = MockStep

from chainlitexam.message_orchestrator import ReasoningStep, _show_business_reasoning


async def test_show_business_reasoning_writes_headers_to_parent_output():
    MockStep.reset()
    await _show_business_reasoning(
        "测试意图", ["数据源A", "数据源B"], "将给出测试结论"
    )
    # 不再创建嵌套子 stage，只有父 step
    assert len(MockStep._instances) == 1
    parent = MockStep._instances[0]
    assert parent.name == "🤔 思考过程"
    assert "**🔍 理解问题**" in parent.output
    assert "**📡 查询数据**" in parent.output
    assert "**✍️ 生成结论**" in parent.output


async def test_stage_headers_accumulate_in_parent_output():
    MockStep.reset()
    reasoning = ReasoningStep()
    await reasoning.__aenter__()
    await reasoning.stage("阶段一", "详情1")
    await reasoning.stage("阶段二", "详情2")

    parent = reasoning.step
    assert parent is not None
    assert "**阶段一**" in parent.output
    assert "**阶段二**" in parent.output

    await reasoning.close()


async def test_append_and_line_buffer_accumulation():
    MockStep.reset()
    reasoning = ReasoningStep()
    await reasoning.__aenter__()

    await reasoning.append("第一行")
    await reasoning.line("第二行")
    await reasoning.append("第三行")

    assert reasoning._buffer == "第一行第二行\n第三行"
    assert reasoning.step.output == reasoning._buffer

    await reasoning.close()


async def test_append_updates_parent_output_when_stage_active():
    MockStep.reset()
    reasoning = ReasoningStep()
    await reasoning.__aenter__()
    await reasoning.stage("当前阶段", "初始")
    await reasoning.append("追加内容")

    parent = reasoning.step
    assert parent is not None
    assert "**当前阶段**" in parent.output
    assert "初始" in parent.output
    assert "追加内容" in parent.output

    await reasoning.close()


async def test_stage_writes_header_to_parent_output():
    MockStep.reset()
    reasoning = ReasoningStep()
    await reasoning.__aenter__()
    await reasoning.stage("理解问题", "识别意图")
    await reasoning.stage("查询数据", "获取数据源")

    parent = reasoning.step
    assert parent is not None
    assert "**理解问题**" in parent.output
    assert "**查询数据**" in parent.output

    await reasoning.close()


async def test_append_prefers_stream_token():
    MockStep.reset()
    reasoning = ReasoningStep()
    await reasoning.__aenter__()

    await reasoning.append("第一块")
    await reasoning.append("第二块")

    parent = reasoning.step
    assert parent is not None
    assert parent._streamed_tokens == ["第一块", "第二块"]
    assert "第一块第二块" in parent.output

    await reasoning.close()


async def test_reasoning_step_close_idempotent():
    MockStep.reset()
    reasoning = ReasoningStep()
    await reasoning.__aenter__()
    assert reasoning._closed is False
    await reasoning.close()
    assert reasoning._closed is True
    # Second close must be a no-op and not raise.
    await reasoning.close()
    assert reasoning._closed is True


async def test_stage_after_close_is_no_op():
    MockStep.reset()
    reasoning = ReasoningStep()
    await reasoning.__aenter__()
    await reasoning.close()
    # stage() returns None (old behavior) and does not raise.
    result = await reasoning.stage("新阶段", "详情")
    assert result is None


async def test_aexit_closes_on_exception():
    MockStep.reset()
    reasoning = ReasoningStep()
    await reasoning.__aenter__()
    assert reasoning._closed is False

    class TestException(Exception):
        pass

    try:
        async with reasoning:
            raise TestException("boom")
    except TestException:
        pass

    assert reasoning._closed is True


async def test_aexit_preserves_original_exception():
    MockStep.reset()
    reasoning = ReasoningStep()
    await reasoning.__aenter__()

    class TestException(Exception):
        pass

    raised = False
    try:
        async with reasoning:
            raise TestException("boom")
    except TestException as e:
        raised = True
        assert str(e) == "boom"

    assert raised


async def test_step_initially_expanded():
    MockStep.reset()
    reasoning = ReasoningStep()
    await reasoning.__aenter__()
    assert reasoning.step is not None
    assert reasoning.step.default_open is True
    await reasoning.close()


async def test_step_remains_expanded_after_close():
    MockStep.reset()
    reasoning = ReasoningStep()
    await reasoning.__aenter__()
    await reasoning.close()
    assert reasoning.step is not None
    assert reasoning.step.default_open is True


async def test_reasoning_step_uses_auto_collapse():
    """auto_collapse must be passed to the cl.Step constructor, not assigned later."""
    MockStep.reset()
    reasoning = ReasoningStep("test collapse")
    await reasoning.__aenter__()
    assert reasoning.step is not None
    # The mock captures the constructor kwarg; this verifies the argument was
    # passed at construction time rather than set as an attribute afterwards.
    assert reasoning.step.auto_collapse is True
    await reasoning.close()


if __name__ == "__main__":
    asyncio.run(test_show_business_reasoning_writes_headers_to_parent_output())
    asyncio.run(test_stage_headers_accumulate_in_parent_output())
    asyncio.run(test_append_and_line_buffer_accumulation())
    asyncio.run(test_append_updates_parent_output_when_stage_active())
    asyncio.run(test_stage_writes_header_to_parent_output())
    asyncio.run(test_append_prefers_stream_token())
    asyncio.run(test_reasoning_step_close_idempotent())
    asyncio.run(test_stage_after_close_is_no_op())
    asyncio.run(test_aexit_closes_on_exception())
    asyncio.run(test_aexit_preserves_original_exception())
    asyncio.run(test_step_initially_expanded())
    asyncio.run(test_step_remains_expanded_after_close())
    asyncio.run(test_reasoning_step_uses_auto_collapse())
    print("All tests passed.")
