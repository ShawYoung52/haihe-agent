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
- `chainlitexam/chain_gzt.py` — Chainlit lifecycle, tool loading, auth, GIS linkage, FastAPI endpoints (~3700 lines). 包含 `_build_orchestrator_callbacks()` / `_build_orchestrator_runtime()` 供 HTTP 问答接口复用
- `chainlitexam/qa_http_api.py` — **HTTP 问答接口适配层**（新增）。用 `init_http_context` 伪造 Chainlit 会话 + 自定义 `CapturingEmitter` 拦截输出，在不改 `message_orchestrator.py` 的前提下把问答能力暴露成 REST 接口。**依赖方向必须单向**：`chain_gzt` → `qa_http_api`，禁止反向 import（会继承 chain_gzt 的模块级副作用）。chain / callbacks 由 `chain_gzt` 在启动时通过 `runtime.configure()` 注入。并发隔离依赖「进程只有一个 event loop」前提（chainlit CLI 满足）
- `chainlitexam/message_orchestrator.py` — Message routing, planner loop, tool execution, answer generation (~5100 lines)。新增 `_route_simple_weather_query` 简单天气规则路由（跳过 planner LLM，省 5-10s），`_set_tool_calls` / `_enforce_*_route` 强制路由辅助函数
- `haihe-weather-analyzer-mcp/rolling_forecast_service.py` — 滚动预报核心。`query_rolling_forecast_core` 调用 `_cached_rolling_forecast_request`（TTL 缓存，默认 10 分钟，`ROLLING_FORECAST_CACHE_TTL` 可调），解决甲方反馈的"数据查询 26s"
- `haihe-weather-analyzer-mcp/haihe_mcp_tools.py` — MCP 工具实现。`_search_poi_core` 加 TTL 缓存（默认 1 小时，`POI_SEARCH_CACHE_TTL` 可调），POI 数据静态，相同关键词频繁查询
- `chainlitexam/external_skill_tools.py` — 合作方路由工具 + **天河 Fixed QA 工具**。`query_tianhe_fixed_qa` 供 planner 调天河 `/api/qa`（Fixed QA 整句匹配），`call_tianhe_qa_api` 真实 HTTP 调用（`TIANHE_QA_API_URL` 环境变量，默认 `http://10.226.188.156:8001/api/qa`），失败返回中文提示不抛异常。**answer 是完整成品（含 HTTP 200 降级文案，文档 9.4），`_run_tool_round` 命中后设 `forced_final_text` 直接收口、跳过 answer LLM 完全原样透传**（值绑定 `_is_tianhe_passthrough`：仅当 `forced_final_text` 仍等于本轮天河原文时跳过前缀/组装，被后续工具覆盖或应急清空即失效，不依赖会话级标志、不跨消息泄漏），避免 LLM 超时把现成答案拖死。**工具级失败与命中/200 降级区分**：`TIANHE_ERROR_TEXTS`（4 条 `_TIANHE_ERR_*` 文案的单一事实源）内的失败提示**不**设 `forced_final_text`，作为普通 ToolMessage 让 planner 回退本地业务工具；200 降级文案（如"智能体服务暂时不可用"）不在集合内、仍原样透传（文档 9.4 不回退）。双轨 prompt（`PLANNER_SYSTEM_PROMPT`/`WEATHER_ASSISTANT_PROMPT`）均有 `0.5.` 段引导：天河固定问答目录风格的标准问法先试 `query_tianhe_fixed_qa`，失败回退本地；0.5 段枚举**文档 §5.2 全部 4 个目录问法**（今天雨下了多长时间/全市现在下了多少雨/市区现在气温和风的实况/暴雨天气的防范建议），并明确"全市/市区/今天"等目录固定词**不算变量**（防 planner 把含"市区"的目录问法误判为"含具体地点"而漏接天河）；`tests/test_tianhe_qa.py::test_fixed_qa_catalog_fully_covered` 锁定 4 目录在双轨 prompt+工具 docstring 全覆盖。对接文档 `qa-api-integration-guide.md`
- `chainlitexam/prompts.py` — `WEATHER_ASSISTANT_PROMPT` system prompt, warning route/summary prompts
- `chainlitexam/tools/rainfall_river_impact.py` — Local wrapper for the rainfall-river impact tool
- `haihe-weather-analyzer-mcp/constants.py` — Shared constants including `DIRECTED_GRAPH_FILENAME` (`river_directed_v6.pkl`) and `RIVER_TABLE_FULL` (`haihe_river_directed_full_v6`); use these instead of hard-coding versioned names
- `haihe-weather-analyzer-mcp/server.py` — MCP server entry point (SSE transport, default port 3333)
- `haihe-weather-analyzer-mcp/tools.py` / `haihe_mcp_tools.py` — Tool implementations (rainfall, river network, warnings, emergency response, RAG)
- `haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py` — MCP-side rainfall-river impact result formatter. 返回结构含 `river_propagation` 河流级传播时间汇总（牵引核心 `_build_river_propagation` 计算：下游河取 Dijkstra `end_distance_km` 最大值、仅直接边河取最长直接河段；河名必须走 `_pick_river_name` 与 GeoJSON 同口径）。`flow_velocity_mps` 入参 0/None=默认 2.0 m/s、负值/NaN 报错（`_resolve_flow_velocity`）；空块统一用 `_empty_propagation()`；`server.py` 需同步覆盖 `DEFAULT_FLOW_VELOCITY_MPS`（与 `DEFAULT_DIRECT_GRAPH_MATCH_KM` 同款模式）
- `haihe-weather-analyzer-mcp/custom_tools/risk_warning_tool.py` — 风险预警 MCP 工具（`query_risk_warning`）。三类配置与同事前端 `hhfw/riskWarnNew/findDataListByConfig` 对齐：`river`（中小河流洪水, model=EC, type=1）、`mountain`（山洪, model=EC, type=2）、`geologic`（地质灾害, model=SCMOC, type=3）。默认服务地址 `http://10.226.107.35:8070`，env `RISK_WARN_BASE`/`RISK_WARN_BASES`/`HHFW_API_BASE` 等可覆盖。`RISK_ALIASES` 必须与 `chainlitexam/fast_paths/risk_warning_fast_paths.py` 的 `_detect_risk_kind` 关键词保持一致（含 `崩塌`/`泥石流`），否则 planner LLM 按用户原文传 `risk_kind` 会被工具拒绝。`ENABLE_FAST_PATHS=false`（默认）时，planner LLM 是主路径，`prompts.py` 第 12 条规则引导路由。
- `haihe-weather-analyzer-mcp/rolling_forecast_grid.py` — 滚动预报网格数据源切换（**按数据可用性切换**：数据湖有 .nc 就用滚动预报，无则降级 EC AIFS）。数据湖路径 `/CMADAAS/DATA/SEVP/BETJ/USR_QXT_YTH/M.3200.0006.M001/TP1H/000/{YYYYMM}/{YYYYMMDD}/{YYYYMMDDHH}/GRID_TJQX_LYPUB_TP1H_AEHH_000_DT_{YYYYMMDDHHMMSS}_000-240_{NNNN}.nc`，env `ROLLING_FORECAST_GRID_ROOT` 可覆盖。NetCDF 结构：dims `time(264)×lat(181)×lon(181)`，变量 `TP1H (time,lat,lon) float32`，`time: -23..240`，`lat: 34-43°N`、`lon: 111-120°E`（海河流域，~5km）。核心函数：`is_flood_season(now)` 仅作参考字段；`select_latest_forecast_cycle(now)` 选最近 08/20 起报；`find_rolling_forecast_grid_file(root, cycle, max_fallback=4)` 按模式发现 .nc、取 NNNN 最大、时次回溯；`read_rolling_forecast_precip(path, start_hour, end_hour)` 切片返回 DataArray；`sample_rolling_forecast_at_stations(nc_path, records, hour, method)` 按站点采样返回 `{station_id: precip_mm}`（含网格边界检查）；`materialize_rolling_forecast_to_files(nc_path, hours, output_dir)` 把各时效 2D 切片写成独立 .nc 文件（兼容现有 GDAL 采样器）；`compute_lead_hours(cycle, start_time, forecast_hours)` 把用户请求窗口换算为相对 cycle 的 lead 小时半开区间（start_time 早于 cycle 钳 0、end 钳 240、start ≥240 抛 ValueError）；`materialize_rolling_forecast_accumulated(nc_path, start_hour, end_hour)` 对 [start, end) 半开区间 TP1H 求和写单张 GeoTIFF（`sum(min_count=1)` 保持全 NaN 像元为 NaN），与 EC `rain_total_Nh.tif` 累计口径一致——**下游 zonal stats 一律走累计路径，禁止再取单时次切片当 N 小时雨量**。`resolve_forecast_grid_source(now, ec_output_path, rolling_root)` 切换入口。**已接入下游**：`emergency_api.py` 的预报判定 + `emergency_response_interface.py` 的 `evaluate_haihe_forecast_emergency_response_core` 都用 `resolve_forecast_grid_source` 切换；前者直接调 `sample_rolling_forecast_at_stations`，后者用 `materialize_rolling_forecast_to_files` 生成 2D .nc 喂给现有 `_forecast_filter_core`（不改共享核心）。`draw_haihe_precip_product.py` 加 `_open_rolling_forecast_nc(path, hour)` 构造 RasterData（xarray 切片 + 垂直翻转 + 像元中心→边界 geotransform），`_open_raster_or_nc` 按扩展名分发；CLI 新增 `--rolling-forecast-nc`。`forecast_product_queue.py` 的 `ForecastProductJob` 加 `source`/`rolling_nc_path` 字段，`enqueue` 时自动 `resolve_forecast_grid_source`，worker 滚动预报分支各时效复用同一 .nc 调 `run_draw_haihe_precip_product`。`analyzers/RainfallAnalyzer.py` 入口 `resolve_forecast_grid_source` + `materialize_rolling_forecast_to_files` 生成 2D .nc 喂给现有 GDAL zonal stats，`data_resource` 标签动态标注数据源。
- `../hhlyqyxt-master/utils/rainfall_impact_geojson.py` — Traction-agent core algorithm for affected rivers (cross-repo dependency; keep `direct_match_km` defaults and graph/table version constants in sync; imports pandas directly, so ensure pandas is installed). **Design principle (v6 redesign):** the pkl directed graph is the topology authority; `full_v6` is a per-edge geometry/attribute lookup table with exactly one row per pkl edge, matched by `(objectid, from_x, from_y, to_x, to_y)` rounded to 6 decimals (≈0.1 m) to absorb float drift. Never reintroduce `ST_Dump` / `GROUP BY objectid` aggregation. **Algorithm invariant:** `_classify_graph_edges` classifies ALL candidate edges within `station_buffer_km` (default 30 km) as `direct_buffer` features — `is_direct_graph_edge=True` for edges within `direct_match_km` (default 10 km), `False` for the 10-30 km buffer-only ring. This avoids the "downstream without upstream" gap where buffer-only edges were previously dropped or mislabeled as downstream at distance 0. Classification uses the SQL-computed real-geometry min distance (`MIN(ST_Distance(geom, station))` from the `ST_DWithin` join), not the pkl endpoint chord; chord distance is only a fallback when the SQL value is missing. `station_buffer_km` defines which edges seed downstream tracing AND the direct_buffer feature set; `direct_match_km` marks the ≤10 km "real direct river segments" subset. **`get_edge_length_km` nan guard:** the production pkl (`E:\tj\line\result\river_directed_v6.pkl`) has `len_km=NaN` on all 34 Luan River edges; `get_edge_length_km(attr, *, from_xy=None, to_xy=None)` falls back to `haversine(from, to)` when the attr value is non-finite or missing, returning 0.0 only when no coordinates are available. `_save_downstream_edge` guards with `not (length_km > 0)` (catches both nan and ≤0). Never re-introduce `max(float(nan), 0.0)` — it returns nan and corrupts Dijkstra distance accumulation. **Downstream geometry:** `_fetch_missing_edge_rows` issues a second `WHERE objectid = ANY(...)` query for downstream edges outside the 30 km buffer. **Direction-agnostic lookup:** `_build_edge_lookup` indexes each row under BOTH `(objectid, from, to)` and `(objectid, to, from)` keys (via `setdefault`), so pkl flow direction vs full_v6 digitization direction mismatch doesn't cause a miss. **Spatial fallback:** when the exact endpoint key fails (full_v6 `from_x`/`from_y`/`to_x`/`to_y` don't match pkl attr values — observed for 112 candidate rows + 4 downstream edges in production, likely because the DB stores geometry endpoints rather than shapefile attribute values), `_match_edge_spatially` finds the candidate row with the same `objectid` whose geometry passes within 100 m of BOTH pkl endpoints (via `_point_to_lines_km`). Used in `_classify_graph_edges`, `_resolve_edge_features`, and `_fetch_missing_edge_rows` (exact key → reversed key → spatial). Unmatched edges fall back to a straight line labeled `geometry_source=pkl_edge_straight_fallback`. **Dedup:** `_save_downstream_edge` skips any edge in `direct_keys` (= all candidate edges), so an edge is emitted at most once across `direct_buffer` + `downstream_50km`. **Clipping & unwrap:** `_clip_geometry_to_keep_km` is pure Python (no Shapely) — picks the longest part of a `MULTILINESTRING`, orients the line by comparing endpoints to the pkl `from` node, then walks haversine segments up to `keep_km`. `_unwrap_geometry` converts single-part `MULTILINESTRING` to `LineString` for front-end rendering. Downstream `length_km` reports `keep_km`, not the DB row's full `len_km`. **Name priority:** `_pick_river_name` tries `src_name → river_name → pkl name`; the Luan static mapping (`_DEFAULT_LUAN_NAME_MAPPING`, overridable via `{graph_stem}_luan_names.json`) is applied ONLY when the picked name is a single CJK character or all sources failed — it never overrides a valid full name. `_normalize_river_name` appends `河` to single CJK chars. `is_luan` gating prevents Haihe single-char names from being relabeled. **Schema guard:** `_ensure_river_columns` validates `{geom, objectid, river_name_col, is_luan, id, src_name, len_km, from_x/from_y/to_x/to_y}` before querying — extend this set whenever `_query_candidate_edge_rows` / `_fetch_missing_edge_rows` reference new hard-coded columns. **Connection lifecycle:** `get_graph()` is called BEFORE `_open_connection()` so a missing/corrupt pkl cannot leak a DB connection. **Empty CSV:** `aggregate_5min_station_pre_to_24h` catches `EmptyDataError` and returns an empty DataFrame with the expected columns, so `build_rain24h_impact_river_geojson` produces a "未找到站点" result instead of crashing. **Removed:** `direct_station_top_n` parameter (was a silent no-op), `_query_downstream_rows` / `_create_downstream_temp` / `_line_wkt` / `_fill_unmatched_downstream_edges` / `_build_fallback_downstream_row` / `_find_direct_graph_starts` (+ helpers) / `_river_feature` / `_feature_geometry_key` / `_drop_downstream_covered_by_direct` (+ Shapely coverage trio) / `_apply_luan_names` (both defs) / `_luan_full_name` / `_clip_linestring_to_fraction` (Shapely-based). Graph/geometry failures are logged rather than swallowed. **2026-07-23 review fixes:** (1) `_build_river_propagation` 下游边原用 pkl `river_name` 命名、与 GeoJSON 不一致，现传入 `candidate_rows`、经共享 `_resolve_edge_row`（正向键->反向键->空间兜底，与 `_resolve_edge_features` 同口径）查 full_v6 row 后再 `_pick_river_name`，滦河单字等不再偏差；(2) `river_propagation` 现已在所有入口输出--`utils/test_rain_impact_internal.py`（内网入口，打印+JSON，含 `direct_rivers`/`downstream_rivers`）与 `utils/rainstorm_impact_map_service.py`（生产服务，落盘 `river_propagation.json`、summary 含传播时间）。**2026-07-24 GeoJSON 属性修复：** (3) 每条 GeoJSON feature properties 现统一含 17 个字段（`min_downstream_distance_km` / `end_downstream_distance_km` / `keep_km` / `clip_fraction` / `min_station_distance_km` / `trigger_station_count` / `trigger_stations` / `propagation_distance_km` / `propagation_time_hours` 等），不适用场景填 0.0/0/[]，QGIS 属性表再无 NULL 列；(4) `_resolve_edge_features` 对直接边优先用 `edge["row"]`（`_classify_graph_edges` 时已存），与 `_build_river_propagation._resolve_row` 同口径——否则重新 lookup 在滦河命名路径（`is_luan`+单字缩写+`luan_mapping`）可能产出不同 row→不同河名→per-edge 与 summary 分组错位（如青龙河/陡河/滦河等）；(5) `flow_velocity_mps` 穿透 `_build_river_geojson`→`_resolve_edge_features`，每条 feature 独立算 `propagation_time_hours` = `propagation_distance_km` / (velocity*3.6)。**内网验证入口：** `utils/intranet_verify_emergency.py`（应急响应 HHLY 拉取+入库+12h 分母 4 项检查，带 3 次重试防 MUSIC 瞬断）、`utils/intranet_verify_rain_impact.py`（GeoJSON 属性完整性+per-edge/summary 一致性 5 项检查），均无需 pytest。
- `../hhlyqyxt-master/ScheduledTask/emergency_response_monitor.py` - Traction-agent 5-minute emergency-response level calculator (cross-repo dependency; writes `QyEmergencyResponseMonitor` via `utils.db.Session`). **Data source (HHLY switch, 2026-07-23):** emergency response independently fetches its OWN HHLY copy - it does NOT touch the shared `yangxiao.csv` (still HHLY_JUECE, written by `stationProcessMin.py` for colleagues' code). New path: `run_emergency_response_monitor(timerange=..., datatime=..., minute_monitor_id=..., client=None)` -> `_fetch_hhly_rainfall_for_emergency(timerange, client)` reuses traction-side `utils.MusicTool.MusicClient` with `HHLY_BASIN_CODES="HHLY"`, `HHLY_MIN_DATA_CODE="SURF_CHN_MUL_MIN"`, `HHLY_MIN_ELEMENTS` (含 Station_levl) -> `compute_emergency_response_stats(df, datatime)` (now accepts DataFrame OR CSV path) -> 入库. **NEVER cross-repo import QA-side (`haihe-weather-analyzer-mcp`) modules** - code is written INTO this file because the intranet server does not lay projects out side-by-side like dev. **National station level is 2-digit:** `NATIONAL_STATION_LEVELS={"11","12","13","16"}`, `_normalize_station_level` strips leading zeros (`"011"->"11"`, `11->"11"`, `None/""->"0"`) - equivalent to the old zfill-3 logic for national counts. **Backward compat:** `stationProcessMin.py:444` is the ONLY production caller; since 2026-07-23 it passes `timerange` (UTC 24h window `[end_time-32h, end_time-8h]`, i.e. BJT end_time shifted -8h for the HHLY API) + `datatime=end_time`(BJT), so the HHLY independent-fetch path is now LIVE in production (no longer reads `yangxiao.csv`). The `csv_path` path remains as fallback: `timerange` + `csv_path` both given -> timerange wins + WARNING; neither -> `ValueError`. **Timezone:** `_fetch_hhly_rainfall_for_emergency` shifts the HHLY-returned UTC `Datetime` +8h to BJT (matching the CSV path), else the 24h window would be off by 8h. **12h ratio denominator (2026-07-23 fix):** `ratio_12h_baoyu = station_12h_baoyu / len(sum_pre_12h)` — 12h-window national-station count, NOT the 24h count; 24h ratios still use `len(sum_pre_24h)`. `total_national_stations` in the result is the 24h count. Stops 12h-stopped-reporting stations from depressing the Ⅲ级 ratio. **Bad datetime:** `compute` uses `pd.to_datetime(..., errors="coerce")` + `dropna(Datetime)` so one bad row no longer crashes the 5-min task. **Untouched:** `_determine_response_level` (tedabaoyu≥0.15->1, dabaoyu≥0.15->2, 12h baoyu≥0.20->3, 24h baoyu≥0.20->4, else 0), `BAOYU_LOWER=50.0`/`DABAOYU_LOWER=100.0`/`TEDABAOYU_LOWER=250.0`, ratio thresholds, 先删后插 persistence, table schema. **Empty handling:** HHLY fetch empty/`EmptyDataError` -> empty DataFrame -> `compute` returns None -> `run` warns + returns None, no DB write (mirrors old CSV-empty branch). API errors propagate to caller.

- `forecast_evaluate 2/forecast_evaluate/scripts/` — 预报检验核心引擎（`forecast_evaluate.py` 图表生成、`analyzer.py` 报告+较差样本判定、`config.py` 路径/要素/产品映射、`batch_download.py` 批量下载）。MCP 工具通过 `sys.path.insert` 导入此目录。
- `haihe-weather-analyzer-mcp/forecast_evaluate_tool.py` — MCP 预报检验工具（`evaluate_forecast` 含 `report_markdown`+`poor_samples`+`chart_paths`；`generate_forecast_charts` 返回柱状/折线/热力图文件路径）。共享 `_validate_params_and_fetch` 参数校验+默认时间+API 调用。
- **Forecast evaluate fast path** — 图表类请求调用 `generate_forecast_charts` → `cl.Image`；文本类请求优先使用 `report_markdown`，回退用排名表格。
- **Forecast evaluate config paths** — `FORECAST_EVAL_DIR` env var 优先（默认 `~/forecast_evaluate_data`）；`PathConfig.BASE_SAVE_DIR`/`PNG_SAVE_DIR`/`OBSIDIAN_VAULT_PATH` 均由此派生。
- **Forecast evaluate prompt rule 13** — 区分图表请求（`generate_forecast_charts`、按用户意图推断 chart_types）与文字请求（`evaluate_forecast`、完整报告）；参数提取规则涵盖 element/test_type/rain_type/time_session。

**Fast path order** in `process_message()`: rainfall img → river plot → rainfall analysis → city avg rainfall → rain duration → today rainfall → weekly forecast → heavy rain check → subbasin forecast → basin areal rainfall → weekend activity → basin weather → general weather → water level → emergency response → poi → risk warning → forecast evaluate (chart → report) → rainstorm impact time → (falls through to planner LLM).

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
- Git Bash 的 `python` 是 Windows Store 占位程序（静默 exit 49 无输出）；测试用项目 venv `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe`（需装 pytest/pytest-asyncio/pandas/requests/langchain-core）
- 选择性运行单个测试文件可能被 `tests/stubs.py` 的 `langchain_core` stub 阻断（stub 只在真实 langchain_core 未加载时安装）；以全量 `python -m pytest tests/` 结果为准
- `test_decision_weather_tool.py` has a pre-existing import failure (`ModuleNotFoundError: No module named 'tools'`) unrelated to forecast evaluate; exclude with `--ignore=chainlitexam/tests/test_decision_weather_tool.py` when running the full suite
- Bash working directory persists across tool calls; use absolute paths when invoking commands outside `chainlitexam/` (the parent directory name contains spaces)
- `haihe-weather-analyzer-mcp/server.py` overrides `fixed_rainfall_impact_tool.DEFAULT_DIRECT_GRAPH_MATCH_KM` at runtime and hard-codes the `get_affected_river_network_by_rainfall` description; keep both in sync with `hhlyqyxt-master/utils/rainfall_impact_geojson.py`
- `fixed_rainfall_impact_tool._resolve_graph_path()` prefers `DIRECTED_GRAPH_FILENAME` in the same directory as the configured graph path and is robust to directory paths and empty filenames; keep this logic in sync with any graph-loader fallback in `tools.py`
- `fixed_rainfall_impact_tool.IMPACT_RULES` uses the `RIVER_TABLE_VERSION` constant; do not hard-code `full_v6` strings
- Versioned river resources are centralized: use `constants.RIVER_TABLE_FULL` / `constants.DIRECTED_GRAPH_FILENAME` in the MCP package, and the module-level constants in `rainfall_impact_geojson.py` in the traction-agent package. When upgrading graph/table versions, update both constants files and `config.ini` together.
- `safe_evaluate_haihe_emergency_response` defaults empty `times` to the current Beijing hour via `TIANJIN_TIMEZONE`; reuse `haihe_mcp_tools._normalize_time_param` for consistent time handling
- `fixed_rainfall_impact_tool._empty_response()` must return the same keys as `_format_mcp_response()`, including `river_geojson` and `rules`, and use the caller-supplied `direct_graph_match_km`
- The `include_background` parameter in `get_affected_river_network_by_rainfall` / `local_get_affected_river_network_by_rainfall` is accepted but not implemented by the upstream builder; do not forward it until `rainfall_impact_geojson.py` adds support
- `fixed_rainfall_impact_tool.py` 的 IMPACT_RULES["direct"] 文字、_empty_response 中 station_buffer_km 硬编码、server.py 工具描述中的 "30km" 均已同步为 20km（2026-07-27 feat/qa-agent-rain-impact-sync）。`_base_response_fields` 顶层包含 `reference_time`（builder result.params.reference_time 透传）。`_normalize_station` 输出含 `rain_end_time`（从 rainfall_result 顶层 time_range 末端派生，所有站点共用）。`IMPACT_RULES["arrival"]` 描述 estimated_arrival_time / t0_source_time / reference_time 语义。GeoJSON feature.properties 的 t0_source_time / estimated_arrival_time 由 builder 直接嵌入，MCP 层原样透传。
- Error text in tool failures and MCP wrappers is scrubbed (IPs/paths removed) before logging or returning to the LLM/user
- LLM model: Qwen3.6-27B via local OpenAI-compatible proxy at `10.226.188.156:8000/v1/`
- Internal service addresses: MUSIC `10.226.90.120`, PostgreSQL `10.226.107.130`, RAG `10.226.188.156:8033` — never include these in user-facing output or checked-in documentation (replace with env-var placeholders like `${ROLLING_FORECAST_API_URL}`)
- Data sources: MUSIC/Tianqing stations (实况), ECMWF AIFS (预报), CMA warnings, PostgreSQL/PostGIS (河网/行政区划), RAG knowledge base
- **Fast-path contract:** every `_try_*_fast_path` in `message_orchestrator.py` must call `_show_business_reasoning(...)`, close the reasoning step on every return path, and reference `thinking_chain` or `generate_fast_path_thinking(...)`; `tests/test_fast_paths.py` enforces this statically
- **Decision-weather dual entry points:** `DecisionWeatherQAService` (fast path) and `query_decision_weather_for_poi` (LangChain tool) both consume `tools/decision_weather_core.py`; when `_normalize_decision_weather_slots`, `_compact_decision_forecast_facts`, or `_decision_hourly_window` change, update both callers. 历史日期路由（`_is_past_date_forecast_payload` → `query_poi_historical_weather`）也走双入口，参数拼装与解包/生成共用 `_decision_historical_window_args` / `_generate_decision_historical_answer_from_raw`，新逻辑必须同时接两入口保持 parity。
- **Decision-weather wrapper parity:** `DecisionWeatherQAService._normalize_slots` is a thin wrapper around `_normalize_decision_weather_slots`; keep signatures in sync so new optional arguments (e.g. `hourly_request`) are forwarded
- **Tool-result unwrapping:** always use `_unwrap_tool_result()` from `utils/tool_result`; do not introduce alternate names like `_unwrap_tool_observation`
- **Decision-weather prompt facts:** `_generate_decision_weather_answer` needs both `"预报时段"` (periods) and `"小时级降雨计算"` (hourly_rain) in `business_facts`; omitting either breaks general or hourly answer formats
- **POI 注意事项（决策天气）:** `query_decision_weather_for_poi` 与 `DecisionWeatherQAService` 在 `_generate_decision_weather_answer` 尾部、表格与数据来源之间追加代码确定性生成的 `⚠ 注意事项`（`_build_poi_reminder_section`）。类别由 `classify_poi_category(name, address, category_1, category_2)` 判定（school→airport→station→scenic→mountain 优先级，mountain/station 只认复合词防"石家庄/唐山/裸站"误判）；scenic 另含 `_POI_KNOWN_SCENIC_NAMES` 知名天津景点名单（五大道/古文化街/意式风情区/天津之眼等，**只对 name 匹配**，名称含 街道/办事处/政务/社区/派出所/医院/村/县/镇/乡 时跳过；裸"盘山"歧义已移除）。**POI 定位区域偏好**：`_decision_pick_first_poi(payload, keyword)` 同名大众点（河西中心/实验中学等）默认主场天津优先；keyword 显式含"天津"时**严格过滤**（只认 名称含天津/地址含天津市/区县名且无外省证据词 `_POI_NON_TIANJIN_REGION_RE` 的点位，宁可查不到也不拿外省同名点冒充）；显式含外省城市/省份词（`_POI_EXPLICIT_REGION_WORDS`，城市在前省份在后）则尊重该城市。`category is not None` 时才调 MCP 工具 `query_poi_hazard_reminders`，工具缺失/失败静默跳过。**隐患点只在预报有降雨时提醒**（`_decision_rain_intensity` 分档，`show_hazard = has_hazard and intensity>0`），且只出**风险研判表**（类型×数量×风险×建议，`_HAZARD_RAIN_RISK` dzzh/sh/zxhl 纯规则矩阵零编造），**不逐条列举隐患点**；累计 0mm 不打印"0 毫米"标题。天气断言只来自 facts 数值，隐患点信息只来自工具返回，**禁止 LLM 编造**。两入口接线保持 parity。
- **隐患点 MCP 工具 `query_poi_hazard_reminders`:** `custom_tools/poi_hazard_reminder_tool.py`，按 haversine 半径（默认 5km，≤50）在 3 张静态表（`t_msis_be_fxyj_dzzh_info` 地灾/`_sh_info` 山洪/`_zxhl_info` 中小河流，**无 geom 列**）过滤，模块级懒加载缓存 `HAZARD_CACHE_TTL`（默认 3600s）。**单表查询失败必须 `conn.rollback()`**，否则 psycopg2 事务中止导致后续表全挂。表名/半径/schema 用 `HAZARD_TABLE_*`/`HAZARD_SCHEMA` env 覆盖。
- **风力解析陷阱：** `_decision_max_wind_level` 解析 `X～Y级转Z级`/`X～Y级阵风Z级` 复合风况时，区间正则与单值 `(\d+)级` 必须都用 `finditer` 取**所有**匹配再取 max；用 `search` 取第一个会命中区间下限，系统性低估最大风力。
- **Basin/river-system future weather:** When users ask about whole-basin or sub-basin future weather (e.g., "海河流域明天天气", "大清河流域未来三天降雨"), the planner must call `get_river_system_rainfall_forecast` first. It returns rainfall statistics per Haihe 9-zone river system from the rolling forecast or EC AIFS grid. Use the river-system table as the primary answer and only call `get_city_rainfall_time_range` for representative-city details when needed. The final answer must use the `data_source` field returned by the tool and must not expose backend details such as table names, file paths, or tool parameters. The rolling-forecast window is aligned to the requested `start_time` via lead-hour offset from the latest available cycle and accumulated over the window (see `resolve_forecast_raster_path`); today and tomorrow therefore return different data. **`query_rolling_forecast` hard guard:** `rolling_forecast_service.is_basin_weather_query()` bounces basin/river-system queries at the tool level (raises `BusinessException` telling the planner to use the river-system tool); bare river names only count outside POI contexts （公园/湿地/附近/沿线/车站 etc.), point mode (lon+lat supplied by decision-weather callers) skips the guard, and the keyword list is a backstop, not exhaustive — the prompt rules in `prompts.py` remain the primary defense.
- **Thinking-summary keyword trap:** in `_build_thinking_summary` (`message_orchestrator.py`), `"河流"` is a substring of `"X河流域"` — the river-network branch must use the `河流(?!域)` negative lookahead, and weather questions must not be prefixed with the river-visualization intro.
- **Answer structure (thinking above answer):** `process_message` 主流程先创建并发送 `ReasoningStep("🤔 思考过程")`，再创建并发送 `stream_msg`（回答）——网页端思考在上、回答在下。fast path 的 `_emit_fast_path_result` 本就正确，勿改回。`_prepend_thinking_summary` 的前置"思考摘要"是业务引导语，保留。
- **Parallel tool execution:** `_run_tool_round` 用 `_PARALLEL_SAFE_TOOLS` 白名单（仅纯数据、无副作用工具）+ `asyncio.gather` + `_PARALLEL_TOOL_CONCURRENCY=4` 信号量并行执行；有副作用工具（预警/滚动预报/图/GIS/决策天气/analyze_rainstorm_impact）保持串行，分支逻辑原样保留。并行结果按 tool_call 顺序归并 ToolMessage。**`tool_step.output` 对串行工具在 `cl.Step` 的 `__aexit__` 后赋值后必须 `await tool_step.update()` 才会渲染**（重构初版在块外赋值导致状态文本丢失，已修复）。
- **Warning table scope trimming:** `warning_workflow._trim_warning_regions_for_scope` 按问法作用域裁剪预警表格"影响区域"列。**`names_district` 必须优先于 `asks_broad`**（否则"天津市蓟州区"被"天津"子串误判为市级折叠成"全市"）；`_is_broad_scoped_warning_query` 用 `[一-龥]{1,4}区(?:县)?` regex 识别区县。`broad_terms` 用模块级 `_BROAD_SCOPE_TERMS`（勿改 `_is_national_and_tianjin_warning_query` 的独立 tuple，它有意省略"全市"）。`_build_warning_table_markdown` 支持 `show_region_column` 参数。
- **已知 flaky/顺序依赖测试：** `tests/test_message_orchestrator.py::test_process_message_skips_fast_paths_when_disabled` 真实创建 `ReasoningStep` 缺 Chainlit context 抛 `ChainlitContextException`，属既有问题，全量套件预期 1 failed，不计为本项目回归。另有 2 个 `_run_tool_round` 测试（`test_run_tool_round_failure_records_tool_message_without_generic_error`、`test_run_tool_round_parallelizes_pure_data_tools`）**单独跑文件时**同样抛 ChainlitContextException（依赖前面测试建立的 context 泄漏，全量套件中通过）——若单独跑 `test_message_orchestrator.py` 看到它们失败属顺序依赖，非回归；修复需给两测试显式 `init_http_context`，暂留待后续。

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

