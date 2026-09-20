# -*- coding: utf-8 -*-
"""探针：验证 MCP tool_interceptor 注入的 header 真能到 MCP 工具体内。

这是「请求级锚定时间」唯一的硬假设——Chainlit 进程按请求塞进 SSE 的 header，
MCP 进程的 `time_source` 能通过 `fastmcp.get_http_headers()` 读回来。

库源码层面成立（已核对已装版本）：
- langchain-mcp-adapters 0.3.2 每次工具调用都新建 SSE session，不存在长连接吞掉每调差异；
- interceptor 的 `request.headers` 会被合并进 connection 的 headers，作用在 httpx client 层，
  `GET /sse` 与 `POST /messages/` 都带；
- MCP SDK 的 SSE server 把 POST 的 Request 放进 `request_ctx`，fastmcp 的
  `get_http_headers()` 正是从这里取。

但必须实测。本机自起一个最小 FastMCP SSE server + 带 interceptor 的 client，不碰内网。

跑法（在 haihe-weather-analyzer-mcp/ 下）：
    <venv>/python.exe probe_reference_time_header.py
期望末行：
    [PROBE] RESULT=OK
"""
from __future__ import annotations

import asyncio
import threading
import time

from fastmcp import FastMCP
from langchain_mcp_adapters.client import MultiServerMCPClient

from time_source import REFERENCE_TIME_HEADER

PROBE_VALUE = "2026-07-10T15:00:00+08:00"
PORT = 3899


def _build_server() -> FastMCP:
    server = FastMCP("probe")

    @server.tool
    def echo_reference_time() -> str:
        """回显本请求携带的锚定时间 header（验证跨进程投递）。"""
        from fastmcp.server.dependencies import get_http_headers

        return (get_http_headers() or {}).get(REFERENCE_TIME_HEADER, "")

    return server


def _serve() -> None:
    # 与生产 server.py 同款：sse transport。
    _build_server().run(transport="sse", host="127.0.0.1", port=PORT, show_banner=False)


async def _probe() -> str:
    # 刻意照抄 mcp_loader._inject_reference_time 的注入方式，而不是 import 它：
    # 探针要在服务器上独立跑，不能依赖 chainlitexam 包（也不带它的重依赖）。
    async def inject(request, handler):
        headers = {**(request.headers or {}), REFERENCE_TIME_HEADER: PROBE_VALUE}
        return await handler(request.override(headers=headers))

    client = MultiServerMCPClient(
        {"probe": {"transport": "sse", "url": f"http://127.0.0.1:{PORT}/sse"}},
        tool_interceptors=[inject],
    )
    tools = await client.get_tools()
    tool = next(t for t in tools if t.name == "echo_reference_time")
    return await tool.ainvoke({})


def _extract_text(result) -> str:
    """工具返回值归一成字符串。

    `tool.ainvoke()` 返回的是 content blocks（`[{"type": "text", "text": ...}]`），
    不是裸字符串；也兼容直接返回 str 的情况。
    """
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        parts = []
        for block in result:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(getattr(block, "text", "")))
        return "".join(parts)
    return str(result)


def main() -> None:
    threading.Thread(target=_serve, daemon=True).start()
    time.sleep(3)  # 等 SSE server 就绪
    try:
        received = _extract_text(asyncio.run(_probe()))
    except Exception as exc:  # 探针失败要把原因说清楚，不要静默
        print(f"[PROBE] sent={PROBE_VALUE}")
        print(f"[PROBE] error={type(exc).__name__}: {exc}")
        print("[PROBE] RESULT=FAIL")
        return
    ok = received == PROBE_VALUE
    print(f"[PROBE] sent={PROBE_VALUE}")
    print(f"[PROBE] received={received!r}")
    print(f"[PROBE] RESULT={'OK' if ok else 'FAIL'}")


if __name__ == "__main__":
    main()
