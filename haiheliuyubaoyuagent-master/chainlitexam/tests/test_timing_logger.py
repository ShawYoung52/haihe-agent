"""Tests for chainlitexam.timing_logger."""

import io
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

# Make ``import chainlitexam`` work when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chainlitexam.timing_logger import TimingLogger


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