"""Unified timing logger for tool and query elapsed time.

Outputs `[TOOL_TIMING]` and `[QUERY_TIMING]` lines to the console so that
performance metrics can be collected from logs.
"""

import time
import uuid


class TimingContext:
    """结构化记录一次问答请求的各阶段耗时，输出 [PERF] 一行。

    不记录用户问题、工具原始结果、内网地址或绝对路径。
    """

    def __init__(self, request_id: str | None = None):
        self.request_id = request_id or str(uuid.uuid4())
        self.stages: dict[str, float] = {}
        self.tool_calls: list[tuple[str, float]] = []
        self.planner_rounds: int = 0
        self.tool_call_count: int = 0
        self._start_ts = time.time()
        self._prev_ts = self._start_ts

    def mark(self, name: str) -> None:
        """记录自上一 mark 到现在的耗时（毫秒），作为 name 阶段。"""
        now = time.time()
        self.stages[name] = (now - self._prev_ts) * 1000.0
        self._prev_ts = now

    def record_planner_round(self) -> None:
        self.planner_rounds += 1

    def record_tool_call(self, tool_name: str, elapsed_ms: float) -> None:
        self.tool_calls.append((tool_name, elapsed_ms))
        self.tool_call_count += 1

    def log(self) -> None:
        parts = [f"request_id={self.request_id}"]
        parts += [f"{name}={ms:.0f}ms" for name, ms in self.stages.items()]
        parts.append(f"planner_rounds={self.planner_rounds}")
        parts.append(f"tool_call_count={self.tool_call_count}")
        parts.append(f"total_ms={(time.time() - self._start_ts) * 1000:.0f}ms")
        per_tool = ",".join(f"{n}:{ms:.0f}" for n, ms in self.tool_calls)
        parts.append(f"tools=[{per_tool}]")
        print(f"[PERF] {' '.join(parts)}")


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
            f"tool={tool_name} elapsed={elapsed:.2f}s status={status}"
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
            f"total_elapsed={total_elapsed:.2f}s status={status}"
        )