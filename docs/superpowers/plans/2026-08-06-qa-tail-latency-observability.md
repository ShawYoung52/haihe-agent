# 问答智能体 P95/P99 观测 + 基线 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 先锁定黄金基线，再增强性能观测（JSON Lines + 统一出口 + 排队/TTFT/字符数 + 统计脚本），全部零业务风险、可回滚。

**Architecture:** ①基线报告 + 问题集 fixtures（不依赖真实工具结果）；②`TimingContext.log()` 改 JSON Lines；③`timing.log()` 统一到 `_log_query_exit` finally（所有退出路径记录一次）；④排队/字符数埋点；⑤`perf_stats.py` 统计脚本。

**Tech Stack:** Python 3.10+, Chainlit（2.9.6/2.11.0）, pytest.

## Global Constraints

- **黄金基线**（GPT 原则 1、2）：修改前全量 252 passed、1 既有 flaky、5 skipped。每个提交后回归，失败即停。
- **禁止大规模重写核心流程**（原则 3）：process_message/_run_tool_round/ask/_run_once 只加埋点，不改逻辑。
- **Shadow 不改变回答**（原则 6）：观测项只记录，不改工具调用/最终答案/GIS。
- **不以提速减数据**（原则 7）：埋点只记耗时，不跳过必要查询。
- **小批量独立提交**（原则 12）：每 Task 一提交。
- 分支：`perf/qa-tail-latency-meteo-domain`（已建）。测试从 `chainlitexam/` 运行，venv `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe`。
- 全量套件预期 1 个既有 flaky `test_process_message_skips_fast_paths_when_disabled`。

---

### Task 0: 黄金基线报告 + 问题集框架

**Files:**
- Create: `docs/performance/baseline-before-optimization.md`
- Create: `chainlitexam/tests/fixtures/meteo_qa_cases.json`
- Test: `chainlitexam/tests/test_meteo_qa_cases.py`

**Interfaces:**
- Consumes: 当前全量测试结果（252 passed）。
- Produces: `meteo_qa_cases.json`（26 类问题，每例含 `question/expected_tools{allowed,required,forbidden}/key_facts/time_scope/spatial_scope/units/forbidden_phrases/should_image/should_gis`）。

- [ ] **Step 1: 写问题集 fixtures**

创建 `tests/fixtures/meteo_qa_cases.json`，至少含以下类别（每类 1-2 例）：

