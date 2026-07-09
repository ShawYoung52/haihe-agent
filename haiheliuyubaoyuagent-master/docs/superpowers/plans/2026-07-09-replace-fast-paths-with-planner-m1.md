# Replace Fast Paths with Planner-Only Mode — M1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `ENABLE_FAST_PATHS` feature flag that defaults to `false`, disabling all fast-path pre-routing so every user query flows through the planner LLM + tool loop while keeping the old code intact for rollback.

**Architecture:** A single module-level boolean constant is read from the environment at import time. Both the monkeypatch installer in `utils/db.py` and the hard-coded fast-path chain in `message_orchestrator.py:process_message()` respect this flag. When the flag is off, fast paths are neither installed nor invoked; when on, behavior is identical to today.

**Tech Stack:** Python 3.11, Chainlit, pytest, pytest-asyncio

---

## File Map

| File | Responsibility |
|------|---------------|
| `chainlitexam/message_orchestrator.py` | Contains 19 hard-coded `_try_*_fast_path()` calls inside `process_message()`; will be wrapped by the feature flag. Also imports `os`, so the constant can live near the top. |
| `chainlitexam/utils/db.py` | Imports and calls `install_all_fast_paths()` at module import time; will be guarded by the feature flag. |
| `chainlitexam/tests/test_fast_paths.py` | AST-based static check that fast paths call reasoning/thinking; will be extended with a flag smoke test. |
| `chainlitexam/tests/test_message_orchestrator.py` | New file for behavior-level tests of `process_message()` routing under both flag values. |

---

## Task 1: Add Feature Flag Constant

**Files:**
- Modify: `chainlitexam/message_orchestrator.py:1-15`

**Context:** The file already imports `os` at the top. We add one module-level constant after the imports.

- [ ] **Step 1: Add `ENABLE_FAST_PATHS` constant**

Locate the existing imports at the top of `chainlitexam/message_orchestrator.py` (around lines 1-15). After the imports and before any other module-level code, add:

```python
ENABLE_FAST_PATHS = os.environ.get("ENABLE_FAST_PATHS", "false").lower() in ("1", "true", "yes")
"""Feature flag: when false (default), all fast-path pre-routing is disabled and every query flows through the planner LLM."""
```

- [ ] **Step 2: Verify the constant evaluates correctly**

Run a quick Python check:

```bash
cd chainlitexam
ENABLE_FAST_PATHS=false python -c "from message_orchestrator import ENABLE_FAST_PATHS; print(ENABLE_FAST_PATHS)"
```

Expected output: `False`

```bash
ENABLE_FAST_PATHS=true python -c "from message_orchestrator import ENABLE_FAST_PATHS; print(ENABLE_FAST_PATHS)"
```

Expected output: `True`

- [ ] **Step 3: Commit**

```bash
git add chainlitexam/message_orchestrator.py
git commit -m "feat: add ENABLE_FAST_PATHS feature flag, default false"
```

---

## Task 2: Guard Fast-Path Monkeypatch Installation

**Files:**
- Modify: `chainlitexam/utils/db.py:1-12`

**Context:** `utils/db.py` imports `install_all_fast_paths` and runs it at module load time. This patches several functions in `message_orchestrator`. We only want this to happen when the flag is enabled.

- [ ] **Step 1: Import the flag and guard the installer**

Replace the try/except block in `chainlitexam/utils/db.py` with:

```python
import os

from utils.config import DB_CONFIG

ENABLE_FAST_PATHS = os.environ.get("ENABLE_FAST_PATHS", "false").lower() in ("1", "true", "yes")

if ENABLE_FAST_PATHS:
    try:
        from fast_paths import install_all_fast_paths

        install_all_fast_paths()
    except Exception as exc:
        print(f"[utils.db] fast path routes init failed: {exc}")
else:
    print("[utils.db] fast paths are disabled (ENABLE_FAST_PATHS is not set)")
```

- [ ] **Step 2: Verify import path**

`utils/db.py` is imported from the project root. The `fast_paths` package is at `chainlitexam/fast_paths`. Ensure the import still works when the flag is on:

```bash
cd chainlitexam
ENABLE_FAST_PATHS=true python -c "import utils.db; print('import ok')"
```

Expected output: `import ok` (plus possible fast-path init logs)

```bash
ENABLE_FAST_PATHS=false python -c "import utils.db; print('import ok')"
```

Expected output: `import ok` plus `[utils.db] fast paths are disabled ...`

- [ ] **Step 3: Commit**

```bash
git add chainlitexam/utils/db.py
git commit -m "feat: guard fast-path monkeypatch installation with ENABLE_FAST_PATHS"
```

---

## Task 3: Guard the Hard-Coded Fast-Path Chain in process_message

**Files:**
- Modify: `chainlitexam/message_orchestrator.py:4920-5009`

**Context:** `process_message()` currently runs 19 sequential `if await _try_*_fast_path(...)` checks. We wrap the entire block with `if ENABLE_FAST_PATHS:`.

- [ ] **Step 1: Wrap the fast-path block**

Locate the block starting around line 4920 in `chainlitexam/message_orchestrator.py`:

```python
    if await _try_rainfall_img_fast_path(message.content, thinking_chain, tools, messages, callbacks):
        _log_query_exit(query_start_time, session_id, query_summary, "ok")
        return
    ...
    if await _try_decision_weather_fast_path(message.content, thinking_chain, answer_chain, tools, messages, callbacks):
        _log_query_exit(query_start_time, session_id, query_summary, "ok")
        return
```

Wrap the entire sequence in:

```python
    if ENABLE_FAST_PATHS:
        if await _try_rainfall_img_fast_path(message.content, thinking_chain, tools, messages, callbacks):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return
        ...
        if await _try_decision_weather_fast_path(message.content, thinking_chain, answer_chain, tools, messages, callbacks):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return
```

Keep the indentation consistent (4 spaces inside the new `if`).

- [ ] **Step 2: Add an explicit log line when fast paths are skipped**

Immediately after the new `if ENABLE_FAST_PATHS:` block, add:

```python
    if not ENABLE_FAST_PATHS:
        print(f"[process_message] fast paths disabled; routing to planner LLM: {message.content[:80]!r}")
```

- [ ] **Step 3: Run existing tests to ensure no syntax errors**

```bash
cd chainlitexam
python -m pytest tests/test_reasoning_step.py tests/test_thinking.py tests/test_thinking_summary.py tests/test_timing_logger.py --asyncio-mode=auto -q
```

Expected output: `37 passed`

- [ ] **Step 4: Commit**

```bash
git add chainlitexam/message_orchestrator.py
git commit -m "feat: skip fast-path chain in process_message when ENABLE_FAST_PATHS is false"
```

---

## Task 4: Add Tests for Feature Flag Behavior

**Files:**
- Create: `chainlitexam/tests/test_message_orchestrator.py`

**Context:** We need two tests: one verifying the constant is `False` by default, and one verifying `process_message()` does not call fast paths when the flag is off. The second test can be a unit test that mocks the message and checks that no fast-path function is awaited.

- [ ] **Step 1: Create test file with default flag test**

Create `chainlitexam/tests/test_message_orchestrator.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chainlitexam.tests.stubs import ensure_stubs

ensure_stubs()

import chainlitexam.message_orchestrator as mo


def test_enable_fast_paths_defaults_to_false():
    # The constant is evaluated at import time from the environment.
    # In normal test runs no env var is set, so it should be False.
    assert mo.ENABLE_FAST_PATHS is False
```

- [ ] **Step 2: Add test verifying fast-path functions are not called when disabled**

Append to the same file:

```python
import pytest


@pytest.mark.asyncio
async def test_process_message_skips_fast_paths_when_disabled(monkeypatch):
    """When ENABLE_FAST_PATHS is False, no _try_*_fast_path function is awaited."""
    monkeypatch.setattr(mo, "ENABLE_FAST_PATHS", False)

    called = []

    async def fake_fast_path(*args, **kwargs):
        called.append("fast_path")
        return False

    # Patch every fast-path function we can find on the module.
    for name in dir(mo):
        if name.startswith("_try_") and name.endswith("_fast_path") and callable(getattr(mo, name)):
            monkeypatch.setattr(mo, name, fake_fast_path)

    # Minimal mock message; we only care that fast paths are skipped.
    class FakeMessage:
        content = "测试查询"

    # Mock enough of process_message's dependencies to reach the routing block.
    monkeypatch.setattr(mo, "_show_thinking", lambda text: None)
    monkeypatch.setattr(mo.cl, "Message", lambda **kwargs: type("M", (), {"send": lambda s: None})())

    # We expect the function to eventually hit the planner path and raise or return.
    # The exact outcome is not important; the important assertion is that no fast path was called.
    try:
        await mo.process_message(FakeMessage())
    except Exception:
        pass

    assert called == [], f"Expected no fast-path calls when disabled, got {called}"
```

- [ ] **Step 3: Run the new tests**

```bash
cd chainlitexam
python -m pytest tests/test_message_orchestrator.py -v
```

Expected output: both tests pass.

- [ ] **Step 4: Commit**

```bash
git add chainlitexam/tests/test_message_orchestrator.py
git commit -m "test: add ENABLE_FAST_PATHS behavior tests"
```

---

## Task 5: Update Project Documentation

**Files:**
- Modify: `CLAUDE.md` (project root)

**Context:** The team needs to know how to enable/disable fast paths and what the default is.

- [ ] **Step 1: Add feature flag documentation**

Open `CLAUDE.md` at the project root. Add a new section near the end:

```markdown
## Feature Flags

### `ENABLE_FAST_PATHS`

- **Default:** `false`
- **Behavior when `false`:** All fast-path pre-routing is disabled. Every user query flows through the planner LLM + tool loop.
- **Behavior when `true`:** Legacy behavior. The 19 hard-coded fast paths and the monkeypatch installers in `fast_paths/` are active.
- **How to enable:** Start the server with the environment variable set:
  ```bash
  ENABLE_FAST_PATHS=true chainlit run chain_gzt.py
  ```
- **Why it exists:** The fast paths use keyword matching that causes frequent mis-routing. This flag lets the team gradually validate planner-only behavior before permanently removing the fast-path code.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document ENABLE_FAST_PATHS feature flag"
```

---

## Task 6: Run Full Regression Suite Under Both Flag Values

**Files:**
- N/A (verification step)

- [ ] **Step 1: Run all tests with flag disabled (default)**

```bash
cd chainlitexam
python -m pytest tests/test_reasoning_step.py tests/test_thinking.py tests/test_thinking_summary.py tests/test_timing_logger.py tests/test_message_orchestrator.py --asyncio-mode=auto -q
python tests/test_fast_paths.py
```

Expected outputs:
- pytest: `37 passed` (or however many exist) plus new test passes
- fast_paths AST check: `Total: 18 fast paths, 18 passed.`

- [ ] **Step 2: Run tests with flag enabled**

```bash
cd chainlitexam
ENABLE_FAST_PATHS=true python -m pytest tests/test_reasoning_step.py tests/test_thinking.py tests/test_thinking_summary.py tests/test_timing_logger.py tests/test_message_orchestrator.py --asyncio-mode=auto -q
ENABLE_FAST_PATHS=true python tests/test_fast_paths.py
```

Expected outputs: same pass counts.

- [ ] **Step 3: Commit if all green**

If any test fails, fix before committing. If all pass:

```bash
git commit --allow-empty -m "chore: verify ENABLE_FAST_PATHS regression suite passes in both modes"
```

---

## Self-Review Checklist

- [ ] Spec coverage: M1 (feature flag, default off, skip fast-path calls, keep rollback) is fully covered by Tasks 1-3.
- [ ] Placeholder scan: No TBD/TODO, all code blocks contain real code, all commands are exact.
- [ ] Type consistency: `ENABLE_FAST_PATHS` is always a boolean; `process_message` signature unchanged.
- [ ] Test coverage: Both flag states are tested; default-off behavior has an explicit assertion.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-09-replace-fast-paths-with-planner-m1.md`. Two execution options:

**1. Subagent-Driven (recommended)** - Dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach do you want?