## HTTP 问答接口

`POST /api/v1/qa/ask`（答案正文 + 图片 URL + GIS 图层 + 思考过程）与
`GET /api/v1/qa/files/{session_id}/{file_id}` 挂在 `api_sub_app` 上。

- **依赖方向**：`chain_gzt` → `qa_http_api`（单向注入）。`qa_http_api.py` 禁止 import `chain_gzt`。
- **核心**：`init_http_context` 伪造 HTTP 会话 + `CapturingEmitter` 拦截输出。`message_orchestrator.py` 零改动。
- **答案归并**：`process_message` 先 `send()` 空消息再 `update()` 填内容，同一消息产多个事件。`merge_answers()` 按 id 取最终态 + 按首现顺序拼接。在出口过滤 `❌`/`⏱️`/`📊` 旁路消息和纯引导语，小程序只拿到干净正文。
- **思考过程**：`reasoning_texts()` 按 id 归并取最终态，过滤 `tool`（原始 JSON 数十 KB）和 `user_message` 类型。
- **图片**：`cl.Image(content=bytes)` 自动落盘到 `chainlit.config.FILES_DIRECTORY/<session_id>/`。TTL 清理由 `run_cleanup_loop` 后台任务负责（`QA_API_CLEANUP_INTERVAL_SECONDS`，默认 300s）。
- **会话回收**：Chainlit 只在 WS 断开时回收 `user_sessions`/`chat_contexts`，HTTP 会话永不走到那条路。`_release_chainlit_session()` 在请求的 `finally` 块里手动 pop，否则无界增长直到 OOM。
- **多轮上下文**：`InMemoryConversationStore`（内存字典 + TTL）。`lock_for(cid)` 防止同会话并发读改写竞态。`prune_history` 保留最近 `QA_API_MAX_HISTORY_TURNS`（默认 10）轮干净的 Human/AI 问答对，丢弃 `ToolMessage`/`SystemMessage`/`tool_calls` 空壳，且始终从 `HumanMessage` 开头。
- **环境变量**：`QA_API_MAX_CONCURRENCY`（4）、`QA_API_TIMEOUT_SECONDS`（180）、`QA_API_FILE_TTL_SECONDS`（1800）、`QA_API_CONVERSATION_TTL_SECONDS`（3600）、`QA_API_MAX_HISTORY_TURNS`（10）、`QA_API_CLEANUP_INTERVAL_SECONDS`（300）、`QA_API_RESPONSE_CACHE_TTL`（300）。非法值回落默认，防止 `Semaphore(-1)` 把服务导入期崩掉。
- **响应缓存**：单轮请求（无 `conversation_id`）相同 `question` + 开关在 `QA_API_RESPONSE_CACHE_TTL`（默认 300s）内直接返回缓存结果，不重新跑完整问答。多轮请求不缓存（保证上下文正确）。`QARuntime._response_cache` 实现。
- **简单天气规则路由**：`message_orchestrator._route_simple_weather_query` 对"今天/明天/后天/周末 + 天气"这类明确高频问题直接路由到 `query_rolling_forecast`（无地点）或 `query_decision_weather_for_poi`（带地点），跳过 planner LLM 调用（省 5-10s）。命中条件严格：排除流域/河系（`_is_basin_or_river_query`）、决策类词（适合/能否/附近等）。模糊问题交回 planner。测试：`tests/test_simple_weather_route.py`（21 条）。
- **脱敏**：响应出口过 `_scrub`（IP/路径/数据库连接串）。日志只记异常类型，不记 `exc_info`（完整 traceback 含内网地址）。
- **鉴权**：本期不做，靠部署时网络层限制。
- **对接文档**：`docs/问答接口对接文档.md`
- **测试**：`chainlitexam/tests/test_qa_http_api.py`（71 条），全部用假 chain，不依赖内网。全量跑时部分测试因 `tests/stubs.py` 的假 `chainlit.Step` 被跳过（已知现象）。

