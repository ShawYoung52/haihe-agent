# Progress Log: 暴雨区域GIS受影响河流显示缺失

**PLAN_ID:** 2026-07-13-rain-impact-rivers  
**会话日期:** 2026-07-13

## Session: 2026-07-13

### Current Status
- **Phase:** 9 - Downstream Tracking Distance Investigation（初步分析输出数据，等待用户确认具体问题点）
- **Started:** 2026-07-13

### Actions Taken
- 读取历史 planning 文件，确认上一任务（M3 / Chainlit 思考折叠）已完成
- 初始化新 isolated plan：`2026-07-13-rain-impact-rivers`
- 调用 `superpowers:systematic-debugging` 与 `planning-with-files` 技能
- 阅读牵引智能体核心文件 `utils/rainfall_impact_geojson.py`、`utils/rainstorm_impact_map_service.py`、`utils/river_city_impact_tool.py`
- 阅读问答智能体调用侧 `chainlitexam/tools/rainfall_river_impact.py`、`chainlitexam/fast_paths/rainstorm_impact_time_fast_path.py`、`haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py`
- 通过 git log 定位关键变更：`a1112b5` 收紧下游起点、`e52d74f` 增加 `direct_station_top_n` 与 3km 匹配
- 形成根因假设：`_find_direct_graph_starts` 过于严格，东部 pkl/full_v5 对齐偏差导致无下游起点，直接河段也可能稀疏，最终 GIS 无河流
- **修复牵引智能体：**
  - `utils/rainfall_impact_geojson.py`：`_find_direct_graph_starts` 改为两阶段（精确匹配 + 站点 30km 兜底），返回诊断统计；默认 `direct_match_km` 从 3km 放宽到 10km
  - `utils/rainstorm_impact_map_service.py`：同步默认 `direct_match_km` 为 10km
  - 新增单元测试 `utils/tests/test_rainfall_impact_geojson.py`，覆盖精确匹配、兜底、空白三种场景
- **修复问答智能体集成：**
  - `haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py`：默认 `direct_graph_match_km` 改为 10km
  - `chainlitexam/tools/rainfall_river_impact.py`：默认 `direct_graph_match_km` 改为 10km，更新 docstring
  - 新增单元测试 `chainlitexam/tests/test_rainfall_river_impact.py`，验证默认/自定义参数透传
- **验证结果：**
  - 牵引智能体：`utils/tests/test_rainfall_impact_geojson.py` 4/4 通过
  - 问答智能体：`chainlitexam/tests` 50/50 通过（含新增 2 个测试）
  - 全部修改文件 `py_compile` 语法检查通过
- **code-review 发现及处理（本轮）：**
  - [x] `_empty_result` 未包含 `direct_match_km` 和 `downstream_start_stats` → 已补充，保持返回结构一致
  - [x] `station_buffer_fallback_edge_count` 原按 `len(starts)` 计节点而非边 → 已改为独立计数 `fallback_edge_count`
  - [x] MCP 响应未暴露 `downstream_start_stats` 诊断信息 → 已在 `start_stats` 中加入
  - [x] 确认 `_find_direct_graph_starts` 新三元素返回在唯一调用处正确解包；无调用方崩溃
  - [ ] `include_background` 参数在 MCP/本地工具中接受但未透传给 builder（历史遗留，超出本次范围）
- **code-review 本轮新发现并修复：**
  - [x] 兜底边被加入 `direct_keys`，可能被下游误标为 `is_direct_graph_edge` → `_add_graph_start` 增加可选 `keys` 参数，兜底阶段传入 `None`
  - [x] `_empty_response` 未包含 `start_stats`，与 `_format_mcp_response` 结构不一致 → 已补齐
  - [x] `_format_mcp_response` 中 `direct_part_match_km` 误从 `result["params"]` 读取（仅空结果含 `params`）→ 改从 `result["downstream_start_stats"]["direct_match_km"]` 读取
  - [x] `chainlitexam/tests/test_rainfall_river_impact.py` 使用 `tool._fn` 在真实 `StructuredTool` 上不存在 → 改为 `tool.func`
  - [x] 新增测试 `test_fallback_starts_not_in_direct_keys` 覆盖兜底边隔离

## Phase 8: Final Verification & Documentation
- **Status:** complete
- **Started:** 2026-07-13
- Actions taken:
  - 使用 `superpowers:verification-before-completion` 完成最终验证：
    - `hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py`：5/5 通过
    - `chainlitexam/tests/` 完整测试套件：50/50 通过
    - `chainlitexam/tests/test_fast_paths.py`：18/18 通过
    - 全部修改文件 `py_compile` 语法检查通过
  - 修复全量测试发现的新问题：
    - `test_rainfall_river_impact.py` 在 decision_weather stub 环境下 `tool.func` 不存在 → 增加 `_call_tool` 辅助函数同时兼容 `_ToolWrapper._fn` 与真实 `StructuredTool.func`
  - 用户内网测试反馈：非空结果缺少 `impact_stations` / `station_geojson` → 在 `build_rainstorm_impact_thematic_map` 成功路径的 `result.update()` 中补齐这两个字段，保持与 `_empty_result` 结构一致
  - 更新内网测试脚本 `utils/test_rain_impact_internal.py`：输出中增加 `impact_stations` 与 `station_geojson`
  - 更新 `CLAUDE.md`：添加暴雨影响河流关键文件与跨仓库同步提醒
  - 更新 `claude-mem`：写入 `rain-impact-river-defaults.md` 并更新 `MEMORY.md` 索引

