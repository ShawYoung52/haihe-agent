# 暴雨影响河流 · 链式传播到达时刻修复 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修复 BFS 中"距离最短"和"最早 t0"来自不同路径导致的到达时刻错误，改为链式传播：每个节点的 water arrival = `min(上游 arrival + 本段旅程)`。

**Architecture:** BFS 中新增 `best_arrival[node]` 字典独立追踪每个节点的最早到达时刻，与 `best_dist[node]`（Dijkstra 距离）并列运行。距离改进 push + arrival 改进 re-push（已有 I1 机制）。回填改为用 `best_arrival[from_node]`。`_resolve_edge_features` 中下游段 `propagation_distance_km` = `keep_km`（本段），`propagation_time_hours` = `keep_km/v`，`t0_source_time` = `best_arrival[from_node]`。

**Tech Stack:** Python 3.9+ / `datetime` / `heapq`（标准库）

## Global Constraints

- 只改：`hlyqyxt-master/utils/rainfall_impact_geojson.py` + `hlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py` + `hlyqyxt-master/utils/intranet_verify_rain_impact.py`
- 直接段所有字段语义不变。
- 下游段 `propagation_distance_km` / `propagation_time_hours` / `t0_source_time` / `estimated_arrival_time` 语义改为链式（breaking）。
- `river_propagation` summary 不受影响。
- `best_arrival` re-push 次数有限（水利河网入度 ≤ 5）。
- 所有测试用 `D:\PythonProject\haiheliuyubaoyuagent-master\.venv\Scripts\python.exe` 绝对路径。

---

### Task 1: Phase 0 — 分支 + baseline

- [ ] **Step 1: 创建分支**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master
git checkout -b feat/rain-impact-chain-arrival
```

- [ ] **Step 2: 记录 baseline 测试数**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/hhlyqyxt-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest utils/tests/test_rainfall_impact_geojson.py -v 2>&1 | tail -5
```

预期：`63 passed`

- [ ] **Step 3: 初始化 ledger**

```bash
mkdir -p /d/PythonProject/haiheliuyubaoyuagent-master/.superpowers/sdd
cat > /d/PythonProject/haiheliuyubaoyuagent-master/.superpowers/sdd/progress.md << 'LEDGER'
# SDD Progress Ledger — feat/rain-impact-chain-arrival
Base commit: <current HEAD>
Baseline: 63 passed
LEDGER
```

---

### Task 2: Phase 1 — BFS `best_arrival` 追踪 + re-push

**Files:**
- Modify: `hlyqyxt-master/utils/rainfall_impact_geojson.py`（`_collect_downstream_edges` 函数，约 line 820-857）

**Interfaces:**
- Consumes: `_collect_downstream_edges(starts, graph, direct_keys, downstream_km)` 签名不变
- Produces: `best_arrival: dict[node, datetime | None]` 字典（BFS 结束时收敛）
- Produces: 每条 downstream edge 的 `t0_source_time` 由回填 loop 从 `best_arrival[from_node]` 读取

- [ ] **Step 1: 写测试 `test_multi_source_convergence_earliest_arrival`**

```python
def test_multi_source_convergence_earliest_arrival():
    """两个不同 t0 的站点汇聚到同一节点时，下游段取物理最早到达。"""
    from utils.rainfall_impact_geojson import _collect_downstream_edges, _save_downstream_edge
    from datetime import datetime, timezone

    t0_early = datetime(2026, 7, 27, 6, 0, 0, tzinfo=timezone.utc)   # 08:00 BJT
    t0_late = datetime(2026, 7, 27, 7, 0, 0, tzinfo=timezone.utc)    # 09:00 BJT

    # 两个 start node：S1 近但 C2 晚，S2 远但 C2 早
    starts = {
        "s1": (0.0, t0_early),   # 短距离，早 t0
        "s2": (0.0, t0_late),    # 长距离，晚 t0（Dijkstra 会优先拓展）
    }

    # 需要 mock graph，构造两条路径都到达同一个下游 node
    # ... 见 Task 5 已有 _MockMultiDiGraph 模式
```

**注**：此测试需 mock graph，和 Task 5 里的 `test_downstream_t0_transitive_convergence` 模式相同。用现有 `_MockMultiDiGraph` fixture。

- [ ] **Step 2: 运行确认 FAIL**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/hhlyqyxt-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest utils/tests/test_rainfall_impact_geojson.py::test_multi_source_convergence_earliest_arrival -v 2>&1
```

- [ ] **Step 3: 实现 `best_arrival` 追踪**

在 `_collect_downstream_edges` 中（`_starts` 解析完成后），新增：

```python
# best_arrival[node] = 最早达到该节点的时刻（水从 start 节点出发沿路径累积）
best_arrival: dict = {}
for node, (dist, t0) in _starts.items():
    if t0 is not None:
        best_arrival[node] = t0
