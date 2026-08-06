# 候选工具召回增强 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 增强候选工具召回影子观测——`ToolCandidateIndex` 加 `candidates_for_top_n`、`[TOOL_CAND]` 日志改 JSON Lines + 补 query_type/Top-N recall、新增 `recall_stats.py` 统计脚本。纯观测，不改 Planner 绑定。

**Architecture:** ①`candidates_for_top_n` 分层召回；②`[TOOL_CAND]` JSON Lines 日志含 query_type/recall_5/8/12；③`recall_stats.py` 离线统计 Top-N Recall + 漏召回列表。

**Tech Stack:** Python 3.10+, pytest.

## Global Constraints

- **纯影子**：不改 `bind_tools(tools)`（Planner 绑定完整工具集）。
- **默认关闭**：`ENABLE_ACTIVE_TOOL_FILTER=false`。
- 日志增强 try/except 兜底，不影响真实流程。
- 分支：`perf/qa-tool-recall`（已建）。测试从 `chainlitexam/` 运行，venv `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe`。
- 全量套件预期 1 个既有 flaky `test_process_message_skips_fast_paths_when_disabled`。

---

### Task 1: ToolCandidateIndex 分层召回 + JSON Lines 日志

**Files:**
- Modify: `chainlitexam/tools/tool_candidate_index.py`
- Modify: `chainlitexam/message_orchestrator.py`
- Test: `chainlitexam/tests/test_tool_candidate_index.py`

**Interfaces:**
- Consumes: `ToolCandidateIndex.candidates_for`（现有）、`_evidence_query_type_from_tool_names`（message_orchestrator 阶段五已加）。
- Produces: `ToolCandidateIndex.candidates_for_top_n(user_text, n) -> list[str]`；`[TOOL_CAND] {json}` 日志（含 query_type/recall_5/8/12/candidates_12）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_tool_candidate_index.py` 追加：

```python
def test_candidates_for_top_n_layers():
    """Top-5/8/12 分层召回，候选按关键词命中顺序。"""
    tools = [
        _fake_tool("query_rolling_forecast", "查询天津滚动预报未来天气"),
        _fake_tool("query_decision_weather_for_poi", "查询具体点位附近天气"),
        _fake_tool("get_effective_warning_info", "查询当前生效预警"),
        _fake_tool("query_water_level", "查询水位"),
        _fake_tool("rag_search", "知识库检索"),
        _fake_tool("query_basin_areal_rainfall", "流域面雨量"),
    ]
    idx = ToolCandidateIndex(tools)
    cands5 = idx.candidates_for_top_n("天津明天天气怎么样", 5)
    cands8 = idx.candidates_for_top_n("天津明天天气怎么样", 8)
    assert len(cands5) <= 5
    assert len(cands8) <= 8
    assert cands5 == cands8[:5]  # 分层一致
    assert "query_rolling_forecast" in cands5  # 天气问法召回滚动预报


def test_candidates_for_top_n_returns_list():
    idx = ToolCandidateIndex([_fake_tool("rag_search", "知识库检索")])
    assert isinstance(idx.candidates_for_top_n("知识库", 3), list)
```

> 注：检查现有 `_fake_tool` helper（test 文件已有），适配。

- [ ] **Step 2: 运行测试确认失败**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_tool_candidate_index.py -v`
Expected: FAIL（`candidates_for_top_n` 不存在）。

- [ ] **Step 3: 实现 `candidates_for_top_n`**

`tools/tool_candidate_index.py` 增加：

```python
    def candidates_for_top_n(self, user_text: str, n: int) -> list[str]:
        """取候选工具前 n 个（按关键词命中顺序，含兜底工具）。"""
        matched: list[str] = []
        for kw, names in self._by_keyword.items():
            if kw in (user_text or ""):
                for name in names:
                    if name not in matched:
                        matched.append(name)
        for name in self._default_candidates:
            if name not in matched:
                matched.append(name)
        return matched[:n]
```

改 `candidates_for` 复用：

```python
    def candidates_for(self, user_text: str, limit: int = 12) -> list[str]:
        return self.candidates_for_top_n(user_text, limit)
```