```json
{
  "cases": [
    {"id": "current_obs", "category": "天气实况", "question": "现在天津天气怎么样", "expected_tools": {"allowed": ["query_current_weather_observation"], "required": ["query_current_weather_observation"], "forbidden": []}, "key_facts": ["温度", "降水"], "time_scope": "current", "spatial_scope": "天津", "units": ["℃", "mm"], "forbidden_phrases": ["根据工具"], "should_image": false, "should_gis": false},
    {"id": "hourly_forecast", "category": "逐小时预报", "question": "未来24小时天津每小时天气", "expected_tools": {"allowed": ["query_rolling_forecast"], "required": ["query_rolling_forecast"], "forbidden": []}, "key_facts": ["逐小时", "降水"], "time_scope": "next24h", "spatial_scope": "天津", "units": ["mm/h"], "forbidden_phrases": [], "should_image": false, "should_gis": false},
    {"id": "multi_day", "category": "未来多日预报", "question": "未来一周天津天气", "expected_tools": {"allowed": ["query_rolling_forecast"], "required": ["query_rolling_forecast"], "forbidden": []}, "key_facts": ["7天"], "time_scope": "7days", "spatial_scope": "天津", "units": ["℃"], "forbidden_phrases": [], "should_image": false, "should_gis": false},
    {"id": "precip_start_end", "category": "降水起止", "question": "天津明天几点开始下雨", "expected_tools": {"allowed": ["query_rolling_forecast"], "required": ["query_rolling_forecast"], "forbidden": []}, "key_facts": ["开始时间"], "time_scope": "tomorrow", "spatial_scope": "天津", "units": ["时"], "forbidden_phrases": [], "should_image": false, "should_gis": false},
    {"id": "accum_rain", "category": "累计雨量", "question": "天津过去24小时累计降雨多少", "expected_tools": {"allowed": ["query_current_weather_observation", "analyze_rainfall_by_time"], "required": [], "forbidden": []}, "key_facts": ["累计"], "time_scope": "past24h", "spatial_scope": "天津", "units": ["mm"], "forbidden_phrases": [], "should_image": false, "should_gis": false},
    {"id": "temp", "category": "气温", "question": "天津明天最高气温多少", "expected_tools": {"allowed": ["query_rolling_forecast"], "required": ["query_rolling_forecast"], "forbidden": []}, "key_facts": ["最高气温"], "time_scope": "tomorrow", "spatial_scope": "天津", "units": ["℃"], "forbidden_phrases": [], "should_image": false, "should_gis": false},
    {"id": "wind", "category": "风力和阵风", "question": "天津明天风力多大", "expected_tools": {"allowed": ["query_rolling_forecast"], "required": ["query_rolling_forecast"], "forbidden": []}, "key_facts": ["风力"], "time_scope": "tomorrow", "spatial_scope": "天津", "units": ["级", "m/s"], "forbidden_phrases": [], "should_image": false, "should_gis": false},
    {"id": "visibility", "category": "能见度和雾", "question": "天津明天能见度如何", "expected_tools": {"allowed": ["query_rolling_forecast"], "required": ["query_rolling_forecast"], "forbidden": []}, "key_facts": ["能见度"], "time_scope": "tomorrow", "spatial_scope": "天津", "units": ["m"], "forbidden_phrases": [], "should_image": false, "should_gis": false},
    {"id": "effective_warning", "category": "当前生效预警", "question": "现在有什么暴雨预警", "expected_tools": {"allowed": ["get_effective_warning_info"], "required": ["get_effective_warning_info"], "forbidden": []}, "key_facts": ["生效", "暴雨"], "time_scope": "current", "spatial_scope": "天津", "units": [], "forbidden_phrases": ["未检索到"], "should_image": false, "should_gis": true},
    {"id": "history_warning", "category": "历史预警", "question": "暴雨预警解除了吗", "expected_tools": {"allowed": ["get_effective_warning_info", "get_history_warning_info"], "required": ["get_history_warning_info"], "forbidden": []}, "key_facts": ["解除"], "time_scope": "current+history", "spatial_scope": "天津", "units": [], "forbidden_phrases": [], "should_image": false, "should_gis": true},
    {"id": "national_warning", "category": "国家级预警", "question": "中央气象台发布了什么预警", "expected_tools": {"allowed": ["get_national_warning_info"], "required": ["get_national_warning_info"], "forbidden": []}, "key_facts": ["中央气象台"], "time_scope": "current", "spatial_scope": "全国/天津", "units": [], "forbidden_phrases": [], "should_image": false, "should_gis": true},
    {"id": "decision_poi", "category": "点位决策天气", "question": "梅江会展中心明天天气如何", "expected_tools": {"allowed": ["query_decision_weather_for_poi"], "required": ["query_decision_weather_for_poi"], "forbidden": []}, "key_facts": ["梅江会展中心"], "time_scope": "tomorrow", "spatial_scope": "点位", "units": ["℃"], "forbidden_phrases": [], "should_image": false, "should_gis": false},
    {"id": "basin_areal", "category": "流域面雨量", "question": "海河流域面雨量多少", "expected_tools": {"allowed": ["query_basin_areal_rainfall"], "required": ["query_basin_areal_rainfall"], "forbidden": []}, "key_facts": ["面雨量"], "time_scope": "current", "spatial_scope": "海河流域", "units": ["mm"], "forbidden_phrases": [], "should_image": false, "should_gis": true}
  ]
}
```

