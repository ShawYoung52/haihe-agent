"""Tests for message_orchestrator feature-flag behavior."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chainlitexam.tests.stubs import ensure_stubs

ensure_stubs()

import chainlitexam.message_orchestrator as mo


def test_enable_fast_paths_defaults_to_false():
    # The constant is evaluated at import time from the environment.
    # In normal test runs no env var is set, so it should be False.
    assert mo.ENABLE_FAST_PATHS is False


import pytest


@pytest.mark.asyncio
async def test_process_message_skips_fast_paths_when_disabled(monkeypatch):
    """When ENABLE_FAST_PATHS is False, no _try_*_fast_path function is awaited."""
    monkeypatch.setattr(mo, "ENABLE_FAST_PATHS", False)

    called = []

    async def fake_fast_path(*args, **kwargs):
        called.append("fast_path")
        return False

    # Patch every fast-path function we can find on the module.
    for name in dir(mo):
        if name.startswith("_try_") and name.endswith("_fast_path") and callable(getattr(mo, name)):
            monkeypatch.setattr(mo, name, fake_fast_path)

    # Minimal mock message; we only care that fast paths are skipped.
    class FakeMessage:
        content = "测试查询"

    async def noop_async(*args, **kwargs):
        return None

    class FakeChain:
        async def ainvoke(self, *args, **kwargs):
            return type("Response", (), {"content": "", "tool_calls": []})()

    callbacks = {
        "astream_thinking_to_reasoning": noop_async,
        "astream_planner_think": lambda *args, **kwargs: type(
            "Response", (), {"content": "", "tool_calls": []}
        )(),
        "need_river_plot": lambda text: False,
        "append_followup_if_needed": lambda text, query: text,
        "stream_text_to_message": noop_async,
        "astream_answer_chain_to_message": lambda *args, **kwargs: "",
    }

    monkeypatch.setattr(mo, "_show_thinking", lambda text: None)
    monkeypatch.setattr(mo.cl, "Message", lambda **kwargs: type("M", (), {"send": noop_async, "remove": noop_async, "update": noop_async})())

    # We expect the function to eventually hit the planner path and raise or return.
    # The exact outcome is not important; the important assertion is that no fast path was called.
    try:
        await mo.process_message(
            FakeMessage(),
            planner_chain=FakeChain(),
            answer_chain=FakeChain(),
            thinking_chain=FakeChain(),
            tools=[],
            messages=[],
            callbacks=callbacks,
        )
    except Exception:
        pass

    assert called == [], f"Expected no fast-path calls when disabled, got {called}"
