# Findings: Rain Impact River Logic Redesign

## Data Pipeline Findings

### v6 数据生成流程
- **脚本:** `E:\tj\line\build_v6_graph_and_import.py`
- **输入:** `E:\tj\line\result\luanhe_merged_v6_split.shp`（258 条 LineString）
- **输出:**
  - `river_directed_v6.pkl`：252 节点 / 258 边
  - `haihe_river_directed_simple_v6`：258 行，每行一条直线
  - `haihe_river_directed_full_v6`：258 行，每行一个 `MultiLineString`（实际只含一条 LineString）

### 关键结论
**`full_v6` 的每一行正好对应 pkl 图的一条边。** 当前代码用 `ST_Dump(geom)` 再按 `objectid` 聚合，把同一 `objectid` 下的多条合法 pkl 边揉在一起，是产生后续对齐问题的根源。

### 属性字段
- `name` / `src_name` / `river_name` 来自 shapefile 属性
- `OBJECTID` 作为整数 objectid
- `is_luan` 标记滦河系（34 行 true）
- `from_x`, `from_y`, `to_x`, `to_y` 记录边的起点/终点
- `len_m`, `len_km` 记录边长

## Current Code Issues

1. **`_query_direct_rows` 使用 `ST_Dump` 和 `GROUP BY objectid`**
   - 把多条 pkl 边合并成一个 MultiLineString
   - 丢失了与 pkl 边的一一对应关系

2. **`_query_downstream_rows` 复杂的 part 匹配逻辑**
   - 试图在聚合后的几何中找到匹配 pkl 边的子段
   - 引入 `match_distance_km` 过滤、fallback 直线、Shapely 去重等补丁

3. **Luan 名称处理过于复杂**
   - 需要在多个阶段（direct/downstream/enrichment）处理单字名称

4. **下游去重依赖 Shapely 可选依赖**
   - 代码需要处理有/无 Shapely 两种情况

## Proposed Solution

以 pkl 边为原子单元，`full_v6` 行为几何/属性 lookup 表。直接查询 `full_v6` 行，不做 Dump/聚合，按 `(objectid, from_x, from_y, to_x, to_y)` 匹配 pkl 边。

## Validation Criteria

- `river_name="未知"` 数量趋近于 0
- 无重复 `edge_key`
- 无孤立河段（所有下游段都与 direct 边在图拓扑上连通）
- 下游距离累计准确
- 测试全部通过
