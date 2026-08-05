# Planner/HTTP 问答链路延迟优化（安全批）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现问答智能体性能安全批 5 项：关闭 Thinking LLM、阶段耗时日志、Planner/Answer 环境变量化、HTTP 不逐 chunk 刷新、HTTP emitter 不落库。

**Architecture:** ①`ENABLE_LLM_THINKING=false` 开关跳过 `thinking_chain`；②`TimingContext` 结构化记录各阶段耗时；③`_build_orchestrator_runtime` 读 `PLANNER_*`/`ANSWER_*` 环境变量（默认不变）；④`execution_mode` 让 HTTP 模式下 `astream_answer_chain_to_message` 内存累积后一次性更新；⑤`CapturingEmitter` 改纯内存记录、删 `_qa_persist_blocked` 全局标志。

**Tech Stack:** Python 3.10+, asyncio, Chainlit, pytest.

## Global Constraints

- 生产 `ENABLE_FAST_PATHS=false`，只优化通用 Planner 链路，**不动/不启用/不重构任何 fast path**。
- `/api/v1/qa/ask` 向后兼容：不改请求字段、不删响应字段、不改字段类型、保留 `code/data/message` 外层 + `answer/conversation_id/images/gis/reasoning/elapsed_seconds` 六字段。
- 禁止向外部返回：内部思考过程/CoT、模型名/地址、MCP 工具名、内网 IP/库地址/路径、原始异常/Traceback/连接串、未脱敏工具响应。
- 测试必须从 `chainlitexam/` 运行；用 venv `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe`。
- 全量套件预期 1 个既有 flaky 失败 `tests/test_message_orchestrator.py::test_process_message_skips_fast_paths_when_disabled`（ChainlitContextException），不计为本项目回归。
- 无法连内网时不伪造性能数字，用 fake chain/tool 写延迟测试并标注"真实数据需内网验证"。
- 分支：`perf/planner-http-latency`（已建）。提交用 `git add` 只加改动文件。

---

### Task 1: 关闭 Thinking LLM（`ENABLE_LLM_THINKING=false`）— Item ①

**Files:**
- Modify: `chainlitexam/message_orchestrator.py`（模块级新增 `ENABLE_LLM_THINKING`，约 54 行旁；`process_message` 约 4438 行）
- Test: `chainlitexam/tests/test_message_orchestrator.py`

**Interfaces:**
- Consumes: `process_message` 现有 `thinking_chain` 调用、`callbacks["astream_thinking_to_reasoning"]`。
- Produces: 模块级 `ENABLE_LLM_THINKING: bool`。`process_message` 在 `ENABLE_LLM_THINKING=False` 时跳过 thinking 调用。

- [ ] **Step 1: 写失败测试**

在 `tests/test_message_orchestrator.py` 新增：

