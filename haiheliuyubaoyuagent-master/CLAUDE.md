# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

海河流域暴雨洪水预报智能体 — a Haihe River Basin meteorological Q&A agent combining Chainlit (chat UI), LangChain (LLM orchestration), FastAPI (REST), and an MCP server (weather/river tools).

## Start / Run

```bash
# Backend MCP server (weather tools)
cd haihe-weather-analyzer-mcp
python server.py

# Frontend agent (Chainlit) — 必须用 `chainlit run`，不要用 `uvicorn chain_gzt:app`
cd chainlitexam
chainlit run chain_gzt.py
```

> **启动方式警告**：`chain_gzt.py` 的用户管理 REST 接口（`/api/v1/admin/users` 等）
> 通过 `chainlit.server.app.router.routes.insert(0, Mount(...))` 注册，只有走
> `chainlit run` 才会触发这段挂载逻辑。用 `uvicorn chain_gzt:app` 启动时虽然本地
> `app` 也 mount 了 `/api/v1`，但 Chainlit 的 chat、socket、登录页都不在那个 app 上，
> 等于半残。统一用 `chainlit run`。

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
- `haihe-weather-analyzer-mcp/custom_tools/risk_warning_tool.py` — 风险预警 MCP 工具（`query_risk_warning`）。三类配置与同事前端 `hhfw/riskWarnNew/findDataListByConfig` 对齐：`river`（中小河流洪水, model=EC, type=1）、`mountain`（山洪, model=EC, type=2）、`geologic`（地质灾害, model=SCMOC, type=3）。默认服务地址 `http://10.226.107.35:8070`，env `RISK_WARN_BASE`/`RISK_WARN_BASES`/`HHFW_API_BASE` 等可覆盖。`RISK_ALIASES` 必须与 `chainlitexam/fast_paths/risk_warning_fast_paths.py` 的 `_detect_risk_kind` 关键词保持一致（含 `崩塌`/`泥石流`），否则 planner LLM 按用户原文传 `risk_kind` 会被工具拒绝。`ENABLE_FAST_PATHS=false`（默认）时，planner LLM 是主路径，`prompts.py` 第 12 条规则引导路由。
- `haihe-weather-analyzer-mcp/rolling_forecast_grid.py` — 滚动预报网格数据源切换（**按数据可用性切换**：数据湖有 .nc 就用滚动预报，无则降级 EC AIFS）。数据湖路径 `/CMADAAS/DATA/SEVP/BETJ/USR_QXT_YTH/M.3200.0006.M001/TP1H/000/{YYYYMM}/{YYYYMMDD}/{YYYYMMDDHH}/GRID_TJQX_LYPUB_TP1H_AEHH_000_DT_{YYYYMMDDHHMMSS}_000-240_{NNNN}.nc`，env `ROLLING_FORECAST_GRID_ROOT` 可覆盖。NetCDF 结构：dims `time(264)×lat(181)×lon(181)`，变量 `TP1H (time,lat,lon) float32`，`time: -23..240`，`lat: 34-43°N`、`lon: 111-120°E`（海河流域，~5km）。核心函数：`is_flood_season(now)` 仅作参考字段；`select_latest_forecast_cycle(now)` 选最近 08/20 起报；`find_rolling_forecast_grid_file(root, cycle, max_fallback=4)` 按模式发现 .nc、取 NNNN 最大、时次回溯；`read_rolling_forecast_precip(path, start_hour, end_hour)` 切片返回 DataArray；`sample_rolling_forecast_at_stations(nc_path, records, hour, method)` 按站点采样返回 `{station_id: precip_mm}`（含网格边界检查）；`materialize_rolling_forecast_to_files(nc_path, hours, output_dir)` 把各时效 2D 切片写成独立 .nc 文件（兼容现有 GDAL 采样器）；`resolve_forecast_grid_source(now, ec_output_path, rolling_root)` 切换入口。**已接入下游**：`emergency_api.py` 的预报判定 + `emergency_response_interface.py` 的 `evaluate_haihe_forecast_emergency_response_core` 都用 `resolve_forecast_grid_source` 切换；前者直接调 `sample_rolling_forecast_at_stations`，后者用 `materialize_rolling_forecast_to_files` 生成 2D .nc 喂给现有 `_forecast_filter_core`（不改共享核心）。`draw_haihe_precip_product.py` 加 `_open_rolling_forecast_nc(path, hour)` 构造 RasterData（xarray 切片 + 垂直翻转 + 像元中心→边界 geotransform），`_open_raster_or_nc` 按扩展名分发；CLI 新增 `--rolling-forecast-nc`。`forecast_product_queue.py` 的 `ForecastProductJob` 加 `source`/`rolling_nc_path` 字段，`enqueue` 时自动 `resolve_forecast_grid_source`，worker 滚动预报分支各时效复用同一 .nc 调 `run_draw_haihe_precip_product`。`analyzers/RainfallAnalyzer.py` 入口 `resolve_forecast_grid_source` + `materialize_rolling_forecast_to_files` 生成 2D .nc 喂给现有 GDAL zonal stats，`data_resource` 标签动态标注数据源。
- `../hhlyqyxt-master/utils/rainfall_impact_geojson.py` — Traction-agent core algorithm for affected rivers (cross-repo dependency; keep `direct_match_km` defaults and graph/table version constants in sync; imports pandas directly, so ensure pandas is installed). **Design principle (v6 redesign):** the pkl directed graph is the topology authority; `full_v6` is a per-edge geometry/attribute lookup table with exactly one row per pkl edge, matched by `(objectid, from_x, from_y, to_x, to_y)` rounded to 6 decimals (≈0.1 m) to absorb float drift. Never reintroduce `ST_Dump` / `GROUP BY objectid` aggregation. **Algorithm invariant:** `_classify_graph_edges` classifies ALL candidate edges within `station_buffer_km` (default 30 km) as `direct_buffer` features — `is_direct_graph_edge=True` for edges within `direct_match_km` (default 10 km), `False` for the 10-30 km buffer-only ring. This avoids the "downstream without upstream" gap where buffer-only edges were previously dropped or mislabeled as downstream at distance 0. Classification uses the SQL-computed real-geometry min distance (`MIN(ST_Distance(geom, station))` from the `ST_DWithin` join), not the pkl endpoint chord; chord distance is only a fallback when the SQL value is missing. `station_buffer_km` defines which edges seed downstream tracing AND the direct_buffer feature set; `direct_match_km` marks the ≤10 km "real direct river segments" subset. **`get_edge_length_km` nan guard:** the production pkl (`E:\tj\line\result\river_directed_v6.pkl`) has `len_km=NaN` on all 34 Luan River edges; `get_edge_length_km(attr, *, from_xy=None, to_xy=None)` falls back to `haversine(from, to)` when the attr value is non-finite or missing, returning 0.0 only when no coordinates are available. `_save_downstream_edge` guards with `not (length_km > 0)` (catches both nan and ≤0). Never re-introduce `max(float(nan), 0.0)` — it returns nan and corrupts Dijkstra distance accumulation. **Downstream geometry:** `_fetch_missing_edge_rows` issues a second `WHERE objectid = ANY(...)` query for downstream edges outside the 30 km buffer. **Direction-agnostic lookup:** `_build_edge_lookup` indexes each row under BOTH `(objectid, from, to)` and `(objectid, to, from)` keys (via `setdefault`), so pkl flow direction vs full_v6 digitization direction mismatch doesn't cause a miss. **Spatial fallback:** when the exact endpoint key fails (full_v6 `from_x`/`from_y`/`to_x`/`to_y` don't match pkl attr values — observed for 112 candidate rows + 4 downstream edges in production, likely because the DB stores geometry endpoints rather than shapefile attribute values), `_match_edge_spatially` finds the candidate row with the same `objectid` whose geometry passes within 100 m of BOTH pkl endpoints (via `_point_to_lines_km`). Used in `_classify_graph_edges`, `_resolve_edge_features`, and `_fetch_missing_edge_rows` (exact key → reversed key → spatial). Unmatched edges fall back to a straight line labeled `geometry_source=pkl_edge_straight_fallback`. **Dedup:** `_save_downstream_edge` skips any edge in `direct_keys` (= all candidate edges), so an edge is emitted at most once across `direct_buffer` + `downstream_50km`. **Clipping & unwrap:** `_clip_geometry_to_keep_km` is pure Python (no Shapely) — picks the longest part of a `MULTILINESTRING`, orients the line by comparing endpoints to the pkl `from` node, then walks haversine segments up to `keep_km`. `_unwrap_geometry` converts single-part `MULTILINESTRING` to `LineString` for front-end rendering. Downstream `length_km` reports `keep_km`, not the DB row's full `len_km`. **Name priority:** `_pick_river_name` tries `src_name → river_name → pkl name`; the Luan static mapping (`_DEFAULT_LUAN_NAME_MAPPING`, overridable via `{graph_stem}_luan_names.json`) is applied ONLY when the picked name is a single CJK character or all sources failed — it never overrides a valid full name. `_normalize_river_name` appends `河` to single CJK chars. `is_luan` gating prevents Haihe single-char names from being relabeled. **Schema guard:** `_ensure_river_columns` validates `{geom, objectid, river_name_col, is_luan, id, src_name, len_km, from_x/from_y/to_x/to_y}` before querying — extend this set whenever `_query_candidate_edge_rows` / `_fetch_missing_edge_rows` reference new hard-coded columns. **Connection lifecycle:** `get_graph()` is called BEFORE `_open_connection()` so a missing/corrupt pkl cannot leak a DB connection. **Empty CSV:** `aggregate_5min_station_pre_to_24h` catches `EmptyDataError` and returns an empty DataFrame with the expected columns, so `build_rain24h_impact_river_geojson` produces a "未找到站点" result instead of crashing. **Removed:** `direct_station_top_n` parameter (was a silent no-op), `_query_downstream_rows` / `_create_downstream_temp` / `_line_wkt` / `_fill_unmatched_downstream_edges` / `_build_fallback_downstream_row` / `_find_direct_graph_starts` (+ helpers) / `_river_feature` / `_feature_geometry_key` / `_drop_downstream_covered_by_direct` (+ Shapely coverage trio) / `_apply_luan_names` (both defs) / `_luan_full_name` / `_clip_linestring_to_fraction` (Shapely-based). Graph/geometry failures are logged rather than swallowed.

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
- **Basin/river-system future weather:** When users ask about whole-basin or sub-basin future weather (e.g., "海河流域明天天气", "大清河流域未来三天降雨"), the planner must call `get_river_system_rainfall_forecast` first. It returns rainfall statistics per Haihe 9-zone river system from the rolling forecast or EC AIFS grid. Use the river-system table as the primary answer and only call `get_city_rainfall_time_range` for representative-city details when needed. The final answer must use the `data_source` field returned by the tool and must not expose backend details such as table names, file paths, or tool parameters.

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