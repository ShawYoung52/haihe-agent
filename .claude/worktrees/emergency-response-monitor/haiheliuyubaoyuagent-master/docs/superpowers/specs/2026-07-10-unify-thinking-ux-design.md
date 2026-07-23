# Unify Thinking UX: Remove `thinking_msg` and Auto-Collapse ReasoningStep

## Problem

The UI currently shows two overlapping "thinking" mechanisms:

1. Legacy `thinking_msg = cl.Message(content="🧭 正在分析问题，请稍候...")` — a plain loading bubble used in `process_message` and every fast path.
2. DeepSeek-style `ReasoningStep` — a collapsible step that streams real-time reasoning and business-stage progress.

Users see redundant loading messages, and the `ReasoningStep` remains expanded after the final answer is sent instead of folding automatically.

## Goals

- Remove all legacy `thinking_msg` loading bubbles.
- Use `ReasoningStep` as the single source of progress indication and real-time thinking.
- Ensure the reasoning step auto-collapses once the final answer is delivered.
- Keep existing fast-path and planner behavior unchanged except for UI presentation.

## Non-Goals

- Change the LLM model or prompts.
- Add new fast paths or alter routing logic.
- Refactor the planner/answer chain execution flow.

## Design

### 1. Remove legacy `thinking_msg`

- Delete the helper `async def _show_thinking(text: str) -> cl.Message`.
- Remove every `thinking_msg` variable and all `await thinking_msg.send()`, `await thinking_msg.update()`, and `await thinking_msg.remove()` calls.
- Remove unused `thinking_msg` parameters from helper functions.

### 2. Move progress text into `ReasoningStep`

Every location that previously updated `thinking_msg` now calls `await reasoning.stage("📡 查询数据", "<progress text>")`.

Examples:

- Fast paths that previously showed "🔍 正在生成降水实况图..." now stage:
  `await reasoning.stage("📡 查询数据", "正在生成降水实况图...")`.
- `process_message` no longer creates a `🧭 正在分析问题` bubble. It stages:
  `await reasoning.stage("🔍 理解问题", "正在分析问题，请稍候...")`.

### 3. Auto-collapse the reasoning step

Update `ReasoningStep.__aenter__` so the underlying `cl.Step` is created with:

```python
self.step = cl.Step(
    name=self.name,
    type="llm",
    parent_id=parent_id,
    auto_collapse=True,
)
```

The existing `close()` method remains responsible for finalizing output; Chainlit's `auto_collapse=True` handles folding the step once the parent run completes.

### 4. Ensure consistent close before final answer

All final-answer code paths already call `_maybe_close_reasoning(reasoning)` before sending the answer. This remains unchanged, but it becomes the only mechanism for finalizing the thinking UI.

### 5. Preserve error/timeout paths

Fast paths that previously removed `thinking_msg` on error now simply return `False` or send an error `cl.Message`. The `ReasoningStep` stays visible so the user can see where the failure occurred.

## Affected Files

- `chainlitexam/message_orchestrator.py` — main target; remove `thinking_msg` everywhere and replace with `reasoning.stage()`.
- `chainlitexam/chain_gzt.py` — if any callbacks or helpers reference `thinking_msg`.

## Testing

- `python tests/test_fast_paths.py` — verifies every fast path still creates and closes a `ReasoningStep`.
- `python tests/test_reasoning_step.py` — verifies `ReasoningStep` behavior.
- `python -m pytest tests/ -v` — full regression suite.
- Manual UI check: ask "本周末天气如何？" and confirm:
  1. No "🧭 正在分析问题" bubble appears.
  2. Reasoning step streams progress and thinking.
  3. After the final answer appears, the reasoning step is collapsed.

## Rollback

If `auto_collapse=True` causes unexpected UI behavior, revert the `ReasoningStep` constructor change while keeping the `thinking_msg` removals.
