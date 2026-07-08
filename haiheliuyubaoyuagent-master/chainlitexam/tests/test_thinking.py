"""Tests for generate_fast_path_thinking helper.

This module mocks ``thinking_chain.ainvoke()`` to verify both the happy path
and the fallback behavior when the chain raises an exception.
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


async def test_generate_fast_path_thinking_returns_content():
    class MockChain:
        async def ainvoke(self, inputs):
            return FakeAIMessage("我将查询实况降雨数据并生成分布图。")

    result = await generate_fast_path_thinking(
        MockChain(),
        user_text="海河流域降雨分布图",
        intent_text="生成海河流域降水实况分布图",
        data_sources=["实况降雨站点数据"],
    )
    assert result == "我将查询实况降雨数据并生成分布图。"


async def test_generate_fast_path_thinking_returns_empty_on_exception():
    class BrokenChain:
        async def ainvoke(self, inputs):
            raise RuntimeError("model unavailable")

    result = await generate_fast_path_thinking(
        BrokenChain(),
        user_text="未来三天海河流域天气",
        intent_text="查询未来天气预报",
        data_sources=["ECMWF AIFS 预报数据"],
    )
    assert result == ""


async def test_generate_fast_path_thinking_returns_empty_when_no_content():
    class EmptyChain:
        async def ainvoke(self, inputs):
            return FakeAIMessage("")

    result = await generate_fast_path_thinking(
        EmptyChain(),
        user_text="水位情况",
        intent_text="查询河网水位",
        data_sources=["河网水位站点数据"],
    )
    assert result == ""


if __name__ == "__main__":
    asyncio.run(test_generate_fast_path_thinking_returns_content())
    asyncio.run(test_generate_fast_path_thinking_returns_empty_on_exception())
    asyncio.run(test_generate_fast_path_thinking_returns_empty_when_no_content())
    print("All tests passed.")
