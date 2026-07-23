# Progress: Rain Impact River Logic Redesign

## 2026-07-17

### 已完成
- 审查 `E:\tj\line\build_v6_graph_and_import.py`，确认 full_v6 与 pkl 边一一对应关系
- 确认当前 `rainfall_impact_geojson.py` 的问题根源：错误的 `ST_Dump` + `GROUP BY objectid`
- 编写设计文档：`docs/superpowers/specs/2026-07-17-rain-impact-redesign-design.md`
- 创建 planning 文件：`task_plan.md`、`findings.md`、`progress.md`
- Phase 3 实现：重构 `rainfall_impact_geojson.py`（删除 ST_Dump/GROUP BY、新增 `_fetch_missing_edge_rows`、纯 Python 方向感知裁剪、Luan 优先级修正、移除 `direct_station_top_n` no-op 参数、`aggregate_5min_station_pre_to_24h` 空 CSV 兜底）
- Phase 4 测试：单测 21/21、fast-path 19/19、chainlitexam pytest 51/51 全部通过
- Phase 5 code-review：7 finder 角度并行 → 4 verifier 并行 → 13 候选（12 CONFIRMED + 1 PLAUSIBLE）全部修复
- Phase 5 code-simplifier：净减 156 行（删除 ~300 行死代码 + Shapely 依赖）
- Phase 6 文档：更新 CLAUDE.md 算法不变式段落、planning 文件、auto-memory

### code-review 发现并修复的 12 个 CONFIRMED 问题
1. 下游边几何只查 30km 缓冲区候选行 → 30-80km 下游退化为直线 → 新增 `_fetch_missing_edge_rows` 按 objectid 补查
2. 同一条 pkl 边同时以 direct_buffer 和 downstream_50km 重复输出 → `_save_downstream_edge` 跳过 direct_keys
3. 距离分类用 pkl 端点弦距而非真实几何 → 改用 SQL `min_station_distance_km`，弦距兜底
4. 裁剪无方向感知 + Shapely 依赖 + MultiLineString 静默失败 → 纯 Python `_clip_geometry_to_keep_km`（最长 part + 方向判定 + haversine 累计）
5. `get_graph()` 在 `_open_connection()` 之后、try/finally 之前 → 泄漏连接 → 调整顺序
6. lookup 键 float 精确相等 + miss 静默 → 6 位小数取整 + 候选行未匹配计数告警
7. `_ensure_river_columns` 未校验新硬编码列 → 扩展 required 集合
8. `float(row.get("len_km"))` 无 None 守卫 → `_feature_length_km` 统一兜底
9. Luan 静态映射覆盖合法 src_name → 仅单字或全部失败时启用
10. 裁剪后 length_km 仍报全长 → 下游段报告 keep_km
11. ~500 行死代码（含 `_apply_luan_names` 双重定义、5 个测死路径的旧测试）→ 全部删除，测试改测 `_classify_graph_edges`
12. `aggregate_5min_station_pre_to_24h` 空 CSV 仍抛 EmptyDataError → 兜底返回空 DataFrame

### 待完成
- 部署环境用内网样本数据做端到端验证（`E:\fsdownload\rain_impact_result.json`）
- 视端到端结果决定是否调整 `direct_match_km` / `station_buffer_km` 默认值

### 验证结果
| 检查项 | 状态 |
|--------|------|
| 设计文档完成 | ✓ |
| 规划文件创建 | ✓ |
| 用户审批 | ✓ |
| 实现完成 | ✓ |
| 单测 28/28 | ✓ |
| fast-path 19/19 | ✓ |
| pytest 51/51 | ✓ |
| code-review 12 项修复 | ✓ |
| code-simplifier 净减 156 行 | ✓ |
| CLAUDE.md 更新 | ✓ |
| auto-memory 更新 | ✓ |
| claude-mem 记录 | ✓ |
| 端到端验证（首轮） | ⚠️ 发现 4 个生产环境问题 |

## 2026-07-17 调试轮次（端到端验证后发现的问题）

### 生产环境输出 `E:\fsdownload\rain_impact_result.json` 暴露的 4 个根因
1. **下游无上游**：`_classify_graph_edges` 只把 ≤10km 边放进 `direct_edges`，56 条 buffer-only 边（10-30km）只进 `buffer_only_keys`（仅统计），其中 44 条被当作 downstream_50km 距离 0 误标、12 条完全消失。
2. **直线河流 + nan 传播**：滦河系边（is_luan=True，34 条）的 `len_km=NaN`（生产 pkl `E:\tj\line\result\river_directed_v6.pkl`）。`get_edge_length_km` 返回 nan → `_save_downstream_edge` 的 `nan <= 0` 为 False 不触发早退 → keep_km/clip_fraction 全成 nan → Dijkstra 距离累积被 nan 污染 → 滦河系下游追踪失效。其中 2 条边还因端点方向不匹配退化为直线。
3. **MultiLineString 未解包**：48 个 direct 特征都是单 part 的 MultiLineString，前端可能按直线弦渲染。
4. **方向敏感 lookup**：`_edge_lookup_key` 顺序敏感，pkl 流向与 full_v6 数字化方向不一致时匹配失败。