## 近期功能（2026-08-05~06 批次）

- **Prompt 拆分**：`WEATHER_ASSISTANT_PROMPT` 拆为 `PLANNER_SYSTEM_PROMPT`（~355行，只含工具选择/参数规则/停止条件）和 `METEO_ANSWER_SYSTEM_PROMPT`（~145行，只含气象表达/格式/结论结构）。`ENABLE_NEW_PLANNER_PROMPT=false`/`ENABLE_NEW_ANSWER_PROMPT=false` 默认关闭，`_build_orchestrator_runtime` 双轨选择。旧 PROMPT 保留不删。
- **证据完整性 shadow**：`tools/meteo_evidence.py` 的 `is_evidence_complete(query_type, tool_results)` 纯函数判断工具结果是否足够回答。`process_message` 记录 `would_early_finalize` shadow 日志（`[EVIDENCE]`），默认不改流程。`TimingContext.evidence` 字段含 `query_type`/`would_early_finalize`。`_QUERY_TYPE_BY_TOOL` 从 tool_name 推断 query_type（**注意 dict 插入顺序即优先级**：warning 最前，forecast/current/water_level/rain 依次）。
- **候选工具召回增强**：`ToolCandidateIndex.candidates_for_top_n(user_text, n)` 分层召回（Top-5/8/12）。`[TOOL_CAND]` 日志改 JSON Lines（含 `query_type`/`recall_5`/`recall_8`/`recall_12`/`candidates_12`）。`scripts/recall_stats.py` 离线统计召回率 + 漏召回列表。`scripts/perf_stats.py` 和 `recall_stats.py` 共享 `_stats_common.read_records()`。
- **LLM 预热**：`chain_gzt._llm_warmup(runtime)` 对 planner/answer 各发一次最小非敏感请求（"请回复一个字：好"），30s 短超时，失败不阻断启动。`ENABLE_LLM_WARMUP=false` 默认关闭。
- **[PERF] 统一出口**：`TimingContext.log()` 输出 JSON Lines（`as_dict`/`to_json`）。`timing.log()` 统一到 `_log_query_exit` finally（幂等 `_logged` + `query_timing_logged` 去重），所有退出路径记录一次。含 `http_queue_wait_ms`/`tool_queue_wait_ms`/`planner_input_chars`/`planner_output_chars`/`answer_input_chars`/`answer_output_chars` 字段。
- **`_QUERY_TYPE_BY_TOOL` 插入顺序依赖**：`message_orchestrator.py:821-831` 的 dict 插入顺序即优先级（warning 工具最前，两轮时 warning 优先于 forecast/current/water_level/rain）。**勿排序或 alphabetize**。

