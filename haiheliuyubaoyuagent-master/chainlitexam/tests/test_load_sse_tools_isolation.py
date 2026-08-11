"""MCP 工具加载容错隔离测试。

验证 mcp_loader.load_sse_tools：
- 单个 MCP server（如第三方 extreme-weather-statistics / 10.226.107.133）连接失败时，
  另一个健康 server（weather / 本机 3333）的工具仍然加载，不被一起拖垮。
- 两个 server 都正常时，工具全量加载（行为不回归）。

背景：旧的"全有或全无"实现把多个 server 放进一个 MultiServerMCPClient，
任一 server 连接失败 → get_tools 抛 ExceptionGroup → 整个返回 []，健康 server 的工具陪葬。
第三方 extreme-weather-statistics（历史极端天气，非核心、机器不稳定）一挂就拖垮全部核心工具。

说明：直接测 mcp_loader（不 import chain_gzt），因此不需要 chainlit 等重依赖，
可在精简解释器（如 Python313）上独立运行。
"""

import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

# 指向 chainlitexam 目录（mcp_loader 所在）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 只 mock 真正缺失的依赖（langchain_mcp_adapters 在精简解释器上可能没装）
for _mod, _cls in (
    ("langchain_mcp_adapters", "MultiServerMCPClient"),
    ("langchain_mcp_adapters.client", "MultiServerMCPClient"),
):
    if _mod not in sys.modules:
        m = types.ModuleType(_mod)
        setattr(m, _cls, type(_cls, (), {}))
        sys.modules[_mod] = m

import pytest

# 第三方历史极端天气服务（extreme-weather-statistics）默认地址的网段，用它模拟"该 server 挂了"
_DEAD_SERVER_MARK = "10.226.107.133"


class _FlakyMCPClient:
    """模拟：配置里只要包含不可达的 .133 server，本次 get_tools 就抛连接错误。

    旧的"全有或全无"实现把两个 server 放进一个 client，.133 一挂整个 get_tools 抛 → 返回 []。
    隔离实现每个 server 一个 client：.133 那个抛、本机 3333 那个正常返回。
    """

    def __init__(self, servers):
        self.servers = servers

    async def get_tools(self):
        for name, cfg in self.servers.items():
            if _DEAD_SERVER_MARK in cfg["url"]:
                raise ConnectionError("All connection attempts failed")
        return [SimpleNamespace(name=f"{name}_tool") for name in self.servers]


@pytest.mark.asyncio
async def test_load_sse_tools_isolates_single_server_failure(monkeypatch):
    """extreme-weather-statistics(.133) 挂掉时，weather(3333) 的工具仍加载，不被拖垮。"""
    import mcp_loader

    monkeypatch.setattr(mcp_loader, "MultiServerMCPClient", _FlakyMCPClient)
    tools = await mcp_loader.load_sse_tools()
    names = {t.name for t in tools}
    assert "weather_tool" in names
    assert "extreme-weather-statistics_tool" not in names


@pytest.mark.asyncio
async def test_load_sse_tools_loads_all_when_both_up(monkeypatch):
    """两个 server 都健康时工具全量加载（不回归）。"""
    import mcp_loader

    class _AllUp:
        def __init__(self, servers):
            self.servers = servers

        async def get_tools(self):
            return [SimpleNamespace(name=f"{n}_tool") for n in self.servers]

    monkeypatch.setattr(mcp_loader, "MultiServerMCPClient", _AllUp)
    tools = await mcp_loader.load_sse_tools()
    names = {t.name for t in tools}
    assert names == {"weather_tool", "extreme-weather-statistics_tool"}
