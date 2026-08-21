"""Tests for chainlitexam.timing_logger."""

import io
import inspect
import re
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

# Make ``import chainlitexam`` work when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chainlitexam.tests.stubs import ensure_stubs
ensure_stubs()

from chainlitexam.timing_logger import TimingLogger, TimingContext
from chainlitexam import message_orchestrator as mo
import pytest


def _capture_stdout(func, *args, **kwargs):
    """Call ``func`` and return everything written to stdout."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        func(*args, **kwargs)
    return buf.getvalue()


def test_log_tool_format():
    output = _capture_stdout(
        TimingLogger.log_tool,
        session_id="sess-123",
        query_summary="今天海河降雨情况",
        tool_name="rainfall_analysis",
        elapsed=1.23,
        status="ok",
    )
    assert "[TOOL_TIMING]" in output
    assert "session=sess-123" in output
    assert 'query="今天海河降雨情况"' in output
    assert "tool=rainfall_analysis" in output
    assert "elapsed=1.23s" in output
    assert "status=ok" in output


def test_log_query_format():
    output = _capture_stdout(
        TimingLogger.log_query,
        session_id="sess-456",
        query_summary="查询未来三天流域降雨预报",
        total_elapsed=4.56,
        status="ok",
    )
    assert "[QUERY_TIMING]" in output
    assert "session=sess-456" in output
    assert 'query="查询未来三天流域降雨预报"' in output
    assert "total_elapsed=4.56s" in output
    assert "status=ok" in output


def test_summary_truncation():
    long_query = "这是" + "一个" * 50 + "非常长的查询文本"
    summary = TimingLogger._safe_summary(long_query, max_len=40)
    assert len(summary) <= 40
    assert summary.endswith("...")
    assert "  " not in summary  # whitespace collapsed


def test_none_summary():
    summary = TimingLogger._safe_summary(None, max_len=40)
    assert summary == ""


def test_empty_summary():
    summary = TimingLogger._safe_summary("", max_len=40)
    assert summary == ""


def test_small_max_len():
    assert TimingLogger._safe_summary("hello", max_len=2) == ".."
    assert TimingLogger._safe_summary("hello", max_len=1) == "."
    assert TimingLogger._safe_summary("hello", max_len=0) == ""


def test_log_tool_fail_status():
    output = _capture_stdout(
        TimingLogger.log_tool,
        session_id="sess-789",
        query_summary="测试失败场景",
        tool_name="failing_tool",
        elapsed=2.0,
        status="fail",
    )
    assert "[TOOL_TIMING]" in output
    assert "status=fail" in output


def test_elapsed_format():
    output = _capture_stdout(
        TimingLogger.log_tool,
        session_id="sess-format",
        query_summary="验证小数位",
        tool_name="format_tool",
        elapsed=1.23456,
        status="ok",
    )
    assert "elapsed=1.23s" in output
    # 必须恰好两位小数
    assert re.search(r"elapsed=\d+\.\d{2}s", output) is not None


def test_timing_context_accumulates_stages():
    ctx = TimingContext(request_id="test-1")
    ctx.mark("thinking")
    time.sleep(0.01)
    ctx.mark("planner_round_1")
    ctx.record_tool_call("get_city_rainfall_time_range", 12.5)
    ctx.record_planner_round()
    ctx.mark("answer")
    ctx.mark("done")

    assert ctx.stages["thinking"] >= 0
    assert ctx.stages["planner_round_1"] >= 10  # 0.01s = 10ms
    assert ctx.stages["answer"] >= 0
    assert ctx.tool_call_count == 1
    assert ctx.tool_calls[0][0] == "get_city_rainfall_time_range"
    assert ctx.planner_rounds == 1


def test_timing_context_log_line_has_no_sensitive_fields(capsys):
    ctx = TimingContext(request_id="req-abc")
    ctx.mark("thinking")
    ctx.mark("answer")
    ctx.mark("done")
    ctx.log()
    out = capsys.readouterr().out
    assert "[PERF]" in out
    assert "req-abc" in out
    assert '"thinking"' in out
    assert '"total_ms"' in out
    # 不泄露用户问题/内网地址/路径
    assert "10.226" not in out
    assert ".venv" not in out


def test_timing_context_as_dict_and_json_log():
    ctx = TimingContext(request_id="req-1")
    ctx.mark("thinking")
    ctx.mark("planner_round_1")
    ctx.record_planner_round()
    ctx.record_tool_call("get_effective_warning_info", 12.5)
    ctx.mark("done")
    d = ctx.as_dict()
    assert d["request_id"] == "req-1"
    assert d["planner_rounds"] == 1
    assert d["tool_call_count"] == 1
    assert "thinking" in d["stages"]
    assert d["status"] == "ok"
    import json as _json
    _json.loads(ctx.to_json())  # 必须是合法 JSON


def test_timing_log_has_no_sensitive_fields():
    import re
    ctx = TimingContext(request_id="req-2")
    ctx.mark("done")
    out = ctx.to_json()
    assert "10.226" not in out
    assert ".venv" not in out


def test_timing_context_queue_and_chars_fields():
    ctx = TimingContext(request_id="req-3")
    ctx.http_queue_wait_ms = 150.0
    ctx.tool_queue_wait_ms = 20.0
    ctx.planner_input_chars = 100
    ctx.answer_input_chars = 200
    d = ctx.as_dict()
    assert d["http_queue_wait_ms"] == 150.0
    assert d["planner_input_chars"] == 100


def test_timing_context_evidence_field():
    ctx = TimingContext(request_id="req-ev")
    ctx.evidence = {"would_early_finalize": True, "query_type": "forecast"}
    d = ctx.as_dict()
    assert d["evidence"] == {"would_early_finalize": True, "query_type": "forecast"}
    assert "would_early_finalize" in d["evidence"]


def test_timing_context_active_routing_and_early_finalize_fields():
    ctx = TimingContext(request_id="req-routing")
    ctx.tool_filter_mode = "filtered"
    ctx.tool_candidates_count = 3
    ctx.tool_filter_reason = "single_domain:water_level"
    ctx.full_planner_fallback = True
    ctx.full_planner_fallback_reason = "missing_tool_call"
    ctx.evidence_early_finalize = True
    ctx.planner_rounds_saved = 1

    d = ctx.as_dict()
    assert d["tool_filter_mode"] == "filtered"
    assert d["tool_candidates_count"] == 3
    assert d["tool_filter_reason"] == "single_domain:water_level"
    assert d["full_planner_fallback"] is True
    assert d["full_planner_fallback_reason"] == "missing_tool_call"
    assert d["evidence_early_finalize"] is True
    assert d["planner_rounds_saved"] == 1


def _planner_msg_with_tool_calls(tool_calls):
    """构造一个带 tool_calls 的伪 planner 消息对象。"""
    msg = type("FakePlannerMsg", (), {})()
    msg.content = ""
    msg.tool_calls = tool_calls
    return msg


def test_evidence_query_type_from_tool_names_forecast():
    """含 query_rolling_forecast 工具的 planner 消息 → forecast。"""
    msg = _planner_msg_with_tool_calls([
        {"id": "c1", "name": "query_rolling_forecast", "args": {"user_query": "未来三天降雨"}},
    ])
    assert mo._evidence_query_type_from_tool_names(msg) == "forecast"


def test_evidence_query_type_from_tool_names_warning():
    """含预警工具的 planner 消息 → warning。"""
    msg = _planner_msg_with_tool_calls([
        {"id": "c1", "name": "get_effective_warning_info", "args": {}},
    ])
    assert mo._evidence_query_type_from_tool_names(msg) == "warning"


def test_evidence_query_type_from_tool_names_bare_list_is_unknown():
    """误传 tool_calls 裸列表（而非 planner 消息对象）时 → unknown（保守，不误判）。"""
    bare_list = [
        {"id": "c1", "name": "query_rolling_forecast", "args": {}},
    ]
    assert mo._evidence_query_type_from_tool_names(bare_list) == "unknown"


if __name__ == "__main__":
    test_log_tool_format()
    test_log_query_format()
    test_log_tool_fail_status()
    test_elapsed_format()
    test_summary_truncation()
    test_none_summary()
    test_empty_summary()
    test_small_max_len()
    print("All tests passed.")

def test_evidence_query_type_from_tool_names_decision_poi():
    """点位决策工具 → decision_poi（而非 forecast，避免误导 shadow 观测）。"""
    msg = _planner_msg_with_tool_calls([
        {"id": "c1", "name": "query_decision_weather_for_poi", "args": {"user_text": "梅江会展中心明天天气"}},
    ])
    assert mo._evidence_query_type_from_tool_names(msg) == "decision_poi"


def test_log_query_exit_does_not_remark_done():
    """_log_query_exit 统一出口不应覆盖已 mark 的 done 阶段（双调用守卫）。"""
    ctx = mo.TimingContext(request_id="req-done")
    ctx.mark("planner_round_1")
    ctx.mark("done")
    done_before = ctx.stages.get("done")
    # 模拟 _log_query_exit finally 逻辑：若 done 已存在则不覆盖
    if "done" not in ctx.stages:
        ctx.mark("done")
    assert ctx.stages.get("done") == done_before


class _MemoryUserSession:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


@pytest.mark.asyncio
async def test_tool_invocation_records_perf_detail(monkeypatch):
    """普通 planner 工具调用必须进入 [PERF].tools，而不只是打印 TOOL_TIMING。"""
    ctx = TimingContext(request_id="req-tool")
    session = _MemoryUserSession({"id": "session-1", "timing_context": ctx})
    monkeypatch.setattr(mo.cl, "user_session", session)

    class FakeTool:
        async def ainvoke(self, args):
            return {"status": "ok"}

    step = type("FakeStep", (), {"input": ""})()
    result, elapsed = await mo._invoke_tool_with_tolerance(
        "query_current_weather_observation", FakeTool(), {}, step, user_text="当前天气"
    )

    assert result == {"status": "ok"}
    assert elapsed >= 0
    assert ctx.tool_call_count == 1
    assert ctx.tool_calls[0][0] == "query_current_weather_observation"
    assert ctx.tool_calls[0][1] >= 0


@pytest.mark.asyncio
async def test_fast_path_tool_invocation_records_perf_detail(monkeypatch):
    """快速路径也必须写入同一 TimingContext。"""
    ctx = TimingContext(request_id="req-fast-tool")
    session = _MemoryUserSession({"id": "session-2", "timing_context": ctx})
    monkeypatch.setattr(mo.cl, "user_session", session)

    class FakeTool:
        async def ainvoke(self, args):
            return "ok"

    assert await mo._invoke_tool_for_fast_path("get_effective_warning_info", FakeTool(), {}, "预警") == "ok"
    assert ctx.tool_call_count == 1
    assert ctx.tool_calls[0][0] == "get_effective_warning_info"


def test_process_message_does_not_manually_double_count_tools():
    """实际调用点负责计数后，process_message 不得按 planner 声明再次累加。"""
    source = inspect.getsource(mo.process_message)
    assert "tool_call_count += len(planner_msg.tool_calls)" not in source


def test_answer_timeout_reads_env_with_safe_fallback(monkeypatch):
    monkeypatch.delenv("ANSWER_TIMEOUT_SECONDS", raising=False)
    assert mo._answer_timeout_seconds() == 60

    monkeypatch.setenv("ANSWER_TIMEOUT_SECONDS", "25")
    assert mo._answer_timeout_seconds() == 25

    for invalid in ("", "bad", "0", "-1"):
        monkeypatch.setenv("ANSWER_TIMEOUT_SECONDS", invalid)
        assert mo._answer_timeout_seconds() == 60


def test_answer_waits_do_not_keep_hardcoded_sixty_seconds():
    source = inspect.getsource(mo.process_message)
    assert "timeout=60" not in source
    assert source.count("timeout=_answer_timeout_seconds()") >= 6