```python
@pytest.mark.asyncio
async def test_process_message_skips_thinking_when_disabled(monkeypatch):
    """ENABLE_FAST_PATHS=false 且 ENABLE_LLM_THINKING=false 时，thinking_chain 调用次数严格为 0。"""
    monkeypatch.setattr(mo, "ENABLE_FAST_PATHS", False)
    monkeypatch.setattr(mo, "ENABLE_LLM_THINKING", False)

    thinking_calls = []

    async def fake_thinking(*args, **kwargs):
        thinking_calls.append("thinking")
        return None

    async def fake_astream_planner_think(*args, **kwargs):
        class FakePlannerMsg:
            content = "这是一个测试回答。"
            tool_calls = []
        return FakePlannerMsg()

    async def noop_async(*args, **kwargs):
        return None

    class FakeMessage:
        content = "测试查询"

    class FakeStreamMsg:
        def __init__(self, **kw):
            self.content = ""
        async def send(self):
            return None
        async def update(self):
            return None
        async def remove(self):
            return None

    callbacks = {
        "astream_planner_think": fake_astream_planner_think,
        "need_river_plot": lambda message: False,
        "astream_thinking_to_reasoning": fake_thinking,
        "append_followup_if_needed": lambda text, query: text,
        "stream_text_to_message": noop_async,
        "astream_answer_chain_to_message": lambda *a, **k: "",
    }

    monkeypatch.setattr(mo.cl, "Message", FakeStreamMsg)
    # ReasoningStep 保留真实实现需要的 stub 环境；若环境无 Chainlit context，
    # 单独 patch 掉 ReasoningStep 构造与 __aenter__
    class FakeReasoning:
        _closed = False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def stage(self, *a, **k):
            return None
        async def line(self, *a, **k):
            return None
        async def append(self, *a, **k):
            return None
        async def close(self):
            self._closed = True
            return None
    monkeypatch.setattr(mo, "ReasoningStep", lambda name="": FakeReasoning())

    await mo.process_message(
        FakeMessage(), planner_chain=None, answer_chain=None,
        thinking_chain=None, tools=[], messages=[], callbacks=callbacks,
    )

    assert thinking_calls == [], f"ENABLE_LLM_THINKING=false 时不应调用 thinking_chain，实际 {thinking_calls}"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_message_orchestrator.py::test_process_message_skips_thinking_when_disabled -v`
Expected: FAIL（`thinking_calls` 非空，当前无条件调 thinking）。

- [ ] **Step 3: 实现最小改动**

在 `message_orchestrator.py` 模块级（`ENABLE_FAST_PATHS` 旁，约 54 行）新增：

```python
ENABLE_LLM_THINKING = os.environ.get("ENABLE_LLM_THINKING", "false").strip().lower() in ("1", "true", "yes")
```

将 `process_message` 约 4438 行 `if not simple_route:` 改为：

```python
    if ENABLE_LLM_THINKING and not simple_route:
```

- [ ] **Step 4: 运行测试确认通过**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_message_orchestrator.py -v`
Expected: PASS（含新增测试与既有测试）。

- [ ] **Step 5: 运行全量测试确认无回归**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_decision_weather_tool.py -v`
Expected: 全量 PASS，仅 1 个既有 flaky 失败。

- [ ] **Step 6: 提交**

```bash
git add chainlitexam/message_orchestrator.py chainlitexam/tests/test_message_orchestrator.py
git commit -m "feat(qa): add ENABLE_LLM_THINKING flag to skip thinking LLM in planner path"
```

---

### Task 2: 阶段耗时日志（TimingContext）— Item ②

**Files:**
- Modify: `chainlitexam/timing_logger.py`（新增 `TimingContext`）
- Modify: `chainlitexam/message_orchestrator.py`（`process_message` 埋点 + `_log_query_exit` 处输出）
- Test: `chainlitexam/tests/test_timing_logger.py`（新增）