> 完整 26 类在实现时补全（未来小时/降水起止已含/暴雨影响/行政区划/防汛应急/水位/河网/上下游/缺测/工具超时/多轮追问/图片/GIS/同会话/异会话等）。核心是字段结构完整、不依赖真实工具结果。

- [ ] **Step 2: 写结构校验测试**

创建 `tests/test_meteo_qa_cases.py`：

```python
"""meteo_qa_cases.json 结构校验。"""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "meteo_qa_cases.json"
REQUIRED = ["id", "category", "question", "expected_tools", "key_facts", "time_scope", "spatial_scope", "units", "forbidden_phrases", "should_image", "should_gis"]


def test_cases_file_exists_and_parses():
    assert FIXTURE.exists(), f"缺少 {FIXTURE}"
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and "cases" in data
    assert len(data["cases"]) >= 20, "至少覆盖 20 类问题"


def test_every_case_has_required_fields():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for case in data["cases"]:
        for field in REQUIRED:
            assert field in case, f"case {case.get('id')} 缺字段 {field}"
        assert set(case["expected_tools"].keys()) == {"allowed", "required", "forbidden"}, \
            f"case {case.get('id')} expected_tools 结构错误"
```

- [ ] **Step 3: 运行测试确认通过**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_meteo_qa_cases.py -v`
Expected: PASS。

- [ ] **Step 4: 写基线报告**

创建 `docs/performance/baseline-before-optimization.md`，记录：
- 全量测试：252 passed, 1 flaky, 5 skipped
- 代表性问答结果（从日志/实测）
- 工具路由：warning→规则路由、rolling→Fix A、decision→规则槽位
- HTTP 响应样例
- 已知 P95/P99：从 `[QUERY_TIMING]` 观察（32s/57s/115s 案例为基线）
- Planner 轮数分布：多数 1 轮（Fix A 后）

- [ ] **Step 5: 运行全量测试确认无回归**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_decision_weather_tool.py -v`
Expected: 全量 PASS（无代码改动，仅加 fixtures/测试/文档）。

- [ ] **Step 6: 提交**

```bash
git add docs/performance/baseline-before-optimization.md chainlitexam/tests/fixtures/meteo_qa_cases.json chainlitexam/tests/test_meteo_qa_cases.py
git commit -m "test(qa): add golden baseline report and meteo QA cases fixture"
```

---

### Task 1: 调用链文档

**Files:**
- Create: `docs/performance/current-qa-call-chain.md`

- [ ] **Step 1: 写调用链文档**

画出非 fast path 调用链，每节点标注：是否调 LLM、输入大小、超时、重试、Semaphore、提前 return、敏感输出：

```
HTTP 排队 (qa_http_api.ask _semaphore) → 获取运行时 (_get_runtime) → init_http_context
  → process_message:
    ├─ is_current_rolling_weather_query 专用路径（LLM 1 次 summary）
    ├─ ReasoningStep 创建
    ├─ 简单天气规则路由（_route_simple_weather_query，无 LLM）
    ├─ THINKING_PLANNER（ENABLE_LLM_THINKING 关→跳过）
    ├─ Planner 第 1 次（astream_planner_think，LLM，60s 超时/连接错误重试）
    ├─ _run_tool_round（并行纯数据工具 + 串行副作用工具）
    ├─ Fix A：滚动预报数据完整 → 跳过第 2 次 Planner → Answer
    ├─ Fix C：Planner 超时+有数据 → 回退 Answer
    ├─ 否则 Planner 第 2 次（LLM）
    └─ Answer（astream_answer_chain_to_message，chainlit 逐 chunk / http 一次）
  → HTTP 结果归并（merge_answers/reasoning_texts）
```

