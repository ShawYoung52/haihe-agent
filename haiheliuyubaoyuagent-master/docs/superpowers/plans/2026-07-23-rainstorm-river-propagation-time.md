# 暴雨影响河流传播时间返回 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 暴雨影响河流工具返回结构新增 `river_propagation` 河流级传播时间汇总字段（核心算法 → MCP 适配 → 问答层透传与展示）。

**Architecture:** 传播距离与时间在牵引智能体核心算法 `rainfall_impact_geojson.py` 内按河流聚合（下游边取 Dijkstra 累计 `end_distance_km` 最大值，仅直接边河流取最长直接河段长度），统一经验流速（默认 2.0 m/s，可配置）；MCP 适配层与 chainlitexam 本地工具纯透传；问答层简报追加一行传播时间说明。

**Tech Stack:** Python 3.10+、pytest、LangChain Tool、fastmcp。

**Spec:** `docs/superpowers/specs/2026-07-23-rainstorm-river-propagation-time-design.md`

## Global Constraints

- 现有返回字段（`affected_rivers`、`segments`、`river_geojson` 等）一律不变；新字段纯增量。
- 流速默认值全链路统一：`DEFAULT_FLOW_VELOCITY_MPS = 2.0`（m/s）。
- 上游/MCP/本地工具入参语义：`flow_velocity_mps <= 0` 在核心层抛 `ValueError`；MCP 层与本地包装层 `0` = 用默认值（调用核心前替换为 2.0），负数报错。
- 下游消费者一律 `.get("river_propagation") or {}` 容忍旧版核心输出。
- 测试运行目录：`chainlitexam` 测试必须从 `haiheliuyubaoyuagent-master/chainlitexam/` 运行；牵引智能体测试从 `hhlyqyxt-master/` 运行；MCP 测试从 `haihe-weather-analyzer-mcp/` 运行。
- **git 提交时只 add 本任务涉及的文件**——工作区存在大量与本需求无关的 `.venv_new` 删除，严禁 `git add -A`。
- 错误文本与文档不得包含内网 IP/路径（沿用现有脱敏约定）。
- 数值精度约定：距离 `round(x, 3)`，传播时间 `round(x, 1)`（与模块现有 `_round` 约定一致）。

---

### Task 1: 核心算法 `_build_river_propagation`（牵引智能体 hhlyqyxt-master）

**Files:**
- Modify: `hhlyqyxt-master/utils/rainfall_impact_geojson.py`
- Test: `hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py`

**Interfaces:**
- Consumes: 现有 `direct_edges`（dict，edge_info 含 `river_name`、`length_km`）、`downstream_edges`（list，含 `river_name`、`end_distance_km`）、`_safe_float`、`_empty_result`、`_validate_params`。
- Produces（后续 Task 依赖的精确签名）:
  - `DEFAULT_FLOW_VELOCITY_MPS = 2.0`（模块级常量）
  - `_build_river_propagation(direct_edges: dict, downstream_edges: list, flow_velocity_mps: float) -> dict`，返回 `{"flow_velocity_mps": float, "rivers": [{"river_name": str, "propagation_distance_km": float, "propagation_time_hours": float, "arrival_estimate_readable": str}]}`，rivers 按 `propagation_time_hours` 降序。
  - `build_rainstorm_impact_thematic_map(..., flow_velocity_mps: float = DEFAULT_FLOW_VELOCITY_MPS)`，返回 dict 新增 `"river_propagation"` 键。
  - `_empty_result(..., flow_velocity_mps: float = DEFAULT_FLOW_VELOCITY_MPS)`，返回 dict 新增同构空块 `"river_propagation": {"flow_velocity_mps": v, "rivers": []}`。
  - `_validate_params(threshold, buffer_km, downstream_km, flow_velocity_mps=DEFAULT_FLOW_VELOCITY_MPS)`。

- [ ] **Step 1: 写失败测试**

在 `hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py` 末尾追加：