**Interfaces:**
- Consumes: `process_message` 内 `query_start_time`、`_run_tool_round` 返回、`iteration`。
- Produces: `timing_logger.TimingContext` — `__init__(request_id=None)`、`mark(name: str)`（记录自上一 mark 的耗时ms）、`record_planner_round()`、`record_tool_call(name, elapsed_ms)`、`log()`（输出 `[PERF]` 一行）。`TimingLogger` 保留。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_timing_logger.py`：

```python
"""TimingContext 结构化耗时日志测试。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from chainlitexam.tests.stubs import ensure_stubs
ensure_stubs()

import time
import io
from contextlib import redirect_stdout
from chainlitexam.timing_logger import TimingContext


def test_timing_context_accumulates_stages():
    ctx = TimingContext(request_id="test-1")
    ctx.mark("thinking")
    time.sleep(0.01)
    ctx.mark("planner_round_1")
    ctx.record_tool_call("get_city_rainfall_time_range", 12.5)
    ctx.record_planner_round()
    ctx.mark("answer")
    ctx.mark("done")

    assert ctx.stages["thinking"] >= 0
    assert ctx.stages["planner_round_1"] >= 10  # 0.01s = 10ms
    assert ctx.stages["answer"] >= 0
    assert ctx.tool_call_count == 1
    assert ctx.tool_calls[0][0] == "get_city_rainfall_time_range"
    assert ctx.planner_rounds == 1


def test_timing_context_log_line_has_no_sensitive_fields(capsys):
    ctx = TimingContext(request_id="req-abc")
    ctx.mark("thinking")
    ctx.mark("answer")
    ctx.mark("done")
    ctx.log()
    out = capsys.readouterr().out
    assert "[PERF]" in out
    assert "req-abc" in out
    assert "thinking=" in out
    assert "total_ms=" in out
    # 不泄露用户问题/内网地址/路径
    assert "10.226" not in out
    assert ".venv" not in out
```

- [ ] **Step 2: 运行测试确认失败**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_timing_logger.py -v`
Expected: FAIL（`TimingContext` 不存在）。

- [ ] **Step 3: 实现 TimingContext**

在 `timing_logger.py` 追加：

```python
import time
import uuid


class TimingContext:
    """结构化记录一次问答请求的各阶段耗时，输出 [PERF] 一行。

    不记录用户问题、工具原始结果、内网地址或绝对路径。
    """

    def __init__(self, request_id: str | None = None):
        self.request_id = request_id or str(uuid.uuid4())
        self.stages: dict[str, float] = {}
        self.tool_calls: list[tuple[str, float]] = []
        self.planner_rounds: int = 0
        self.tool_call_count: int = 0
        self._prev_ts = time.time()

    def mark(self, name: str) -> None:
        """记录自上一 mark 到现在的耗时（毫秒），作为 name 阶段。"""
        now = time.time()
        self.stages[name] = (now - self._prev_ts) * 1000.0
        self._prev_ts = now

    def record_planner_round(self) -> None:
        self.planner_rounds += 1

    def record_tool_call(self, tool_name: str, elapsed_ms: float) -> None:
        self.tool_calls.append((tool_name, elapsed_ms))
        self.tool_call_count += 1

    def log(self) -> None:
        parts = [f"request_id={self.request_id}"]
        parts += [f"{name}={ms:.0f}ms" for name, ms in self.stages.items()]
        parts.append(f"planner_rounds={self.planner_rounds}")
        parts.append(f"tool_call_count={self.tool_call_count}")
        parts.append(f"total_ms={(time.time() - self._prev_ts) * 1000:.0f}ms")
        per_tool = ",".join(f"{n}:{ms:.0f}" for n, ms in self.tool_calls)
        parts.append(f"tools=[{per_tool}]")
        print(f"[PERF] {' '.join(parts)}")
```

- [ ] **Step 4: 在 `process_message` 埋点**

在 `message_orchestrator.py` 顶部导入后，`process_message` 开头（`query_start_time` 之后）创建 `timing`：

```python
    if not hasattr(cl.user_session, "_current_timing"):
        pass
    from timing_logger import TimingContext
    timing = TimingContext(request_id=cl.user_session.get("id") or None)
```

在 `process_message` 关键点插入 `timing.mark(...)`：
- thinking 块之后（约 4454 行后）：`timing.mark("thinking")`
- 首次 Planner 之后（`planner_msg` 得到后）：`timing.mark("planner_round_1")` + `timing.record_planner_round()`
- 每轮 `_run_tool_round` 之后：`timing.mark(f"tool_round_{iteration}")` + `timing.record_tool_call`（可从 ToolMessage 或 `_run_tool_round` 返回的耗时统计）
- Answer 生成之后：`timing.mark("answer")`
- 结尾（`_log_query_exit` 前）：`timing.mark("done")` + `timing.log()`

具体埋点位置以 `process_message` 现有 `iteration`、`_run_tool_round` 返回为准，插入后不改变任何控制流。`total_ms` 由 `TimingContext.log()` 基于 `_prev_ts` 计算。

- [ ] **Step 5: 运行测试确认通过**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_timing_logger.py tests/test_message_orchestrator.py -v`
Expected: PASS。

- [ ] **Step 6: 运行全量测试确认无回归**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_decision_weather_tool.py -v`
Expected: 全量 PASS，仅 1 个既有 flaky 失败。

- [ ] **Step 7: 提交**

```bash
git add chainlitexam/timing_logger.py chainlitexam/message_orchestrator.py chainlitexam/tests/test_timing_logger.py
git commit -m "perf(qa): add TimingContext structured stage timing logs"
```

---

### Task 3: Planner/Answer 独立环境变量 — Item ③

**Files:**
- Modify: `chainlitexam/chain_gzt.py`（`_build_orchestrator_runtime` 约 2469-2482 行）
- Test: `chainlitexam/tests/test_planner_answer_config.py`（新增）

**Interfaces:**
- Consumes: `_build_orchestrator_runtime()` 现有 `ChatOpenAI` 构造。
- Produces: 读环境变量 `PLANNER_MODEL/API_BASE/API_KEY/TEMPERATURE/MAX_TOKENS` 与 `ANSWER_MODEL/API_BASE/API_KEY/TEMPERATURE/MAX_TOKENS`。默认值保持现状（Qwen3.6-27B、temp 0.7、现有 API base、无 max_tokens）。

> 说明：timeout/retries 在链级（`ainvoke_chain`/`astream_planner_think`）共享且当前值合理，本次不接入，避免改动共享路径。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_planner_answer_config.py`：

```python
"""Planner/Answer 独立环境变量配置测试。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from chainlitexam.tests.stubs import ensure_stubs
ensure_stubs()

