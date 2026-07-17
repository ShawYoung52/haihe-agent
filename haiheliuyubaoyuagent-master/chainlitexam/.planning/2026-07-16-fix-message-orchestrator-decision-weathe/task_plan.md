# Task Plan: Fix message_orchestrator and decision_weather_core errors

## Goal
Fix the runtime NameError caused by a missing `_decision_hourly_window` import and the `test_fast_paths.py` failure caused by `_try_big_rain_forecast_fast_path` not following the reasoning-step convention.

## Current Phase
Complete

## Phases

### Phase 1: Root Cause Investigation
- [x] Read error messages and run test_fast_paths.py
- [x] Inspect recent diff for `message_orchestrator.py` and `decision_weather_core.py`
- [x] Confirm `_decision_hourly_window` is used but not imported
- [x] Confirm `_try_big_rain_forecast_fast_path` lacks `_show_business_reasoning` and `thinking_chain`
- [x] Identify additional runtime errors via code-review (`_unwrap_tool_observation`, `_normalize_slots` signature, missing `预报时段`)
- **Status:** complete

### Phase 2: Planning & Structure
- [x] Create planning files via planning-with-files
- [x] Define minimal fix approach
- **Status:** complete

### Phase 3: Implementation
- [x] Add `_decision_hourly_window` to the import list in `message_orchestrator.py`
- [x] Refactor `_try_big_rain_forecast_fast_path` to accept `thinking_chain` and use `_show_business_reasoning`
- [x] Update `process_message()` call site to pass `thinking_chain`
- [x] Remove stray `await thinking_msg.remove()` in `_try_weekly_forecast_fast_path`
- [x] Fix `_unwrap_tool_observation` → `_unwrap_tool_result`
- [x] Fix `DecisionWeatherQAService._normalize_slots` to forward `hourly_request`
- [x] Restore `预报时段` in `business_facts`
- [x] Update `query_decision_weather_for_poi` tool to use `hourly_request`
- [x] Import `_parse_decision_dt` for `_fmt_forecast_period_label`
- [x] Define `_dt = datetime` in `_parse_areal_rainfall_time`
- **Status:** complete

### Phase 4: Testing & Verification
- [x] Run `python tests/test_fast_paths.py`
- [x] Run `python -m pytest tests/ -v`
- [x] Verify no runtime/import regressions
- **Status:** complete

### Phase 5: Review & Documentation
- [x] Code review with `code-review` skill (9 angles, critical bugs identified and fixed)
- [x] Apply simplifier (removed dead `_fmt_range`, moved interval normalization to else branch)
- [x] Update `CLAUDE.md` if conventions changed
- [x] Save relevant findings to claude-mem
- **Status:** complete

## Final Status
All tests pass: `test_fast_paths.py` 19/19, `pytest` 51/51. Runtime errors and unresolved references resolved. Remaining diagnostics are pre-existing lint/type warnings, not runtime errors.

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| Reuse `_show_business_reasoning` pattern in `_try_big_rain_forecast_fast_path` | Matches every other fast path and satisfies `test_fast_paths.py` |
| Keep fix minimal, no unrelated refactoring | User only asked to fix the regression; avoid scope creep |
| Fix `query_decision_weather_for_poi` tool parity with fast path | Both entry points must support the new `hourly_request` semantics |
| Use `_invoke_tool_for_fast_path` in `_query_weekly_rolling_forecast` | Consistent with other fast paths and records TimingLogger metrics |
| Sort rolling periods by parsed datetime | Safer than lexicographic string sort if upstream formats vary |

## Errors Encountered
| Error | Resolution |
|-------|------------|
| `_decision_hourly_window` used but not imported | Add to `from tools.decision_weather_core import (...)` |
| `_try_big_rain_forecast_fast_path` fails fast-path static checks | Add `thinking_chain` param, `_show_business_reasoning`, and proper `reasoning.close()` |
| `_unwrap_tool_observation` is undefined | Replace with imported `_unwrap_tool_result` |
| `DecisionWeatherQAService._normalize_slots` only accepts `slots` | Update signature to accept and forward `hourly_request` |
| `business_facts` missing `预报时段` | Add `"预报时段": facts.get("periods") or []` |
| `query_decision_weather_for_poi` ignores hourly modes | Compute `_decision_hourly_window` and pass through normalization/forecast facts |
| Weekly fast path data-source label stale | Change from `ECMWF AIFS 预报数据` to `天津市气象台滚动预报` to match actual tool |
| Big-rain fast path misses `昨天`/`昨日` past markers | Add them to `past_words` to avoid future-oriented answer for past queries |
| `_query_weekly_rolling_forecast` bypasses TimingLogger | Replace direct `tool.ainvoke` with `_invoke_tool_for_fast_path` |
| `_aggregate_rolling_daily_rows` sorts by raw string | Sort by parsed datetime via `_parse_decision_dt` |
