# Findings & Decisions: 暴雨影响河流代码质量全面审计

**PLAN_ID:** 2026-07-15-rain-impact-quality-audit  
**日期:** 2026-07-15

## Requirements
- 消除输出 GeoJSON 中的重复河段、遗漏下游、孤立河段等异常
- 提升 `rainfall_impact_geojson.py` 的逻辑清晰度与可维护性
- 确保修复不会破坏已有的 fast path / planner 调用链

## Research Findings

### 用户提供的样本分析
文件：`E:\fsdownload\rain_impact_result.json.river.geojson`

- 总河段数：138
- 按类型：direct_buffer=108，downstream_50km=30
- 重复 objectid 数量：34 个
  - objectid=91（北运河）出现 9 段：6 direct + 3 downstream
  - objectid=70（永定河）出现 6 段：2 direct + 4 downstream（含 3.408 km 孤立短段）
  - objectid=19（滦河）出现 6 段：全为 direct
- 有直接影响但无下游的 objectid：57 个
- 仅有下游无直接影响的 objectid：8 个

### 代码审查结论（并行 Agent 分析）

**重复河段来源：**
1. `_query_direct_rows` 用 `ST_Dump` 拆分 `MULTILINESTRING` 后按 `geom` GROUP BY，同一 objectid 的多个不连通子段全部保留（置信度 95）。
2. `_query_downstream_rows` 一个 objectid 可能对应多个 pkl edge，每个 edge 返回一段，产生多个 downstream 要素（置信度 90）。
3. `_build_river_geojson` 的 `_feature_geometry_key` 仅对 `(objectid, 完整 geometry json)` 去重，几何不同则保留（置信度 85）。

**下游缺失来源：**
1. `_query_downstream_rows` 的 `NOT EXISTS (SELECT 1 FROM tmp_rain24h_impact_stations s WHERE ST_DWithin(p.geom, s.geom, buffer))` 会丢弃任何重新进入站点 30 km 缓冲区的下游段（置信度 95）。
2. `GeometryType(line_geom) = 'LINESTRING'` 过滤掉 `ST_LineMerge` 后仍为 `MULTILINESTRING` 的河段（置信度 85）。
3. `match_distance_km <= station_buffer_km` 的直线距离阈值对弯曲/长河流可能过严（置信度 70）。

**孤立河段来源：**
1. `_find_direct_graph_starts` 把站点 30 km 内所有 pkl 边作为起点，即使该边未命中真实直接河段（置信度 90）。
2. `_query_downstream_rows` 的 `ROW_NUMBER()` 按距离选最近子段，可能选中与主河道不连通的短小分支（置信度 85）。
3. `_query_direct_rows` 的 `ST_Dump` 把不连通子段全部作为 direct_buffer 输出（置信度 80）。

## Root Cause

核心根因是 **pkl 图拓扑与 full_v6 表几何在 objectid 级别不是一一对应**：
- full_v6 中同一 `objectid` 的 `geom` 常为 `MULTILINESTRING`，包含主河道和若干不连通/短小分支；
- pkl 图按拓扑把河流切成多个 edge，每个 edge 通过 `objectid` 回查 full_v6；
- 当前代码在回查时按 `objectid` 匹配并选最近子段，无法保证选中的子段与 pkl edge 在拓扑/几何上真正连通；
- 同时 `NOT EXISTS` 站点缓冲区过滤又额外删除了一部分本应显示的下游段。

这导致三类现象：
- **重复**：同一 objectid 的多段都被保留；
- **下游缺失**：下游段回入站点缓冲区被过滤，或被 `LINESTRING` 过滤丢弃；
- **孤立**：最近子段选择策略选中不连通分支。

## Fix Strategy

1. 移除 `_query_downstream_rows` 中基于站点缓冲区的 `NOT EXISTS` 全局过滤，改为依赖 `_drop_downstream_covered_by_direct` 几何去重；避免误删合法下游段。
2. 直接使用 `river_parts`  dumped 后的 `LINESTRING` 子段，不再 `ST_LineMerge` 后二次拆分。
3. 在 `_query_downstream_rows` 中优先选择能覆盖 pkl edge `to` 节点（下游起点）的几何子段，并根据 `from`/`to` 在线上的相对位置判断裁剪方向；当 `from` 不在数据库几何上时，默认向 `to_frac` 增加方向裁剪，保证下游段起点与上游直接段相连。
4. 对 `_query_direct_rows` 的直接河段，同一 objectid 内合并为 `MULTILINESTRING`，减少重复。
5. 在 `_build_river_geojson` 中增强去重：同一 objectid 的下游段若被直接河段几何覆盖（90% 重叠比例）则丢弃。

## Verification
- 内网重新运行 `utils/test_rain_impact_internal.py` 生成新的 `rain_impact_result.json.river.geojson`
- 重复 objectid 数量应显著下降
- 之前无下游的直接影响 objectid 应部分恢复下游
- 下游段起点应尽可能与直接河段终点相接
- 单元测试 `utils/tests/test_rainfall_impact_geojson.py` 全部通过

## Technical Decisions
| Decision | Rationale |
|----------|-----------|
| 优先移除 `NOT EXISTS` 全局过滤 | 该过滤删除的是“地理上经过站点缓冲区”的下游段，与“是否已用直接河段表达”是不同语义；几何去重已在后处理中处理 |
| 以 `to` 节点为下游裁剪固定起点 | `to` 是 pkl 图边的下游起点，数据库几何通常包含该点；`from` 可能不在数据库几何上，不应作为裁剪锚点 |
| 不强制要求 full_v6 数据清洗 | 数据对齐是长期工作，代码层先通过更智能的子段选择和方向判断降低异常输出 |
| 保留 `is_luan` 用于名称映射，不用于 JOIN 过滤 | 避免非滦河河流因 `is_luan` 不一致被错误匹配 |
| 90% 重叠比例作为 downstream/direct 去重阈值 | `covers` 要求完全覆盖过于严格，90% 重叠可在保留有效延伸段的同时删除明显重复段 |

## Resources
- `hhlyqyxt-master/utils/rainfall_impact_geojson.py`
- `E:\fsdownload\rain_impact_result.json.river.geojson`
