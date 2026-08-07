"""读取 [TOOL_CAND] JSON Lines 日志，输出候选工具召回统计。

用法：
  python scripts/recall_stats.py < tool_cand.jsonl
  python scripts/recall_stats.py tool_cand.jsonl
"""
import json

from scripts._stats_common import read_records


def _parse_tool_cand_line(line: str) -> dict | None:
    line = line.strip()
    if "[TOOL_CAND] " not in line:
        return None
    try:
        return json.loads(line.split("[TOOL_CAND] ", 1)[1])
    except (ValueError, IndexError):
        return None


def _parse_recall_frac(frac: str) -> tuple[int, int]:
    try:
        hit, total = frac.split("/")
        return int(hit), int(total)
    except (ValueError, AttributeError):
        return 0, 0


def summarize(records: list[dict]) -> dict:
    by_type: dict[str, int] = {}
    recall = {"top5": [0, 0], "top8": [0, 0], "top12": [0, 0]}
    missed: dict[str, int] = {}
    for r in records:
        qtype = r.get("query_type", "unknown")
        by_type[qtype] = by_type.get(qtype, 0) + 1
        for key, pair in (("recall_5", recall["top5"]), ("recall_8", recall["top8"]), ("recall_12", recall["top12"])):
            hit, total = _parse_recall_frac(r.get(key, "0/0"))
            pair[0] += hit
            pair[1] += total
        candidates = set(r.get("candidates_12") or [])
        for tool in r.get("actual") or []:
            if tool not in candidates:
                missed[tool] = missed.get(tool, 0) + 1
    return {
        "total_requests": len(records),
        "by_query_type": by_type,
        "top5_recall": {"hit": recall["top5"][0], "total": recall["top5"][1]},
        "top8_recall": {"hit": recall["top8"][0], "total": recall["top8"][1]},
        "top12_recall": {"hit": recall["top12"][0], "total": recall["top12"][1]},
        "missed_tools": sorted(missed.items(), key=lambda kv: kv[1], reverse=True),
    }


def main() -> None:
    print(json.dumps(summarize(read_records(_parse_tool_cand_line)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()