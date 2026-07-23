"""Tests for message_orchestrator feature-flag behavior."""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chainlitexam.tests.stubs import ensure_stubs

ensure_stubs()

import chainlit
import chainlitexam.message_orchestrator as mo


def test_enable_fast_paths_defaults_to_false(monkeypatch):
    """ENABLE_FAST_PATHS reflects the ENABLE_FAST_PATHS environment variable at import time."""
    monkeypatch.setenv("ENABLE_FAST_PATHS", "false")
    importlib.reload(mo)
    assert mo.ENABLE_FAST_PATHS is False

    monkeypatch.setenv("ENABLE_FAST_PATHS", "true")
    importlib.reload(mo)
    assert mo.ENABLE_FAST_PATHS is True


@pytest.mark.asyncio
async def test_process_message_skips_fast_paths_when_disabled(monkeypatch):
    """When ENABLE_FAST_PATHS is False, no _try_*_fast_path function is awaited and process_message returns normally."""
    monkeypatch.setattr(mo, "ENABLE_FAST_PATHS", False)

    called = []

    async def fake_fast_path(*args, **kwargs):
        called.append("fast_path")
        return False

    # Patch every fast-path function we can find on the module.
    for name in dir(mo):
        if name.startswith("_try_") and name.endswith("_fast_path") and callable(getattr(mo, name)):
            monkeypatch.setattr(mo, name, fake_fast_path)

    class FakeMessage:
        content = "测试查询"

    class FakePlannerMsg:
        content = "这是一个测试回答。"
        tool_calls = []

    async def fake_astream_planner_think(*args, **kwargs):
        return FakePlannerMsg()

    async def noop_async(*args, **kwargs):
        return None

    class FakeMessageObj:
        content = ""
        send = noop_async
        remove = noop_async
        update = noop_async

    callbacks = {
        "astream_planner_think": fake_astream_planner_think,
        "need_river_plot": lambda message: False,
        "astream_thinking_to_reasoning": noop_async,
        "append_followup_if_needed": lambda text, query: text,
        "stream_text_to_message": noop_async,
        "astream_answer_chain_to_message": lambda *a, **k: "",
    }

    monkeypatch.setattr(mo.cl, "Message", lambda **kwargs: FakeMessageObj())

    result = await mo.process_message(
        FakeMessage(),
        planner_chain=None,
        answer_chain=None,
        thinking_chain=None,
        tools=[],
        messages=[],
        callbacks=callbacks,
    )

    assert called == [], f"Expected no fast-path calls when disabled, got {called}"
    assert result is None


@pytest.mark.asyncio
async def test_run_tool_round_failure_records_tool_message_without_generic_error(monkeypatch):
    """工具在 _run_tool_round 中失败时，不应再向用户发送固定的通用错误消息，
    而应把失败信息以 ToolMessage 形式交给 planner 自行组织回答。"""

    class FakeTool:
        name = "evaluate_haihe_forecast_emergency_response"

    class FakePlannerMsg:
        tool_calls = [
            {
                "name": FakeTool.name,
                "args": {"start_time": "2026-07-13 02:00:00"},
                "id": "call-1",
            }
        ]

    async def fake_invoke_tool_with_tolerance(*args, **kwargs):
        raise Exception("未找到起报 2026071302 的 12h/24h 预报文件。")

    monkeypatch.setattr(mo, "_invoke_tool_with_tolerance", fake_invoke_tool_with_tolerance)
    monkeypatch.setattr(mo.cl, "Step", chainlit.Step)

    sent_messages: list[dict] = []
    monkeypatch.setattr(mo.cl, "Message", lambda **kwargs: type("CapturingMessage", (), {
        "send": lambda self: sent_messages.append(kwargs),
        "remove": lambda self: None,
        "update": lambda self: None,
    })())

    callbacks = {"tool_observation_to_text": lambda obs: str(obs)}
    messages = []

    forced, ree, bundles = await mo._run_tool_round(
        FakePlannerMsg(), [FakeTool()], messages, "测试", 1, callbacks
    )

    assert sent_messages == [], f"Expected no generic cl.Message, got {sent_messages}"
    assert forced is None
    assert ree is None
    assert bundles == []
    assert len(messages) == 1
    tool_msg = messages[0]
    assert tool_msg.content.startswith(f"工具 {FakeTool.name} 执行失败")
    assert "该数据暂不可用" in tool_msg.content
    assert "2026071302" in tool_msg.content


def _impact_result_with_propagation():
    return {
        "time_range_readable": "2026-07-22 08:00 ~ 2026-07-23 08:00",
        "rainfall_threshold_mm": 50.0,
        "affected_rivers": ["滦河"],
        "affected_zone_77_regions": ["滦河山区"],
        "affected_admin_divisions": ["承德市"],
        "total_segments": 3,
        "affected_segments": 3,
        "river_propagation": {
            "flow_velocity_mps": 2.0,
            "rivers": [
                {
                    "river_name": "滦河",
                    "propagation_distance_km": 48.2,
                    "propagation_time_hours": 6.7,
                    "arrival_estimate_readable": "约6.7小时",
                    "has_downstream": True,
                }
            ],
        },
    }


def test_brief_includes_propagation_summary():
    brief = mo._build_affected_river_network_brief(_impact_result_with_propagation(), "暴雨影响哪些河系")
    assert "按经验流速 2.0 m/s 估算" in brief
    assert "约6.7小时" in brief
    assert "48.2" in brief
    assert "滦河" in brief
    assert "传播至下游最远约" in brief


def test_brief_direct_only_propagation_uses_pass_through_wording():
    result = _impact_result_with_propagation()
    result["river_propagation"]["rivers"][0]["has_downstream"] = False
    brief = mo._build_affected_river_network_brief(result, "暴雨影响哪些河系")
    assert "洪水通过需约6.7小时" in brief
    assert "传播至下游" not in brief


def test_brief_skips_propagation_line_when_keys_missing():
    result = _impact_result_with_propagation()
    result["river_propagation"]["rivers"] = [{"propagation_time_hours": 1.0}]
    brief = mo._build_affected_river_network_brief(result, "暴雨影响哪些河系")
    assert "经验流速" not in brief
    assert "None" not in brief


def test_brief_without_propagation_block_stays_compatible():
    result = _impact_result_with_propagation()
    del result["river_propagation"]
    brief = mo._build_affected_river_network_brief(result, "暴雨影响哪些河系")
    assert "经验流速" not in brief
    assert "滦河" in brief  # 既有河系列表不受影响
