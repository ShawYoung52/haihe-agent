"""Shared helpers for JSON Lines stats scripts (perf_stats / recall_stats)."""

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


def read_records(parse_line: Callable[[str], dict[str, Any] | None]) -> list[dict[str, Any]]:
    """Read JSON Lines records from a file argument or stdin.

    Each line is passed to *parse_line*; only non-None results are kept.
    """
    records: list[dict[str, Any]] = []
    if len(sys.argv) > 1:
        for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
            rec = parse_line(line)
            if rec is not None:
                records.append(rec)
    else:
        for line in sys.stdin:
            rec = parse_line(line)
            if rec is not None:
                records.append(rec)
    return records