## 性能批次（2026-08-12，生成端限速 + 应急并发）

背景：之前优化全在压「输入」（prompt 拆分/历史裁剪/候选召回/工具并行），但生产日志显示 planner 输入仅 2064 字符仍 60s 超时——瓶颈在**生成端**，不在输入。

- **LLM max_tokens 默认上限**：`chain_gzt._build_chat_llm(role)` 工厂统一构造 planner/answer（`_build_orchestrator_runtime` 改用之）。**始终给 max_tokens 上限**，不再默认无界：`PLANNER_MAX_TOKENS`（默认 **2048**）、`ANSWER_MAX_TOKENS`（默认 **4096**），env 可覆盖；0/非法值回退默认。⚠️ PLANNER 取 2048 而非 1024：planner 除输出 tool_call 外，有时被复用为面向用户的完整回答（`message_orchestrator.py:4728`），1024 会截断。
- **Qwen3 思考块开关**：`LLM_DISABLE_THINKING=true` 时 `_build_chat_llm` 经 `extra_body={"chat_template_kwargs":{"enable_thinking":False}}` 关闭 `<think>` 隐藏推理块（planner 提速最大杠杆）。**默认关**——本地无法验证内网 vLLM/SGLang 是否支持，默认关时生产行为与现状完全一致；确认服务端支持后再开。新增辅助 `_env_bool`。
- **应急时间段判定并发**：`haihe_mcp_tools._evaluate_one_synoptic_time(ts, basin_codes, allowed_station_levels)` 抽出单时次流水线（fetch→filter→evaluate→report→event dict，单时次失败容错返回 None）。`evaluate_emergency_response_by_time_range` 对 4 个整点时次改 `ThreadPoolExecutor` 并发取数（`EMERGENCY_FETCH_WORKERS`，默认 4），串行 ~127s → 并发≈最慢单次。线程安全依据：`_observation_fetch_core` 每次 `new MusicClient()`（独立 Session）。返回结构/排序/max_level 聚合口径不变。测试 `tests/test_emergency_time_range_parallel.py`（并发计数器证明 + 单点容错 + 排序聚合）、`chainlitexam/tests/test_build_chat_llm.py`。
- **planner 第 2 次调用超时兜底（Fix B，8-12 生产日志"今天雨下了多长时间"超时无输出）**：根因是本地 Qwen LLM 服务响应波动（输入已压缩到 ~3400 字符仍 60s 超时，同查询一败一成），而非输入膨胀。修复两层：① `_astream_planner_think_retry_once` 在 orchestrator 层对超时重试一次（`chain_gzt.astream_planner_think` 超时立即重抛不重试的契约不动）；② 重试仍超时时，非滚动工具走 `_assemble_tool_observations_fallback` 把本轮已成功取回的工具观测（当前轮 ToolMessage 不被 `_compress_messages` 截断）用代码拼接输出，**保证"拿到数据必有输出"**、不再撞慢 LLM；`_is_failed_tool_observation` 过滤失败/占位/天河哨兵观测，无可组装数据则维持原错误路径。滚动完整时（Fix C 场景）不加 planner 重试、Fix C 行为零改动——二者由 `_has_complete_rolling_forecast` 互斥分流。

