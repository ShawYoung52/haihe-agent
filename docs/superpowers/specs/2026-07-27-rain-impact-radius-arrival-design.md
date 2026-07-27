# 暴雨影响河流 · 半径收敛 + 预计到达时间契约设计

- **状态**：草案（brainstorming 已完成，待用户 review）
- **日期**：2026-07-27
- **作用域**：仅 `hhlyqyxt-master/utils/rainfall_impact_geojson.py` 及其相邻测试 / 内网验证脚本
- **契约类别**：向后兼容（新增字段，不修改老字段语义）
- **模型分工**：DeepSeek v4 Flash = 主力执行；DeepSeek v4 Pro = 架构师 / 高级审查
- **相关记忆**：`[[rain-impact-geojson-consistency]]`、`[[traction-review-scope-rule]]`、`[[haihe-project-env-quirks]]`、`[[deepseek-model-constraint]]`、`[[user-full-process-workflow]]`

---

## 1. 目标

**业务目标**

1. 把「暴雨影响河流」的**站点半径**从 30km 收敛到 20km，避免直接影响水系覆盖面过大。
2. 在 GeoJSON 每条 feature 上同时提供**相对小时数**与**钟表时刻（预计到达时间）**，让前端能直接展示"预计 15:30 到达"。
3. 河流传播总结（`river_propagation.rivers[*]`）附带每条河的最早/最晚到达时刻边界。

**非目标**

- 不改数据库表结构、pkl 图、上游 HHLY 数据源。
- 不改 `emergency_*` / `river_city_impact_tool` / `rainstorm_impact_map_service` 的对外签名。
- 不改老字段（`propagation_time_hours` 等）的语义。
- 不做前端改造（前端为独立仓库，本 spec 只交付后端契约）。

## 2. 参数与契约变更

### 2.1 参数默认值

| 参数 | 旧默认 | 新默认 | 备注 |
|---|---|---|---|
| `station_buffer_km` | 30.0 | **20.0** | `build_rainstorm_impact_thematic_map` / `build_rain24h_impact_river_geojson` 均更新 |
| `direct_match_km` | 10.0 | 10.0（不变） | 圈内更严格的"直接图边"标记阈值 |
| `downstream_km` | 50.0 | 50.0（不变） | 下游传播距离，与站点半径无关 |

`_validate_params` 补充上界：`0 < station_buffer_km <= 500`，防误传大值。

### 2.2 GeoJSON feature.properties 新增字段

所有 feature（direct_buffer 与 downstream_50km）**统一**含以下字段（[[rain-impact-geojson-consistency]] 属性统一原则）：

| 字段 | 类型 | 语义 | 缺 T0 时 |
|---|---|---|---|
| `t0_source_time` | `string \| null` (ISO 8601 UTC，末尾 `Z`) | 直接段=归属 trigger 站中最早 `rain_end_time`；下游段=BFS 路径上上游 direct 段中最早 `rain_end_time`。 | `null` |
| `estimated_arrival_time` | `string \| null` (同上格式) | `t0_source_time + propagation_time_hours`；下游段 propagation 语义为"从 direct 出口到该 edge 尾"。 | `null` |

`propagation_time_hours`（既有）**保留不动**。

### 2.3 顶层 result 新增字段

| 字段路径 | 类型 | 语义 |
|---|---|---|
| `params.reference_time` | `string \| null` (ISO UTC Z) | 所有 `rainstorm_stations` 中最早 `rain_end_time` |
| `river_propagation.rivers[*].earliest_arrival_time` | `string \| null` | 该河所有 features 中非空 `estimated_arrival_time` 的 min |
| `river_propagation.rivers[*].latest_arrival_time` | `string \| null` | 同上，max |

### 2.4 时间格式契约

- 单一格式：`YYYY-MM-DDTHH:MM:SSZ`（UTC，秒级，末尾 `Z`）。
- 单一入口：`_iso_utc(dt: datetime | None) -> str | None`。
- naive datetime → 视为 `Asia/Shanghai` 再转 UTC（生产 CSV 为本地时间）。
- 无效输入 / 缺失 → `None`，配 `logger.warning`，不抛异常。

## 3. 数据流

