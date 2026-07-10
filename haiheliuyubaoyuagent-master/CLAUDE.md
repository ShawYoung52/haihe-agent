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
- `haihe-weather-analyzer-mcp/server.py` — MCP server entry point (SSE transport, default port 3333)
- `haihe-weather-analyzer-mcp/tools.py` / `haihe_mcp_tools.py` — Tool implementations (rainfall, river network, warnings, emergency response, RAG)

**Fast path order** in `process_message()`: rainfall img → river plot → rainfall analysis → city avg rainfall → rain duration → today rainfall → weekly forecast → heavy rain check → subbasin forecast → basin areal rainfall → weekend activity → basin weather → general weather → water level → emergency response → poi → risk warning → rainstorm impact time → (falls through to planner LLM).

## Development Conventions

- **Python 3.10+** with `async`/`await` throughout
- Chainlit uses **custom build** from `frontend/` (Vite + React + TypeScript + Tailwind)
- Config: `chainlitexam/.chainlit/config.toml` (CoT display, auth, session timeout, allowed origins)
- `message_orchestrator.py` recently consolidated lazy imports (`time`, `base64`, `traceback`, `httpx`) to module level — do not re-add inline imports
- Tool display names are in module-level `TOOL_DISPLAY_NAMES` dict in `message_orchestrator.py`
- `_invoke_tool_with_tolerance()` returns `(result, elapsed)` tuple — always unpack both values
- Tool results are unwrapped with `_unwrap_tool_result()` from `chainlitexam/utils/tool_result.py`; do not add new local unwrapping logic
- Verification: run `python tests/test_fast_paths.py` for fast-path static checks and `python -m pytest tests/ -v` for the full suite
- LLM model: Qwen3.6-27B via local OpenAI-compatible proxy at `10.226.188.156:8000/v1/`
- Internal service addresses: MUSIC `10.226.90.120`, PostgreSQL `10.226.107.130`, RAG `10.226.188.156:8033` — never include these in user-facing output
- Data sources: MUSIC/Tianqing stations (实况), ECMWF AIFS (预报), CMA warnings, PostgreSQL/PostGIS (河网/行政区划), RAG knowledge base

## Superpowers Integration

This project uses the superpowers plugin for disciplined development:
- **Before implementing any feature**: invoke `superpowers:brainstorming` to design, then `superpowers:writing-plans` to plan
- **Before marking work done**: invoke `superpowers:verification-before-completion`
- **Before merging**: invoke `code-review` to find risks, `superpowers:finishing-a-development-branch` for proper cleanup
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