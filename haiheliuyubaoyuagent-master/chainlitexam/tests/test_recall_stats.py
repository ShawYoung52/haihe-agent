"""recall_stats.py 统计逻辑测试。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.recall_stats import _parse_tool_cand_line, summarize


def test_parse_tool_cand_line():
    line = '[TOOL_CAND] {"request": "s1", "query_type": "forecast", "actual": ["query_rolling_forecast"], "recall_5": "1/1", "recall_8": "1/1", "recall_12": "1/1", "candidates_12": ["query_rolling_forecast"]}'
    rec = _parse_tool_cand_line(line)
    assert rec is not None
    assert rec["query_type"] == "forecast"
    assert rec["actual"] == ["query_rolling_forecast"]


def test_parse_tool_cand_line_ignores_non_cand():
    assert _parse_tool_cand_line("not a tool cand line") is None


def test_summarize_recall():
    records = [
        {"query_type": "forecast", "actual": ["a"], "recall_5": "1/1", "recall_8": "1/1", "recall_12": "1/1", "candidates_12": ["a"]},
        {"query_type": "forecast", "actual": ["a", "b"], "recall_5": "1/2", "recall_8": "1/2", "recall_12": "1/2", "candidates_12": ["a"]},
    ]
    s = summarize(records)
    assert s["total_requests"] == 2
    assert s["by_query_type"]["forecast"] == 2
    # Top-5 recall: (1+1)/(1+2) = 2/3
    assert s["top5_recall"] == {"hit": 2, "total": 3}
    # 漏召回：b 在 actual 但不在 candidates_12
    assert ("b", 1) in s["missed_tools"]
