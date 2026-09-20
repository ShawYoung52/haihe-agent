# -*- coding: utf-8 -*-
"""运行时按日期分槽：MCP 工具表只加载一次，日期槽位有上限。

锁死两件事：
1. 【当前日期】前缀按调用方传入的日期渲染（而不是进程当前时间）——这是
   请求级锚点能生效的关键，否则所有用户共享一个烤死的 prompt 前缀。
2. MCP 工具表跨日期共享一次加载——分槽不能退化成"每个日期重连一次内网"。
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("chainlit.emitter", reason="需要真实 Chainlit 包")

import chain_gzt  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_runtime():
    chain_gzt._clear_orchestrator_runtime_cache()
    yield
    chain_gzt._clear_orchestrator_runtime_cache()


# ---------------------------------------------------------------- 纯函数


def test_prompt_date_prefix_uses_given_day():
    prefix = chain_gzt._prompt_date_prefix("2026-07-10")
    assert "2026年07月10日" in prefix
    assert "星期五" in prefix
    assert prefix.endswith("\n\n")


def test_prompt_date_prefix_weekday_is_correct():
    # 2026-07-10 是周五；07-13 是周一。用两个不同星期几防映射写反。
    assert "星期一" in chain_gzt._prompt_date_prefix("2026-07-13")
    assert "星期日" in chain_gzt._prompt_date_prefix("2026-07-12")


def test_cache_key_varies_by_day():
    assert chain_gzt._orchestrator_runtime_cache_key("2026-07-10")[0] == "2026-07-10"
    assert chain_gzt._orchestrator_runtime_cache_key("2026-07-11")[0] == "2026-07-11"
    assert chain_gzt._orchestrator_runtime_cache_key("2026-07-10") == \
        chain_gzt._orchestrator_runtime_cache_key("2026-07-10")


# ---------------------------------------------------------------- 槽位


@pytest.fixture()
def _offline_llm(monkeypatch):
    """不碰内网：ChatOpenAI 构造本身不发请求，给个占位 key 即可。

    刻意不 stub `_build_chat_llm` 返回假对象——`planner_template | llm.bind_tools(...)`
    要求真的是 Runnable，假对象会让这层测试测不到真实接线。
    """
    for role in ("PLANNER", "ANSWER"):
        monkeypatch.setenv(f"{role}_API_KEY", "EMPTY")
        monkeypatch.setenv(f"{role}_API_BASE", "http://127.0.0.1:9/v1")


def _stub_load(monkeypatch, counter):
    async def fake_load_sse_tools():
        counter["n"] += 1
        return []

    monkeypatch.setattr(chain_gzt, "load_sse_tools", fake_load_sse_tools)


def test_mcp_tools_loaded_once_across_days(monkeypatch, _offline_llm):
    """两个日期两个槽位，但 MCP 工具表只列一次（这正是分槽设计的意义）。"""
    calls = {"n": 0}
    _stub_load(monkeypatch, calls)

    async def main():
        await chain_gzt._get_orchestrator_runtime("2026-07-10")
        await chain_gzt._get_orchestrator_runtime("2026-07-11")
        await chain_gzt._get_orchestrator_runtime("2026-07-10")

    asyncio.run(main())
    assert calls["n"] == 1


def test_same_day_reuses_slot(monkeypatch, _offline_llm):
    calls = {"n": 0}
    _stub_load(monkeypatch, calls)

    async def main():
        first = await chain_gzt._get_orchestrator_runtime("2026-07-10")
        second = await chain_gzt._get_orchestrator_runtime("2026-07-10")
        return first, second

    first, second = asyncio.run(main())
    assert first is second


def test_runtime_slots_are_capped(monkeypatch, _offline_llm):
    calls = {"n": 0}
    _stub_load(monkeypatch, calls)
    monkeypatch.setenv("ORCHESTRATOR_RUNTIME_MAX_DAYS", "2")

    async def main():
        for day in ("2026-07-10", "2026-07-11", "2026-07-12"):
            await chain_gzt._get_orchestrator_runtime(day)
        return chain_gzt._orchestrator_runtime_state()["runtimes"]

    runtimes = asyncio.run(main())
    assert len(runtimes) == 2
    assert all(key[0] != "2026-07-10" for key in runtimes), "最老的槽位应被淘汰"


def test_generation_bump_clears_all_slots(monkeypatch, _offline_llm):
    """显式清缓存（切换系统时间兜底路径）必须把槽位和 MCP 工具表一起丢掉。"""
    calls = {"n": 0}
    _stub_load(monkeypatch, calls)

    async def main():
        await chain_gzt._get_orchestrator_runtime("2026-07-10")
        chain_gzt._clear_orchestrator_runtime_cache()
        await chain_gzt._get_orchestrator_runtime("2026-07-10")

    asyncio.run(main())
    assert calls["n"] == 2, "generation 变化后 MCP 工具表应重新加载"
