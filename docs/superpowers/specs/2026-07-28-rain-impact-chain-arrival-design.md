# 暴雨影响河流 · 链式传播到达时刻修复设计

- **状态**：草稿（brainstorming 已完成，待用户 review）
- **日期**：2026-07-28
- **作用域**：`hhlyqyxt-master/utils/rainfall_impact_geojson.py` 中的 BFS 传播逻辑 + per-edge 字段语义
- **前置 PR**：`fe609e9` (arrival time contract)、`30ab42a` (MCP sync)、`856de1b` (datetime fix)
- **相关记忆**：`[[rain-impact-arrival-time-contract]]`、`[[rain-impact-geojson-consistency]]`、`[[qa-agent-rain-impact-sync]]`

---

## 1. 问题

当前 BFS 实现把"距离最短"和"最早 t0"作为两个独立追踪变量（`best_dist` / `best_t0`），但它们可能来自不同水流路径：

```
站点 S1（雨止 08:00）              站点 S2（雨止 09:00）
    |                                    |
direct edge A（5km）                direct edge B（3km）
arrival_A = 08:41                   arrival_B = 09:25
    |                                    |
    +----------→ 合流点 X ←-------------+
                    |
              downstream C（10km）

当前代码：
  Dijkstra 最短距离到 X = 13km（B 路径：近）
  best_t0[X] = min(08:00, 09:00) = 08:00（S1：最早）
  arrival_C = 08:00 + 13km/7.2kmh = 09:48  ← 错
  - 距离(13km)来自 B 路径、t0(08:00)来自 S1，混搭无物理意义

正确物理：
  S1→X：08:00 + 15/7.2 = 10:05
  S2→X：09:00 + 13/7.2 = 10:48
  最早到达 X = 10:05（S1 路径，距离长但出发早）
  → C 出口 = 10:05 + 10/7.2 = 11:28
```

**修复目标**：下游段"到达时刻"跟随具体水流路径链式传播，而非按独立变量 min 混搭。

## 2. 设计方案

### 2.1 BFS 新增 `best_arrival[node]`

在 `_collect_downstream_edges` 中追踪每个节点的真实最早到达时刻：

- 起始节点：`best_arrival[node] = start_nodes_t0[node]`
- 遍历边 (node, v) 时：
  - `arrival_at_v = best_arrival[node] + edge_length / velocity`
  - `best_arrival[v] = min(best_arrival[v], arrival_at_v)`
  - 如果 `best_arrival[v]` 改进，以 v 的当前最短距离 re-push v（自洽于已有的 I1 BFS re-push 机制）

### 2.2 下游边到达时刻 = 链式传播

不再 `t0_source_time + end_distance_km / velocity`（两者可能来自不同路径），而是：

- `t0_source_time` = `best_arrival[from_node]`（水进入本段入口的时刻）
- `estimated_arrival_time` = `best_arrival[from_node] + keep_km / velocity`（水走完本段后的时刻）

### 2.3 per-edge 字段语义改为"本段"（非累计）

| 字段（下游段） | 旧 | 新 |
|---|---|---|
| `propagation_distance_km` | `end_distance_km`（累计） | `keep_km`（本段距离） |
| `propagation_time_hours` | `end_distance_km / v`（累计时间） | `keep_km / v`（本段时间） |
| `t0_source_time` | 最早 trigger 站 rain_end_time（所有上游 min） | `best_arrival[from_node]`（水进入本段入口的时刻） |
| `estimated_arrival_time` | `t0 + end_distance_km / v` | `t0 + keep_km / v`（同路径，自洽） |

**直接段不变**：`t0_source_time = trigger_rain_end_time`，`estimated_arrival_time = t0 + length_km / v`，`propagation_distance_km = length_km`，`propagation_time_hours = length_km / v`。

### 2.4 `best_arrival` re-push 收敛性

`best_arrival[v]` 单调递减（只取 min）。每个节点最多被 re-push `入度-1` 次。水利河网入度 ≤ 5，实际 O(E)。

