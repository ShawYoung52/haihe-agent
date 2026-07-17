# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

海河流域暴雨洪水预报智能体 — a Haihe River Basin meteorological Q&A agent combining Chainlit (chat UI), LangChain (LLM orchestration), FastAPI (REST), and an MCP server (weather/river tools).

## Start / Run

```bash
# Backend MCP server (weather tools)
cd haihe-weather-analyzer-mcp
python server.py

# Frontend agent (Chainlit)
cd chainlitexam
chainlit run chain_gzt.py
```

## Architecture

```
User (Browser) → Chainlit UI → chain_gzt.py (lifecycle + FastAPI + auth)
                                  ↓
                    process_message() [message_orchestrator.py]
                                  ↓
          ┌───────────────────────┼───────────────────────┐
     Fast Paths (14+)      Planner LLM (Qwen3.6-27B)   Answer LLM
          ↓                       ↓                       ↑
    Direct tool call    _run_tool_round() → tools       Final text
                                  ↓
     ┌────────────────────────────┼────────────────────────────┐
  MCP SSE tools     Local tools (rain_analysis)   Partner skills
  (weather server)  (MUSIC/Tianqing station data)  (Hydro/Emergency)
```

**Key files:**
- `chainlitexam/chain_gzt.py` — Chainlit lifecycle, tool loading, auth, GIS linkage, FastAPI endpoints (~3500 lines)
- `chainlitexam/message_orchestrator.py` — Message routing, fast paths (warning/rainfall/river/weather), planner loop, tool execution, answer generation (~4700 lines)
- `chainlitexam/prompts.py` — `WEATHER_ASSISTANT_PROMPT` system prompt, warning route/summary prompts
- `chainlitexam/tools/rainfall_river_impact.py` — Local wrapper for the rainfall-river impact tool
- `haihe-weather-analyzer-mcp/constants.py` — Shared constants including `DIRECTED_GRAPH_FILENAME` (`river_directed_v6.pkl`) and `RIVER_TABLE_FULL` (`haihe_river_directed_full_v6`); use these instead of hard-coding versioned names
- `haihe-weather-analyzer-mcp/server.py` — MCP server entry point (SSE transport, default port 3333)
- `haihe-weather-analyzer-mcp/tools.py` / `haihe_mcp_tools.py` — Tool implementations (rainfall, river network, warnings, emergency response, RAG)
- `haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py` — MCP-side rainfall-river impact result formatter
- `../hhlyqyxt-master/utils/rainfall_impact_geojson.py` — Traction-agent core algorithm for affected rivers (cross-repo dependency; keep `direct_match_km` defaults and graph/table version constants in sync; imports pandas directly, so ensure pandas is installed). **Algorithm invariant:** downstream tracing starts from all pkl edges within the 30 km station buffer; `direct_match_km` only marks which of those edges are "real direct river segments" (`is_direct_graph_edge`). Do not revert to direct-match-only start selection. **Data-alignment caveat:** pkl graph topology and `full_v6` geometries are not always 1:1 at the `objectid` level; the same `objectid` may contain disconnected `MULTILINESTRING` parts. The builder handles this by dumping parts, merging direct-buffer parts per `objectid`, and choosing downstream parts that cover both pkl edge endpoints before falling back to nearest/ longest. **Name fallback:** if `full_{RIVER_TABLE_VERSION}.src_name` is empty and produces `river_name="未知"`, the builder falls back to the pkl graph name for the same `objectid` (lazily, only when any feature needs enrichment). **Luan River naming:** the Luan River system uses single-character abbreviations (e.g., `青` → `青河`, `东` → `东河`). `_apply_luan_names()` expands single-character names and then replaces them with full names from `_DEFAULT_LUAN_NAME_MAPPING` (objectid → full name), gated on `is_luan=true` so Haihe single-character names are not relabeled. An external JSON `{graph_stem}_luan_names.json` in the same directory as the pkl graph overrides/extends the built-in mapping without redeploying code. **Luan/Haihe disambiguation:** `_query_direct_rows` returns `is_luan` from the database; `_create_downstream_temp` stores the pkl edge's `is_luan`; `_save_downstream_edge` propagates it from pkl edges; `_query_downstream_rows` JOINs on `objectid` and uses `match_priority` to prefer DB rows whose `is_luan` matches the pkl edge, while the downstream feature's `is_luan` is taken from the pkl edge (`e.is_luan`) so `_apply_luan_names()` can correct the name even if the geometry matched a Haihe DB row; `_river_feature` writes `properties.is_luan`. **Spurious-match filter:** downstream rows with `match_distance_km > station_buffer_km` are dropped. **Downstream/buffer overlap:** downstream segments are no longer filtered by a global `NOT EXISTS` station-buffer check; instead, segments geometrically covered by same-`objectid` direct-buffer features are dropped (requires Shapely; without Shapely, overlaps may appear as duplicates). **Optional dedup:** if Shapely is installed, downstream geometries covered by a direct-buffer geometry of the same `objectid` are dropped; environments without Shapely degrade gracefully. Graph/geometry failures are logged rather than swallowed.