- [ ] **Step 4: 实现 JSON Lines 日志**

`message_orchestrator.py` 的 `[TOOL_CAND]` 块改为：

```python
    if planner_msg.tool_calls and callbacks.get("tool_candidate_index"):
        try:
            idx = callbacks["tool_candidate_index"]
            actual = [tc["name"] for tc in planner_msg.tool_calls]
            qtype = _evidence_query_type_from_tool_names(planner_msg)
            top5 = idx.candidates_for_top_n(message.content, 5)
            top8 = idx.candidates_for_top_n(message.content, 8)
            top12 = idx.candidates_for(message.content, limit=12)
            def _recall(top: list) -> str:
                hit = [t for t in actual if t in top]
                return f"{len(hit)}/{len(actual)}"
            print(f"[TOOL_CAND] {json.dumps({'request': session_id, 'query_type': qtype, 'actual': actual, 'recall_5': _recall(top5), 'recall_8': _recall(top8), 'recall_12': _recall(top12), 'candidates_12': top12}, ensure_ascii=False)}")
        except Exception:
            pass
```

> 注意：`json` 已在 message_orchestrator 顶部 import。`_evidence_query_type_from_tool_names` 已存在（阶段五）。

- [ ] **Step 5: 运行测试确认通过**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_tool_candidate_index.py tests/test_timing_logger.py -v`
Expected: PASS。

- [ ] **Step 6: 运行全量测试确认无回归**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_decision_weather_tool.py -v`
Expected: 全量 PASS，仅 1 个既有 flaky。

- [ ] **Step 7: 提交**

```bash
git add chainlitexam/tools/tool_candidate_index.py chainlitexam/message_orchestrator.py chainlitexam/tests/test_tool_candidate_index.py
git commit -m "perf(qa): layered tool candidate recall with JSON Lines shadow log"
```

---

### Task 2: recall_stats.py 统计脚本

**Files:**
- Create: `chainlitexam/scripts/recall_stats.py`
- Test: `chainlitexam/tests/test_recall_stats.py`

**Interfaces:**
- Consumes: `[TOOL_CAND] {json}` 行。
- Produces: `recall_stats.summarize(records) -> dict`（请求总数、按 query_type 分布、Top-5/8/12 平均 recall、漏召回工具列表）；CLI 读文件/stdin。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_recall_stats.py`：

```python
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
    assert "b" in s["missed_tools"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_recall_stats.py -v`
Expected: FAIL（`scripts.recall_stats` 不存在）。

- [ ] **Step 3: 实现脚本**

创建 `scripts/recall_stats.py`：

```python
"""读取 [TOOL_CAND] JSON Lines 日志，输出候选工具召回统计。

用法：
  python scripts/recall_stats.py < tool_cand.jsonl
  python scripts/recall_stats.py tool_cand.jsonl
"""
import json
import sys
from pathlib import Path


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
    records = []
    if len(sys.argv) > 1:
        for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
            rec = _parse_tool_cand_line(line)
            if rec:
                records.append(rec)
    else:
        for line in sys.stdin:
            rec = _parse_tool_cand_line(line)
            if rec:
                records.append(rec)
    print(json.dumps(summarize(records), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_recall_stats.py -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add chainlitexam/scripts/recall_stats.py chainlitexam/tests/test_recall_stats.py
git commit -m "feat(qa): add tool recall stats script"
```

---

## Self-Review

**1. Spec coverage**：`candidates_for_top_n`（Task 1）✓；JSON Lines 日志 + query_type/Top-N recall（Task 1）✓；`recall_stats.py`（Task 2）✓。

**2. Placeholder scan**：无 TBD。

**3. Type consistency**：`candidates_for_top_n(user_text, n) -> list[str]` 在 Task 1 定义与测试一致；`recall_stats.summarize/._parse_tool_cand_line` 在 Task 2 定义与测试一致。

**风险**：纯影子观测，不改绑定。`_evidence_query_type_from_tool_names` 复用阶段五 helper。日志 JSON 化后旧格式无消费者（`recall_stats.py` 是新解析器）。