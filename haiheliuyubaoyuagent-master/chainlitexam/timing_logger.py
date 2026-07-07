"""Unified timing logger for tool and query elapsed time.

Outputs `[TOOL_TIMING]` and `[QUERY_TIMING]` lines to the console so that
performance metrics can be collected from logs.
"""


class TimingLogger:
    """Lightweight helper for logging timing metrics."""

    @staticmethod
    def _safe_summary(text: str | None, max_len: int = 40) -> str:
        """Clean whitespace and truncate ``text`` to ``max_len`` characters."""
        if text is None:
            text = ""
        text = str(text)
        cleaned = " ".join(text.split())
        if max_len <= 0:
            return ""
        if len(cleaned) <= max_len:
            return cleaned
        if max_len <= 3:
            return "." * max_len
        return cleaned[: max_len - 3] + "..."

    @staticmethod
    def log_tool(
        session_id: str | None,
        query_summary: str | None,
        tool_name: str | None,
        elapsed: float,
        status: str | None = "ok",
    ) -> None:
        """Log a single tool invocation timing."""
        session_id = session_id or "unknown"
        query_summary = TimingLogger._safe_summary(query_summary)
        tool_name = tool_name or "unknown"
        status = status or "ok"
        print(
            f"[TOOL_TIMING] session={session_id} query=\"{query_summary}\" "
            f"tool={tool_name} elapsed={elapsed}s status={status}"
        )

    @staticmethod
    def log_query(
        session_id: str | None,
        query_summary: str | None,
        total_elapsed: float,
        status: str | None = "ok",
    ) -> None:
        """Log the total elapsed time for a user query."""
        session_id = session_id or "unknown"
        query_summary = TimingLogger._safe_summary(query_summary)
        status = status or "ok"
        print(
            f"[QUERY_TIMING] session={session_id} query=\"{query_summary}\" "
            f"total_elapsed={total_elapsed}s status={status}"
        )