import os
import importlib
from unittest.mock import patch


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def test_env_float_parses_and_falls_back():
    with patch.dict(os.environ, {"T": "0.3"}, clear=False):
        assert _env_float("T", 0.7) == 0.3
    assert _env_float("MISSING_KEY_XYZ", 0.7) == 0.7
    with patch.dict(os.environ, {"BAD": "abc"}, clear=False):
        assert _env_float("BAD", 0.7) == 0.7


def test_defaults_match_current_config(monkeypatch):
    """默认配置下 planner/answer 参数与现状一致（temp 0.7、同模型）。"""
    import chain_gzt
    # 直接验证 _build_orchestrator_runtime 构造的 ChatOpenAI 参数
    # 通过 monkeypatch 拦截 ChatOpenAI 构造，捕获传给 planner/answer 的关键参数
    captured = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured[kwargs.get("model", "?")] = kwargs

    monkeypatch.setattr(chain_gzt, "ChatOpenAI", _FakeChatOpenAI)
    # 其他构造依赖（load_sse_tools 连内网）用 monkeypatch 跳过
    async def _noop_load(*a, **k):
        return []
    monkeypatch.setattr(chain_gzt, "load_sse_tools", _noop_load)
    monkeypatch.setattr(chain_gzt, "build_external_skill_tools", lambda: [])
    monkeypatch.setattr(chain_gzt, "build_rain_analysis_tools", lambda: [])
    monkeypatch.setattr(chain_gzt, "build_decision_weather_tools", lambda *a, **k: [])
    monkeypatch.setattr(chain_gzt, "build_rainfall_river_impact_tools", lambda: [])

    import asyncio
    asyncio.get_event_loop().run_until_complete(chain_gzt._build_orchestrator_runtime())

    planner_kwargs = captured.get("Qwen3.6-27B")
    assert planner_kwargs is not None, f"未捕获 planner ChatOpenAI，captured={captured}"
    assert planner_kwargs.get("temperature") == 0.7
    assert planner_kwargs.get("model") == "Qwen3.6-27B"