**Fast path order** in `process_message()`: rainfall img → river plot → rainfall analysis → city avg rainfall → rain duration → today rainfall → weekly forecast → heavy rain check → subbasin forecast → basin areal rainfall → weekend activity → basin weather → general weather → water level → emergency response → poi → risk warning → rainstorm impact time → (falls through to planner LLM).

## Development Conventions

- **Python 3.10+** with `async`/`await` throughout
- Chainlit uses **custom build** from `frontend/` (Vite + React + TypeScript + Tailwind)
- Config: `chainlitexam/.chainlit/config.toml` (CoT display, auth, session timeout, allowed origins)
- `message_orchestrator.py` recently consolidated lazy imports (`time`, `base64`, `traceback`, `httpx`) to module level — do not re-add inline imports
- Tool display names are in module-level `TOOL_DISPLAY_NAMES` dict in `message_orchestrator.py`
- `_invoke_tool_with_tolerance()` returns `(result, elapsed)` tuple — always unpack both values
- Tool results are unwrapped with `_unwrap_tool_result()` from `chainlitexam/utils/tool_result.py`; do not add new local unwrapping logic
- Progress indication uses `ReasoningStep.stage()`; do not add new `cl.Message` loading bubbles
- Reasoning steps auto-collapse after the final answer via `auto_collapse=True` when running Chainlit >= 2.10.0; older versions fall back to `default_open=False` on close
- Tool failures in `_run_tool_round()` are recorded as `ToolMessage` and surfaced to the planner LLM; do not send standalone `cl.Message` error bubbles for individual tool failures
- When emergency-response tools (e.g. `safe_evaluate_haihe_emergency_response`) are invoked in the same round, skip both the warning-only hybrid answer path and any `forced_final_text` short-circuit; let the planner synthesize a response that prioritizes the emergency-response result
- Verification: run `python tests/test_fast_paths.py` for fast-path static checks and `python -m pytest tests/ -v` for the full suite
- Tests must run from `chainlitexam/`; running from the repo root causes `ModuleNotFoundError: No module named 'utils'`
- Bash working directory persists across tool calls; use absolute paths when invoking commands outside `chainlitexam/` (the parent directory name contains spaces)
- `haihe-weather-analyzer-mcp/server.py` overrides `fixed_rainfall_impact_tool.DEFAULT_DIRECT_GRAPH_MATCH_KM` at runtime and hard-codes the `get_affected_river_network_by_rainfall` description; keep both in sync with `hhlyqyxt-master/utils/rainfall_impact_geojson.py`
- `fixed_rainfall_impact_tool._resolve_graph_path()` prefers `DIRECTED_GRAPH_FILENAME` in the same directory as the configured graph path and is robust to directory paths and empty filenames; keep this logic in sync with any graph-loader fallback in `tools.py`
- `fixed_rainfall_impact_tool.IMPACT_RULES` uses the `RIVER_TABLE_VERSION` constant; do not hard-code `full_v6` strings
- Versioned river resources are centralized: use `constants.RIVER_TABLE_FULL` / `constants.DIRECTED_GRAPH_FILENAME` in the MCP package, and the module-level constants in `rainfall_impact_geojson.py` in the traction-agent package. When upgrading graph/table versions, update both constants files and `config.ini` together.
- `safe_evaluate_haihe_emergency_response` defaults empty `times` to the current Beijing hour via `TIANJIN_TIMEZONE`; reuse `haihe_mcp_tools._normalize_time_param` for consistent time handling
- `fixed_rainfall_impact_tool._empty_response()` must return the same keys as `_format_mcp_response()`, including `river_geojson` and `rules`, and use the caller-supplied `direct_graph_match_km`
- The `include_background` parameter in `get_affected_river_network_by_rainfall` / `local_get_affected_river_network_by_rainfall` is accepted but not implemented by the upstream builder; do not forward it until `rainfall_impact_geojson.py` adds support
- Error text in tool failures and MCP wrappers is scrubbed (IPs/paths removed) before logging or returning to the LLM/user
- LLM model: Qwen3.6-27B via local OpenAI-compatible proxy at `10.226.188.156:8000/v1/`
- Internal service addresses: MUSIC `10.226.90.120`, PostgreSQL `10.226.107.130`, RAG `10.226.188.156:8033` — never include these in user-facing output or checked-in documentation (replace with env-var placeholders like `${ROLLING_FORECAST_API_URL}`)
- Data sources: MUSIC/Tianqing stations (实况), ECMWF AIFS (预报), CMA warnings, PostgreSQL/PostGIS (河网/行政区划), RAG knowledge base
- **Fast-path contract:** every `_try_*_fast_path` in `message_orchestrator.py` must call `_show_business_reasoning(...)`, close the reasoning step on every return path, and reference `thinking_chain` or `generate_fast_path_thinking(...)`; `tests/test_fast_paths.py` enforces this statically
- **Decision-weather dual entry points:** `DecisionWeatherQAService` (fast path) and `query_decision_weather_for_poi` (LangChain tool) both consume `tools/decision_weather_core.py`; when `_normalize_decision_weather_slots`, `_compact_decision_forecast_facts`, or `_decision_hourly_window` change, update both callers
- **Decision-weather wrapper parity:** `DecisionWeatherQAService._normalize_slots` is a thin wrapper around `_normalize_decision_weather_slots`; keep signatures in sync so new optional arguments (e.g. `hourly_request`) are forwarded
- **Tool-result unwrapping:** always use `_unwrap_tool_result()` from `utils/tool_result`; do not introduce alternate names like `_unwrap_tool_observation`
- **Decision-weather prompt facts:** `_generate_decision_weather_answer` needs both `"预报时段"` (periods) and `"小时级降雨计算"` (hourly_rain) in `business_facts`; omitting either breaks general or hourly answer formats