每节点标注：
- `Planner 第 1 次`：调 LLM；输入=messages 字符数；超时 60s；重试=连接错误；无 Semaphore；可提前 return（简单天气路由跳过）；输出=tool_calls/content
- `_run_tool_round`：不调 LLM；并行工具经 `_PARALLEL_TOOL_SEMAPHORE`（并发 4）；单个工具 60s 容错重试
- `Answer`：调 LLM；HTTP 模式一次写入；chainlit 模式逐 chunk；无重试

- [ ] **Step 2: 提交**

```bash
git add docs/performance/current-qa-call-chain.md
git commit -m "docs(qa): add current non-fast-path QA call chain"
```

---

### Task 2: TimingContext JSON Lines + 统一出口

**Files:**
- Modify: `chainlitexam/timing_logger.py`（`TimingContext.log()` 改 JSON）
- Modify: `chainlitexam/message_orchestrator.py`（`_log_query_exit` 统一输出 timing；移除主循环 `timing.log()`）
- Test: `chainlitexam/tests/test_timing_logger.py`

**Interfaces:**
- Consumes: `_log_query_exit(query_start_time, session_id, query_summary, status)`；`cl.user_session` 存 timing。
- Produces: `TimingContext.log() -> None` 输出 `[PERF] {json}`；`TimingContext.as_dict() -> dict`（供统一出口）。`_log_query_exit` 在 finally 读 `cl.user_session.get("timing_context")` 并 `log()`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_timing_logger.py` 追加：

```python
def test_timing_context_as_dict_and_json_log():
    ctx = TimingContext(request_id="req-1")
    ctx.mark("thinking")
    ctx.mark("planner_round_1")
    ctx.record_planner_round()
    ctx.record_tool_call("get_effective_warning_info", 12.5)
    ctx.mark("done")
    d = ctx.as_dict()
    assert d["request_id"] == "req-1"
    assert d["planner_rounds"] == 1
    assert d["tool_call_count"] == 1
    assert "thinking" in d["stages"]
    assert d["status"] == "ok"
    import json as _json
    _json.loads(ctx.to_json())  # 必须是合法 JSON


def test_timing_log_has_no_sensitive_fields():
    import re
    ctx = TimingContext(request_id="req-2")
    ctx.mark("done")
    out = ctx.to_json()
    assert "10.226" not in out
    assert ".venv" not in out
```

- [ ] **Step 2: 运行测试确认失败**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_timing_logger.py -v`
Expected: FAIL（`as_dict`/`to_json` 不存在）。

- [ ] **Step 3: 实现 TimingContext**

在 `timing_logger.py` 的 `TimingContext` 增加：

```python
    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "stages": {name: round(ms, 1) for name, ms in self.stages.items()},
            "planner_rounds": self.planner_rounds,
            "tool_call_count": self.tool_call_count,
            "tools": [{"name": n, "ms": round(ms, 1)} for n, ms in self.tool_calls],
            "total_ms": round((time.time() - self._start_ts) * 1000.0, 1),
            "status": self.status,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False)

    def log(self) -> None:
        print(f"[PERF] {self.to_json()}")
```

在 `__init__` 加 `self.status: str = "ok"`。

> 注意：`import json` 需在 timing_logger.py 顶部。

- [ ] **Step 4: 统一出口**

改 `message_orchestrator.py`：
1. `process_message` 开头（`query_start_time` 后）`cl.user_session.set("timing_context", timing)`。
2. 移除主循环末尾（约 4965 行）的 `timing.mark("done"); timing.log()`，只留 `timing.mark("done")`（或在 `_log_query_exit` 内 mark）。
3. `_log_query_exit` finally 里：

```python
    finally:
        cl.user_session.set("query_timing_logged", True)
        try:
            timing = cl.user_session.get("timing_context")
            if timing is not None and not getattr(timing, "_logged", False):
                timing.status = status
                timing.mark("done")  # 若尚未 mark
                timing.log()
                timing._logged = True
        except Exception:
            pass
```