### MCP 工具取数缓存 + 多时次合并（2026-08-12，全问题类型性能优化 · A1-A7 + B + C 已交付）

口径（用户逐条确认）：**`ENABLE_FAST_PATHS` 保持关闭**（fast path 不作优化向量）；**实况类短 TTL 60-120s**；**思考过程必须显示**（主路径 LLM 行为不动）。主路径 LLM（planner+answer，2 次 LLM）已优化到底，剩余优化集中在 MCP 工具取数。复用既有模式：模块级 dict `{key:(ts,value)}` + 惰性过期 + env 可调 TTL + `threading.Lock`；**只有成功结果才写缓存，错误/失败不写**；实况类键含当前时次桶（跨时次必 miss）。

- **A1 `query_current_weather_observation`**（current_weather_observation_service.py）：`CURRENT_WEATHER_CACHE_TTL` 默认 **60s**；键 = `时次桶(YYYYMMDDHH)|hours_back`。
- **A2 `query_poi_nearest_observation`**（custom_tools/poi_nearest_observation_tool.py）：工具体提取为 `_query_poi_nearest_observation_core`；`POI_NEAREST_OBS_CACHE_TTL` 默认 **60s**；键 = `入参|时次桶`（POI 部分仍走 `_search_poi_core` 已有 3600s 缓存）。
- **A3 `get_tianjin_wind_warning_assessment`**（haihe_mcp_tools.py）：工具体提取为 `_query_tianjin_wind_warning_core`；`TIANJIN_WIND_CACHE_TTL` 默认 **120s**；键 = `request_time`；接口失败（`wind_observation_api_failed`）不写缓存。
- **A4 预警 4 工具**（haihe_mcp_tools.py，缓存放辅助函数层）：`WARNING_INFO_CACHE_TTL` **120s**（effective/history 各自由 `warning_status` 做键；`include_raw=True` 排查路径不缓存）；`TODAY_WARNING_SUMMARY_CACHE_TTL` **120s**；`NATIONAL_WARNING_CACHE_TTL` **120s**（键=`keywords|max_items`）。
- **A5 `query_water_level`**（tools.py）：工具体提取为 `_query_water_level_core`；`WATER_LEVEL_CACHE_TTL` **120s**；默认查询键=`河名|类型|今日零点`（跨天必 miss、TTL 管新鲜度），显式时间段按完整时间段做键；接口失败不写缓存。
- **A6 `rag_search`**（haihe_mcp_tools.py）：工具体提取为 `_rag_search_core`；`RAG_SEARCH_CACHE_TTL` **600s**（知识库静态，非实况）；键=`query|kb_key`；unknown_kb_key/rag_api_failed 不写缓存。
- **A7 `search_poi_by_distance`**（haihe_mcp_tools.py `_search_poi_by_distance_core`）：`POI_BY_DISTANCE_CACHE_TTL` **3600s**（与 `_search_poi_core` 同族补齐）；键=`keyword|lon|lat|size|distance_km`。
- **B MUSIC 多时次合并**（current_weather_observation_service.py）：`_query_same_successful_time` 用 `times` 逗号连接把「6 候选 × 2 接口」合并为 **region/basin 各 1 次调用**，`_group_records_by_time` 按时次切回、从新到旧选第一个「region 覆盖完整 + basin 非空」时次（与原循环语义等价）。**安全回退**：服务端只回单时次或合并请求抛错时自动回退逐时次串行（行为不变），多时次检测 = region 与 basin 都返回 ≥2 个不同时次。生产确认探针：`haihe-weather-analyzer-mcp/probe_music_multi_times.py`（用户内网跑）。`query_poi_nearest_observation` 的 `_query_station_records` 合并未做（循环复杂：2 模式×2 元素集×nearest 语义，风险大于收益，且 A2 缓存已覆盖重复查询）。
- **C `scripts/perf_stats.py`**：`summarize` 新增 `stages_ms`（`[PERF].stages` 分阶段 p50 降序）、`tool_share_of_total_pct`（tool 取数占总耗时百分比）、`tools_per_request`（每请求工具数，对比多时次合并 12→2），用于前后对比证明收益。

