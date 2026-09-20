"""MCP SSE 工具加载（按 server 隔离容错）。

每个 MCP server 独立连接、独立容错：单个 server（如第三方历史极端天气服务
extreme-weather-statistics / 10.226.107.133）连接失败时，只丢失该 server 的工具，
不拖垮其他健康 server（如本机 weather / 3333 的核心工具）。

从 chain_gzt 抽出，便于在无 chainlit 重依赖的环境下单独测试。
"""

import asyncio
import os

from langchain_mcp_adapters.client import MultiServerMCPClient

from utils import time_source

# server 名 → (环境变量名, 默认 SSE 地址)
_SERVER_URLS = {
    "weather": ("MCP_SERVER_URL", "http://localhost:3333/sse"),
    "extreme-weather-statistics": ("EXTRM_SERVER_URL", "http://10.226.107.133:8000/sse"),
}


def _server_urls() -> dict:
    return {name: os.getenv(env, default) for name, (env, default) in _SERVER_URLS.items()}


async def _inject_reference_time(request, handler):
    """把请求级锚定时间塞进 MCP header，让 MCP 进程的 time_source 读到。

    只在请求级锚点存在时才注入：没带锚点的请求一字节不加，MCP 侧继续自己读全局
    覆盖文件，行为与本次改动前逐字一致。

    背景：Chainlit 进程与 MCP 进程是两个进程，原先只能靠共享 JSON 文件传"现在"，
    那是个进程级标量——一人切时间、所有用户（含天河小程序）都跟着变。header 挂在
    **每次工具调用新建的 SSE session** 上（langchain-mcp-adapters 0.3.2 每次调用
    都会 create_session，没有长连接会吞掉每调差异），所以能做到按请求而不是按进程。
    """
    value = time_source.request_override_header_value()
    if value is None:
        return await handler(request)
    headers = {**(request.headers or {}), time_source.REFERENCE_TIME_HEADER: value}
    return await handler(request.override(headers=headers))


async def _load_one_server_tools(name: str, url: str):
    """连接单个 MCP server 并取回其工具列表。失败时异常向上抛，由调用方按 server 隔离。"""
    client = MultiServerMCPClient(
        {name: {"transport": "sse", "url": url}},
        tool_interceptors=[_inject_reference_time],
    )
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
