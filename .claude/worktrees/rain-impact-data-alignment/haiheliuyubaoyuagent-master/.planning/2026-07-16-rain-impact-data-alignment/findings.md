# Findings & Decisions: 暴雨影响河流数据对齐（pkl/full_v6）

**PLAN_ID:** 2026-07-16-rain-impact-data-alignment  
**日期:** 2026-07-16

## Requirements
- 从数据侧消除 rain impact 输出中的孤立河段、下游断裂、河段跳跃
- 在 `haihe_river_directed_full_v6` 清洗 与 `river_directed_v6.pkl` 重生成 两种方案中选择最优路径
- 确保所选方案与现有 `rainfall_impact_geojson.py` 及 MCP 调用链兼容

## Research Findings

### 历史审计结论（2026-07-15）
- 核心根因：**pkl 图拓扑与 full_v6 表几何在 objectid 级别不是一一对应**
- full_v6 中同一 `objectid` 的 `geom` 常为 `MULTILINESTRING`，包含主河道和若干不连通/短小分支
- pkl 图按拓扑把河流切成多个 edge，每个 edge 通过 `objectid` 回查 full_v6
- 代码层已通过子段选择、方向判断、几何去重等手段大幅缓解，但用户反馈仍残留部分异常

### 当前样本分析（E:\fsdownload\rain_impact_result.json.river.geojson）
- 总河段数：**104**
  - `direct_buffer`: 72
  - `downstream_50km`: 32
- **重复 objectid**: 20 个
  - objectid=70（永定河）: 5 段（1 direct + 4 downstream）
  - objectid=92（大清河）: 4 段（1 direct + 3 downstream）
  - objectid=112（海河）: 4 段（全 downstream）
  - objectid=14: 3 段（中亭河、瀑河）
  - objectid=57: 3 段（永定新河）
  - objectid=90: 3 段（子牙河）
- **同一 objectid 对应多条不同河流**：这是 full_v6 数据层面的根本问题
  - objectid=10: 二滦河 + 任河大水渠
  - objectid=14: 中亭河 + 瀑河
  - objectid=6: 北拒马河 + 洋河
  - objectid=7: 唐河 + 洋河
  - objectid=4: 洒河 + 漕河
  - objectid=9: 任文干渠 + 陡河
  - objectid=15: 老牛河 + 赵王新河
- **孤立河段**（无共享端点）: 15 条
- **有直接影响但无下游的河段**: 56 条，涉及 53 个 objectid
- **仅有下游无直接上游的河段**: 22 条
- **端点间隙**（0–0.01°）: 88 处

### 数据生产脚本梳理（E:\tj\line）
- `build_river_network.py`：从 Shapefile 建无向图、节点分割、BFS 定流向、输出 pkl/SHP/GeoJSON
  - 输入：Shapefile 线矢量
  - 输出：`river_graph.pkl`（有向图）、`river_directed.shp`、节点、拓扑文本
  - 关键：`objectid` 不在 pkl edge 属性中，只在 `name` 和原始 `line_idx` 中
- `find_breakpoints.py` / `find_dangling_endpoints.py`：检测断点与悬挂端点
- `fix_intersections.py` / `fix_flow_direction.py`：修复交叉与流向
- `processriver.py`：早期处理脚本（clip、sjoin、shp→pkl）
- `summarize_geometry.py` / `view_graph.py`：几何统计与可视化

## Root Cause

核心根因可分解为两个层次：

1. **full_v6 数据质量问题（scheme 1 针对的问题）**
   - 同一 `objectid` 下存在多条互不连通的河流（不同 `river_name`）
   - 同一 `objectid` 下存在主河道 + 孤立短小分支
   - 这导致 `_query_direct_rows` 用 `ST_Dump` 拆分后，把本不应属于同一段的河流都作为直接河段输出

2. **pkl 与 full_v6 拓扑/几何不对齐（scheme 2 针对的问题）**
   - pkl 图按节点将河流切分为多个 edge，每个 edge 通过 `objectid` 回查 full_v6
   - full_v6 中对应 `objectid` 是完整 MULTILINESTRING，不是按 pkl edge 切好的子段
   - `_query_downstream_rows` 必须在完整几何中重新定位、裁剪，容易产生断裂/跳跃/孤立