## Phase 9: Downstream Tracking Distance Investigation
- **Status:** complete
- **Started:** 2026-07-13
- **Continued:** 2026-07-15
- **Finalized:** 2026-07-15
- User concern: 下游追踪逻辑中，若已追踪 20km，下一条应只剩 30km 额度，而不是继续按 50km 计算
- 2026-07-15 user provided concrete sample files at `E:/fsdownload/rain_impact_result.json*`:
  - Analyzed 144 river features; found 23+1 `river_name="未知"` features and 29 duplicate objectid groups.
  - Confirmed Yongding River (`objectid="70"`) appears 5 times: 2 direct-buffer parts (one disconnected near Tianjin) + 3 downstream clipped pieces.
  - Confirmed `objectid="2"` downstream feature lacks name; pkl graph carries `牤牛河` in edge_key.
- **Fixes implemented:**
  - `hhlyqyxt-master/utils/rainfall_impact_geojson.py`:
    - 用 pkl 图按 objectid 建立名称映射，回填 direct/downstream 中 `river_name="未知"` 的要素。
    - `_query_downstream_rows` SQL 中当 `db_river_name` 为 `"未知"` 时回退到 pkl 图名称。
    - 下游匹配增加 `match_distance_km <= station_buffer_km` 过滤，剔除 pkl/full_v6 对齐偏差导致的远距离误匹配。
    - 增加 Shapely 可选依赖：若下游河段几何被同 objectid 直接河段覆盖，则剔除。
    - `direct_rivers`/`downstream_rivers` 改为从最终 GeoJSON 要素汇总，保证名称回填后一致。
    - **2026-07-15 补充**：增加 `_normalize_river_name`，将单字河系名（如滦河系的“青”“东”）规范化为“X河”。
    - **收尾补充**：增加模块 logger；graph-load/geometry 失败时记录日志而不是静默吞掉；名称映射按需构建；无下游段时跳过 Shapely 工作。
  - `haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py`:
    - 更新 `IMPACT_RULES`，补充 name_fallback、match_filter、downstream_dedupe 说明。
    - **收尾补充**：`_resolve_graph_path` 处理目录路径和空文件名，避免 `Path.with_name` 抛 `ValueError`；`IMPACT_RULES` 使用 `RIVER_TABLE_VERSION` 常量，不再硬编码 `full_v6`；`_format_mcp_response` 对 `river_summary=None` 做防御。
  - `hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py`:
    - 新增 5 个单元测试覆盖名称映射、名称回填、下游去重、汇总名称过滤、整体 GeoJSON 构建。
    - **收尾补充**：Shapely 依赖的测试使用 `pytest.importorskip("shapely")` 跳过。
- **Verification:**
  - 对用户样本 GeoJSON 运行修复逻辑：`river_name="未知"` 从 23 降至 0。
  - 牵引智能体测试：`utils/tests/test_rainfall_impact_geojson.py` 12/12 通过。
  - 问答智能体完整测试：`chainlitexam/tests/` 51/51 通过。
  - Fast path 静态检查：`chainlitexam/tests/test_fast_paths.py` 18/18 通过。
  - 全部修改文件 `py_compile` 语法检查通过。
- **Remaining limitations:**
  - Yongding `objectid="70"` 仍出现 5 次：其中天津附近 21.322 km 段与主河段几何不连通，是 full_v6 表同一 objectid 包含远距 MultiLineString 部分的数据质量问题；下游三段与直接段之间也存在几何间隙，源于 pkl 拓扑与 full_v6 几何对齐偏差。此类“孤立/重复感”需通过数据对齐或拓扑合并进一步解决，本次仅做名称回填与误匹配过滤。
- **Next:** 无。计划已关闭。

### Errors
| Error | Resolution |
|-------|------------|
| 牵引智能体测试环境缺少 pandas/psycopg2 | 在测试文件顶部用 `types.ModuleType` 做最小 stub |
| 问答智能体测试工具 wrapper 为 async stub | 使用 `_call_tool` 辅助函数兼容 `_ToolWrapper._fn` 与 `StructuredTool.func` |
| 首次 Write 文件落到 `chainlitexam/chainlitexam/tests` | 移动到正确位置并清理嵌套目录 |
| 2026-07-15 内网 `AmbiguousColumn` | `_query_downstream_rows` 中 `SELECT e.*, p.is_luan` 导致 `is_luan` 歧义；已改为显式列选择。 |
| 2026-07-15 内网 objectid=70 异常短河段 | 下游匹配按 `match_distance_km` 只选最近段，导致同一 objectid 的孤立短段被选中；已改为优先选择长度能容纳 `keep_km` 的河段，再按距离排序。 |

## 5-Question Reboot Check
| Question | Answer |
|----------|--------|
| Where am I? | Phase 8 已完成，但用户对内网测试结果的下游追踪逻辑提出新疑问，需进一步调查 |
| Where am I going? | 分析实际输出数据，定位用户感知的下游追踪问题，必要时修复并重新验证 |
| What's the goal? | 让东部等暴雨区域能在十四所GIS正确显示受影响河流 |
| What have you learned? | 下游起点策略从「站点30km」收紧为「真实河段3km匹配」后，对齐差区域会空白；10km + 站点兜底可恢复；兜底边必须与 direct_keys 隔离；全量测试要考虑 decision_weather stub 对 @tool 的污染；需确认用户对下游累计距离语义的理解 |
| What have you done? | 完成牵引/问答两侧修复、code-review、code-simplifier、最终验证（55+18 测试通过），更新 CLAUDE.md 与 claude-mem；已读取内网输出并初步分析下游距离字段，暂未发现 end > 50 或 keep 异常 |