```
stations (list[dict], 含 rain_end_time)
    ▼
_normalize_stations
    - _normalize_end_time：字段名尝试顺序 rain_end_time → end_time → time
    - naive → Asia/Shanghai → UTC datetime
    - 顺带算 params.reference_time = min(rain_end_time)
    ▼
_query_candidate_edge_rows            (buffer_km=20)
    ▼
_classify_graph_edges
    - 每条 direct_edge 新增 trigger_rain_end_time = min(trigger 站.rain_end_time)
    ▼
_collect_downstream_edges  (BFS)
    - start_nodes 从对应 direct_edge 继承 t0（该站点 rain_end_time）
    - BFS：pending 结构 {node: (distance_km, t0_utc)}
    - 同一 node 多路径合并：distance 取 min，T0 也取 min
    - 每条 downstream_edge 落 t0_source_time = min(所有可达 start_node.t0)
    ▼
_resolve_edge_features
    - direct 段：t0 = edge["trigger_rain_end_time"]
    - downstream 段：t0 = edge["t0_source_time"]
    - propagation_time_hours 已算（不动）
    - estimated_arrival_time = t0 + timedelta(hours=propagation_time_hours) if t0 else None
    - 输出走 _iso_utc()
    ▼
_build_river_propagation
    - 每条河 features 遍历，earliest/latest arrival = min/max(estimated_arrival_time)
```

## 4. 关键实现细节

### 4.1 新增辅助函数

- `_normalize_end_time(raw: Any) -> datetime | None`：支持 ISO 字符串 / `datetime` / `pandas.Timestamp` / naive；naive 按 Asia/Shanghai 转 UTC；异常返回 None + warning。
- `_iso_utc(dt: datetime | None) -> str | None`：`dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")`；None → None。所有对外时间字段唯一格式化入口。

### 4.2 `_collect_downstream_edges` 签名升级

- 老签名：`pending: dict[node, distance_km]`
- 新签名：`pending: dict[node, tuple[distance_km, datetime | None]]`
- 兼容分支：入参检测——若 value 为标量数字，自动包装成 `(value, None)`，保留老测试可用。

### 4.3 下游段 T0 溯源的口径

- 下游 `propagation_time_hours = end_distance_km / velocity_kmh`，其中 `end_distance_km` 已是"从 direct 出口到该 edge 尾"的累计距离。
- 故 `estimated_arrival_time (下游) = t0(最早上游 rain_end_time) + end_distance_km / velocity_kmh`，业务口径自洽。

### 4.4 直接段 T0 溯源的口径

- 直接段 `propagation_time_hours = length_km / velocity_kmh`（沿用 `_feature_length_km`）。
- `t0 = min(该 edge 所有 trigger 站 rain_end_time)`。

## 5. 错误处理与降级

- 所有站点 rain_end_time 缺失 → 全部 feature 的 `t0_source_time` / `estimated_arrival_time` = `null`；`params.reference_time` = `null`；`propagation_time_hours` 不受影响。
- 部分站点缺失 → 缺失站不参与 T0 min 运算；其他站正常。
- 时区解析失败 → warning + 返 None。
- `t0` 存在但 `propagation_time_hours` = NaN（不应发生，[[rain-impact-geojson-consistency]] 已修）→ 走已有 fallback，arrival = null，warning。
- `t0` 存在且 `propagation_time_hours = 0`（起点边）→ `arrival == t0`，正确。
- 下游 BFS 环路：既有 `visited` 保证不重入，T0 只在首次到达时确定（min）。

## 6. 测试策略

### 6.1 单元测试增量（`hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py`）

按 TDD 顺序（先写测试再实现）：

1. `test_default_station_buffer_km_is_20`
2. `test_validate_params_rejects_zero_buffer`（沿用）
3. `test_validate_params_rejects_absurd_buffer`
4. `test_normalize_end_time_naive_bj_to_utc`
5. `test_normalize_end_time_iso_string`（Z / +08:00 / naive 各一）
6. `test_normalize_end_time_invalid_returns_none`
7. `test_direct_edge_arrival_equals_t0_plus_propagation`
8. `test_downstream_edge_t0_is_min_upstream_rain_end`
9. `test_arrival_none_when_rain_end_missing`
10. `test_params_reference_time_is_earliest`
11. `test_river_propagation_arrival_bounds`
12. `test_iso_utc_format_regex`
13. `test_backward_compat_missing_rain_end_time`
14. `test_downstream_edges_pending_backward_compat`

**基线**：现有 43 passed（`test_rainfall_impact_geojson.py`）+ 30（emergency）= 73。目标合入后 43 → 57，总数 87。

### 6.2 内网验证（`hhlyqyxt-master/utils/intranet_verify_rain_impact.py`）

现有 5 项 → 新增第 6 项 `verify_arrival_time_consistency`：

- 每个有 `t0_source_time` 的 feature 必须有 `estimated_arrival_time`。
- ISO UTC 正则：`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`。
- 直接段：`|arrival - t0 - propagation_time_hours * 3600| ≤ 1s`。
- 下游段：`arrival ≥ t0`（时间不倒流）。
- `params.reference_time == min(feature.t0_source_time)`（跨所有非空）。
- `river_propagation.rivers[*].earliest_arrival_time == min(该河 features estimated_arrival_time)`。

