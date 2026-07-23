# Unify Thinking UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove all legacy `thinking_msg` loading bubbles, move progress indication into `ReasoningStep`, and make the reasoning step auto-collapse after the final answer is sent.

**Architecture:** Keep the existing `ReasoningStep` class but enable `auto_collapse=True` on its underlying `cl.Step`. Delete the `_show_thinking` helper and every `thinking_msg` variable across `message_orchestrator.py`, replacing progress updates with `reasoning.stage()` calls. `process_message` and all fast paths finalize the UI by calling `_maybe_close_reasoning(reasoning)` before streaming the final answer.

**Tech Stack:** Python 3.11, Chainlit, pytest.

---

## File Structure

- `chainlitexam/message_orchestrator.py` — primary file; removes `thinking_msg`, updates `ReasoningStep`, updates fast paths and `process_message`.
- `chainlitexam/tests/test_fast_paths.py` — static check that every fast path creates/closes `ReasoningStep`; already exists and should keep passing.
- `chainlitexam/tests/test_reasoning_step.py` — reasoning step behavior tests; may need a new assertion for `auto_collapse`.

---

## Task 1: Enable Auto-Collapse in `ReasoningStep`

**Files:**
- Modify: `chainlitexam/message_orchestrator.py:85`
- Test: `chainlitexam/tests/test_reasoning_step.py`

- [ ] **Step 1: Add a failing test for `auto_collapse=True`**

In `chainlitexam/tests/test_reasoning_step.py`, append:

```python
@pytest.mark.asyncio
async def test_reasoning_step_uses_auto_collapse():
    from chainlitexam.message_orchestrator import ReasoningStep

    reasoning = ReasoningStep("test collapse")
    await reasoning.__aenter__()
    assert reasoning.step.auto_collapse is True
    await reasoning.__aexit__(None, None, None)
```

Run: `python -m pytest chainlitexam/tests/test_reasoning_step.py::test_reasoning_step_uses_auto_collapse -v`
Expected: FAIL — `AssertionError` because `auto_collapse` is not set.

- [ ] **Step 2: Set `auto_collapse=True` in `ReasoningStep.__aenter__`**

Modify `chainlitexam/message_orchestrator.py` around line 85:

```python
self.step = cl.Step(
    name=self.name,
    type="llm",
    parent_id=parent_id,
    auto_collapse=True,
)
```

Remove any explicit `default_open` setting if it conflicts, but keep `self.step.show_input = "markdown"` and the existing reset logic.

Run: `python -m pytest chainlitexam/tests/test_reasoning_step.py::test_reasoning_step_uses_auto_collapse -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add chainlitexam/message_orchestrator.py chainlitexam/tests/test_reasoning_step.py
git commit -m "feat: enable auto_collapse on ReasoningStep"
```

---

## Task 2: Remove `_show_thinking` Helper

**Files:**
- Modify: `chainlitexam/message_orchestrator.py:1563-1566`
- Test: `chainlitexam/tests/test_fast_paths.py` (regression only)

- [ ] **Step 1: Delete the helper function**

Remove:

```python
async def _show_thinking(text: str) -> cl.Message:
    """发送并返回一条"正在思考/查询"提示消息，供 fast path 统一使用。"""
    msg = cl.Message(content=text)
    await msg.send()
    return msg
```

Run: `python -m py_compile chainlitexam/message_orchestrator.py`
Expected: success.

- [ ] **Step 2: Run fast-path regression test**

Run: `python chainlitexam/tests/test_fast_paths.py`
Expected: 18/18 PASS.

- [ ] **Step 3: Commit**

```bash
git add chainlitexam/message_orchestrator.py
git commit -m "refactor: remove unused _show_thinking helper"
```

---

## Task 3: Replace `thinking_msg` in Fast Paths with `reasoning.stage()`

**Files:**
- Modify: `chainlitexam/message_orchestrator.py` (all `_try_*_fast_path` functions)
- Test: `chainlitexam/tests/test_fast_paths.py`

- [ ] **Step 1: Identify and convert each fast path**

For every fast path in `message_orchestrator.py`:

1. Remove `thinking_msg = ...` / `await _show_thinking(...)` / `await thinking_msg.send()`.
2. Remove all `await thinking_msg.update(...)` and `await thinking_msg.remove()`.
3. Replace progress text with `await reasoning.stage("📡 查询数据", "<原 thinking_msg 文案，去掉 emoji 前缀>")`.

Examples:

- `_try_warning_fact_fast_path` line ~1054: remove `thinking_msg = await _show_thinking("🧭 正在判断预警接口，请稍候...")`; replace later `thinking_msg.content = f"🔔 正在调用{display_names}..."; await thinking_msg.update()` with `await reasoning.stage("📡 查询数据", f"正在调用{display_names}...")`.
- `_try_rainfall_img_fast_path` line ~2577: remove `thinking_msg = await _show_thinking("🔍 正在生成降水实况图，请稍候...")`; the existing `reasoning` is already created via `_show_business_reasoning`, so just stage progress.
- `_try_water_level_fast_path` line ~3630: remove `thinking_msg = await _show_thinking(f"🔍 正在查询{river_name}水位情况，请稍候...")`; use `reasoning.stage("📡 查询数据", f"正在查询{river_name}水位情况...")`.

Keep the existing `reasoning = await _show_business_reasoning(...)` and `generate_fast_path_thinking(...)` calls unchanged.

- [ ] **Step 2: Ensure every fast path still closes reasoning on all returns**

Run: `python chainlitexam/tests/test_fast_paths.py`
Expected: 18/18 PASS.

- [ ] **Step 3: Compile and full test**

Run:
```bash
python -m py_compile chainlitexam/message_orchestrator.py
python -m pytest chainlitexam/tests/ -v --ignore=chainlitexam/.venv_new
```
Expected: 46+ PASS.

- [ ] **Step 4: Commit**

```bash
git add chainlitexam/message_orchestrator.py
git commit -m "refactor: replace fast-path thinking_msg with reasoning.stage"
```

---

## Task 4: Replace `thinking_msg` in `process_message`

**Files:**
- Modify: `chainlitexam/message_orchestrator.py:4675-4998`
- Test: `chainlitexam/tests/test_message_orchestrator.py`

- [ ] **Step 1: Remove `thinking_msg` creation and usage in `process_message`**

In `process_message`:

1. Delete: `thinking_msg = cl.Message(content="🧭 正在分析问题，请稍候..."); await thinking_msg.send()`.
2. Replace `thinking_msg.content = "🔧 正在查询 ..."; await thinking_msg.update()` with `await reasoning.stage("📡 查询数据", "正在查询 ...")`.
3. Replace `thinking_msg.content = "✍️ 正在整理回答..."; await thinking_msg.update()` with `await reasoning.stage("✍️ 生成结论", "正在整理回答...")`.
4. Remove all `await thinking_msg.remove()` calls.
5. Keep `_maybe_close_reasoning(reasoning)` calls before final answers.

- [ ] **Step 2: Verify no references remain**

Run:
```bash
grep -n "thinking_msg" chainlitexam/message_orchestrator.py
```
Expected: no output.

Run:
```bash
python -m py_compile chainlitexam/message_orchestrator.py
python -m pytest chainlitexam/tests/ -v --ignore=chainlitexam/.venv_new
```
Expected: all tests PASS.

- [ ] **Step 3: Commit**

```bash
git add chainlitexam/message_orchestrator.py
git commit -m "refactor: remove thinking_msg from process_message"
```

---

## Task 5: Final Verification and CLAUDE.md Update

**Files:**
- Modify: `CLAUDE.md`
- Test: manual + automated

- [ ] **Step 1: Update CLAUDE.md**

In `CLAUDE.md` Development Conventions, add:

```diff
- Tool results are unwrapped with `_unwrap_tool_result()` from `chainlitexam/utils/tool_result.py`; do not add new local unwrapping logic
+ Tool results are unwrapped with `_unwrap_tool_result()` from `chainlitexam/utils/tool_result.py`; do not add new local unwrapping logic
+ Progress indication uses `ReasoningStep.stage()`; do not add new `cl.Message` loading bubbles
+ Reasoning steps auto-collapse after the final answer via `auto_collapse=True`
```

- [ ] **Step 2: Run full verification suite**

```bash
python -m py_compile chainlitexam/message_orchestrator.py chainlitexam/chain_gzt.py
python chainlitexam/tests/test_fast_paths.py
python -m pytest chainlitexam/tests/ -v --ignore=chainlitexam/.venv_new
```
Expected: 18/18 fast paths PASS, 46+ pytest PASS.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md with ReasoningStep conventions"
```

---

## Self-Review

1. **Spec coverage:**
   - Remove all `thinking_msg` — covered by Tasks 2, 3, 4.
   - Use `ReasoningStep` for progress — covered by Tasks 3, 4.
   - Auto-collapse — covered by Task 1.
   - Preserve error/timeout paths — implicit in keeping `finally: await reasoning.close()` blocks.

2. **Placeholder scan:** No TBD/TODO/"similar to"/"appropriate error handling" found.

3. **Type consistency:** `ReasoningStep` constructor change is localized; no signature changes elsewhere.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-10-unify-thinking-ux-implementation-plan.md`.

Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints.

Which approach?