### 前后端遍历优化批（2026-08-14，均不改变问答结果）

遍历三路（chainlitexam 前端 / MCP 后端 / HTTP·流式·观测）后的安全优化，全部 TDD。口径不变：**不改变问答结果**（模型输入/数据/回答文本逐字不动）；只省重复工作、重复取数、无效 I/O。

**前端（chainlitexam）**：
- **A1 HTTP 免 data-layer 落库**（qa_http_api.py `_suppress_chainlit_data_layer` + `_run_once` 包住 process_message）：HTTP 客户端不读 DB（答案走 CapturingEmitter、多轮走 InMemoryConversationStore），每请求 ~20-50 次 fire-and-forget PG 写 + 孤儿 thread/step 行是纯浪费。必须同时置 `_data_layer_initialized=True` 防 `get_data_layer()` 懒加载重建真层（chain_gzt 手工装 SQLAlchemy 时未置该标志）。
- **A2 `stream_text_to_message` 按 execution_mode 累积**（chain_gzt.py）：HTTP 下不逐 32 字块 update+sleep，一次更新；chainlit 模式逐块不变。`_build_orchestrator_callbacks` 用 `_stream_text` 包装注入 mode。
- **A3 `_response_cache` 修剪**（qa_http_api.py）：`RESPONSE_CACHE_MAX_SIZE`（默认 200）超限时 `_maybe_prune_response_cache` 清过期条目，防无界增长。
- **A4 answer 流式连接错误重试**（chain_gzt.py `astream_answer_chain_to_message`）：ConnectionError/httpx 连接/读超时在无部分内容时重试（`ANSWER_MAX_RETRIES` 默认 2），与 planner 对称；非连接错误/超时保持原回退。