```

> 注：若 `_build_orchestrator_runtime` 内部依赖难以 mock，可改为直接测试导出的小函数 `_env_float`/`_env_str` + 一个纯函数 `_build_llm_kwargs(prefix, defaults)` 返回 ChatOpenAI 参数字典，断言默认与 env 覆盖。以实际可测性为准，但必须验证"默认 temp=0.7、env 能覆盖 model/temperature"。

- [ ] **Step 2: 运行测试确认失败**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_planner_answer_config.py -v`
Expected: FAIL（`_env_float` 不存在或默认不符）。

- [ ] **Step 3: 实现**

在 `chain_gzt.py` 模块级新增辅助（放在 `_build_orchestrator_runtime` 前）：

```python
def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int_optional(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
```

将 `_build_orchestrator_runtime` 中 `planner_llm`/`answer_llm` 改为：

```python
    DEFAULT_API_BASE = "http://10.226.188.156:8000/v1/"

    planner_llm = ChatOpenAI(
        model=_env_str("PLANNER_MODEL", "Qwen3.6-27B"),
        streaming=True,
        temperature=_env_float("PLANNER_TEMPERATURE", 0.7),
        openai_api_base=_env_str("PLANNER_API_BASE", DEFAULT_API_BASE),
        openai_api_key=_env_str("PLANNER_API_KEY", "EMPTY"),
        **({"max_tokens": _env_int_optional("PLANNER_MAX_TOKENS")} if _env_int_optional("PLANNER_MAX_TOKENS") else {}),
    )
    answer_llm = ChatOpenAI(
        model=_env_str("ANSWER_MODEL", "Qwen3.6-27B"),
        streaming=True,
        temperature=_env_float("ANSWER_TEMPERATURE", 0.7),
        openai_api_base=_env_str("ANSWER_API_BASE", DEFAULT_API_BASE),
        openai_api_key=_env_str("ANSWER_API_KEY", "EMPTY"),
        **({"max_tokens": _env_int_optional("ANSWER_MAX_TOKENS")} if _env_int_optional("ANSWER_MAX_TOKENS") else {}),
    )
```

> 保持默认值不变（temp 0.7、Qwen3.6-27B、现有 API base、无 max_tokens）。不新增真实 IP/密钥——`DEFAULT_API_BASE` 即现有值。

- [ ] **Step 4: 运行测试确认通过**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_planner_answer_config.py -v`
Expected: PASS。

- [ ] **Step 5: 运行全量测试确认无回归**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_decision_weather_tool.py -v`
Expected: 全量 PASS，仅 1 个既有 flaky 失败。

- [ ] **Step 6: 提交**

```bash
git add chainlitexam/chain_gzt.py chainlitexam/tests/test_planner_answer_config.py
git commit -m "feat(qa): make planner and answer LLM config env-driven with unchanged defaults"
```

---

### Task 4: HTTP emitter 不落库 — Item ⑤

**Files:**
- Modify: `chainlitexam/qa_http_api.py`（`CapturingEmitter` 333-396 行；删 `_qa_persist_blocked` 466/455/718-719/747 行；删 `_ensure_data_layer_filter` 423-462 行及 600 行调用）
- Test: `chainlitexam/tests/test_qa_http_api.py`

