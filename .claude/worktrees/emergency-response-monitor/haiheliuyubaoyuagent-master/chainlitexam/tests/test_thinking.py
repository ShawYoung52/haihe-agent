"""Tests for generate_fast_path_thinking helper.

This module mocks ``thinking_chain.astream()`` to verify streaming append
behavior, as well as the timeout and fallback exception paths.
"""

import asyncio
import sys
from pathlib import Path

# Make ``import chainlitexam`` work when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Install minimal stubs for optional dependencies.
from chainlitexam.tests.stubs import ensure_stubs

ensure_stubs()

from chainlitexam.message_orchestrator import generate_fast_path_thinking


class FakeAIMessage:
    def __init__(self, content: str):
        self.content = content


class FakeReasoningStep:
    """Minimal ReasoningStep stand-in that records appended text."""

    def __init__(self):
        self.output = ""

    async def append(self, text: str):
        self.output += text

    async def line(self, text: str):
        await self.append(text + "\n")


async def test_generate_fast_path_thinking_appends_content():
    class MockChain:
        async def astream(self, inputs):
            for token in ["我将", "查询", "实况降雨数据", "并生成分布图。"]:
                yield FakeAIMessage(token)

    reasoning = FakeReasoningStep()
    await generate_fast_path_thinking(
        MockChain(),
        user_text="海河流域降雨分布图",
        intent_text="生成海河流域降水实况分布图",
        data_sources=["实况降雨站点数据"],
        reasoning=reasoning,
    )
    assert reasoning.output == "我将查询实况降雨数据并生成分布图。"


async def test_generate_fast_path_thinking_appends_exception_message():
    class BrokenChain:
        async def astream(self, inputs):
            raise RuntimeError("model unavailable")
            yield FakeAIMessage("")  # makes this an async generator

    reasoning = FakeReasoningStep()
    await generate_fast_path_thinking(
        BrokenChain(),
        user_text="未来三天海河流域天气",
        intent_text="查询未来天气预报",
        data_sources=["ECMWF AIFS 预报数据"],
        reasoning=reasoning,
    )
    assert "思考生成遇到异常" in reasoning.output
    assert "继续为您查询数据" in reasoning.output


async def test_generate_fast_path_thinking_appends_timeout_message():
    class TimeoutChain:
        async def astream(self, inputs):
            yield FakeAIMessage("开始思考...")
            raise asyncio.TimeoutError()

    reasoning = FakeReasoningStep()
    await generate_fast_path_thinking(
        TimeoutChain(),
        user_text="水位情况",
        intent_text="查询河网水位",
        data_sources=["河网水位站点数据"],
        reasoning=reasoning,
    )
    assert "开始思考..." in reasoning.output
    assert "思考生成超时" in reasoning.output
    assert "继续为您查询数据" in reasoning.output


async def test_generate_fast_path_thinking_handles_empty_content():
    class EmptyChain:
        async def astream(self, inputs):
            yield FakeAIMessage("")

    reasoning = FakeReasoningStep()
    await generate_fast_path_thinking(
        EmptyChain(),
        user_text="水位情况",
        intent_text="查询河网水位",
        data_sources=["河网水位站点数据"],
        reasoning=reasoning,
    )
    assert reasoning.output == ""


if __name__ == "__main__":
    asyncio.run(test_generate_fast_path_thinking_appends_content())
    asyncio.run(test_generate_fast_path_thinking_appends_exception_message())
    asyncio.run(test_generate_fast_path_thinking_appends_timeout_message())
    asyncio.run(test_generate_fast_path_thinking_handles_empty_content())
    print("All tests passed.")