**MCP 后端**：
- **B1 `forecast_evaluate_tool` 缓存顺序缺陷**：原 `_validate_params_and_fetch` 在缓存查询前就调检验 API（1h 缓存形同虚设）。拆成 `_parse_evaluate_params`（廉价校验/解析）+ `_fetch_evaluate_api`（昂贵取数），`_evaluate_forecast_core` 顺序 = 解析 → 缓存命中 → 取数（仅 miss）。
- **B2 应急实况判定缓存**（haihe_mcp_tools `evaluate_emergency_response_core`）：`EMERGENCY_EVALUATION_CACHE_TTL` 默认 **120s**，键=判定入参（不含 include_records）；include_records=True 不缓存。省同参重复 24h 分钟取数（~43s）；不触碰 `_evaluate_one_synoptic_time`/`_fetch_minute_hourly_curve`。
- **B3 静态历史降雨系列缓存**（custom_tools，共享装饰器 `custom_tools/_ttl_cache.py` `make_ttl_cache`，只缓存 status=="ok"）：`LAST_MONTH_AREAL_CACHE_TTL=3600s`、`YEAR_TO_DATE_AREAL_CACHE_TTL=120s`、`LAST_YEAR_MAX_CACHE_TTL=3600s`、`HISTORICAL_SAME_PERIOD_CACHE_TTL=600s`、`HISTORICAL_WEATHER_CACHE_TTL=600s`。
- **B4 `query_risk_warning` 缓存**（risk_warning_tool）：`RISK_WARNING_CACHE_TTL=120s`，键=类型|时间窗|extra（region 不上接口不进键）。

**基础设施**：
- **MUSIC client 单例**（haihe_mcp_tools `_get_music_client()`）：复用 requests.Session/TCP 连接（tools.py 既有同款先例）。`_observation_fetch_core`/`_forecast_fetch_core` 线程池路径保留 `new MusicClient()` 独立 Session；`_query_tianjin_wind_warning_core` 也保留 `new MusicClient()`（测试 mock `hmt.MusicClient` 需要，单例跨测试持久化会让 mock 失效）。
- **`_load_mcp_config` 惰性缓存**（chainlitexam/tools/rainfall_river_impact.py）：config.ini 静态，不再每次工具调用重读磁盘。

**报告不修（被 message_orchestrator.py 并行会话占用，不能提交）**：`record_tool_call` 生产未调用 → `[PERF].tools` 恒空（perf_stats 的 tool_share/tools_per_request 生产无数据，需补埋点后生效）；answer 60s 硬超时加 `ANSWER_TIMEOUT_SECONDS` env（六处 wait_for 在该文件）。**本轮延后**：静态映射缓存（nine-zone WKT / fine→9 / 河道几何 / 分区边界）、Web 端运行时进程级缓存（跨会话污染风险，需日期失效设计）、POI 最近观测多时次合并（风险>收益）。

### 14所 basin_drawing 出图接口（2026-08-14，新功能接入）

接入 14所「可出图分区列表 + 实况/预报格点雨量或面雨量出图」两个接口，让用户问「XX分区降水图/面雨量分布图/格点预报降水图」时能出图。口径（用户逐条确认）：**单工具参数化**（sceneType/productType 由 planner 按 docstring 路由）；**图片返回代理 URL**（答案 markdown 图链 + HTTP `images` 字段）；**base 默认 `${BASIN_DRAWING_API_BASE}`**（env 可覆盖）；时间自动规整到 10 分钟刻度；**只有带 children 的一级分区可出图**。

- **新工具**（`haihe-weather-analyzer-mcp/custom_tools/basin_drawing_tool.py`，已注册 `server.py`）：
  - `query_basin_drawing_areas()`：GET `/openapi/basin_drawing/areas`，返回归一化分区树 + `supported_count`（带 children 的一级分区数）。`BASIN_DRAWING_AREAS_CACHE_TTL` 默认 **3600s**（分区静态）。
  - `generate_basin_rainfall_image(scene_type, product_type, parent_area_id, area_codes, begin_time, end_time, main_title, sub_title, show_rain_value, show_area_name, forecast_time="", force_create=0)`：POST `/openapi/basin_drawing/image`，返回 `image_url`（代理 URL，相对路径自动拼 base）。校验：scene/product 合法性、产品-场景兼容（STATION_RAIN 仅实况、GRID_RAIN 仅预报）、begin<end、跨度≤10天；FORECAST 未传 forecast_time 自动取最近 08/20 起报时次。
  - **路由口径**：分区面雨量/雨量分布图→AREA_RAIN；实况站点雨量图→STATION_RAIN（仅 REALTIME）；格点预报降水图→GRID_RAIN（仅 FORECAST）。与旧 `get_station_rainfall_real_img`（8001 端口 base64 工具）并存，docstring 已区分。