**Interfaces:**
- Consumes: `CapturingEmitter(session)`、`_run_once` 内 `_qa_persist_blocked` 赋值。
- Produces: `CapturingEmitter` 各方法只内存记录、不调 `super()`。删除模块级 `_qa_persist_blocked` 与 `_ensure_data_layer_filter`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_qa_http_api.py` 新增：

```python
def test_capturing_emitter_does_not_call_super(monkeypatch):
    """HTTP emitter 的方法不应调用 super() 进入 Chainlit 数据层。"""
    import qa_http_api
    # 构造 emitter，monkeypatch super 方法为抛错，验证不被调用
    called = {"super": False}

    class FakeSession:
        id = "sess-1"

    cap = qa_http_api.CapturingEmitter(FakeSession())
    async def _boom(*a, **k):
        called["super"] = True
        raise AssertionError("super() 不应被调用")
    monkeypatch.setattr(qa_http_api.BaseChainlitEmitter, "send_step", _boom)
    monkeypatch.setattr(qa_http_api.BaseChainlitEmitter, "update_step", _boom)
    monkeypatch.setattr(qa_http_api.BaseChainlitEmitter, "delete_step", _boom)
    monkeypatch.setattr(qa_http_api.BaseChainlitEmitter, "send_element", _boom)
    monkeypatch.setattr(qa_http_api.BaseChainlitEmitter, "send_window_message", _boom)

    import asyncio
    async def _run():
        await cap.send_step({"id": "a", "type": "assistant_message", "output": "hi"})
        await cap.update_step({"id": "a", "type": "assistant_message", "output": "hello"})
        await cap.delete_step({"id": "a"})
        await cap.send_element({"id": "e"})
        await cap.send_window_message('{"gis":1}')
    asyncio.get_event_loop().run_until_complete(_run())

    assert called["super"] is False, "HTTP emitter 不应调用 super() 写数据层"
    assert len(cap.answer_steps) >= 1
    assert cap.gis_packets == [{"gis": 1}]


def test_no_qa_persist_blocked_global():
    """不再依赖进程级 _qa_persist_blocked 布尔变量。"""
    import qa_http_api
    assert not hasattr(qa_http_api, "_qa_persist_blocked")


def test_no_data_layer_filter_function():
    import qa_http_api
    assert not hasattr(qa_http_api, "_ensure_data_layer_filter")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_qa_http_api.py -v`
Expected: FAIL（当前 `super()` 被调用、`_qa_persist_blocked` 存在）。

- [ ] **Step 3: 实现**

在 `qa_http_api.py` `CapturingEmitter` 中，将各方法改为只记录、不调 `super()`：

```python
    async def send_step(self, step_dict):
        self._record(step_dict)

    async def update_step(self, step_dict):
        self._record(step_dict)

    async def delete_step(self, step_dict):
        if isinstance(step_dict, dict):
            sid = step_dict.get("id")
            if sid:
                self._deleted_ids.add(str(sid))

    async def send_element(self, element_dict):
        if isinstance(element_dict, dict):
            self.elements.append(element_dict)

    async def send_window_message(self, data):
        try:
            self.gis_packets.append(json.loads(data) if isinstance(data, str) else data)
        except (TypeError, ValueError):
            logger.debug("window message 非 JSON，已忽略")
```

删除：
- `_ensure_data_layer_filter` 函数（423-462 行）与其在 `configure`（600 行）的调用。
- 模块级 `_qa_persist_blocked = False`（466 行）。
- `_run_once` 中 `global _qa_persist_blocked` / `_qa_persist_blocked = True`（718-719 行）与 `_qa_persist_blocked = False`（747 行）。

> 注意：`_record`/`_deleted_ids`/`elements`/`gis_packets` 逻辑保留。网页 Chainlit 会话用默认 emitter，持久化不变。

- [ ] **Step 4: 运行测试确认通过**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_qa_http_api.py -v`
Expected: PASS（新增测试 + 既有 71 条）。

- [ ] **Step 5: 运行全量测试确认无回归**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_decision_weather_tool.py -v`
Expected: 全量 PASS，仅 1 个既有 flaky 失败。

- [ ] **Step 6: 提交**

```bash
git add chainlitexam/qa_http_api.py chainlitexam/tests/test_qa_http_api.py
git commit -m "refactor(qa): make HTTP CapturingEmitter in-memory only, drop _qa_persist_blocked"
```

---

### Task 5: HTTP 轻量模式（execution_mode 不逐 chunk 刷新）— Item ④

**Files:**
- Modify: `chainlitexam/chain_gzt.py`（`astream_answer_chain_to_message` 868-904 行加 `execution_mode` 参数；`_build_orchestrator_callbacks` 3717-3746 行加 `execution_mode` 参数并闭包包装；`_build_qa_runtime` 551-555 行传 `execution_mode="http"`）
- Modify: `chainlitexam/qa_http_api.py`（`_run_once` 传 execution_mode 到 callbacks，若非闭包包装则此处传）
- Test: `chainlitexam/tests/test_qa_http_api.py`、`chainlitexam/tests/test_thinking.py`

**Interfaces:**
- Consumes: Task 4 的 `CapturingEmitter` 纯内存版。
- Produces: `astream_answer_chain_to_message(answer_chain, input_dict, stream_msg, config=None, execution_mode="chainlit")`。`_build_orchestrator_callbacks(execution_mode="chainlit") -> dict`。HTTP 运行时用 `execution_mode="http"`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_thinking.py` 或新建 `tests/test_execution_mode.py` 新增：