```

- [ ] **Step 4: 主循环中传播 `best_arrival`**

在现有距离 push 逻辑之后，新增 arrival 传播：

```python
for u, v, key, attr in iter_out_edges(graph, node):
    edge_length = get_edge_length_km(attr,
        from_xy=_parse_node_xy(u), to_xy=_parse_node_xy(v))
    next_distance = distance + edge_length
    if next_distance > downstream_km:
        continue

    next_distance = _save_downstream_edge(
        edges, u, v, key, attr, distance, downstream_km, direct_keys)

    # ---- 距离 Dijkstra（不变）----
    if next_distance <= downstream_km and next_distance < best_dist.get(v, math.inf):
        best_dist[v] = next_distance
        heapq.heappush(heap, (next_distance, next(seq), v))

    # ---- 到达时刻链式传播（新增）----
    if best_arrival.get(node) is not None and edge_length > 0:
        travel_hours = edge_length / velocity_kmh
        from datetime import timedelta
        arrival_at_v = best_arrival[node] + timedelta(hours=travel_hours)

        existing = best_arrival.get(v)
        if existing is None or arrival_at_v < existing:
            best_arrival[v] = arrival_at_v
            # 用 v 当前最短距离 re-push 触发出边重放（让下游也看到改进的 arrival）
            heapq.heappush(heap, (best_dist.get(v, next_distance), next(seq), v))
```

注意 `velocity_kmh` 需要在函数开头定义（第 797 行附近）：
```python
velocity_mps = float(flow_velocity_mps) if 'flow_velocity_mps' in dir() else DEFAULT_FLOW_VELOCITY_MPS
velocity_kmh = velocity_mps * 3.6
```

实际上检查 `_collect_downstream_edges` 当前签名，它不接收 `flow_velocity_mps` 参数。需要把 velocity 传到这个函数，或者在函数内用 `DEFAULT_FLOW_VELOCITY_MPS * 3.6`。

**关键决策**：要在 `_collect_downstream_edges` 中访问 `velocity_kmh`。当前这个函数没有这个参数。两个方案：
- A: 给 `_collect_downstream_edges` 新增可选参数 `flow_velocity_mps: float = DEFAULT_FLOW_VELOCITY_MPS`
- B: 在函数内部硬编码 `velocity_kmh = DEFAULT_FLOW_VELOCITY_MPS * 3.6`

**用方案 A**（已存在的 `_save_downstream_edge` 也不需要 velocity，只有 BFS 中的 arrival 传播需要）。

修改 `_collect_downstream_edges` 签名：
```python
def _collect_downstream_edges(
    starts: dict, graph, direct_keys: set[str], downstream_km: float,
    flow_velocity_mps: float = DEFAULT_FLOW_VELOCITY_MPS,
) -> list[dict]:
```

在 `build_rainstorm_impact_thematic_map` 中调用时传 `flow_velocity_mps=flow_velocity_mps`。

- [ ] **Step 5: 更新回填 loop 用 `best_arrival`**

```python
# 回填 t0_source_time 到 edge：用 best_arrival[from_node]（链式到达时刻）
for edge in edges.values():
    edge["t0_source_time"] = best_arrival.get(edge["from_node"])
    del edge["from_node"]
```

- [ ] **Step 6: 运行确认 PASS**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/hhlyqyxt-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest utils/tests/test_rainfall_impact_geojson.py::test_multi_source_convergence_earliest_arrival -v 2>&1
```

预期：PASS。

- [ ] **Step 7: Commit**

```bash
git add hlyqyxt-master/utils/rainfall_impact_geojson.py hlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py
git commit -m "feat(rain-impact): BFS best_arrival tracking + chain propagation"
```

---

### Task 3: Phase 2 — `_resolve_edge_features` 下游段链式语义

**Files:**
- Modify: `hlyqyxt-master/utils/rainfall_impact_geojson.py`（`_resolve_edge_features` 函数，约 line 916-927）

**Interfaces:**
- Consumes: `edge["t0_source_time"]`（已是 `best_arrival[from_node]` 而非 `trigger_rain_end_time`）
- Consumes: `edge["keep_km"]`（本段距离）
- Produces: `propagation_distance_km = keep_km`（下游段）、`propagation_time_hours = keep_km/v`（下游段）

- [ ] **Step 1: 修改 `_resolve_edge_features` 下游段传播距离**