距离 re-push 和 arrival re-push 分离触发：
- 距离改进 → 按常规 Dijkstra push
- 到达时刻改进（距离不变）→ 用 v 当前最短距离 re-push，不污染距离最优性

## 3. 下游 summary 不变

`_build_river_propagation` 从 `downstream_edges` 字典读累计 `end_distance_km`，不受 per-edge 语义变化影响。`earliest_arrival_time` / `latest_arrival_time` 从 feature 收集（Task 6 已实现），链式传播后自动正确。

## 4. 契约兼容性

| 消费者 | 影响 |
|---|---|
| 下游 per-edge `propagation_distance_km` / `propagation_time_hours` | **breaking：语义从累计变为本段** |
| `estimated_arrival_time` | 单源等价；多源从错误变正确 |
| `t0_source_time`（下游段） | **breaking：从 trigger_rain_end_time 变为 entry 到达** |
| `river_propagation` summary | 无影响 |
| 直接段 | 无影响 |
| 前端 GeoJSON 渲染 | 字段名不变 |
| QGIS | 属性表列名不变，值变化 |

## 5. 边界处理

- `best_arrival[node] = None` → 不传播 arrival；下游段 `t0_source_time = None`
- BFS 环路：既有 `visited` 防重入
- re-push 有限：`best_arrival` 单调递减 `min(datetime)`，有限步收敛

## 6. 测试策略

**新增（TDD 顺序）**：
1. `test_downstream_propagation_is_keep_km_not_end_distance`
2. `test_downstream_propagation_time_is_keep_km_over_velocity`
3. `test_downstream_arrival_chains_from_best_arrival`
4. `test_multi_source_convergence_earliest_arrival`
5. `test_downstream_t0_is_arrival_at_entry`
6. `test_downstream_no_arrival_when_no_start_arrival`

**更新老测试**（断言改为链式语义）：
- `test_downstream_edge_t0_is_min_upstream_rain_end`
- `test_downstream_t0_transitive_convergence`
- `test_downstream_edge_t0_takes_min_when_multiple_starts_converge`

**内网验证调整**：
- 第 6 项下游段 `arrival - t0 ≈ keep_km / v`（用本段距离，200s 容忍）

## 7. 执行编排

- **Phase 0** — 分支 + baseline（pytest 63 passed）
- **Phase 1** — BFS `best_arrival` 追踪 + re-push
- **Phase 2** — `_resolve_edge_features` 链式语义 + 回填改为 `best_arrival`
- **Phase 3** — 测试：更新老 + 新增 6 条
- **Phase 4** — 内网验证第 6 项调整
- **Phase 5** — code-simplifier + Pro 最终审查
- **Phase 6** — finishing (Push + main merge + memory)

**M-Gate**：
- M1: Phase 1 BFS best_arrival 生效，老 BFS 不回归
- M2: Phase 2-3 per-edge 下游段全为链式语义，所有新老测试全绿
- M3: Phase 5 Pro 审查通过
- M4: Phase 6 main 已 push

## 8. 模型分工

| 阶段 | 模型 | 职责 |
|---|---|---|
| brainstorming | Opus（本会话） | 业务口径对齐 |
| writing-plans | 本会话 | Phase 编排 |
| TDD 红/绿执行 | DeepSeek v4 Flash 行为约束 | 分 phase 落地 |
| code-simplifier | Flash 行为约束 | 清理 |
| code-review 最终审 | DeepSeek v4 Pro 行为约束 | PR 前审查 |
| github / claude-mem | — | 交付 + 记忆 |

## 9. 交付物

- `hhlyqyxt-master/utils/rainfall_impact_geojson.py`（BFS + resolve 改）
- `hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py`（更新 + 新增）
- `hhlyqyxt-master/utils/intranet_verify_rain_impact.py`（第 6 项更新）
- `docs/superpowers/specs/2026-07-28-rain-impact-chain-arrival-design.md`（本文档）
- `docs/superpowers/plans/2026-07-28-rain-impact-chain-arrival-plan.md`（下一步产出）
- 新记忆 `[[rain-impact-chain-arrival]]`