## 7. 契约兼容性矩阵

| 消费者 | 影响 | 处理 |
|---|---|---|
| 老前端（不认新字段） | 无影响 | 无需协调 |
| 新前端 | 直接读 `feature.properties.estimated_arrival_time` | 契约文档明确通知 |
| `rainstorm_impact_map_service.py` | 透传，不解析 | 不改 |
| `river_city_impact_tool.py` | 同上 | 不改 |
| `emergency_*`（HHLY 独立数据源） | 无关 | 不改 |
| pytest 现有 43 条 | 只影响 `station_buffer_km=30` 默认值断言 | grep + 精确更新 |
| QGIS 打开 river.geojson | 属性表多 2 列 | 与 [[rain-impact-geojson-consistency]] 属性统一原则一致 |

## 8. 命名一致性检查（防 [[rain-impact-geojson-consistency]] 教训重现）

- summary 侧 `earliest_arrival_time` == 该河所有 features 里非空 `estimated_arrival_time` 的 min（测试 11 显式断言）。
- per-edge 与 summary 字段名前缀统一：`estimated_` / `earliest_` / `latest_`。
- per-edge 直接段与下游段字段集**完全一致**（缺 T0 时字段留 null，不缺列）。

## 9. 执行编排（Phase 划分，供 writing-plans 消费）

- **Phase 0** — 分支 + baseline snapshot
- **Phase 1** — `station_buffer_km` 默认 20 + `_validate_params` 上界
- **Phase 2** — `_normalize_end_time` + `_iso_utc` + `params.reference_time`
- **Phase 3** — direct_edge `trigger_rain_end_time`
- **Phase 4** — 下游 BFS T0 传播 + 老签名兼容
- **Phase 5** — feature 契约 + summary 契约
- **Phase 6** — 内网验证第 6 项
- **Phase 7** — `/simplify` + Pro `code-review`
- **Phase 8** — GitHub PR + claude-mem 落新记忆

**门禁（M-Gate）**

- M1（Phase 1 完成）：默认值 20 生效、pytest 全绿。
- M2（Phase 5 完成）：新增 14 条测试全绿，老 43 条不回归。
- M3（Phase 6 完成）：内网 6 项全绿或"待现场"标记明确。
- M4（Phase 7-8 完成）：Pro 审查通过、PR merge ready。

## 10. 风险与预案

| 风险 | 概率 | 预案 |
|---|---|---|
| naive datetime 时区推断错（生产数据不是 BJ） | 低 | 归一化函数记 warning + 保留 raw；变化只需改 `_normalize_end_time`。 |
| 生产用字段名 `end_time`（非 `rain_end_time`） | 中 | 归一化按顺序尝试 `rain_end_time` → `end_time` → `time` → None。 |
| 前端不认 UTC | 中 | 契约文档明确 UTC + Z；前端本地化用 `dayjs.utc(str).local()`。 |
| Windows Store python 占位陷阱 | 高（本机） | 显式使用 `.venv/Scripts/python.exe` 绝对路径（[[haihe-project-env-quirks]]）。 |
| git add 误吞不相关文件 | 中 | 只 `git add` 精确路径（[[haihe-project-env-quirks]]）。 |

## 11. 模型分工（[[deepseek-model-constraint]]）

| 阶段 | 模型 | 职责 |
|---|---|---|
| brainstorming | Opus（本会话） | 业务口径对齐 |
| writing-plans 审查 | **DeepSeek v4 Pro** | 架构 / 契约 / 边界评审 |
| TDD 红灯/绿灯执行 | **DeepSeek v4 Flash** | 按 phase 落地测试与实现 |
| code-simplifier | Flash | 清理重复 / 命名 / 简化 |
| code-review | **DeepSeek v4 Pro** | PR 前高级审查 |
| context7 | 按需 | datetime / tz 用法确认（预计用不上） |
| github / claude-mem / claude-md-management | — | 交付 + 记忆落地 |

## 12. 交付物清单

- `hhlyqyxt-master/utils/rainfall_impact_geojson.py`（改）
- `hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py`（增 14 条测试）
- `hhlyqyxt-master/utils/intranet_verify_rain_impact.py`（增第 6 项）
- `docs/superpowers/specs/2026-07-27-rain-impact-radius-arrival-design.md`（本文档）
- 后续 `docs/superpowers/plans/2026-07-27-rain-impact-radius-arrival-plan.md`（writing-plans 阶段产出）
- 新记忆 `[[rain-impact-arrival-time-contract]]`（PR 合入后落）
