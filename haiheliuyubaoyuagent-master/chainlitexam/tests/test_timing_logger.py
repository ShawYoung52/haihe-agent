"""Tests for chainlitexam.timing_logger."""

import logging
from chainlitexam.timing_logger import TimingLogger


class _CaptureHandler(logging.Handler):
    """Simple handler that records every emitted log record."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _capture_log(func, *args, **kwargs):
    """Call ``func`` while capturing log records from the timing logger."""
    handler = _CaptureHandler()
    target_logger = logging.getLogger("chainlitexam.timing_logger")
    target_logger.addHandler(handler)
    target_logger.setLevel(logging.INFO)
    original_propagate = target_logger.propagate
    target_logger.propagate = False
    try:
        func(*args, **kwargs)
    finally:
        target_logger.propagate = original_propagate
        target_logger.removeHandler(handler)
    return handler.records


def test_log_tool_format():
    records = _capture_log(
        TimingLogger.log_tool,
        session_id="sess-123",
        query_summary="今天海河降雨情况",
        tool_name="rainfall_analysis",
        elapsed=1.23,
        status="ok",
    )
    assert len(records) == 1
    message = records[0].getMessage()
    assert "[TOOL_TIMING]" in message
    assert "session=sess-123" in message
    assert 'query="今天海河降雨情况"' in message
    assert "tool=rainfall_analysis" in message
    assert "elapsed=1.23s" in message
    assert "status=ok" in message


def test_log_query_format():
    records = _capture_log(
        TimingLogger.log_query,
        session_id="sess-456",
        query_summary="查询未来三天流域降雨预报",
        total_elapsed=4.56,
        status="ok",
    )
    assert len(records) == 1
    message = records[0].getMessage()
    assert "[QUERY_TIMING]" in message
    assert "session=sess-456" in message
    assert 'query="查询未来三天流域降雨预报"' in message
    assert "total_elapsed=4.56s" in message
    assert "status=ok" in message


def test_summary_truncation():
    long_query = "这是" + "一个" * 50 + "非常长的查询文本"
    summary = TimingLogger._safe_summary(long_query, max_len=40)
    assert len(summary) <= 43  # 40 chars + "..."
    assert summary.endswith("...")
    assert "  " not in summary  # whitespace collapsed


def test_empty_session_id():
    records = _capture_log(
        TimingLogger.log_tool,
        session_id="",
        query_summary="test",
        tool_name="tool",
        elapsed=0.1,
    )
    assert "session=unknown" in records[0].getMessage()


if __name__ == "__main__":
    test_log_tool_format()
    test_log_query_format()
    test_summary_truncation()
    test_empty_session_id()
    print("All tests passed.")