当前代码层修复（2026-07-15）已尽量缓解问题 2，但问题 1 无法通过代码完全解决：只要一个 objectid 下面有两条不同的河，代码就无法判断该留哪一条。

## Candidate Solutions

### 方案 A：清洗 `haihe_river_directed_full_v6`
**目标：** 每个 `objectid` 对应一条单一、连通的河流几何。

**步骤：**
1. 对 full_v6 按 `objectid` 分组，用 `ST_Dump` 拆分为子段
2. 按名称/连通性/长度聚类：
   - 若同一 objectid 下子段对应不同河流名称，按名称拆分为多条记录，每条赋予独立 objectid
   - 若同一 objectid 下主河道带短小孤立分支，保留主河道，将分支拆分为独立记录或删除
3. 更新 `objectid` 序列，保证唯一性
4. 可选：重新计算河段起终点、流向字段

**优点：**
- 不改动 pkl 图，与现有 `river_directed_v6.pkl` 兼容
- 代码层 `_query_direct_rows` 可直接按 objectid 取到连通的一段河流
- 对下游追踪：full_v6 几何与 pkl edge 的 `to` 节点更易对齐

**缺点：**
- full_v6 是权威数据源，修改后可能影响其他业务（GIS 展示、水文模型）
- 需要维护清洗规则，未来数据更新需重新清洗
- 清洗过程可能丢失合法但短小的分支

### 方案 B：重新生成 `river_directed_v6.pkl`
**目标：** 每个 pkl edge 的几何端点与 full_v6 子段端点精确对应，最好一个 edge 对应一个 objectid 子段。

**步骤：**
1. 以当前 full_v6 为输入，按节点拆分并建立有向图
2. 为每条 pkl edge 记录：
   - 对应的 full_v6 `objectid`
   - 子段序号或几何范围（起点/终点坐标）
3. 输出新的 `river_directed_v6.pkl`
4. 同步更新 `constants.DIRECTED_GRAPH_FILENAME` 与 `config.ini`

**优点：**
- 拓扑与几何天然一致，代码层回查逻辑可大幅简化
- 可以保留 full_v6 不变，避免影响其他业务

**缺点：**
- 若 full_v6 本身存在同一 objectid 多河流问题（问题 1），pkl 也无法正确处理
- 需要重新生成并验证整个图；`objectid` 索引约定可能变化
- 不能单独解决问题 1

### 方案 C：先清洗 full_v6，再重生成 pkl（推荐）
**目标：** 同时解决问题 1 和问题 2。

**步骤：**
1. 按方案 A 清洗 full_v6，使每个 objectid 对应单一连通河流
2. 用清洗后的 full_v6 按方案 B 重新生成 pkl
3. 简化 `rainfall_impact_geojson.py` 中的子段选择、方向判断、去重逻辑
4. 回归测试 + 内网样本验证

**优点：**
- 从根本上消除两类不对齐
- 代码层可大幅简化，维护成本降低
- 未来数据更新流程清晰：先清洗 full_v6，再重生成 pkl

**缺点：**
- 工作量最大
- 需要协调其他依赖 full_v6 的业务
- 需要一次性验证整个流域

## Verification
- 使用清洗/重生成的数据重新运行 rain impact 工具
- 对比异常河段数量变化：重复 objectid、孤立段、无下游直接段、端点间隙
- 运行单元测试 `utils/tests/test_rainfall_impact_geojson.py` 与 fast path 静态检查

## Technical Decisions
| Decision | Rationale |
|----------|-----------|
| 待用户确认后填写 | 待用户确认后填写 |

## Resources
- `E:\fsdownload\rain_impact_result.json.river.geojson`
- `E:\fsdownload\rain_impact_oid_repeat.json`
- `E:\tj\line\build_river_network.py`
- `E:\tj\line\find_breakpoints.py`
- `E:\tj\line\find_dangling_endpoints.py`
- `hhlyqyxt-master/utils/rainfall_impact_geojson.py`
- `haihe-weather-analyzer-mcp/constants.py`