```python
# ---------------------------------------------------------------------------
# 传播时间估算（_build_river_propagation）
# ---------------------------------------------------------------------------


def _direct_edge(name: str, length_km: float) -> dict:
    return {"edge_key": f"k-{name}-{length_km}", "river_name": name, "length_km": length_km}


def _downstream_edge(name: str, end_distance_km: float) -> dict:
    return {"edge_key": f"d-{name}-{end_distance_km}", "river_name": name, "end_distance_km": end_distance_km}


def test_build_river_propagation_uses_max_downstream_end_distance():
    direct = {"a": _direct_edge("滦河", 3.0)}
    downstream = [_downstream_edge("滦河", 36.0), _downstream_edge("滦河", 12.0)]
    result = rig._build_river_propagation(direct, downstream, 2.0)
    assert result["flow_velocity_mps"] == 2.0
    assert len(result["rivers"]) == 1
    river = result["rivers"][0]
    assert river["river_name"] == "滦河"
    assert river["propagation_distance_km"] == 36.0
    assert river["propagation_time_hours"] == 5.0  # 36 / 7.2
    assert river["arrival_estimate_readable"] == "约5.0小时"


def test_build_river_propagation_direct_only_uses_longest_direct_length():
    direct = {"a": _direct_edge("东河", 1.8), "b": _direct_edge("东河", 3.6)}
    result = rig._build_river_propagation(direct, [], 2.0)
    river = result["rivers"][0]
    assert river["propagation_distance_km"] == 3.6
    assert river["propagation_time_hours"] == 0.5  # 3.6 / 7.2
    assert river["arrival_estimate_readable"] == "约30分钟"


def test_build_river_propagation_skips_non_finite_and_sorts_desc():
    direct = {"a": _direct_edge("甲河", float("nan")), "b": _direct_edge("乙河", 7.2)}
    downstream = [_downstream_edge("丙河", 72.0)]
    result = rig._build_river_propagation(direct, downstream, 2.0)
    names = [r["river_name"] for r in result["rivers"]]
    assert names == ["丙河", "乙河"]  # 甲河 NaN 被跳过；10.0h 的丙河排在 1.0h 的乙河前


def test_build_river_propagation_empty():
    assert rig._build_river_propagation({}, [], 2.0) == {"flow_velocity_mps": 2.0, "rivers": []}


def test_validate_params_rejects_non_positive_flow_velocity():
    with pytest.raises(ValueError):
        rig._validate_params(50.0, 30.0, 50.0, 0.0)
    with pytest.raises(ValueError):
        rig._validate_params(50.0, 30.0, 50.0, -1.0)


def test_empty_result_includes_river_propagation_block():
    result = rig._empty_result(
        stations=[],
        threshold=50.0,
        buffer_km=30.0,
        downstream_km=50.0,
        direct_match_km=10.0,
        schema="public",
        table="t",
        graph_path=None,
        extra=None,
        flow_velocity_mps=3.0,
    )
    assert result["river_propagation"] == {"flow_velocity_mps": 3.0, "rivers": []}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /d D:\PythonProject\haiheliuyubaoyuagent-master\hhlyqyxt-master && python -m pytest utils/tests/test_rainfall_impact_geojson.py -v -k propagation`
Expected: FAIL（`AttributeError: module 'rainfall_impact_geojson' has no attribute '_build_river_propagation'` 等）

- [ ] **Step 3: 实现核心算法**

`hhlyqyxt-master/utils/rainfall_impact_geojson.py` 改动（`math` 已在模块顶部导入，无需新增 import）：

a) 模块级常量（放在 `DEFAULT_DIRECT_GRAPH_MATCH_KM` 等现有常量附近）：

```python
DEFAULT_FLOW_VELOCITY_MPS = 2.0  # 经验洪水波传播速度，≈7.2 km/h
```

b) `_validate_params` 改为：

```python
def _validate_params(threshold: float, buffer_km: float, downstream_km: float, flow_velocity_mps: float = DEFAULT_FLOW_VELOCITY_MPS) -> None:
    if threshold < 0:
        raise ValueError("rainfall_threshold_mm 不能为负数")
    if buffer_km <= 0:
        raise ValueError("station_buffer_km 必须大于 0")
    if downstream_km < 0:
        raise ValueError("downstream_km 不能为负数")
    if flow_velocity_mps <= 0:
        raise ValueError("flow_velocity_mps 必须大于 0")
```

c) 新增两个函数（放在 `_sorted_feature_river_names` 附近）：