- [ ] **Step 5: 运行测试确认通过**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_timing_logger.py tests/test_message_orchestrator.py -v`
Expected: PASS。

- [ ] **Step 6: 运行全量测试确认无回归**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_decision_weather_tool.py -v`
Expected: 全量 PASS，仅 1 个既有 flaky。

- [ ] **Step 7: 提交**

```bash
git add chainlitexam/timing_logger.py chainlitexam/message_orchestrator.py chainlitexam/tests/test_timing_logger.py
git commit -m "perf(qa): emit [PERF] as JSON Lines from unified query-exit hook"
```

---

### Task 3: 排队 + 字符数埋点

**Files:**
- Modify: `chainlitexam/qa_http_api.py`（HTTP semaphore 排队计时）
- Modify: `chainlitexam/message_orchestrator.py`（工具排队计时 + planner/answer 字符数）
- Test: `chainlitexam/tests/test_timing_logger.py`

**Interfaces:**
- Consumes: `TimingContext`（Task 2 的 `as_dict`）；`_PARALLEL_TOOL_SEMAPHORE`。
- Produces: `TimingContext.http_queue_wait_ms`/`tool_queue_wait_ms`/`planner_input_chars`/`answer_input_chars`/`planner_output_chars`/`answer_output_chars` 字段。

- [ ] **Step 1: 写失败测试**

在 `tests/test_timing_logger.py` 追加：

```python
def test_timing_context_queue_and_chars_fields():
    ctx = TimingContext(request_id="req-3")
    ctx.http_queue_wait_ms = 150.0
    ctx.tool_queue_wait_ms = 20.0
    ctx.planner_input_chars = 100
    ctx.answer_input_chars = 200
    d = ctx.as_dict()
    assert d["http_queue_wait_ms"] == 150.0
    assert d["planner_input_chars"] == 100
```

- [ ] **Step 2: 运行测试确认失败**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_timing_logger.py -v`
Expected: FAIL（字段不存在）。

- [ ] **Step 3: 实现字段**

`timing_logger.py` `TimingContext.__init__` 加默认字段，`as_dict` 加输出：

```python
    # __init__ 追加：
    self.http_queue_wait_ms: float = 0.0
    self.tool_queue_wait_ms: float = 0.0
    self.planner_input_chars: int = 0
    self.planner_output_chars: int = 0
    self.answer_input_chars: int = 0
    self.answer_output_chars: int = 0
```

```python
    # as_dict 追加：
    "http_queue_wait_ms": round(self.http_queue_wait_ms, 1),
    "tool_queue_wait_ms": round(self.tool_queue_wait_ms, 1),
    "planner_input_chars": self.planner_input_chars,
    "planner_output_chars": self.planner_output_chars,
    "answer_input_chars": self.answer_input_chars,
    "answer_output_chars": self.answer_output_chars,
```

- [ ] **Step 4: HTTP 排队计时**

`qa_http_api.py` 的 `ask` 方法（`async with self._semaphore` 前）：

```python
        import time as _time
        sem_wait_start = _time.time()
        async with self._semaphore:
            sem_wait_ms = (_time.time() - sem_wait_start) * 1000
            # 从 cl.user_session 取 timing 设置 http_queue_wait_ms（若无则忽略）
            ...
```

> 注：`ask` 在 `qa_http_api.py`，`process_message` 的 timing 存在 `cl.user_session`。`_run_once` 创建 session 后、`process_message` 内设置 timing。排队发生在 `_run_once` 之前，需在 `ask` 里把排队时间写入 session 供后续读取。实现时以 `cl.user_session.get/set` 传递。

- [ ] **Step 5: 工具排队计时**

`message_orchestrator.py` `_invoke_tools_in_parallel` 的 `_invoke_one`（`async with _PARALLEL_TOOL_SEMAPHORE` 前）：

```python
    async def _invoke_one(tool_call):
        sem_start = time.time()
        async with _PARALLEL_TOOL_SEMAPHORE:
            tool_queue_ms = (time.time() - sem_start) * 1000
            # timing 累计 tool_queue_wait_ms
            ...
