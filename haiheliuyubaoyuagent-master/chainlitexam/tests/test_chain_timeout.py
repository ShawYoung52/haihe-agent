"""Planner 推理超时不重试、连接错误重试测试。

验证 chain_gzt.ainvoke_chain / astream_planner_think：
- 推理超时（asyncio.TimeoutError）不重试，调用次数为 1，直接抛出。
- 连接类错误（ConnectionError / httpx.ConnectError / httpx.ReadTimeout）重试直到成功或耗尽。
"""

import asyncio
import os
import sys
import types
from pathlib import Path

import httpx

# 跳过 SQLAlchemyDataLayer 初始化，避免 asyncpg 依赖（与 test_execution_mode.py 一致）
os.environ["CHAINLIT_ENABLE_DB"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# 只 mock 真正缺失的依赖（不 clobber 真实 chainlit）
for _mod, _cls in (
    ("langchain_mcp_adapters", "MultiServerMCPClient"),
    ("langchain_mcp_adapters.client", "MultiServerMCPClient"),
    ("langchain_openai", "ChatOpenAI"),
):
    if _mod not in sys.modules:
        m = types.ModuleType(_mod)
        setattr(m, _cls, type(_cls, (), {}))
        sys.modules[_mod] = m

import pytest


class _TimeoutChain:
    """每次都抛 asyncio.TimeoutError，并记录调用次数。"""

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, input_dict, config=None):
        self.calls += 1
        raise asyncio.TimeoutError()

    async def astream(self, input_dict, config=None):
        self.calls += 1
        raise asyncio.TimeoutError()
        yield  # pragma: no cover


class _ConnErrorChain:
    """前 max_retries-1 次抛 ConnectionError，之后成功。"""

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, input_dict, config=None):
        self.calls += 1
        if self.calls < 2:
            raise ConnectionError("connect failed")
        return "ok"


class _ReadTimeoutChain:
    """抛 httpx.ReadTimeout，验证其被归为可重试的连接/限流类错误。"""

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, input_dict, config=None):
        self.calls += 1
        if self.calls < 2:
            raise httpx.ReadTimeout("read timed out")
        return "ok"


class _Chunk:
    """带 .content 的流式 chunk（模拟 AIMessageChunk 的最小接口）。"""

    def __init__(self, text):
        self.content = text


class _AstreamTimeoutChain:
    """astream 每次抛 asyncio.TimeoutError。"""

    def __init__(self):
        self.calls = 0

    async def astream(self, input_dict, config=None):
        self.calls += 1
        raise asyncio.TimeoutError()
        yield  # pragma: no cover


class _AstreamConnErrorChain:
    """astream 前 max_retries-1 次抛 ConnectionError，之后 yield 一个 chunk。"""

    def __init__(self):
        self.calls = 0

    async def astream(self, input_dict, config=None):
        self.calls += 1
        if self.calls < 2:
            raise ConnectionError("connect failed")
        yield _Chunk("ok")


class _FakeReasoningStep:
    async def append(self, token):
        pass


async def test_ainvoke_chain_timeout_no_retry(monkeypatch):
    """推理超时（TimeoutError）不重试：只调用 1 次，直接抛错。"""
    import chain_gzt

    monkeypatch.setenv("PLANNER_MAX_RETRIES", "2")
    chain = _TimeoutChain()
    with pytest.raises(asyncio.TimeoutError):
        await chain_gzt.ainvoke_chain(chain, {"messages": []})
    assert chain.calls == 1, f"超时不应重试，实际调用 {chain.calls} 次"


async def test_ainvoke_chain_conn_error_retries(monkeypatch):
    """连接错误（ConnectionError）重试直到成功。"""
    import chain_gzt

    monkeypatch.setenv("PLANNER_MAX_RETRIES", "2")
    result = await chain_gzt.ainvoke_chain(_ConnErrorChain(), {"messages": []})
    assert result == "ok"


async def test_astream_planner_think_timeout_no_retry(monkeypatch):
    """astream_planner_think 推理超时不重试：只调用 1 次，直接抛错。"""
    import chain_gzt

    monkeypatch.setenv("PLANNER_MAX_RETRIES", "2")
    chain = _AstreamTimeoutChain()
    with pytest.raises(asyncio.TimeoutError):
        await chain_gzt.astream_planner_think(chain, {"messages": []}, _FakeReasoningStep())
    assert chain.calls == 1, f"超时不应重试，实际调用 {chain.calls} 次"


async def test_astream_planner_think_conn_error_retries(monkeypatch):
    """astream_planner_think 连接错误重试直到成功。"""
    import chain_gzt

    monkeypatch.setenv("PLANNER_MAX_RETRIES", "2")
    result = await chain_gzt.astream_planner_think(
        _AstreamConnErrorChain(), {"messages": []}, _FakeReasoningStep()
    )
    assert result.content == "ok"


async def test_ainvoke_chain_httpx_read_timeout_retries(monkeypatch):
    """httpx.ReadTimeout 视为连接/限流类错误，重试直到成功。"""
    import chain_gzt

    monkeypatch.setenv("PLANNER_MAX_RETRIES", "2")
    result = await chain_gzt.ainvoke_chain(_ReadTimeoutChain(), {"messages": []})
    assert result == "ok"
