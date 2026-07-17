# Findings & Decisions: 暴雨区域GIS受影响河流显示缺失

**PLAN_ID:** 2026-07-13-rain-impact-rivers  
**日期:** 2026-07-13

## Requirements
- 牵引智能体应能根据暴雨区域正确计算并返回受影响的河流
- 十四所GIS应能根据牵引智能体返回的数据渲染受影响河流
- 问答智能体应能正确调用牵引智能体并展示受影响河流信息

## Research Findings
- 牵引智能体工具目录：`C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)\hhlyqyxt-master\utils`
- 问题示例：东边暴雨区域存在，但十四所GIS看不到受影响河流
- 核心算法文件：`utils/rainfall_impact_geojson.py`
- 关键函数：
  - `_query_direct_rows`：按暴雨站 30km 缓冲区查询真实河段（直接影响）
  - `_find_direct_graph_starts`：从真实河段匹配 pkl 拓扑边，作为下游 50km 追踪起点
  - `_collect_downstream_edges`：沿 pkl 有向图向下游追踪
  - `_query_downstream_rows`：将下游边回 full_v5 表匹配并截断
- 代码历史关键提交：
  - `a1112b5`：将下游追踪起点从「站点 30km 内 pkl 边」收紧为「必须匹配真实直接河段」
  - `e52d74f`：增加 `direct_station_top_n`、去重 key 改为 `(objectid, geometry)`、`direct_match_km` 默认 3km
- 评审版测试脚本 `test_rainfall_impact_geojson_db_local.py` 仍采用「站点 30km 内 pkl 边」作为下游起点，且 `direct_graph_match_km` 默认 15km

## Root Cause
`_find_direct_graph_starts` 过于严格：
1. 只接受能同时匹配真实直接河段 objectid/name 且几何距离 ≤ `direct_match_km`（默认 3km）的 pkl 边；
2. 完全移除了「站点 30km 内 pkl 边」兜底起点。

在东部等区域，pkl 拓扑与 full_v5 真实河段在 objectid、名称或几何上可能存在 >3km 的偏差，导致匹配不到任何 pkl 起点，下游追踪为空。若同时真实直接河段也稀疏或空，则最终 GeoJSON 没有河流要素，十四所GIS无法显示受影响河流。

## Fix Applied
1. `utils/rainfall_impact_geojson.py`：
   - `_find_direct_graph_starts` 改为两阶段：先精确匹配真实直接河段，未命中时回退到暴雨站 30km 内 pkl 边；
   - 默认 `direct_match_km` 从 3km 放宽到 10km；
   - 返回 `downstream_start_stats` 诊断统计，便于排查精确匹配/兜底占比。
2. `utils/rainstorm_impact_map_service.py`：同步默认 `direct_match_km` 为 10km。
3. `haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py`：默认 `direct_graph_match_km` 改为 10km。
4. `chainlitexam/tools/rainfall_river_impact.py`：默认 `direct_graph_match_km` 改为 10km，更新 docstring。

## Verification
- 牵引智能体：`utils/tests/test_rainfall_impact_geojson.py` 5/5 通过
- 问答智能体：`chainlitexam/tests/` 50/50 通过（含新增 2 个 rainfall_river_impact 测试）
- Fast path 静态检查：`chainlitexam/tests/test_fast_paths.py` 18/18 通过
- 全部修改文件 `py_compile` 语法检查通过

## Post-Review Fixes Applied
1. **兜底边不再污染 `direct_keys`**：`rainfall_impact_geojson.py` 的 `_add_graph_start` 支持可选 `keys` 参数；阶段二兜底仅写入 `starts`，避免下游被误标为 `is_direct_graph_edge`。
2. **空响应结构一致**：`fixed_rainfall_impact_tool.py` 的 `_empty_response` 补齐 `start_stats`（含 `downstream_start_stats`）。
3. **`direct_part_match_km` 读取正确**：改从 `result["downstream_start_stats"]["direct_match_km"]` 读取，避免仅空结果含 `params` 时的默认值偏差。
4. **测试兼容 `_ToolWrapper` stub**：`test_rainfall_river_impact.py` 新增 `_call_tool` 辅助函数，同时兼容 decision_weather 测试安装的 `_ToolWrapper._fn` 与真实 `StructuredTool.func`。
5. **新增兜底隔离回归测试**：`test_fallback_starts_not_in_direct_keys` 确保兜底阶段不进入 `direct_keys`。
6. **内网测试反馈：非空结果缺少暴雨站点 JSON**：`build_rainstorm_impact_thematic_map` 的空结果 `_empty_result` 包含 `impact_stations` 和 `station_geojson`，但成功路径的 `result.update()` 遗漏了这两个字段；已补齐，使问答智能体 `fixed_rainfall_impact_tool.py` 能正确返回站点信息。

## Phase 9: New Findings from User Sample Data (2026-07-15)

User provided `E:\fsdownload\rain_impact_result.json` (and `.river.geojson`/`.station.geojson`) with concrete anomalies.

### Data Sample Summary
- Total river features: 144
- Direct buffer features: 63 (indices 0–62 plus a few others)
- Downstream 50km features: 47 (indices 63–144)
- 23 direct-buffer features and 1 downstream feature have `river_name == "未知"`
- 29 duplicate groups by `(objectid, name, start, end)` where name/start/end are null for direct-buffer rows