## Superpowers Integration

This project uses the superpowers plugin for disciplined development:
- **Before implementing any feature**: invoke `superpowers:brainstorming` to design, then `superpowers:writing-plans` to plan
- **Before marking work done**: invoke `superpowers:verification-before-completion`
- **Before merging**: invoke `code-review` to find risks, `superpowers:finishing-a-development-branch` for proper cleanup
  - If no PR exists or `gh` CLI is unavailable, launch parallel agents against `git diff HEAD` to check CLAUDE.md compliance, obvious bugs, git history, and previous PR comments instead
- **For bug fixes**: invoke `superpowers:systematic-debugging`
- **For refactor/cleanup**: invoke `code-review`, then `code-simplifier` agent, then `superpowers:verification-before-completion`, and end with `claude-md-management:revise-claude-md`
- **Specs directory**: `docs/superpowers/specs/`

## Feature Flags

### `ENABLE_FAST_PATHS`

- **Default:** `false`
- **Behavior when `false`:** All fast-path pre-routing is disabled. Every user query flows through the planner LLM + tool loop.
- **Behavior when `true`:** Legacy behavior. The 18 hard-coded fast paths and the monkeypatch installers in `fast_paths/` are active.
- **How to enable:** Start the server with the environment variable set:
  ```bash
  ENABLE_FAST_PATHS=true chainlit run chain_gzt.py
  ```
- **Why it exists:** The fast paths use keyword matching that causes frequent mis-routing. This flag lets the team gradually validate planner-only behavior before permanently removing the fast-path code.