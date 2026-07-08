"""Tests for the business summary prefix builder."""

import sys
import types
from pathlib import Path

# Make ``import chainlitexam`` work when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Provide minimal stubs for optional dependencies so the module can be imported
# in environments that do not have Chainlit/LangChain installed.
if "chainlit" not in sys.modules:
    chainlit = types.ModuleType("chainlit")

    class _Step:
        def __init__(self, **kwargs):
            self.name = kwargs.get("name", "")
            self.parent_id = kwargs.get("parent_id")
            self.type = kwargs.get("type", "")
            self.input = ""
            self.output = ""
            self.show_input = ""

        async def send(self):
            pass

        async def update(self):
            pass

    chainlit.Step = _Step
    chainlit.Message = type(
        "Message", (), {"send": lambda self: None, "remove": lambda self: None}
    )
    chainlit.user_session = types.SimpleNamespace(
        get=lambda *a, **k: None, set=lambda *a, **k: None
    )
    sys.modules["chainlit"] = chainlit

if "langchain_core.messages" not in sys.modules:
    lcms = types.ModuleType("langchain_core.messages")
    for name in ("ToolMessage", "HumanMessage", "AIMessage"):
        setattr(lcms, name, type(name, (), {}))
    sys.modules["langchain_core.messages"] = lcms
    sys.modules["langchain_core"] = types.ModuleType("langchain_core")

if "httpx" not in sys.modules:
    sys.modules["httpx"] = types.ModuleType("httpx")

from chainlitexam.message_orchestrator import _build_thinking_summary


def test_rainfall_distribution_summary():
    result = _build_thinking_summary("海河流域降雨分布图")
    assert isinstance(result, str)
    assert result.startswith("已生成海河流域降水实况分布图")


def test_forecast_summary():
    result = _build_thinking_summary("未来三天降雨如何")
    assert isinstance(result, str)
    assert "预报数据" in result


def test_empty_query():
    assert _build_thinking_summary("") == ""


def test_has_chart():
    result = _build_thinking_summary("未来三天降雨如何", has_chart=True)
    assert "并生成相关图表" in result


if __name__ == "__main__":
    test_rainfall_distribution_summary()
    test_forecast_summary()
    test_empty_query()
    test_has_chart()
    print("All tests passed.")
