# -*- coding: utf-8 -*-
"""MCP interceptor：只在请求级锚点存在时才注入 header。

锁死"没带锚点就一字节不加"——那保证没带锚点的请求（含所有旧客户端）行为
与改动前逐字一致，MCP 侧继续自己读全局覆盖文件。

注意：本文件**不在模块级 import** `langchain_mcp_adapters.interceptors`。全量跑时
别的测试文件会把假 `langchain_mcp_adapters` 装进 sys.modules（见
tests/test_execution_mode.py），那是个非包 ModuleType，
`langchain_mcp_adapters.interceptors` 会导入失败、把整个测试文件带崩。改用下面的
最小替身测我们自己的逻辑，另留一条对真实类的守卫断言。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

import pytest

import mcp_loader  # noqa: E402
from utils import time_source  # noqa: E402


@dataclass
class _FakeRequest:
    """`MCPToolCallRequest` 的最小替身：只保留 interceptor 用到的 members。

    真实类（langchain_mcp_adapters.interceptors）暴露的正是 `headers` 读 +
    `override(headers=...)` 返回新实例，跨进程投递本身由探针脚本实测。
    """

    name: str = "t"
    args: dict = field(default_factory=dict)
    server_name: str = "weather"
    headers: dict | None = None

    def override(self, **overrides):
        return replace(self, **overrides)


@pytest.fixture()
def sim_file(tmp_path, monkeypatch):
    f = tmp_path / "sim.json"
    monkeypatch.setenv("SIM_TIME_FILE", str(f))
    time_source._invalidate()
    yield f
    time_source._invalidate()


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
    await mcp_loader._inject_reference_time(_FakeRequest(headers={"X-Existing": "1"}), handler)
    assert seen["request"].headers == {"X-Existing": "1"}


@pytest.mark.asyncio
async def test_fixed_anchor_injected(sim_file):
    seen, handler = await _capture()
    token = time_source.set_request_override("2026-03-05T08:30:00+08:00")
    try:
        await mcp_loader._inject_reference_time(_FakeRequest(), handler)
    finally:
        time_source.reset_request_override(token)
    assert seen["request"].headers[time_source.REFERENCE_TIME_HEADER] == "2026-03-05T08:30:00+08:00"


@pytest.mark.asyncio
async def test_real_anchor_injected_as_sentinel(sim_file):
    """real 也要发：否则 MCP 侧会回落全局文件，"我要真实时间"就失效了。"""
    seen, handler = await _capture()
    token = time_source.set_request_override("real")
    try:
        await mcp_loader._inject_reference_time(_FakeRequest(), handler)
    finally:
        time_source.reset_request_override(token)
    assert seen["request"].headers[time_source.REFERENCE_TIME_HEADER] == "real"


@pytest.mark.asyncio
async def test_existing_headers_preserved(sim_file):
    seen, handler = await _capture()
    token = time_source.set_request_override("2026-03-05T08:30:00+08:00")
    try:
        await mcp_loader._inject_reference_time(_FakeRequest(headers={"X-Existing": "1"}), handler)
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
        result = await mcp_loader._inject_reference_time(_FakeRequest(), handler)
    finally:
        time_source.reset_request_override(token)
    assert result == {"content": "原始结果"}


@pytest.mark.asyncio
async def test_original_request_not_mutated(sim_file):
    """override 走不可变模式：原 request 的 headers 不能被就地改掉。"""
    original = _FakeRequest(headers={"X-Existing": "1"})
    _, handler = await _capture()
    token = time_source.set_request_override("2026-03-05T08:30:00+08:00")
    try:
        await mcp_loader._inject_reference_time(original, handler)
    finally:
        time_source.reset_request_override(token)
    assert original.headers == {"X-Existing": "1"}


def test_interceptor_is_registered_on_mcp_clients():
    """接线断言：interceptor 必须真的挂到 MultiServerMCPClient 上。"""
    import inspect

    src = inspect.getsource(mcp_loader._load_one_server_tools)
    assert "tool_interceptors" in src, "interceptor 没挂上，header 永远不会被注入"
    assert "_inject_reference_time" in src


def test_real_library_request_class_still_has_the_members_we_use():
    """对真实类的守卫：我们依赖 `headers` 与 `override(headers=...)`。

    全量跑时该模块可能被别的测试文件的假模块挡住 → 跳过，以单独运行本文件为准。
    """
    try:
        from langchain_mcp_adapters.interceptors import MCPToolCallRequest
    except Exception:
        pytest.skip("langchain_mcp_adapters 被其它测试文件的假模块替换，跳过真实类断言")

    req = MCPToolCallRequest(name="t", args={}, server_name="weather", headers={"a": "1"})
    assert req.headers == {"a": "1"}
    assert req.override(headers={"b": "2"}).headers == {"b": "2"}
    assert req.headers == {"a": "1"}, "override 必须是不可变的"


# ---------------------------------------------------------------- 跨进程契约


def _mcp_time_source_path():
    from pathlib import Path

    return Path(__file__).resolve().parents[2] / "haihe-weather-analyzer-mcp" / "time_source.py"


def test_time_source_copies_stay_identical():
    """两包的 time_source.py 必须内容一致。

    Chainlit 侧按它注入 header、MCP 侧按它读回；只改一边会让锚点**静默失效**
    （不报错，只是永远回落真实时间，表现为"设了时间没生效"这种最难查的问题）。
    """
    from pathlib import Path

    cl_path = Path(__file__).resolve().parents[1] / "utils" / "time_source.py"
    mcp_path = _mcp_time_source_path()
    if not mcp_path.exists():
        pytest.skip("MCP 包不在同级目录（跨仓库独立部署时跳过）")

    def _norm(p):
        return p.read_text(encoding="utf-8").replace("\r\n", "\n")

    assert _norm(cl_path) == _norm(mcp_path), \
        "chainlitexam/utils/time_source.py 与 haihe-weather-analyzer-mcp/time_source.py 已不一致"


def test_header_constant_matches_mcp_side_copy():
    """跨进程契约的核心常量必须同值（两文件一致时自然成立，这里显式再锁一次）。"""
    import re

    from utils import time_source

    mcp_path = _mcp_time_source_path()
    if not mcp_path.exists():
        pytest.skip("MCP 包不在同级目录（跨仓库独立部署时跳过）")

    match = re.search(
        r'^REFERENCE_TIME_HEADER\s*=\s*"([^"]+)"',
        mcp_path.read_text(encoding="utf-8"),
        re.M,
    )
    assert match, "MCP 侧 time_source.py 里找不到 REFERENCE_TIME_HEADER"
    assert match.group(1) == time_source.REFERENCE_TIME_HEADER