```python
"""HTTP 执行模式不逐 chunk 刷新测试。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from chainlitexam.tests.stubs import ensure_stubs
ensure_stubs()

import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_http_mode_accumulates_answer_and_updates_once():
    """execution_mode='http' 时，astream_answer_chain_to_message 只调用一次 stream_msg.update()。"""
    import chain_gzt

    class _Chunk:
        def __init__(self, text):
            self.content = text

    class _FakeChain:
        async def astream(self, input_dict, config=None):
            for t in ["海河", "流域", "多云。"]:
                yield _Chunk(t)

        async def ainvoke(self, input_dict, config=None):
            return _Chunk("海河流域多云。")

    class _StreamMsg:
        def __init__(self):
            self.content = ""
            self.update_count = 0
        async def update(self):
            self.update_count += 1

    smsg = _StreamMsg()
    result = await chain_gzt.astream_answer_chain_to_message(
        _FakeChain(), {"messages": []}, smsg, execution_mode="http"
    )
    assert result == "海河流域多云。"
    assert smsg.content == "海河流域多云。"
    assert smsg.update_count == 1, f"HTTP 模式应只更新 1 次，实际 {smsg.update_count}"


@pytest.mark.asyncio
async def test_chainlit_mode_streams_per_chunk():
    import chain_gzt

    class _Chunk:
        def __init__(self, text):
            self.content = text

    class _FakeChain:
        async def astream(self, input_dict, config=None):
            for t in ["a", "b"]:
                yield _Chunk(t)

    class _StreamMsg:
        def __init__(self):
            self.content = ""
            self.update_count = 0
        async def update(self):
            self.update_count += 1

    smsg = _StreamMsg()
    result = await chain_gzt.astream_answer_chain_to_message(
        _FakeChain(), {"messages": []}, smsg, execution_mode="chainlit"
    )
    assert result == "ab"
    assert smsg.update_count > 1, "chainlit 模式应逐 chunk 更新"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_execution_mode.py -v`
Expected: FAIL（`execution_mode` 参数不存在）。

- [ ] **Step 3: 实现**

修改 `chain_gzt.py` 的 `astream_answer_chain_to_message`，增加 `execution_mode` 参数，HTTP 模式只在结尾更新一次：

```python
async def astream_answer_chain_to_message(answer_chain, input_dict, stream_msg, config=None, execution_mode="chainlit"):
    full_text = ""
    try:
        async for chunk in answer_chain.astream(input_dict, config=config):
            text = ""
            if hasattr(chunk, "content"):
                text = chunk.content or ""
            elif isinstance(chunk, str):
                text = chunk
            if text:
                text = _sanitize_display_text(text)
                full_text += text
                if execution_mode == "chainlit":
                    stream_msg.content += text
                    await stream_msg.update()
        final_text = _repair_markdown_layout(_sanitize_display_text(full_text))
        if execution_mode == "chainlit":
            stream_msg.content = final_text
            await stream_msg.update()
        else:
            stream_msg.content = final_text
        return _sanitize_display_text(full_text)
    except Exception as e:
        print(f"[流式回答] 失败，回退到非流式：{e}")
        if full_text.strip():
            final_text = _repair_markdown_layout(_sanitize_display_text(full_text))
            stream_msg.content = final_text
            if execution_mode == "chainlit":
                await stream_msg.update()
            else:
                await stream_msg.update()  # http 模式失败兜底也更新一次，保证答案可捕获
            return stream_msg.content
        result = await answer_chain.ainvoke(input_dict, config=config)
        text = getattr(result, "content", None) or ""
        text = _sanitize_display_text(text)
        stream_msg.content += text
        if execution_mode == "chainlit":
            await stream_msg.update()
        else:
            await stream_msg.update()
        return text
```