```python
def _propagation_readable(hours: float) -> str:
    """传播时间的可读表述：<1 小时显示分钟，否则显示小时。"""
    if hours < 1:
        return f"约{max(int(round(hours * 60)), 1)}分钟"
    return f"约{hours:.1f}小时"


def _build_river_propagation(
    direct_edges: dict[str, dict],
    downstream_edges: list[dict],
    flow_velocity_mps: float,
) -> dict:
    """按河流聚合暴雨影响传播时间估算。

    口径：下游边取 Dijkstra 累计 end_distance_km 最大值（暴雨入口视为 0km）；
    仅有直接边、无下游边的河流取直接边中最长 length_km（影响就地发生）。
    """
    velocity_kmh = float(flow_velocity_mps) * 3.6
    direct_len: dict[str, float] = {}
    downstream_dist: dict[str, float] = {}
    for edge in (direct_edges or {}).values():
        name = str(edge.get("river_name") or "").strip()
        length = _safe_float(edge.get("length_km"))
        if not name or length is None or not math.isfinite(length) or length <= 0:
            continue
        direct_len[name] = max(direct_len.get(name, 0.0), float(length))
    for edge in downstream_edges or []:
        name = str(edge.get("river_name") or "").strip()
        dist = _safe_float(edge.get("end_distance_km"))
        if not name or dist is None or not math.isfinite(dist) or dist <= 0:
            continue
        downstream_dist[name] = max(downstream_dist.get(name, 0.0), float(dist))

    rivers = []
    for name in sorted(set(direct_len) | set(downstream_dist)):
        distance_km = downstream_dist.get(name, direct_len[name])
        hours = round(distance_km / velocity_kmh, 1)
        rivers.append({
            "river_name": name,
            "propagation_distance_km": round(distance_km, 3),
            "propagation_time_hours": hours,
            "arrival_estimate_readable": _propagation_readable(hours),
        })
    rivers.sort(key=lambda r: r["propagation_time_hours"], reverse=True)
    return {"flow_velocity_mps": float(flow_velocity_mps), "rivers": rivers}
```

d) `_empty_result` 签名加 `flow_velocity_mps: float = DEFAULT_FLOW_VELOCITY_MPS`，返回 dict 的 `"river_summary"` 之后加：

```python
        "river_propagation": {"flow_velocity_mps": float(flow_velocity_mps), "rivers": []},
```

e) `build_rainstorm_impact_thematic_map` 签名加 `flow_velocity_mps: float = DEFAULT_FLOW_VELOCITY_MPS`（放在 `extra_summary` 之前）；首行校验改为 `_validate_params(rainfall_threshold_mm, station_buffer_km, downstream_km, flow_velocity_mps)`；`_empty_result(...)` 调用加 `flow_velocity_mps=flow_velocity_mps`；`result.update({...})` 中 `"river_summary"` 之后加：

```python
        "river_propagation": _build_river_propagation(direct_edges, downstream_edges, flow_velocity_mps),
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /d D:\PythonProject\haiheliuyubaoyuagent-master\hhlyqyxt-master && python -m pytest utils/tests/test_rainfall_impact_geojson.py -v`
Expected: 全部 PASS（含该文件既有用例，确认无回归）

- [ ] **Step 5: 提交**

```bash
git add hhlyqyxt-master/utils/rainfall_impact_geojson.py hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py
git commit -m "feat(rain-impact): add river-level propagation time estimation"
```

---

### Task 2: MCP 适配层透传（haihe-weather-analyzer-mcp）

**Files:**
- Modify: `haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py`
- Modify: `haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/server.py:78`（工具描述字符串）
- Test: `haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py`（新建；MCP 仓库测试惯例为根目录 `test_*.py`）

**Interfaces:**
- Consumes: Task 1 的 `build_rainstorm_impact_thematic_map(..., flow_velocity_mps=...)` 及其返回的 `river_propagation`。
- Produces:
  - `DEFAULT_FLOW_VELOCITY_MPS = 2.0`（MCP 侧常量）
  - `_resolve_flow_velocity(flow_velocity_mps: float) -> float`：0→2.0，负数→ValueError，正数原样。
  - `build_affected_river_network_result(..., flow_velocity_mps: float = 0.0)`；注册工具 `get_affected_river_network_by_rainfall` 同步新增同名参数。
  - 响应 dict（有结果与空结果）都含 `"river_propagation"` 键。

- [ ] **Step 1: 写失败测试**

新建 `haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py`：

