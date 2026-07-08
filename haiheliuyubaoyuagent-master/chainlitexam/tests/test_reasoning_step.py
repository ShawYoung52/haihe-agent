"""Tests for ReasoningStep and _show_business_reasoning.

Chainlit is not required for these unit tests; we monkeypatch ``chainlit.Step``
with a lightweight ``MockStep`` before importing ``message_orchestrator``.
"""

import asyncio
import sys
import types
from pathlib import Path

# Make ``import chainlitexam`` work when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class MockStep:
    """Lightweight stand-in for ``chainlit.Step``."""

    _instances: list["MockStep"] = []

    def __init__(self, **kwargs):
        self.name = kwargs.get("name", "")
        self.parent_id = kwargs.get("parent_id")
        self.type = kwargs.get("type", "")
        self.input = ""
        self.output = ""
        self.show_input = ""
        self.id = kwargs.get("id") or f"mock-step-{len(MockStep._instances)}"
        MockStep._instances.append(self)

    async def send(self):
        pass

    async def update(self):
        pass

    @classmethod
    def reset(cls):
        cls._instances.clear()


# Mock optional dependencies before importing message_orchestrator.
if "chainlit" not in sys.modules:
    chainlit = types.ModuleType("chainlit")
    chainlit.Step = MockStep
    chainlit.Message = type(
        "Message", (), {"send": lambda self: None, "remove": lambda self: None}
    )
    chainlit.user_session = types.SimpleNamespace(
        get=lambda *a, **k: None, set=lambda *a, **k: None
    )
    sys.modules["chainlit"] = chainlit
else:
    import chainlit

    chainlit.Step = MockStep

if "langchain_core.messages" not in sys.modules:
    lcms = types.ModuleType("langchain_core.messages")
    for name in ("ToolMessage", "HumanMessage", "AIMessage"):
        setattr(lcms, name, type(name, (), {}))
    sys.modules["langchain_core.messages"] = lcms
    sys.modules["langchain_core"] = types.ModuleType("langchain_core")

if "httpx" not in sys.modules:
    sys.modules["httpx"] = types.ModuleType("httpx")

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


if __name__ == "__main__":
    asyncio.run(test_show_business_reasoning_creates_three_stages())
    asyncio.run(test_reasoning_step_close_idempotent())
    asyncio.run(test_stage_after_close_returns_none())
    print("All tests passed.")