第 917-921 行，将：
```python
prop_distance = (
    float(edge.get("end_distance_km") or 0)
    if not is_direct
    else _feature_length_km(row, edge, impact_type)
)
```

改为：
```python
if not is_direct:
    # 下游段：本段距离（keep_km），非累计。链式语义。
    prop_distance = float(edge.get("keep_km") or 0)
else:
    prop_distance = _feature_length_km(row, edge, impact_type)
```

- [ ] **Step 2: 运行测试验证（老测试可能有多条失败，需要逐条更新）**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/hhlyqyxt-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest utils/tests/test_rainfall_impact_geojson.py -v 2>&1 | grep -E "PASSED|FAILED|ERROR" | tail -30
```

- [ ] **Step 3: 更新受影响的测试断言**

逐条检查 FAILED 的测试，将其断言从 `end_distance_km/v` 改为 `keep_km/v` 语义：

- `test_downstream_edge_t0_is_min_upstream_rain_end` → `test_downstream_edge_t0_is_arrival_at_entry`
- `test_downstream_t0_transitive_convergence` → 断言 arrival 沿路径链式递增
- `test_downstream_edge_t0_takes_min_when_multiple_starts_converge` → 取最早物理到达

- [ ] **Step 4: 运行全部确认通过**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/hhlyqyxt-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest utils/tests/test_rainfall_impact_geojson.py -v 2>&1 | tail -5
```

预期：≥ 64 passed（新 + 更新后）

- [ ] **Step 5: Commit**

```bash
git add hlyqyxt-master/utils/rainfall_impact_geojson.py hlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py
git commit -m "feat(rain-impact): downstream per-edge chain semantics (keep_km/v)"
```

---

### Task 4: Phase 3 — 新增 6 条测试 + 内网验证更新

**Files:**
- Modify: `hlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py`
- Modify: `hlyqyxt-master/utils/intranet_verify_rain_impact.py`

- [ ] **Step 1: 新增 6 条测试**

```python
def test_downstream_propagation_is_keep_km_not_end_distance():
    """下游段 propagation_distance_km = keep_km（本段距离）而非 end_distance_km（累计）。"""
    ...

def test_downstream_propagation_time_is_keep_km_over_velocity():
    """下游段 propagation_time_hours = keep_km/v。"""
    ...

def test_downstream_arrival_chains_from_best_arrival():
    """estimated_arrival_time = t0_source_time + keep_km/v（链式传播）。"""
    ...

def test_multi_source_convergence_earliest_arrival():
    """两个不同 t0 的站点汇聚，下游段取最早物理到达。"""
    ...

def test_downstream_t0_is_arrival_at_entry():
    """下游段 t0_source_time = best_arrival[from_node]（水进入本段入口的时刻）。"""
    ...

def test_downstream_no_arrival_when_no_start_arrival():
    """best_arrival 全为 None 时，下游段 t0_source_time / estimated_arrival_time = None。"""
    ...
```

- [ ] **Step 2: 更新内网验证第 6 项**

`verify_arrival_time_consistency` 中下游段校验改为用 `keep_km` 替代 `end_distance_km`：

```python
# 下游段：arrival >= t0（时间不倒流），arrival - t0 ≈ keep_km/v
if not is_direct and prop_hours and math.isfinite(prop_hours):
    keep_km = props.get("keep_km", 0)
    expected_hours = keep_km / velocity_kmh
    if abs(prop_hours - expected_hours) > 0.2:  # 200s 容忍
        issues += 1
```

- [ ] **Step 3: 运行全部测试**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/hhlyqyxt-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest utils/tests/test_rainfall_impact_geojson.py -v 2>&1 | tail -5
```

- [ ] **Step 4: Commit**

```bash
git add hlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py hlyqyxt-master/utils/intranet_verify_rain_impact.py
git commit -m "test(rain-impact): chain-arrival tests + intranet verify update"
```

---

### Task 5: Phase 4 — code-simplifier + Pro 最终审查

- [ ] **Step 1: 简化检查**（去掉死代码、统一命名）
- [ ] **Step 2: 运行全部测试确认无回归**
- [ ] **Step 3: 使用 `superpowers:requesting-code-review` 请求 Pro 审查**

---

### Task 6: Phase 5 — finishing (Push + main merge + memory)

- [ ] **Step 1: 确认最终测试全绿**
- [ ] **Step 2: 合入 main + push + 删 feature 分支**
- [ ] **Step 3: 落 claude-mem 新记忆 `[[rain-impact-chain-arrival]]`**
- [ ] **Step 4: 更新 MEMORY.md 索引**
