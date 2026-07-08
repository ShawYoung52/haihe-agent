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
        self.collapsed = None
        self.id = kwargs.get("id") or f"mock-step-{len(MockStep._instances)}"
        MockStep._instances.append(self)

    async def send(self):
        pass

    async def update(self):
        pass

    @classmethod
    def reset(cls):
        cls._instances.clear()


# Ensure message_orchestrator uses our mock when it imports chainlit.Step.
chainlit.Step = MockStep

from chainlitexam.message_orchestrator import ReasoningStep, _show_business_reasoning


async def test_show_business_reasoning_creates_three_stages():
    MockStep.reset()
    await _show_business_reasoning(
        "测试意图", ["数据源A", "数据源B"], "将给出测试结论"
    )
    # 1 parent step + 3 business stages
    assert len(MockStep._instances) == 4
    names = [s.name for s in MockStep._instances]
    assert names[0] == "🤔 思考过程"
    assert names[1] == "🔍 理解问题"
    assert names[2] == "📡 查询数据"
    assert names[3] == "✍️ 生成结论"


async def test_stage_parent_id_relationships():
    MockStep.reset()
    reasoning = ReasoningStep()
    await reasoning.__aenter__()
    await reasoning.stage("子阶段1", "详情1")
    await reasoning.stage("子阶段2", "详情2")

    assert len(MockStep._instances) == 3
    parent = MockStep._instances[0]
    child1 = MockStep._instances[1]
    child2 = MockStep._instances[2]

    assert parent.id is not None
    assert child1.parent_id == parent.id
    assert child2.parent_id == parent.id

    await reasoning.close()


async def test_append_and_line_buffer_accumulation():
    MockStep.reset()
    reasoning = ReasoningStep()
    await reasoning.__aenter__()

    await reasoning.append("第一行")
    await reasoning.line("第二行")
    await reasoning.append("第三行")

    # Without an active sub-stage, text accumulates in the parent buffer.
    assert reasoning._buffer == "第一行第二行\n第三行"
    assert reasoning.step.output == reasoning._buffer

    await reasoning.close()


async def test_append_updates_current_stage():
    MockStep.reset()
    reasoning = ReasoningStep()
    await reasoning.__aenter__()
    await reasoning.stage("当前阶段", "初始")
    await reasoning.append("追加内容")

    assert reasoning._current_stage is not None
    assert reasoning._current_stage.output == "初始追加内容"

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


async def test_stage_after_close_returns_none():
    MockStep.reset()
    reasoning = ReasoningStep()
    await reasoning.__aenter__()
    await reasoning.close()
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
    assert reasoning.step.collapsed is False
    await reasoning.close()


async def test_step_collapses_on_close():
    MockStep.reset()
    reasoning = ReasoningStep()
    await reasoning.__aenter__()
    await reasoning.close()
    assert reasoning.step is not None
    assert reasoning.step.collapsed is True


if __name__ == "__main__":
    asyncio.run(test_show_business_reasoning_creates_three_stages())
    asyncio.run(test_stage_parent_id_relationships())
    asyncio.run(test_append_and_line_buffer_accumulation())
    asyncio.run(test_append_updates_current_stage())
    asyncio.run(test_reasoning_step_close_idempotent())
    asyncio.run(test_stage_after_close_returns_none())
    asyncio.run(test_aexit_closes_on_exception())
    asyncio.run(test_aexit_preserves_original_exception())
    asyncio.run(test_step_initially_expanded())
    asyncio.run(test_step_collapses_on_close())
    print("All tests passed.")