```python
"""fixed_rainfall_impact_tool 传播时间透传测试（无需数据库/pkl/网络）。"""
from __future__ import annotations

import pytest

import fixed_rainfall_impact_tool as frit

_PROPAGATION = {
    "flow_velocity_mps": 2.0,
    "rivers": [
        {
            "river_name": "滦河",
            "propagation_distance_km": 48.2,
            "propagation_time_hours": 6.7,
            "arrival_estimate_readable": "约6.7小时",
        }
    ],
}


def _builder_result(**overrides):
    result = {
        "segments": [],
        "river_geojson": None,
        "downstream_start_stats": {},
        "affected_rivers": ["滦河"],
        "impact_stations": [],
        "river_propagation": _PROPAGATION,
    }
    result.update(overrides)
    return result


def test_resolve_flow_velocity_defaults_and_rejects_negative():
    assert frit._resolve_flow_velocity(0) == 2.0
    assert frit._resolve_flow_velocity(0.0) == 2.0
    assert frit._resolve_flow_velocity(3.0) == 3.0
    with pytest.raises(ValueError):
        frit._resolve_flow_velocity(-1)


def test_empty_response_carries_empty_propagation_block():
    resp = frit._empty_response({"time_range_readable": "t"}, 50.0, set(), set(), 10.0)
    assert resp["river_propagation"] == {"flow_velocity_mps": 2.0, "rivers": []}


def test_format_mcp_response_passthrough_propagation():
    resp = frit._format_mcp_response(_builder_result(), {"time_range_readable": "t"}, 50.0, set(), set())
    assert resp["river_propagation"]["rivers"][0]["propagation_time_hours"] == 6.7


def test_format_mcp_response_fills_default_block_when_core_lacks_field():
    result = _builder_result()
    del result["river_propagation"]
    resp = frit._format_mcp_response(result, {"time_range_readable": "t"}, 50.0, set(), set())
    assert resp["river_propagation"] == {"flow_velocity_mps": 2.0, "rivers": []}


def test_build_result_forwards_velocity_to_builder(monkeypatch):
    captured = {}

    def fake_builder(stations, **kwargs):
        captured.update(kwargs)
        return _builder_result(
            river_propagation={"flow_velocity_mps": kwargs["flow_velocity_mps"], "rivers": []}
        )

    monkeypatch.setattr(frit, "_load_impact_builder", lambda: fake_builder)
    rainfall_result = {
        "time_range_readable": "t",
        "level_analysis": [
            {"level": "暴雨", "stations": [{"name": "s1", "lon": 117.0, "lat": 39.0, "rainfall": 80.0}]}
        ],
    }
    frit.build_affected_river_network_result(
        time_str="20260723080000",
        start_time="",
        end_time="",
        rainfall_threshold_mm=50.0,
        max_edges=100,
        include_background=True,
        downstream_km=50.0,
        direct_graph_match_km=10.0,
        pg_conf={},
        analyze_rainfall_core=lambda *a, **k: rainfall_result,
        rain_levels=[("暴雨", 50.0, 99.9)],
        graph_path=None,
        flow_velocity_mps=3.0,
    )
    assert captured["flow_velocity_mps"] == 3.0


def test_build_result_zero_velocity_uses_default(monkeypatch):
    captured = {}

    def fake_builder(stations, **kwargs):
        captured.update(kwargs)
        return _builder_result()

    monkeypatch.setattr(frit, "_load_impact_builder", lambda: fake_builder)
    rainfall_result = {
        "time_range_readable": "t",
        "level_analysis": [
            {"level": "暴雨", "stations": [{"name": "s1", "lon": 117.0, "lat": 39.0, "rainfall": 80.0}]}
        ],
    }
    frit.build_affected_river_network_result(
        time_str="20260723080000",
        start_time="",
        end_time="",
        rainfall_threshold_mm=50.0,
        max_edges=100,
        include_background=True,
        downstream_km=50.0,
        direct_graph_match_km=10.0,
        pg_conf={},
        analyze_rainfall_core=lambda *a, **k: rainfall_result,
        rain_levels=[("暴雨", 50.0, 99.9)],
        graph_path=None,
    )
    assert captured["flow_velocity_mps"] == 2.0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /d D:\PythonProject\haiheliuyubaoyuagent-master\haiheliuyubaoyuagent-master\haihe-weather-analyzer-mcp && python -m pytest test_fixed_rainfall_impact_propagation.py -v`
