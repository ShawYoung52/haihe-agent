"""读取 [PERF] JSON Lines 日志，输出 P95/P99 等统计。

用法：
  python scripts/perf_stats.py < perf.jsonl
  python scripts/perf_stats.py perf.jsonl
"""
import json

from scripts._stats_common import read_records


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
    stage_times: dict[str, list[float]] = {}
    tool_share: list[float] = []
    tools_per_req: list[float] = []
    for r in records:
        for name, ms in (r.get("stages") or {}).items():
            if isinstance(ms, (int, float)):
                stage_times.setdefault(name, []).append(ms)
        for t in r.get("tools", []):
            if isinstance(t, dict):
                name = t.get("name", "?")
                tool_times[name] = tool_times.get(name, 0) + t.get("ms", 0)
        tools = [t for t in r.get("tools", []) if isinstance(t, dict)]
        tools_per_req.append(float(len(tools)))
        tool_ms = sum(t.get("ms", 0) for t in tools)
        total = r.get("total_ms")
        if isinstance(total, (int, float)) and total > 0:
            tool_share.append(tool_ms / total * 100.0)
    stages_ms = {
        name: compute_percentiles(vals)
        for name, vals in sorted(
            stage_times.items(),
            key=lambda kv: compute_percentiles(kv[1])["p50"],
            reverse=True,
        )
    }
    return {
        "total_requests": len(records),
        "total_ms": compute_percentiles(totals),
        "planner_rounds_dist": rounds,
        # [PERF].stages 分阶段耗时分布（按 p50 降序），用于定位慢在 planner/tool/answer
        "stages_ms": stages_ms,
        # tool 取数耗时占总耗时百分比（前后对比：缓存命中后应显著下降）
        "tool_share_of_total_pct": compute_percentiles(tool_share),
        # 每请求工具调用次数（前后对比：多时次合并 12→2 应在此体现）
        "tools_per_request": compute_percentiles(tools_per_req),
        "top_tools_by_ms": sorted(tool_times.items(), key=lambda kv: kv[1], reverse=True)[:10],
    }


def main() -> None:
    print(json.dumps(summarize(read_records(_parse_perf_line)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()