"""读取 [PERF] JSON Lines 日志，输出 P95/P99 等统计。

用法：
  python scripts/perf_stats.py < perf.jsonl
  python scripts/perf_stats.py perf.jsonl
"""
import json
import sys
from pathlib import Path


def _parse_perf_line(line: str) -> dict | None:
    line = line.strip()
    if "[PERF] " not in line:
        return None
    try:
        return json.loads(line.split("[PERF] ", 1)[1])
    except (ValueError, IndexError):
        return None


def compute_percentiles(values: list[float]) -> dict:
    if not values:
        return {"p50": 0, "p90": 0, "p95": 0, "p99": 0}
    s = sorted(values)
    n = len(s)
    def _p(p):
        idx = min(n - 1, int(p * n))
        return round(s[idx], 1)
    return {"p50": _p(0.50), "p90": _p(0.90), "p95": _p(0.95), "p99": _p(0.99)}


def summarize(records: list[dict]) -> dict:
    totals = [r.get("total_ms", 0) for r in records if isinstance(r.get("total_ms"), (int, float))]
    rounds = {}
    for r in records:
        n = r.get("planner_rounds", 0)
        rounds[n] = rounds.get(n, 0) + 1
    tool_times = {}
    for r in records:
        for t in r.get("tools", []):
            name = t.get("name", "?")
            tool_times[name] = tool_times.get(name, 0) + t.get("ms", 0)
    return {
        "total_requests": len(records),
        "total_ms": compute_percentiles(totals),
        "planner_rounds_dist": rounds,
        "top_tools_by_ms": sorted(tool_times.items(), key=lambda kv: kv[1], reverse=True)[:10],
    }


def main() -> None:
    records = []
    if len(sys.argv) > 1:
        for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
            rec = _parse_perf_line(line)
            if rec:
                records.append(rec)
    else:
        for line in sys.stdin:
            rec = _parse_perf_line(line)
            if rec:
                records.append(rec)
    print(json.dumps(summarize(records), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