Expected: FAIL（`AttributeError: ... no attribute '_resolve_flow_velocity'` / TypeError 多余关键字参数）

- [ ] **Step 3: 实现 MCP 适配层改动**

`fixed_rainfall_impact_tool.py`：

a) 常量区（`DEFAULT_DIRECT_GRAPH_MATCH_KM` 之后）：

```python
DEFAULT_FLOW_VELOCITY_MPS = 2.0
```

b) `IMPACT_RULES` 增加条目：

```python
    "propagation": "传播时间按统一经验流速 flow_velocity_mps（默认 2.0 m/s ≈ 7.2 km/h）估算：河流级传播距离 ÷ 流速；下游河流取 Dijkstra 累计 end_distance_km 最大值，仅直接受影响的河流取最长直接河段长度。",
```

c) 新增解析函数（放在 `_station_reaches_threshold` 前）：

```python
def _resolve_flow_velocity(flow_velocity_mps: float) -> float:
    """0 = 使用默认经验流速；负数报错；正数原样返回。"""
    value = float(flow_velocity_mps or 0.0)
    if value < 0:
        raise ValueError("flow_velocity_mps 不能为负数")
    return value if value > 0 else DEFAULT_FLOW_VELOCITY_MPS
```

d) `_base_response_fields` 签名尾部加 `river_propagation: dict | None = None`，返回 dict 中 `"river_geojson"` 之后加：

```python
        "river_propagation": river_propagation
        or {"flow_velocity_mps": DEFAULT_FLOW_VELOCITY_MPS, "rivers": []},
```

e) `_empty_response` 签名尾部加 `flow_velocity_mps: float = DEFAULT_FLOW_VELOCITY_MPS`，`_base_response_fields(...)` 调用加 `river_propagation={"flow_velocity_mps": float(flow_velocity_mps), "rivers": []}`。

f) `_format_mcp_response` 的 `_base_response_fields(...)` 调用加 `river_propagation=result.get("river_propagation")`。

g) `build_affected_river_network_result` 签名尾部加 `flow_velocity_mps: float = 0.0`；函数体开头：

```python
    velocity = _resolve_flow_velocity(flow_velocity_mps)
```

`_empty_response(...)` 调用加 `flow_velocity_mps=velocity`；`builder(...)` 调用加 `flow_velocity_mps=velocity`。

h) 注册工具 `get_affected_river_network_by_rainfall` 签名加 `flow_velocity_mps: float = 0.0`（docstring 补一行：`- flow_velocity_mps: 经验流速 m/s，0 表示默认 2.0。`），`build_affected_river_network_result(...)` 调用加 `flow_velocity_mps=flow_velocity_mps`。

i) `server.py:78` 工具描述改为：

```python
                    "get_affected_river_network_by_rainfall - 暴雨影响河流专题图（30km直接不截断，下游50km截断；直接河段匹配10km口径对齐牵引智能体；返回 river_propagation 河流级传播时间估算，默认经验流速2m/s）",
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /d D:\PythonProject\haiheliuyubaoyuagent-master\haiheliuyubaoyuagent-master\haihe-weather-analyzer-mcp && python -m pytest test_fixed_rainfall_impact_propagation.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/server.py haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py
git commit -m "feat(rain-impact): pass through river propagation time in MCP tool response"
```

---

### Task 3: chainlitexam 本地工具透传

**Files:**
- Modify: `haiheliuyubaoyuagent-master/chainlitexam/tools/rainfall_river_impact.py`
- Test: `haiheliuyubaoyuagent-master/chainlitexam/tests/test_rainfall_river_impact.py`

**Interfaces:**
- Consumes: Task 2 的 `build_affected_river_network_result(..., flow_velocity_mps=...)`。
- Produces: `local_get_affected_river_network_by_rainfall(..., flow_velocity_mps: float = 0.0)`（0 = MCP 层默认，与 Task 2 语义一致）。

- [ ] **Step 1: 写失败测试**

`test_rainfall_river_impact.py` 末尾追加：

