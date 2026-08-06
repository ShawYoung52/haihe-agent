# 证据完整性提前收口（Shadow）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `is_evidence_complete(query_type, tool_results)` 证据完整性判断，在 `process_message` 记录 `would_early_finalize` shadow 日志（默认不改流程），为将来扩展 Fix A 到非滚动预报场景打基础。

**Architecture:** ①`tools/meteo_evidence.py` 新增纯函数 `is_evidence_complete`（从结构化 bundles 判断）；②`process_message` 的 Fix A 判断附近加 shadow 记录（`ENABLE_EVIDENCE_EARLY_FINALIZE=false` 时只记录）；③`TimingContext` 加 `evidence` 字段输出到 `[PERF]`。

**Tech Stack:** Python 3.10+, Chainlit（2.9.6/2.11.0）, pytest.

## Global Constraints

- **默认关闭**：`ENABLE_EVIDENCE_EARLY_FINALIZE=false`。关闭时只记录 shadow 日志，不改真实流程。
- **不改现有 Fix A**：Fix A 已上线验证，不回归。
- **判断保守**：query_type 无映射/字段缺失/工具失败 → 返回 False（不跳过）。
- **不以提速减数据**：证据不完整时绝不跳过。
- `is_evidence_complete` 输入用**结构化 bundles**（`rolling_forecast_bundles`/`warning_bundles`），不解析 ToolMessage 文本。
- 分支：`perf/qa-evidence-finalize`（已建）。测试从 `chainlitexam/` 运行，venv `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe`。
- 全量套件预期 1 个既有 flaky `test_process_message_skips_fast_paths_when_disabled`。

---

### Task 1: `meteo_evidence.py` — `is_evidence_complete` 纯函数

**Files:**
- Create: `chainlitexam/tools/meteo_evidence.py`
- Test: `chainlitexam/tests/test_meteo_evidence.py`

**Interfaces:**
- Consumes: `tools/rolling_forecast_response._query_category`（query_type 分类）。
- Produces: `is_evidence_complete(query_type: str, tool_results: list[dict]) -> bool`。

**tool_results 约定**：list of dict，元素为 `{"tool_name": str, "bundle": dict}`。`bundle` 为结构化结果（滚动预报 bundle 有 `code_section`/`data_source`；预警 bundle 有 `records`）。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_meteo_evidence.py`：

```python
"""meteo_evidence.is_evidence_complete 测试。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from chainlitexam.tests.stubs import ensure_stubs
ensure_stubs()

from tools.meteo_evidence import is_evidence_complete


def test_forecast_complete_when_rolling_bundle_has_table():
    """预报类，滚动预报 bundle 有 code_section → 证据完整。"""
    results = [{"tool_name": "query_rolling_forecast", "bundle": {"code_section": "| 表格 |", "data_source": "天津市气象台滚动预报"}}]
    assert is_evidence_complete("forecast", results) is True


def test_forecast_incomplete_without_code_section():
    results = [{"tool_name": "query_rolling_forecast", "bundle": {"code_section": ""}}]
    assert is_evidence_complete("forecast", results) is False


def test_forecast_incomplete_empty_results():
    assert is_evidence_complete("forecast", []) is False


def test_warning_complete_with_records():
    results = [{"tool_name": "get_effective_warning_info", "bundle": {"records": [{"eventType": "暴雨", "severity": "黄色"}]}}]
    assert is_evidence_complete("warning", results) is True


def test_warning_incomplete_no_records():
    results = [{"tool_name": "get_effective_warning_info", "bundle": {"records": []}}]
    assert is_evidence_complete("warning", results) is False


def test_current_complete_with_observation_time():
    results = [{"tool_name": "query_current_weather_observation", "bundle": {"observation_time": "2026-08-06 14:00"}}]
    assert is_evidence_complete("current", results) is True


def test_unknown_query_type_is_conservative():
    results = [{"tool_name": "query_rolling_forecast", "bundle": {"code_section": "表格"}}]
    assert is_evidence_complete("unknown_kind", results) is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_meteo_evidence.py -v`
Expected: FAIL（`tools.meteo_evidence` 不存在）。

- [ ] **Step 3: 实现 `meteo_evidence.py`**

创建 `tools/meteo_evidence.py`：

```python
"""气象证据完整性判断（阶段五，默认只记录不改变流程）。

