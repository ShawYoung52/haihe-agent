# LLM 冷启动预热 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在启动预热中增加可选真实 LLM 推理（`ENABLE_LLM_WARMUP=false` 默认关），让 Planner/Answer 完成一次最小非敏感推理，降低首请求冷启动延迟。

**Architecture:** ①`_warmup_qa` 加 `ENABLE_LLM_WARMUP` 开关；②新增 `_llm_warmup(runtime)` 对 planner/answer chain 各 ainvoke 一次最小请求。

**Tech Stack:** Python 3.10+, Chainlit（2.9.6/2.11.0）, pytest.

## Global Constraints

- **默认关闭**：`ENABLE_LLM_WARMUP=false`。关闭时行为不变（只构建运行时）。
- 预热请求内容"请回复一个字：好"——非敏感，不含真实用户数据/内网地址/工具结果。
- 预热失败不阻断启动（try/except 兜底）。
- 预热不写 Chainlit 数据层、不进 `_response_cache`。
- 分支：`perf/qa-llm-warmup`（已建）。测试从 `chainlitexam/` 运行，venv `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe`。
- 全量套件预期 1 个既有 flaky `test_process_message_skips_fast_paths_when_disabled`。

---

### Task 1: LLM 预热开关 + 函数

**Files:**
- Modify: `chainlitexam/chain_gzt.py`（`_warmup_qa` ~540 行 + 新增 `_llm_warmup`）
- Test: `chainlitexam/tests/test_llm_warmup.py`

**Interfaces:**
- Consumes: `qa_http_api.runtime._get_runtime()`（已存在）、`runtime["planner_chain"]`/`runtime["answer_chain"]`。
- Produces: `async def _llm_warmup(runtime: dict) -> None`；`ENABLE_LLM_WARMUP` 环境变量读取。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_llm_warmup.py`：

```python
"""LLM 预热测试。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from chainlitexam.tests.stubs import ensure_stubs
ensure_stubs()

import asyncio
import pytest


class _FakeChain:
    def __init__(self):
        self.calls = 0
    async def ainvoke(self, input_dict, config=None):
        self.calls += 1
        return type("R", (), {"content": "好"})()


@pytest.mark.asyncio
async def test_llm_warmup_invokes_both_chains():
    """ENABLE_LLM_WARMUP=true 时 planner 和 answer 各 ainvoke 一次。"""
    import chain_gzt
    planner = _FakeChain()
    answer = _FakeChain()
    await chain_gzt._llm_warmup({"planner_chain": planner, "answer_chain": answer})
    assert planner.calls == 1
    assert answer.calls == 1


@pytest.mark.asyncio
async def test_llm_warmup_tolerates_failure():
    """预热失败（chain 抛错）不抛异常。"""
    import chain_gzt
    class _Broken:
        async def ainvoke(self, *a, **k):
            raise RuntimeError("boom")
    await chain_gzt._llm_warmup({"planner_chain": _Broken(), "answer_chain": _Broken()})
```

> 注：`chain_gzt` 模块顶层 import 可能连内网 MCP（`load_sse_tools`），但 `_llm_warmup` 只在调用时用传入的 chain，不触发 MCP。若 import 卡住，用 stub 方式导入（参考 test_chain_timeout.py 的 `CHAINLIT_ENABLE_DB=0` + minimal mock）。

- [ ] **Step 2: 运行测试确认失败**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_llm_warmup.py -v`
Expected: FAIL（`_llm_warmup` 不存在）。

- [ ] **Step 3: 实现 `_llm_warmup`**

`chain_gzt.py` 新增（放在 `_warmup_qa` 前）：

```python
async def _llm_warmup(runtime: dict) -> None:
    """对 Planner 和 Answer 发最小非敏感请求，触发一次真实推理预热。"""
    warmup_msg = [HumanMessage(content="请回复一个字：好")]
    for name, chain_key in (("planner", "planner_chain"), ("answer", "answer_chain")):
        chain = runtime.get(chain_key)
        if chain is None:
            continue
        try:
            await asyncio.wait_for(chain.ainvoke({"messages": warmup_msg}), timeout=30)
            print(f"[LLM-WARMUP] {name} done")
        except Exception as e:
            print(f"[LLM-WARMUP] {name} failed: {type(e).__name__}")
```

> 注意：`HumanMessage` 已在 chain_gzt.py import（检查顶部，若没有则加 `from langchain_core.messages import HumanMessage`）。

- [ ] **Step 4: 修改 `_warmup_qa` 加开关**

```python
@cl.on_app_startup
async def _warmup_qa():
    if not qa_http_api.runtime.configured:
        qa_http_api.runtime.configure(_build_qa_runtime)
    try:
        print("[QA-API] warming up...")
        runtime = await qa_http_api.runtime._get_runtime()
        if os.environ.get("ENABLE_LLM_WARMUP", "false").strip().lower() in ("1", "true", "yes"):
            await _llm_warmup(runtime)
            print("[QA-API] ready (with LLM warmup)")
        else:
            print("[QA-API] ready (LLM warmup disabled)")
    except Exception as e:
        print(f"[QA-API] warmup failed (will lazy-load): {type(e).__name__}")
```

- [ ] **Step 5: 运行测试确认通过**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_llm_warmup.py -v`
Expected: PASS。

- [ ] **Step 6: 运行全量测试确认无回归**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_decision_weather_tool.py -v`
Expected: 全量 PASS，仅 1 个既有 flaky。

- [ ] **Step 7: 提交**

```bash
git add chainlitexam/chain_gzt.py chainlitexam/tests/test_llm_warmup.py
git commit -m "feat(qa): optional LLM warmup on startup (ENABLE_LLM_WARMUP, default off)"
```

---

## Self-Review

**1. Spec coverage**：`_llm_warmup`（Task 1）✓；`ENABLE_LLM_WARMUP` 开关（Task 1）✓；失败兜底 ✓；默认关闭 ✓。

**2. Placeholder scan**：无 TBD。

**3. Type consistency**：`_llm_warmup(runtime: dict) -> None` 定义与测试一致。

**风险**：默认关闭行为不变。预热失败只打日志。预热请求非敏感。内网验证确认降首请求延迟后再启用。