```python
def test_local_tool_passes_flow_velocity_default():
    """未显式传 flow_velocity_mps 时透传 0（由 MCP 层替换为默认 2.0）。"""
    _, impact_mod = _run_patched_tool(time_str="20250713080000")
    call_kwargs = impact_mod.build_affected_river_network_result.call_args.kwargs
    assert call_kwargs["flow_velocity_mps"] == 0.0


def test_local_tool_allows_custom_flow_velocity():
    _, impact_mod = _run_patched_tool(time_str="20250713080000", flow_velocity_mps=3.0)
    call_kwargs = impact_mod.build_affected_river_network_result.call_args.kwargs
    assert call_kwargs["flow_velocity_mps"] == 3.0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /d D:\PythonProject\haiheliuyubaoyuagent-master\haiheliuyubaoyuagent-master\chainlitexam && python -m pytest tests/test_rainfall_river_impact.py -v -k flow_velocity`
Expected: FAIL（`TypeError: ... got an unexpected keyword argument 'flow_velocity_mps'`）

- [ ] **Step 3: 实现透传**

`rainfall_river_impact.py`：

a) `_call_affected_river_network` 签名尾部加 `flow_velocity_mps: float`，`impact_mod.build_affected_river_network_result(...)` 调用加 `flow_velocity_mps=flow_velocity_mps`。

b) `local_get_affected_river_network_by_rainfall` 签名 `direct_graph_match_km: float = 10.0` 之后加 `flow_velocity_mps: float = 0.0`；docstring 参数列表补一行：`- flow_velocity_mps: 经验流速 m/s，0 表示默认 2.0。`；`_call_affected_river_network(...)` 调用加 `flow_velocity_mps=flow_velocity_mps`。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /d D:\PythonProject\haiheliuyubaoyuagent-master\haiheliuyubaoyuagent-master\chainlitexam && python -m pytest tests/test_rainfall_river_impact.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add haiheliuyubaoyuagent-master/chainlitexam/tools/rainfall_river_impact.py haiheliuyubaoyuagent-master/chainlitexam/tests/test_rainfall_river_impact.py
git commit -m "feat(rain-impact): forward flow velocity param in local rainfall river impact tool"
```

---

### Task 4: 问答层简报与提示词

**Files:**
- Modify: `haiheliuyubaoyuagent-master/chainlitexam/message_orchestrator.py:997-1034`（`_build_affected_river_network_brief`）
- Modify: `haiheliuyubaoyuagent-master/chainlitexam/prompts.py:344`（规则 2.5）
- Test: `haiheliuyubaoyuagent-master/chainlitexam/tests/test_message_orchestrator.py`

**Interfaces:**
- Consumes: Task 2 响应中的 `river_propagation` 块（`{"flow_velocity_mps": float, "rivers": [...]}`，rivers 已按传播时间降序）。
- Produces: `_build_affected_river_network_brief(result_data, user_text)` 输出末尾（分区/行政区块之后、专题图说明行之前）新增一行传播时间说明；`river_propagation` 缺失或 `rivers` 为空时不加该行。

- [ ] **Step 1: 写失败测试**

`test_message_orchestrator.py` 末尾追加（沿用该文件既有 `import message_orchestrator as mo` 的导入方式；若该文件用 stub，按文件顶部现有模式导入）：

```python
def _impact_result_with_propagation():
    return {
        "time_range_readable": "2026-07-22 08:00 ~ 2026-07-23 08:00",
        "rainfall_threshold_mm": 50.0,
        "affected_rivers": ["滦河"],
        "affected_zone_77_regions": ["滦河山区"],
        "affected_admin_divisions": ["承德市"],
        "total_segments": 3,
        "affected_segments": 3,
        "river_propagation": {
            "flow_velocity_mps": 2.0,
            "rivers": [
                {
                    "river_name": "滦河",
                    "propagation_distance_km": 48.2,
                    "propagation_time_hours": 6.7,
                    "arrival_estimate_readable": "约6.7小时",
                }
            ],
        },
    }


def test_brief_includes_propagation_summary():
    brief = mo._build_affected_river_network_brief(_impact_result_with_propagation(), "暴雨影响哪些河系")
    assert "按经验流速 2.0 m/s 估算" in brief
    assert "约6.7小时" in brief
    assert "48.2" in brief
    assert "滦河" in brief