is_evidence_complete(query_type, tool_results) 判断当前工具结果是否足以直接回答，
无需再查询。设计为纯函数，便于测试。shadow 记录时由 process_message 调用。
"""
from __future__ import annotations

from typing import Any


# query_type → 必需字段映射（按工具 bundle 结构判断）
_REQUIRED_BUNDLE_KEYS: dict[str, set[str]] = {
    "forecast": {"code_section"},   # 滚动预报 bundle 有代码生成的表格即完整
    "rain": {"code_section"},
    "temperature": {"code_section"},
    "activity": {"code_section"},
    "visibility": {"code_section"},
    "warning": {"records"},          # 预警 bundle 有 records 即完整（空 records 视为"已解除"需 LLM 判断）
    "current": {"observation_time"},
    "water_level": {"water_level_m"},
}

# 已知但不支持提前收口的 query_type（保守返回 False）
_KNOWN_UNSAFE: set[str] = {"river", "impact", "unknown"}


def _bundle_complete(required_keys: set[str], bundle: dict) -> bool:
    if not isinstance(bundle, dict):
        return False
    for key in required_keys:
        value = bundle.get(key)
        # records 空列表视为不完整（预警无记录时需 LLM 判断"已解除"）
        if value is None:
            return False
        if isinstance(value, (list, tuple)) and not value:
            return False
        if isinstance(value, str) and not value.strip():
            return False
    return True


def is_evidence_complete(query_type: str, tool_results: list[dict]) -> bool:
    """判断证据是否完整。

    tool_results: list of {"tool_name": str, "bundle": dict}。
    仅当 query_type 有映射、存在至少一个结果、且该结果满足必需字段时返回 True。
    """
    if not tool_results:
        return False
    if query_type in _KNOWN_UNSAFE:
        return False
    required = _REQUIRED_BUNDLE_KEYS.get(query_type)
    if required is None:
        return False  # 无映射 → 保守 False
    return any(_bundle_complete(required, r.get("bundle") or {}) for r in tool_results)
```

> 注：`_REQUIRED_BUNDLE_KEYS` 的 `observation_time`/`water_level_m` 字段需要 `_run_tool_round` 构造 bundle 时带出（Task 2 处理）。

- [ ] **Step 4: 运行测试确认通过**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_meteo_evidence.py -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add chainlitexam/tools/meteo_evidence.py chainlitexam/tests/test_meteo_evidence.py
git commit -m "feat(qa): add is_evidence_complete pure function"
```

---

### Task 2: Shadow 记录 + TimingContext evidence 字段

**Files:**
- Modify: `chainlitexam/timing_logger.py`（`TimingContext` 加 `evidence` 字段 + `as_dict` 输出）
- Modify: `chainlitexam/message_orchestrator.py`（Fix A 判断附近加 shadow 记录）
- Test: `chainlitexam/tests/test_timing_logger.py`

**Interfaces:**
- Consumes: `is_evidence_complete`（Task 1）、`_query_category`、`TimingContext`（Task 2 加 `evidence`）。
- Produces: `TimingContext.evidence: dict`（含 `would_early_finalize`/`query_type`）；`process_message` shadow 日志 `[EVIDENCE]`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_timing_logger.py` 追加：

```python
def test_timing_context_evidence_field():
    ctx = TimingContext(request_id="req-ev")
    ctx.evidence = {"would_early_finalize": True, "query_type": "forecast"}
    d = ctx.as_dict()
    assert d["evidence"] == {"would_early_finalize": True, "query_type": "forecast"}
    assert "would_early_finalize" in d["evidence"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_timing_logger.py -v`
Expected: FAIL（`evidence` 字段不存在）。

- [ ] **Step 3: 实现 TimingContext evidence 字段**

`timing_logger.py` `TimingContext.__init__` 加 `self.evidence: dict = {}`，`as_dict` 加 `"evidence": self.evidence`。

- [ ] **Step 4: 实现 shadow 记录**

`message_orchestrator.py` 的 `process_message` 主循环，Fix A 判断（约 4783 行）附近加：

```python
        # 阶段五 shadow：记录证据完整性判断，不改变真实流程。
        # ENABLE_EVIDENCE_EARLY_FINALIZE=true 时才用 would_early 参与跳过决策。
        try:
            from tools.meteo_evidence import is_evidence_complete
            from tools.rolling_forecast_response import _query_category
            qtype = _query_category(message.content)
            tool_results = [
                {"tool_name": "query_rolling_forecast", "bundle": b}
                for b in rolling_forecast_bundles
            ]
            would_early = is_evidence_complete(qtype, tool_results)
            timing = cl.user_session.get("timing_context")
            if timing is not None:
                timing.evidence = {"would_early_finalize": would_early, "query_type": qtype}
            print(f"[EVIDENCE] query_type={qtype} would_early_finalize={would_early}")
        except Exception:
            pass
```

> 注：`ENABLE_EVIDENCE_EARLY_FINALIZE` 环境变量本期不实际参与跳过（Fix A 已覆盖滚动预报）；该开关在后续扩展时用。此 shadow 块只在 Fix A 未触发（非滚动预报）时提供 `would_early_finalize` 观测数据。

- [ ] **Step 5: 运行测试确认通过**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_timing_logger.py tests/test_meteo_evidence.py -v`
Expected: PASS。

- [ ] **Step 6: 运行全量测试确认无回归**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_decision_weather_tool.py -v`
Expected: 全量 PASS，仅 1 个既有 flaky。

- [ ] **Step 7: 提交**

```bash
git add chainlitexam/timing_logger.py chainlitexam/message_orchestrator.py chainlitexam/tests/test_timing_logger.py
git commit -m "perf(qa): record would_early_finalize evidence shadow in timing"
```

---

## Self-Review

**1. Spec coverage**：`is_evidence_complete`（Task 1）✓；shadow 记录（Task 2）✓；TimingContext evidence 字段（Task 2）✓；默认不改流程 ✓。

**2. Placeholder scan**：无 TBD。Task 1 的 `_REQUIRED_BUNDLE_KEYS` 完整定义。

**3. Type consistency**：`is_evidence_complete(query_type: str, tool_results: list[dict]) -> bool` 在 Task 1 定义、Task 2 调用一致；`TimingContext.evidence: dict` 在 Task 2 定义与测试一致。

**风险**：shadow 块 try/except 兜底，不影响真实流程。`_query_category` 复用现有分类口径。当前 `rolling_forecast_bundles` 只有 `code_section` 字段，`observation_time`/`water_level_m` 字段在 bundle 中尚未带出——但 Task 1 测试用构造的 dict 覆盖，生产 shadow 对 current/water_level 会保守返回 False（不跳过），符合"不确定不跳过"原则。