```

> 注：`_invoke_one` 在 `_run_tool_round` 内部，timing 对象经 `cl.user_session` 获取，或作为参数传入。实现时以最小侵入记录。

- [ ] **Step 6: 字符数埋点**

`message_orchestrator.py` planner 调用前：

```python
        timing = cl.user_session.get("timing_context")
        if timing is not None:
            timing.planner_input_chars = sum(len(str(m.content or "")) for m in messages)
        planner_msg = await callbacks["astream_planner_think"](...)
        if timing is not None:
            timing.planner_output_chars = len(str(planner_msg.content or ""))
```

answer 调用同理（`answer_input_chars` = messages 字符数，`answer_output_chars` = answer 文本长度）。

- [ ] **Step 7: 运行测试确认通过**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_timing_logger.py -v`
Expected: PASS。

- [ ] **Step 8: 运行全量测试确认无回归**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_decision_weather_tool.py -v`
Expected: 全量 PASS，仅 1 个既有 flaky。

- [ ] **Step 9: 提交**

```bash
git add chainlitexam/timing_logger.py chainlitexam/qa_http_api.py chainlitexam/message_orchestrator.py chainlitexam/tests/test_timing_logger.py
git commit -m "perf(qa): record queue-wait and input/output char metrics"
```

---

### Task 4: 统计脚本 perf_stats.py

**Files:**
- Create: `chainlitexam/scripts/perf_stats.py`
- Test: `chainlitexam/tests/test_perf_stats.py`

**Interfaces:**
- Consumes: `[PERF] {json}` 行（stdin 或文件）。
- Produces: p50/p90/p95/p99、Planner 轮数分布、工具耗时排行、排队时间排行。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_perf_stats.py`：

```python
"""perf_stats.py 统计逻辑测试。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.perf_stats import compute_percentiles, summarize


def test_compute_percentiles():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    p = compute_percentiles(vals)
    assert p["p50"] == pytest.approx(3.0)
    assert p["p90"] >= 4.0
    assert p["p99"] <= 5.0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_perf_stats.py -v`
Expected: FAIL（`scripts.perf_stats` 不存在）。

- [ ] **Step 3: 实现脚本**

创建 `scripts/perf_stats.py`：

```python
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
```

> 注：`scripts/` 需有 `__init__.py` 或按现有脚本目录方式导入（检查 `chainlitexam/scripts/` 现有结构）。

- [ ] **Step 4: 运行测试确认通过**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_perf_stats.py -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add chainlitexam/scripts/perf_stats.py chainlitexam/tests/test_perf_stats.py
git commit -m "perf(qa): add [PERF] JSON Lines stats script"
```

---

## Self-Review

**1. Spec coverage**：
- 黄金基线报告 + 问题集 → Task 0 ✓
- 调用链文档 → Task 1 ✓
- TimingContext JSON + 统一出口 → Task 2 ✓
- 排队 + 字符数 → Task 3 ✓
- 统计脚本 → Task 4 ✓
- GPT 13 原则（小批量、禁重写、shadow 不改回答、回归即停）→ 各 Task Global Constraints ✓

**2. Placeholder scan**：Task 3 的 HTTP/工具排队计时标注"实现时以最小侵入记录"——因 `_run_once`/`_invoke_one` 的 timing 传递需现场确认，但给出了插入点。无 TBD。

**3. Type consistency**：`TimingContext.as_dict()/to_json()/status` 在 Task 2 定义、Task 3 扩展字段一致。`perf_stats.compute_percentiles/summarize` 在 Task 4 定义与测试一致。

**风险**：全为观测性改动。统一出口把 timing 从主循环移入 `_log_query_exit`，用 `cl.user_session` 传递——需确认 `_run_once` 的 session 生命周期（finally 释放前 `_log_query_exit` 已调用，可读 timing）。若传递失败，`_log_query_exit` 的 try/except 兜底，不影响回答。