def test_brief_without_propagation_block_stays_compatible():
    result = _impact_result_with_propagation()
    del result["river_propagation"]
    brief = mo._build_affected_river_network_brief(result, "暴雨影响哪些河系")
    assert "经验流速" not in brief
    assert "滦河" in brief  # 既有河系列表不受影响
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /d D:\PythonProject\haiheliuyubaoyuagent-master\haiheliuyubaoyuagent-master\chainlitexam && python -m pytest tests/test_message_orchestrator.py -v -k propagation`
Expected: FAIL（`assert '按经验流速 2.0 m/s 估算' in brief`）

- [ ] **Step 3: 实现简报与提示词改动**

a) `message_orchestrator.py` 的 `_build_affected_river_network_brief`，在 `if affected_admins:` 块之后、`lines.extend(["", f"专题图已按受影响河段高亮渲染...` 之前插入：

```python
    propagation = result_data.get("river_propagation") or {}
    propagation_rivers = propagation.get("rivers") or []
    if propagation_rivers:
        top = propagation_rivers[0]
        velocity = propagation.get("flow_velocity_mps", 2.0)
        lines.extend([
            "",
            f"按经验流速 {velocity} m/s 估算，影响预计{top.get('arrival_estimate_readable', '')}"
            f"传播至下游最远约 {top.get('propagation_distance_km')} 公里（{top.get('river_name')}）。",
        ])
```

b) `prompts.py` 规则 2.5（第 344 行）末尾追加一句：

```
若工具返回含 river_propagation 传播时间估算，回答需一并说明预计传播时间，并注明“按经验流速估算”。
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /d D:\PythonProject\haiheliuyubaoyuagent-master\haiheliuyubaoyuagent-master\chainlitexam && python -m pytest tests/test_message_orchestrator.py -v && python tests/test_fast_paths.py`
Expected: pytest 全部 PASS；fast_paths 静态检查通过（快路径契约：`_show_business_reasoning`/reasoning 关闭等未被破坏）

- [ ] **Step 5: 提交**

```bash
git add haiheliuyubaoyuagent-master/chainlitexam/message_orchestrator.py haiheliuyubaoyuagent-master/chainlitexam/prompts.py haiheliuyubaoyuagent-master/chainlitexam/tests/test_message_orchestrator.py
git commit -m "feat(rain-impact): show propagation time estimate in affected-river brief"
```

---

### Task 5: 全链路回归验证

**Files:** 无改动，仅验证。

- [ ] **Step 1: 牵引智能体测试**

Run: `cd /d D:\PythonProject\haiheliuyubaoyuagent-master\hhlyqyxt-master && python -m pytest utils/tests/ -v`
Expected: 全部 PASS

- [ ] **Step 2: MCP 侧测试**

Run: `cd /d D:\PythonProject\haiheliuyubaoyuagent-master\haiheliuyubaoyuagent-master\haihe-weather-analyzer-mcp && python -m pytest test_fixed_rainfall_impact_propagation.py -v`
Expected: 全部 PASS

- [ ] **Step 3: chainlitexam 全量测试**

Run: `cd /d D:\PythonProject\haiheliuyubaoyuagent-master\haiheliuyubaoyuagent-master\chainlitexam && python -m pytest tests/ -v && python tests/test_fast_paths.py`
Expected: 全部 PASS

- [ ] **Step 4: 质量流程（按 CLAUDE.md Superpowers Integration 约定）**

按顺序执行：
1. `code-review` 对 `git diff` 未推送提交做审查，修复确认的问题；
2. `code-simplifier` 对本次改动做简化检查；
3. `superpowers:verification-before-completion`；
4. `claude-md-management:revise-claude-md` 把 `river_propagation` 字段约定补入 CLAUDE.md（`fixed_rainfall_impact_tool` 条目处）；
5. 推送：`git push`（只推送本需求相关提交）。

---

## Self-Review 记录

- **Spec 覆盖**：spec §3→Task 1；§4→Task 2（含 server.py 描述、IMPACT_RULES、本地包装签名同步→Task 3）；§5→Task 4；§6 错误处理→Task 1c/2c/4 的容忍逻辑；§7 测试→各 Task Step 1 + Task 5。
- **类型一致性**：`flow_velocity_mps` 全链同名；`_build_river_propagation` / `_resolve_flow_velocity` / `DEFAULT_FLOW_VELOCITY_MPS` 在 Produces 与消费处拼写一致；`river_propagation` 结构三处（核心、MCP、brief）字段名一致。
- **占位符扫描**：无 TBD/TODO；所有代码步骤含完整代码。