### Issue 1: Missing River Names (`未知`)
**Evidence:**
- idx=124 downstream feature has `objectid="2"`, `river_name="未知"`, but its `edge_key` ends with pkl graph name `牤牛河`.
- idx=91 direct-buffer feature has `objectid="2"` and its name (console-mojibake) matches the same pkl graph name.
- 22 other direct-buffer features have `river_name="未知"` (e.g., objectids 1, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 19, 21).

**Root Cause:**
- `_query_direct_rows` uses `COALESCE(NULLIF(TRIM(r.src_name::text), ''), '未知')` as the only name source.
- `_query_downstream_rows` uses `COALESCE(NULLIF(TRIM(db_river_name), ''), river_name)` where `river_name` is the pkl graph name. However, `db_river_name` already became the literal string `"未知"` in the `river_parts` CTE, so `NULLIF` does not trigger fallback to the pkl name.
- The `full_v6` table apparently has empty/missing `src_name` for many objectids, while the pkl graph (`river_directed_v6.pkl`) carries the correct name in `src_name`/`name`/`river_name` attributes.

### Issue 1b: Luan River System Single-Character Names
**Evidence:**
- User clarified that the Luan River system was merged later, so `full_v6.river_name` (or `src_name`) for those rivers is stored as a single character that should be read as `X河` (e.g., `青` → `青河`, `东` → `东河`).
- Re-inspection of the sample GeoJSON found one single-character name `东` (objectid=8, direct_buffer); in other datasets/user DB views, names like `青` appear for Luan tributaries.

**Root Cause:**
- The builder treats the database `river_name` value literally. It does not expand the Luan River convention of single-character abbreviations into full river names (`X河`).

### Issue 2: Duplicate / Isolated Segments (Yongding River example)
**Evidence:**
- `objectid="70"` / `永定河` appears 5 times:
  - idx=69: direct_buffer, length 144.971 km, triggered by Beijing-area stations
  - idx=70: direct_buffer, length 21.322 km, triggered by 武清崔黄口 (Tianjin area), 25.3 km from station
  - idx=128: downstream_50km, keep 22.308 km
  - idx=129: downstream_50km, keep 50.0 km, `is_direct_graph_edge=true`
  - idx=130: downstream_50km, keep 27.692 km, `is_direct_graph_edge=true`
- idx=70 is geographically disconnected from the main Yongding geometry (idx=69/128/129/130), producing an isolated segment.
- idx=128/130 together cover 0–50 km; idx=129 covers the downstream portion from a separate start node, creating redundant/overlapping-looking pieces of the same objectid.

**Root Cause Hypotheses:**
1. `_query_direct_rows` uses `ST_Dump(r.geom)` and returns every part of a MultiLineString as a separate feature. If the same `objectid` contains geographically disjoint parts (data quality issue in `full_v6`), each part is emitted, including spurious far-away pieces.
2. `_find_direct_graph_starts` adds **every** pkl edge within 30 km of a station as a downstream start (distance 0). On long rivers, multiple edges along the same river become independent starts; `_collect_downstream_edges` then traces 50 km downstream from each, producing clipped segments that can overlap or abut in confusing ways.
3. `_query_downstream_rows` clips each downstream edge independently and does not re-clip at the station-buffer boundary, so downstream segments can extend back into the 30 km direct-buffer zone, visually duplicating direct-buffer features.

### Issue 3: Console / File Encoding
The geojson file is UTF-8, but the Windows console prints Chinese names as mojibake. Saved JSON/TSV summaries confirm actual content is correct UTF-8; no code change needed for encoding.

## Known Out-of-Scope Issues
- `include_background` 参数在 MCP/本地工具中接受但未透传给 `build_rainstorm_impact_thematic_map`（历史遗留，本次未改动）。
- 兜底策略在阶段一完全空白时启用 30km 站点缓冲区，dense basin 仍可能产生较多起点；当前通过 10km 直接匹配优先降低触发概率，后续可继续根据 `downstream_start_stats` 调优。

## Technical Decisions
| Decision | Rationale |
|----------|-----------|
| 先修牵引智能体，再修问答智能体 | 根因在牵引侧，问答侧为调用/展示层 |
| 保留精确匹配优先，兜底仅在没有精确起点时启用 | 既避免 `a1112b5` 想解决的「周边不相连河系被起追」问题，又能在对齐差时给出结果 |
| `direct_match_km` 默认 10km | 3km 在真实数据对齐偏差下太严；30km 又太宽易引入无关河段；10km 是兼顾精度与容错的折中 |
| 返回 `downstream_start_stats` | 便于线上排查东部等区域是精确匹配还是兜底触发，后续可据此继续调优 |
| 兜底边不加入 `direct_keys` | 保持 `is_direct_graph_edge` 语义纯净：只有真实直接河段匹配到的 pkl 边才被视为直接图边 |

## Resources
- `hhlyqyxt-master/utils`（牵引智能体工具）
- `chainlitexam/message_orchestrator.py`（问答智能体编排层）
- `chainlitexam/chain_gzt.py`（问答智能体生命周期与工具加载）