### 修复
- `_classify_graph_edges`：所有候选边（direct + buffer-only）都进 `direct_edges`，用 `is_direct_graph_edge` 区分 ≤10km 与 10-30km；`direct_keys = set(direct_edges)` 包含 buffer-only 边，`_save_downstream_edge` 跳过它们（遍历继续），消除重复和缺口。
- `get_edge_length_km`：新增 `from_xy`/`to_xy` 关键字参数；属性值非有限或缺失时回退到 haversine(from, to)；`_save_downstream_edge` 守卫改为 `not (length_km > 0)` 同时捕获 nan 和 ≤0。
- `_build_edge_lookup`：每行同时按 (objectid, from, to) 和 (objectid, to, from) 两个方向建索引（`setdefault`）；`_fetch_missing_edge_rows` 和 `_resolve_edge_features` 检查双向。
- `_unwrap_geometry`：单 part MultiLineString 解包为 LineString。

### 生产 pkl 验证
- 34 条滦河边长度全部有限（haversine 兜底），0 条仍 nan
- 滦河 oid=19 边：keep_km=42.01、clip_fraction=1.0（不再 nan）
- 方向无关 lookup 对反向 DB 行命中

### code-review 结论
聚焦审查 4 个修复：无 blocker/major。唯一 minor：`_fetch_missing_edge_rows` 的 `unmatched // 2` 在两条 pkl 边共享 objectid 且端点互换的罕见场景下少计（仅影响告警计数，不影响数据正确性）。

### 最终验证
| 检查项 | 状态 |
|--------|------|
| 单测 32/32（含 4 个空间兜底测试） | ✓ |
| fast-path 19/19 | ✓ |
| pytest 51/51 | ✓ |
| 生产 pkl 4 条问题边空间匹配验证 | ✓ |
| code-review 无 blocker | ✓ |
| 端到端复测 | ⏳ 待用户重新部署下载 |

## 2026-07-17 第三轮调试（滦河系"不存在的直线"）

### 根因
部署后日志：`full_v6 候选行 112 条未能匹配到 pkl 边` + 4 条 `pkl_edge_straight_fallback`（objectid 21/19/1/19，均在滦河系 (118,40) 区域）。排除假设：节点坐标 vs attr 坐标在 6 位小数完全一致（258/258 匹配）。真正原因：**full_v6 表的 `from_x`/`from_y`/`to_x`/`to_y` 与 pkl attr 的对应值不一致**（DB 可能存了几何端点 ST_StartPoint/ST_EndPoint 而非 shapefile 属性值，或精度不同），导致精确端点键系统性失配。

### 修复
新增 `_match_edge_spatially`：在同 objectid 候选行中找几何同时经过 pkl from/to 两个端点（100m 容忍，`_point_to_lines_km`）的行。匹配链：精确端点键 → 反向端点键 → 空间兜底。应用于 `_classify_graph_edges`、`_resolve_edge_features`、`_fetch_missing_edge_rows`。

### 验证
- 4 条问题边用 mock 几何（经过 pkl 端点但 from_x/from_y 故意写错）全部空间匹配成功
- 32/32 单测、19/19 fast-path、51/51 pytest 全通过

## 2026-07-17 第四轮：问答智能体集成核查 + IMPACT_RULES 修正

### 集成链路确认
问答智能体（Chainlit）通过两条路径汇入核心算法 `build_rainstorm_impact_thematic_map`：
1. **Fast path** `message_orchestrator._try_affected_river_network_by_rainfall_fast_path` → `_find_tool(tools, "get_affected_river_network_by_rainfall")` → MCP SSE 工具 → `fixed_rainfall_impact_tool.build_affected_river_network_result` → 核心
2. **Planner** LLM → 本地工具 `local_get_affected_river_network_by_rainfall`（`chainlitexam/tools/rainfall_river_impact.py`）→ 同一 `build_affected_river_network_result` → 核心

签名核查：所有调用方（`fixed_rainfall_impact_tool.py`、`rainfall_river_impact.py`、`rainstorm_impact_map_service.py`、`test_rain_impact_internal.py`）均不传已移除的 `direct_station_top_n` 参数。`get_edge_length_km` 签名变更（加 `from_xy`/`to_xy` 关键字参数）不影响外部调用方（MCP `tools.py` 和 `monitorservice.py` 有各自本地定义）。

### 发现并修复：IMPACT_RULES 文本过时
`fixed_rainfall_impact_tool.IMPACT_RULES` 描述的是旧算法（ST_Dump、match_distance_km 过滤、Shapely 去重、pkl 图名称回填），这些机制已在前几轮全部移除。规则文本会返回给 LLM 和用户，造成误导。已更新全部 7 条规则文本以反映真实算法（三级匹配、结构去重、_pick_river_name 优先级链、纯 Python 裁剪），保持 API 键兼容。

### 验证
- 32/32 单测、19/19 fast-path、51/51 pytest 全通过
- IMPACT_RULES 7 个键完整（direct/downstream/direction/dedupe/name_fallback/match_filter/downstream_dedupe），无 ST_Dump 引用
- `server.py:78` 工具描述（"30km直接不截断，下游50km截断"）仍准确，无需改动
