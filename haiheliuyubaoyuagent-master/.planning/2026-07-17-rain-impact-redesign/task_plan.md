# Task Plan: Rain Impact River Logic Redesign (v6)

**PLAN_ID:** 2026-07-17-rain-impact-redesign  
**创建时间:** 2026-07-17  
**Goal:** 重新设计暴雨影响河流算法，以 pkl 有向图为拓扑权威，`full_v6` 为几何/属性 lookup 表，解决未知名称、重复、孤立、下游断裂等问题。

## Current Phase
Phase 6: Documentation & Memory (完成)

## Phases

### Phase 1: Requirements & Design Review
- [x] 确认数据生成流程：`build_v6_graph_and_import.py` 生成 258 行 full_v6，每行对应一条 pkl 边
- [x] 编写设计文档 `docs/superpowers/specs/2026-07-17-rain-impact-redesign-design.md`
- [x] 用户审查并批准设计文档
- **Status:** completed

### Phase 2: Planning
- [x] 创建 task_plan.md / findings.md / progress.md
- [x] 明确需要修改的文件和接口
- [x] 列出测试用例
- **Status:** completed

### Phase 3: Implementation
- [x] 重构 `rainfall_impact_geojson.py`：
  - [x] 删除 `ST_Dump` / `GROUP BY objectid` 的直接河段查询
  - [x] 新增 full_v6 行 lookup（按 objectid + 端点，6 位小数取整）
  - [x] 重写分类为基于 SQL 真实几何距离（弦距兜底）
  - [x] 新增 `_fetch_missing_edge_rows` 为下游边补查 full_v6 几何
  - [x] `_save_downstream_edge` 跳过 direct_keys，消除重复输出
  - [x] 纯 Python 方向感知裁剪 `_clip_geometry_to_keep_km`，移除 Shapely 依赖
  - [x] 简化 Luan 名称映射（单字或全部失败时才用映射，不覆盖全名）
  - [x] 删除 Shapely 可选去重、`_query_downstream_rows`、`_find_direct_graph_starts`、`_river_feature`、`_apply_luan_names`、`_clip_linestring_to_fraction` 等死代码
- [x] `fixed_rainfall_impact_tool.py` 的 `IMPACT_RULES` 和参数透传无需算法改动（已对齐）
- [x] `emergency_response_monitor.py` 兄弟读取器 `aggregate_5min_station_pre_to_24h` 空 CSV 兜底
- [x] 移除 `direct_station_top_n` 静默 no-op 参数
- **Status:** completed

### Phase 4: Testing & Verification
- [x] 运行 `utils/tests/test_rainfall_impact_geojson.py`：21/21 通过（含 9 个新增覆盖修复点的测试）
- [x] 运行 `chainlitexam/tests/test_fast_paths.py`：19/19 通过
- [x] 运行 `chainlitexam` 完整 pytest：51/51 通过
- [ ] 使用内网样本数据做端到端验证（待部署环境验证）
- **Status:** completed（单测层；端到端待部署）

### Phase 5: Code Review & Simplification
- [x] code-review 7 个 finder 角度并行审查（3 正确性 + 3 清理 + 1 altitude）
- [x] 4 组 verifier 并行验证，13 个候选 → 12 CONFIRMED + 1 PLAUSIBLE
- [x] 修复全部 CONFIRMED 项（下游几何、连接泄漏、重复输出、弦距分类、裁剪方向、len_km、Luan 优先级、列校验、死代码、no-op 参数、空 CSV）
- [x] code-simplifier：净减 156 行（删除 ~300 行死代码 + Shapely 依赖）
- **Status:** completed

### Phase 6: Documentation & Memory
- [x] 更新 CLAUDE.md 算法不变式段落（设计原则、算法不变式、下游几何、去重、裁剪、名称优先级、schema 校验、连接生命周期、空 CSV、已移除清单）
- [x] 更新 planning 文件（task_plan / progress / findings）
- [x] 更新 auto-memory（rain-impact-river-defaults / cleanup / name-fallback / data-alignment）
- [x] claude-mem 记录最终决策
- **Status:** completed

## Key Questions
1. 是否保留 `direct_match_km` 和 `station_buffer_km` 的语义？（保留）
2. `full_v6` 行 lookup 键使用 objectid + 4 个端点坐标是否足够唯一？（是，并加 6 位小数取整吸收精度漂移）
3. 下游边几何裁剪是否仍需要？（是，按 keep_km 比例裁剪，方向感知）
4. 是否需要保留 `downstream_start_stats`？（是，保持接口兼容）

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| 以 pkl 图为拓扑权威 | full_v6 每行对应一条 pkl 边，避免 objectid 聚合导致的问题 |
| full_v6 做空间预过滤 + SQL 真实几何距离分类 | 兼顾 PostGIS 空间索引和分类正确性（弦距会误判蜿蜒河段）|
| 删除 Shapely 依赖（去重 + 裁剪均改纯 Python）| pkl 边天然唯一无需去重；裁剪用 haversine 累计 + 方向判定即可 |
| 简化 Luan 名称映射 | 仅在单字缩写或全部来源失败时启用，不覆盖合法全名 |
| 移除 direct_station_top_n 参数 | 全仓库零调用方，且已是静默 no-op |
| 下游几何补查用 objectid = ANY(...) | 一次查询覆盖所有缺失下游边，按端点键精确匹配 |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| API token limit (262144) 中断 | 上一会话 finder/verifier 输出过大 | 拆分多轮，progress.md 记录断点 |
| `_apply_luan_names` 双重定义互相遮蔽 | code-review 发现 | 删除两处定义，逻辑合并进 `_pick_river_name` |
