"""Unified timing logger for tool and query elapsed time.

Outputs `[TOOL_TIMING]` and `[QUERY_TIMING]` lines to the console so that
performance metrics can be collected from logs.
"""

import logging

logger = logging.getLogger(__name__)


class TimingLogger:
    """Lightweight helper for logging timing metrics."""

    @staticmethod
    def _safe_summary(text: str, max_len: int = 40) -> str:
        """Clean whitespace and truncate ``text`` to ``max_len`` characters."""
        if text is None:
            text = ""
        text = str(text)
        cleaned = " ".join(text.split())
        if len(cleaned) > max_len:
            cleaned = cleaned[:max_len] + "..."
        return cleaned

    @staticmethod
    def log_tool(
        session_id: str,
        query_summary: str,
        tool_name: str,
        elapsed: float,
        status: str = "ok",
    ) -> None:
        """Log a single tool invocation timing."""
        session_id = session_id or "unknown"
        query_summary = TimingLogger._safe_summary(query_summary)
        tool_name = tool_name or "unknown"
        status = status or "ok"
        logger.info(
            "[TOOL_TIMING] session=%s query=\"%s\" tool=%s elapsed=%ss status=%s",
            session_id,
            query_summary,
            tool_name,
            elapsed,
            status,
        )

    @staticmethod
    def log_query(
        session_id: str,
        query_summary: str,
        total_elapsed: float,
        status: str = "ok",
    ) -> None:
        """Log the total elapsed time for a user query."""
        session_id = session_id or "unknown"
        query_summary = TimingLogger._safe_summary(query_summary)
        status = status or "ok"
        logger.info(
            "[QUERY_TIMING] session=%s query=\"%s\" total_elapsed=%ss status=%s",
            session_id,
            query_summary,
            total_elapsed,
            status,
        )