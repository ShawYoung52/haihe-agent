# 预警查询路由 LLM 规则化 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用纯规则替代 `_route_warning_tools` 的 LLM 路由调用，省 1 次 LLM（预警查询提速 5-10s）。

**Architecture:** 新增 `_route_warning_tools_rule_based(user_text)`，`_route_warning_tools` 规则优先、LLM 兜底。

**Tech Stack:** Python 3.10+, pytest.

## Global Constraints

- 内网 Chainlit 2.9.6，不重装。
- 规则抽不出 → 回退 LLM，不改变行为。
- 不改 `_generate_warning_core_and_advice`（必要的结论 LLM）。
- 测试从 `chainlitexam/` 运行；用 venv `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe`。
- 全量套件预期 1 个既有 flaky 失败 `test_process_message_skips_fast_paths_when_disabled`。
- 分支：`perf/qa-warning-route-rule`（已建）。提交 `git add` 只加改动文件。

---

### Task 1: 预警路由规则化

**Files:**
- Modify: `chainlitexam/tools/warning_workflow.py`
- Test: `chainlitexam/tests/test_warning_workflow.py`

**Interfaces:**
- Consumes: `_normalize_warning_route`、`_infer_national_warning_keywords`。
- Produces: `_route_warning_tools_rule_based(user_text: str) -> dict | None`（与 `_normalize_warning_route` 返回格式兼容）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_warning_workflow.py` 新增：

```python
def test_rule_based_warning_route_effective():
    """"现在有什么预警"应路由到生效预警接口。"""
    route = wf._route_warning_tools_rule_based("现在有什么预警？")
    assert route is not None
    assert "get_effective_warning_info" in route["tool_names"]


def test_rule_based_warning_route_history():
    """"暴雨预警解除了吗"应包含历史预警接口。"""
    route = wf._route_warning_tools_rule_based("暴雨预警解除了吗？")
    assert route is not None
    assert "get_history_warning_info" in route["tool_names"]


def test_rule_based_warning_route_today():
    """"今天发布了哪些预警"应包含今日动态接口。"""
    route = wf._route_warning_tools_rule_based("今天发布了哪些预警？")
    assert route is not None
    assert "get_today_warning_summary" in route["tool_names"]


def test_rule_based_warning_route_national():
    """"中央气象台和天津预警"应同时包含国家与本地接口。"""
    route = wf._route_warning_tools_rule_based("中央气象台和天津市发布的预警信息")
    assert route is not None
    assert "get_national_warning_info" in route["tool_names"]
    assert "get_effective_warning_info" in route["tool_names"]


def test_rule_based_warning_route_falls_back_without_keyword():
    """无预警关键词时返回 None（回退 LLM）。"""
    assert wf._route_warning_tools_rule_based("明天天气怎么样") is None
    assert wf._route_warning_tools_rule_based("") is None
```

> 注：`wf` 为现有测试文件的 import 别名（检查是 `import warning_workflow as wf` 还是 `from tools import warning_workflow as wf`）。

- [ ] **Step 2: 运行测试确认失败**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_warning_workflow.py::test_rule_based_warning_route_effective -v`
Expected: FAIL（`_route_warning_tools_rule_based` 不存在）。

- [ ] **Step 3: 实现**

在 `warning_workflow.py` 新增 `_route_warning_tools_rule_based`（`_route_warning_tools` 前，约 452 行前），并在 `_route_warning_tools` 开头加规则优先：

```python
def _route_warning_tools_rule_based(user_text: str) -> dict | None:
    t = (user_text or "").strip()
    if not t or "预警" not in t:
        return None
    tool_names = ["get_effective_warning_info"]
    national = any(k in t for k in ("国家局", "中央气象台", "中央台", "全国", "周边", "华北", "京津冀", "北京", "河北"))
    history = any(k in t for k in ("解除了吗", "解除预警", "已解除", "解除的", "历史预警", "过去", "此前"))
    today = any(k in t for k in ("今天新发", "今日新发", "今日发布", "今天发布", "今日预警", "今天预警", "新发", "动态"))
    if national:
        has_local = any(k in t for k in ("天津", "我市", "全市"))
        tool_names = ["get_effective_warning_info", "get_national_warning_info"] if has_local else ["get_national_warning_info"]
    if history and "get_history_warning_info" not in tool_names:
        tool_names.append("get_history_warning_info")
    if today and "get_today_warning_summary" not in tool_names:
        tool_names.append("get_today_warning_summary")
    return _normalize_warning_route({
        "tool_names": tool_names,
        "national_keywords": _infer_national_warning_keywords(t, None),
        "reason": "规则路由",
    })
```

改 `_route_warning_tools` 开头：

```python
async def _route_warning_tools(answer_chain: Any, user_text: str, callbacks: dict[str, Any]) -> dict[str, Any]:
    rule_route = _route_warning_tools_rule_based(user_text)
    if rule_route:
        print(f"[WarningWorkflow] rule route={json.dumps(rule_route, ensure_ascii=False)}")
        return rule_route
    # 回退：现有 LLM 路由
    prompt = _fill_prompt(...)  # 原逻辑
    ...
```

- [ ] **Step 4: 运行测试确认通过**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_warning_workflow.py -v`
Expected: PASS（含新增测试与既有测试）。

- [ ] **Step 5: 运行全量测试确认无回归**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_decision_weather_tool.py -v`
Expected: 全量 PASS，仅 1 个既有 flaky 失败。

- [ ] **Step 6: 提交**

```bash
git add chainlitexam/tools/warning_workflow.py chainlitexam/tests/test_warning_workflow.py
git commit -m "perf(qa): rule-based warning tool routing with LLM fallback"
```

---

## Self-Review

**1. Spec coverage**：规则路由（Task 1）✓；LLM 兜底 ✓；不改结论 LLM ✓。
**2. Placeholder scan**：无 TBD/TODO。
**3. Type consistency**：`_route_warning_tools_rule_based -> dict | None` 定义与调用一致。

**风险**：规则路由保守选接口，多接口冗余查询浪费一点时间但不影响回答正确性；`_filter_warning_records_for_user` 下游按问法过滤。