"""MCP SSE 工具加载（按 server 隔离容错）。

每个 MCP server 独立连接、独立容错：单个 server（如第三方历史极端天气服务
extreme-weather-statistics / 10.226.107.133）连接失败时，只丢失该 server 的工具，
不拖垮其他健康 server（如本机 weather / 3333 的核心工具）。

从 chain_gzt 抽出，便于在无 chainlit 重依赖的环境下单独测试。
"""

import asyncio
import os

from langchain_mcp_adapters.client import MultiServerMCPClient

# server 名 → (环境变量名, 默认 SSE 地址)
_SERVER_URLS = {
    "weather": ("MCP_SERVER_URL", "http://localhost:3333/sse"),
    "extreme-weather-statistics": ("EXTRM_SERVER_URL", "http://10.226.107.133:8000/sse"),
}


def _server_urls() -> dict:
    return {name: os.getenv(env, default) for name, (env, default) in _SERVER_URLS.items()}


async def _load_one_server_tools(name: str, url: str):
    """连接单个 MCP server 并取回其工具列表。失败时异常向上抛，由调用方按 server 隔离。"""
    client = MultiServerMCPClient({name: {"transport": "sse", "url": url}})
    return await client.get_tools()


async def load_sse_tools():
    """按 server 隔离加载 MCP 工具：任一 server 失败只降级该 server，其余照常。"""
    servers = _server_urls()
    results = await asyncio.gather(
        *(_load_one_server_tools(name, url) for name, url in servers.items()),
        return_exceptions=True,
    )
    all_tools = []
    for name, res in zip(servers, results):
        if isinstance(res, BaseException):
            # 日志脱敏（CLAUDE.md）：只打 server 名与异常类型，不打印 SSE URL（含内网 IP）与完整异常 repr。
            print(f"❌ MCP server [{name}] 加载失败（{type(res).__name__}）→ 该服务工具降级，不影响其他 server")
            continue
        print(f"✅ MCP server [{name}] 加载成功，{len(res)} 个工具")
        all_tools.extend(res)
    print(f"✅ MCP 工具合计 {len(all_tools)} 个：{[t.name for t in all_tools]}")
    return all_tools