- **HTTP `images` 字段 + 答案展示**（chainlitexam/qa_http_api.py）：
  - `_build_image_payload(emitter, session, answer="")` 扩展：答案文本里的 markdown 图链 `![title](https://…)` 追加为外部 images 条目（`{name, url, mime:"image/png"}`）；本地文件元素逻辑不变。`_run_once` 已传 `answer=answer`。
  - **`_scrub` 图片代理 allowlist**：默认只放行 `${IMAGE_URL_ALLOW_HOSTS}`（env 可扩展，**authority 精确匹配**，非前缀匹配），放行 URL 先占位保护后还原，保证图链能展示；**其余内网 IP 照常脱敏**（安全边界不变）。⚠️ 改动了脱敏逻辑，若生产需要放行其它图片代理主机用 env 扩展，不要放宽默认；且改 `BASIN_DRAWING_API_BASE` 时必须同步扩展 `IMAGE_URL_ALLOW_HOSTS`（二者独立配置）。
- 测试：`tests/test_basin_drawing_tool.py`（areas 归一化/缓存/失败不缓存 + image body/时间规整/参数校验/相对路径拼接）、`tests/test_qa_http_images.py`（images 外部图链 + _scrub allowlist）。

### 历史日期 → 历史实况查询（2026-08-13）

客户反馈"查历史数据查不出来"（如"8月10号某某地方天气怎么样"）。三重根因 + 修复：

- **根因一（"号"字不识别）**：`_extract_explicit_query_dates` 的短格式正则在 `8月10号` 中只匹配到 `8月10`，`号` 被当分隔符吞掉 → **静默丢失日期**（连未来日期也丢）。修复：短格式加 `(?:日|号)`，并新增裸日正则解析 `10号`；`月|日|号|年` 占位消费 span 防重复匹配。**裸"N号"加后缀守卫** `_BARE_DAY_PLACE_SUFFIX`（楼馆室院栋房门…教学医病办…）：`3号教学楼/5号病房/2号院` 等门牌/建筑编号不当日期（第/数字前置守卫防台风编号）。
- **根因二（过去日期回退未来预报）**：`8月10日`（今天 8/13）被推送下一年 → 240h 错误；`2025年8月10日` 被当 reforecast 服务返回"历史日期的未来预报"。修复：`query_rolling_forecast_core` 对过去日历日返回结构化 `past_date` 标记（`status:"past_date"`、`query_mode:"historical_obs_request"`、`historical_window:{target_start,target_end}`），不再静默回退未来预报、不再抛 240h 原始异常；远期未来（>240h）返回 `out_of_range` 结构化提示。
- **根因三（无历史实况工具）**：原先完全没有历史实况查询工具（`get_station_history`/`query_time_range` 为注释占位；`historical_same_period_rainfall_tool` 是十年平均非实际天气）。新增 MCP 工具 **`query_poi_historical_weather`**（custom_tools/historical_weather_service.py），从 MUSIC `SURF_CHM_MUL_HOR` 聚合自动站逐时观测（BJT 02/08/14/20 四个时次）生成与预报 `periods` 兼容的每日行（weather/tmax/tmin/EDA/rain/visibility），`data_source="自动站历史实况"`。最近 `MAX_HISTORICAL_DAYS=10` 天。
- **决策天气双入口接线**：`query_decision_weather_for_poi`（planner 工具）+ `DecisionWeatherQAService`（fast path）在 forecast 调用后检测 `_is_past_date_forecast_payload`，命中则调 `query_poi_historical_weather`（参数经 core 辅助 `_decision_historical_window_args` 拼装，解包+生成走 `_generate_decision_historical_answer_from_raw`，双路径共用零重复），走 `_generate_decision_historical_answer`（复用 `_generate_decision_weather_answer`，标题 `【X历史实况】`、prompt 强制"实况/当日实际"措辞，禁止"预计/将/未来"）。
- **历史实况同样追加"注意事项"**（2026-08-13 增强）：历史分支先 `classify_poi_category` + 共享辅助 `_decision_fetch_hazard_context`（双入口各自 lambda 传 invoke 方式，预报/历史两分支共用；无类别/工具缺失/失败静默跳过），经 `_generate_decision_historical_answer(..., poi_category=, hazard_points=)` 透传进 facts；`_build_poi_reminder_section` 按 `_decision_is_historical_facts(facts)` 走历史式措辞（"当日实际有降雨/当日累计降雨约 X 毫米/当日实际降雨约 X 毫米（强度），周边灾害风险研判如下"），预报才用"预计/未来"；`_decision_historical_day_label` 按窗口天数判定（end 为次日零点排他边界，≤1 天判单日→"当日"，多日→"该时段"）。
- **日期解析规则（2026-08-14 修订）**：无年份日期一律按当前日历解释——今年/当月已发生 = 今年历史实况，未发生 = 预报。**删除了原 15 天规则**（≤15 天保持当年、>15 天推明年）：推明年/下月对 240h 时效的滚动预报永远无法回答，导致"7月11日"这类同一年已过去 >15 天日期被推成 2027-07-11 → 240h 越界 → "暂无具体天气预报信息"。完整年份严格按该年解析。`昨天/前天` 直接映射最近两天（max 1 天窗口）。
- **orchestrator 历史直调分支**（2026-08-14）：planner 直调 `query_poi_historical_weather`（未走 `query_decision_weather_for_poi`）时，`_run_tool_round` 用同一历史格式化器组装回答并追加隐患点注意事项（`classify_poi_category` + 自调 `query_poi_hazard_reminders` + `_generate_decision_historical_answer` → forced_final_text 收口），保证无论 planner 选哪个工具，历史回答格式统一、都带注意事项。`_run_tool_round` 新增 `answer_chain=None` 参数（默认 None 向后兼容，无 answer_chain 时回退原 else 分支）。
- **零编造约束（review 修复）**：某日所有观测时次均无降水要素（`rains` 空）→ `rainfall_mm=None`、天气现象 `"无降水数据"` 而非 `"无降雨"`（缺测≠实测 0mm）。**单日锚定站**：当日首个有数据的时次确定锚定站，后续时次优先取该站自身记录（`_station_id` 匹配），锚定站缺报才回退该时次最近站——防 tmax/tmin/累计雨量由多台混拼。`no_data` 消息日期从 `start_time` 回退（真实工具 no_data 分支无 `forecast_start_time`）。
- **槽位剥离**：`_extract_decision_slots_rule_based` 头部剥离 `昨天|前天|昨日`，并清理前导虚词/量词残留（`^[的了是有在从到给为想问看这那份去]+`）——"8月10号天津大学/昨天天津大学/前天的天津大学/10月份去天津大学"均得位置名"天津大学"。

关键文件：`haihe-weather-analyzer-mcp/rolling_forecast_service.py`、`haihe-weather-analyzer-mcp/custom_tools/historical_weather_service.py`、`chainlitexam/tools/decision_weather_core.py`、`chainlitexam/tools/decision_weather.py`、`chainlitexam/tools/decision_weather_fast_path.py`、`chainlitexam/message_orchestrator.py`、`chainlitexam/prompts.py`。

测试：`haihe-weather-analyzer-mcp/tests/test_rolling_forecast_past_date.py`（28）+ `test_historical_weather_service.py`、`chainlitexam/tests/test_decision_weather_tool.py`（44 含历史路由 + 历史式注意事项 + fast-path parity）。