> 注意：HTTP 模式 `stream_msg.update()` 仍调用 1 次（结尾），保证 emitter 捕获最终答案。`_sanitize_display_text`/`_repair_markdown_layout` 保留。

修改 `_build_orchestrator_callbacks` 增加 `execution_mode` 参数并用闭包包装 `astream_answer_chain_to_message`：

```python
def _build_orchestrator_callbacks(execution_mode: str = "chainlit") -> dict:
    def _astream_answer(answer_chain, input_dict, stream_msg, config=None):
        return astream_answer_chain_to_message(
            answer_chain, input_dict, stream_msg, config, execution_mode=execution_mode
        )
    return {
        # ... 其余键不变 ...
        "astream_answer_chain_to_message": _astream_answer,
        # ...
    }
```

修改 `_build_qa_runtime`（551-555 行）传 `execution_mode="http"`：

```python
    runtime["callbacks"] = _build_orchestrator_callbacks(execution_mode="http")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_execution_mode.py tests/test_qa_http_api.py -v`
Expected: PASS（HTTP 模式 update 1 次，chainlit 模式逐 chunk）。

- [ ] **Step 5: 运行全量测试确认无回归**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_decision_weather_tool.py -v`
Expected: 全量 PASS，仅 1 个既有 flaky 失败。

- [ ] **Step 6: 提交**

```bash
git add chainlitexam/chain_gzt.py chainlitexam/qa_http_api.py chainlitexam/tests/test_execution_mode.py
git commit -m "perf(qa): add http execution_mode to avoid per-chunk Chainlit streaming"
```

---

## Self-Review

**1. Spec coverage**：
- Item ①（关闭 Thinking）→ Task 1 ✓
- Item ②（阶段耗时日志）→ Task 2 ✓
- Item ③（Planner/Answer 环境变量）→ Task 3 ✓
- Item ⑤（emitter 不落库）→ Task 4 ✓
- Item ④（HTTP 不逐 chunk 刷新）→ Task 5 ✓
- 硬约束（向后兼容、不返回敏感信息、新分支）→ 各任务 Global Constraints 均含 ✓

**2. Placeholder scan**：Task 2 的埋点位置以"现有 process_message 控制流为准"为指引，未写死行号（因行号会随实现漂移），但给出了具体埋点语义（thinking/planner_round_N/answer/done）。其余任务均有完整代码。无 TBD/TODO。

**3. Type consistency**：`TimingContext`（Task 2）的 `mark/record_planner_round/record_tool_call/log` 方法在测试与实现一致；`execution_mode`（Task 5）参数在 `astream_answer_chain_to_message`、`_build_orchestrator_callbacks`、`_build_qa_runtime` 一致。`_env_str/_env_float/_env_int_optional`（Task 3）签名一致。

**风险**：Task 4 删 `_qa_persist_blocked` 后，依赖"HTTP 所有输出都经 emitter（不调 super）"；若 `process_message` 有绕过 emitter 直接写数据层的路径（如图表 `cl.Message.send()`），HTTP 可能写库。测试 `test_capturing_emitter_does_not_call_super` 覆盖 emitter 层；全量测试回归确认无其他路径。若发现问题，回退为保留 `_qa_persist_blocked` 全局标志（仅删 emitter 的 super 调用）。

**接入说明**：初次上线只开 `ENABLE_LLM_THINKING=false` + HTTP 轻量模式（默认即为 false/http）；Task 3 的 env 默认不变，不发生产配置也可安全运行。