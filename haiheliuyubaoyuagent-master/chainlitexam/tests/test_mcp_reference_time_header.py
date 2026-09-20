# -*- coding: utf-8 -*-
"""MCP interceptor：只在请求级锚点存在时才注入 header。

锁死"没带锚点就一字节不加"——那保证没带锚点的请求（含所有旧客户端）行为
与改动前逐字一致，MCP 侧继续自己读全局覆盖文件。
"""
from __future__ import annotations

import pytest

pytest.importorskip("langchain_mcp_adapters", reason="需要 langchain-mcp-adapters")

from langchain_mcp_adapters.interceptors import MCPToolCallRequest  # noqa: E402

import mcp_loader  # noqa: E402
from utils import time_source  # noqa: E402


@pytest.fixture()
def sim_file(tmp_path, monkeypatch):
    f = tmp_path / "sim.json"
    monkeypatch.setenv("SIM_TIME_FILE", str(f))
    time_source._invalidate()
    yield f
    time_source._invalidate()


def _request(headers=None):
    return MCPToolCallRequest(name="t", args={}, server_name="weather", headers=headers)


async def _capture():
    """跑一次 interceptor，返回 handler 实际收到的 request。"""
    seen = {}

    async def handler(req):
        seen["request"] = req
        return "ok"

    return seen, handler


@pytest.mark.asyncio
async def test_no_request_anchor_injects_nothing(sim_file):
    """没带请求锚点时一字节不加——MCP 侧继续自己读全局文件。"""
    time_source.set_override_from_text("2026-07-10 15:00:00")
    seen, handler = await _capture()
    await mcp_loader._inject_reference_time(_request(headers={"X-Existing": "1"}), handler)
    assert seen["request"].headers == {"X-Existing": "1"}


@pytest.mark.asyncio
async def test_fixed_anchor_injected(sim_file):
    seen, handler = await _capture()
    token = time_source.set_request_override("2026-03-05T08:30:00+08:00")
    try:
        await mcp_loader._inject_reference_time(_request(), handler)
    finally:
        time_source.reset_request_override(token)
    assert seen["request"].headers[time_source.REFERENCE_TIME_HEADER] == "2026-03-05T08:30:00+08:00"


@pytest.mark.asyncio
async def test_real_anchor_injected_as_sentinel(sim_file):
    """real 也要发：否则 MCP 侧会回落全局文件，"我要真实时间"就失效了。"""
    seen, handler = await _capture()
    token = time_source.set_request_override("real")
    try:
        await mcp_loader._inject_reference_time(_request(), handler)
    finally:
        time_source.reset_request_override(token)
    assert seen["request"].headers[time_source.REFERENCE_TIME_HEADER] == "real"


@pytest.mark.asyncio
async def test_existing_headers_preserved(sim_file):
    seen, handler = await _capture()
    token = time_source.set_request_override("2026-03-05T08:30:00+08:00")
    try:
        await mcp_loader._inject_reference_time(_request(headers={"X-Existing": "1"}), handler)
    finally:
        time_source.reset_request_override(token)
    assert seen["request"].headers["X-Existing"] == "1"
    assert time_source.REFERENCE_TIME_HEADER in seen["request"].headers


@pytest.mark.asyncio
async def test_handler_result_passes_through(sim_file):
    """interceptor 不得改写工具返回值。"""
    async def handler(req):
        return {"content": "原始结果"}

    token = time_source.set_request_override("2026-03-05T08:30:00+08:00")
    try:
        result = await mcp_loader._inject_reference_time(_request(), handler)
    finally:
        time_source.reset_request_override(token)
    assert result == {"content": "原始结果"}
