"""perf_stats.py 统计逻辑测试。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.perf_stats import _parse_perf_line, compute_percentiles, summarize


def test_compute_percentiles():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    p = compute_percentiles(vals)
    assert p["p50"] == pytest.approx(3.0)
    assert p["p90"] >= 4.0
    assert p["p99"] <= 5.0


def test_compute_percentiles_empty():
    assert compute_percentiles([]) == {"p50": 0, "p90": 0, "p95": 0, "p99": 0}


def test_parse_perf_line():
    rec = _parse_perf_line('[PERF] {"total_ms": 123.4, "planner_rounds": 2}')
    assert rec == {"total_ms": 123.4, "planner_rounds": 2}


def test_parse_perf_line_ignores_plain_lines():
    assert _parse_perf_line("some normal log line") is None
    assert _parse_perf_line("") is None
    assert _parse_perf_line('[PERF] not-json{') is None


def test_summarize():
    records = [
        {"total_ms": 100.0, "planner_rounds": 1,
         "tools": [{"name": "a", "ms": 30.0}, {"name": "b", "ms": 40.0}]},
        {"total_ms": 200.0, "planner_rounds": 2,
         "tools": [{"name": "a", "ms": 50.0}]},
        {"total_ms": "bad", "planner_rounds": 2},
    ]
    s = summarize(records)
    assert s["total_requests"] == 3
    assert s["planner_rounds_dist"] == {1: 1, 2: 2}
    # total_ms 只统计数值型记录（"bad" 被过滤）
    # 最近秩插值：n=2 时 int(0.5*2)=1 取上界 s[1]=200.0
    assert s["total_ms"]["p50"] == pytest.approx(200.0)
    # 工具耗时按累计排序，只保留前 10
    assert s["top_tools_by_ms"][0] == ("a", 80.0)
    assert s["top_tools_by_ms"][1] == ("b", 40.0)
