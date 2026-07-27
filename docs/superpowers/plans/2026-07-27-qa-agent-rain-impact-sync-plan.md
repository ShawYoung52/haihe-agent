# 问答智能体暴雨影响河流 · MCP 层契约同步 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 问答智能体 MCP 层（`fixed_rainfall_impact_tool.py`）与牵引层（`rainfall_impact_geojson.py`）契约同步：站点缓冲半径默认 20km、GeoJSON feature 携带 `t0_source_time` / `estimated_arrival_time`、顶层 result 带 `reference_time`。

**Architecture:** MCP 层动态 import 牵引层 builder，builder 侧已更新。本 plan 只在 MCP 层修正硬编码 30→20、补 `_derive_rain_end_time` 从 rainfall_result 派生 rain_end_time 传给 builder、在 `_base_response_fields` 透传 `reference_time`。不涉及签名变更或 builder 修改。

**Tech Stack:** Python 3.9+，`dateutil.parser` 用于字符串解析（既有可能已依赖）。

## Global Constraints

- 只改以下文件（Phase 0 确认 chainlitexam 和 verify 脚本不需改）：
  - `haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py`
  - `haihe-weather-analyzer-mcp/server.py`
  - `haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py`
  - `haiheliuyubaoyuagent-master/CLAUDE.md`（追加契约说明）
- 不动：`hhlyqyxt-master/*`、`chainlitexam/*`、`scripts/verify_river_propagation_offline.py`、builder 签名、MCP 工具签名。
- 所有新增字段向后兼容（缺失→None/null）。
- 时间格式：`YYYY-MM-DDTHH:MM:SSZ`（UTC，秒级，末尾 Z）—— builder 侧 `_iso_utc` 产出。
- `_derive_rain_end_time` 宽松解析，无匹配返回 None（不 raise）。
- 只用 `git add` 精确路径（[[haihe-project-env-quirks]]）。
- 所有测试用 `.venv\Scripts\python.exe` 绝对路径执行（Windows Store python 陷阱）。

---

### Task 1: Phase 0 — 分支 + baseline

**Files:**
- 无代码改动

**Interfaces:**
- Consumes: 无
- Produces: baseline 记录

- [ ] **Step 1: 创建分支**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master
git checkout -b feat/qa-agent-rain-impact-sync
```

- [ ] **Step 2: 记录 baseline 测试数**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master
# 必须有 venv 且装了 pytest。用绝对路径 python
ls /d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe 2>&1
# 如果不存在，先创建
# py -m venv /d/PythonProject/haiheliuyubaoyuagent-master/.venv
# pip install pytest pandas requests python-dateutil
```

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py -v 2>&1 | tail -10
```

预期：`7 passed`（现有 7 条测试）。

```bash
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest chainlitexam/tests/test_rainfall_river_impact.py -v 2>&1 | tail -3
```

预期：视环境而定，可能因 langchain 依赖缺失被跳过（记录即可）。

- [ ] **Step 3: 确认无遗漏硬编码**

```bash
grep -rn "30km\|station_buffer_km.*30\b" haihe-weather-analyzer-mcp/ --include="*.py" 2>&1
```

预期只显示以下 3 个位置（待 Phase 1 修）：
- `fixed_rainfall_impact_tool.py:23` IMPACT_RULES["direct"] 文字
- `fixed_rainfall_impact_tool.py:215` _empty_response
- `server.py:80` 工具描述

- [ ] **Step 4: 记录 baseline 到 ledger**

```bash
mkdir -p .superpowers/sdd
cat > .superpowers/sdd/progress.md << 'LEDGER'
# SDD Progress Ledger — feat/qa-agent-rain-impact-sync

Base commit: <current HEAD>

## Baseline
- pytest fixed_rainfall_impact_propagation: 7 passed
- chainlitexam rainfall_river_impact: 无硬编码（已确认，不需改）
- verify_river_propagation_offline: 30.0 仅用于参数校验，不是默认值断言（不需改）
- CLAUDE.md: 有 MCP 层 keep-in-sync 说明（83-84 行），需追加

## Tasks
LEDGER
```

---

### Task 2: Phase 1 — MCP 硬编码 30→20

**Files:**
- Modify: `haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py`（第 23、215 行）
- Modify: `haihe-weather-analyzer-mcp/server.py`（第 80 行）
- Modify: `haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py`（新增测试 1-2）

**Interfaces:**
- Consumes: 无
- Produces: MCP 层无 30km 硬编码

- [ ] **Step 1: 写测试 `test_default_station_buffer_km_matches_traction_agent`**

```python
def test_default_station_buffer_km_matches_traction_agent():
    """IMPACT_RULES["direct"] 应描述 20km 而非 30km。"""
    import fixed_rainfall_impact_tool as frit
    rules = frit.IMPACT_RULES
    direct_text = rules.get("direct", "")
    assert "20km" in direct_text, f"IMPACT_RULES.direct 应含 20km，实际: {direct_text}"
    assert "30km" not in direct_text, "IMPACT_RULES.direct 不应含 30km"
```

- [ ] **Step 2: 写测试 `test_empty_response_station_buffer_km_is_20`**

```python
def test_empty_response_station_buffer_km_is_20():
    """_empty_response 的 station_buffer_km 应为 20.0。"""
    import fixed_rainfall_impact_tool as frit
    resp = frit._empty_response(
        rainfall_result={},
        threshold_mm=50.0, zones=set(), admins=set(),
    )
    start_stats = resp.get("start_stats", {})
    downstream_start_stats = start_stats.get("downstream_start_stats", {})
    assert downstream_start_stats.get("station_buffer_km") == 20.0, \
        f"station_buffer_km 应为 20.0，实际: {downstream_start_stats.get('station_buffer_km')}"
```

- [ ] **Step 3: 运行确认 FAIL**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py::test_default_station_buffer_km_matches_traction_agent -v 2>&1
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py::test_empty_response_station_buffer_km_is_20 -v 2>&1
```

预期：`FAILED`。

- [ ] **Step 4: 修改 `fixed_rainfall_impact_tool.py` 第 23 行**

```python
"direct": f"full_{RIVER_TABLE_VERSION} 中位于暴雨站点 station_buffer_km（默认 20km）缓冲区内的候选行全部作为 direct_buffer 输出；其中距站点 ≤ direct_match_km（默认 10km）的标记 is_direct_graph_edge=true。距离分类用 SQL 真实几何最近距离，非 pkl 端点弦距。",
```

- [ ] **Step 5: 修改 `fixed_rainfall_impact_tool.py` 第 215 行**

```python
"station_buffer_km": 20.0,
```

- [ ] **Step 6: 修改 `server.py` 第 80 行**

找到第 80 行（工具描述中 "30km直接不截断"），修改为 "20km直接不截断"。同时 description 第一行末尾附带 arrival 说明（可选，但建议）：

```python
"get_affected_river_network_by_rainfall - 暴雨影响河流专题图（20km直接不截断，下游50km截断；直接河段匹配10km口径对齐牵引智能体；返回 river_propagation 河流级传播时间估算 + per-feature estimated_arrival_time 钟表时刻，默认经验流速2m/s）",
```

- [ ] **Step 7: 运行确认 PASS**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py -v 2>&1 | tail -10
```

预期：`9 passed`（基线 7 + 新增 2）。

- [ ] **Step 8: Commit**

```bash
git add -A haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py haihe-weather-analyzer-mcp/server.py haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py
git commit -m "feat(qa-agent): MCP hardcoded 30km→20km (IMPACT_RULES, _empty_response, server.py desc)"
```

---

### Task 3: Phase 2 — rain_end_time 派生 + 传递

**Files:**
- Modify: `haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py`
- Modify: `haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py`

**Interfaces:**
- Consumes: `_normalize_station(station, level)` 签名增加 `rain_end_time: str | None = None`
- Produces: `_derive_rain_end_time(rainfall_result) -> str | None`
- Produces: `_normalize_station` 输出新增 `rain_end_time: str | None`
- Produces: `_extract_rainstorm_stations` 内部调用 `_derive_rain_end_time` 一次，传给每个 station

- [ ] **Step 1: 写测试 `test_derive_rain_end_time_from_time_range_readable`**

```python
def test_derive_rain_end_time_from_time_range_readable():
    """从 time_range_readable（"至 YYYY-MM-DD HH:MM"）派生 ISO 结束时刻。"""
    import fixed_rainfall_impact_tool as frit
    result = {"time_range_readable": "2026-07-27 15:30 至 2026-07-28 07:30"}
    end = frit._derive_rain_end_time(result)
    assert end is not None
    # 结果应为 ISO 格式字符串，如 "2026-07-28 07:30" 或类似
    assert "07:30" in str(end) or "07:30" in end
```

- [ ] **Step 2: 写测试 `test_derive_rain_end_time_returns_none_when_missing`**

```python
def test_derive_rain_end_time_returns_none_when_missing():
    """完全无时间字段时返回 None，不抛异常。"""
    import fixed_rainfall_impact_tool as frit
    assert frit._derive_rain_end_time({}) is None
    assert frit._derive_rain_end_time({"foo": "bar"}) is None
    assert frit._derive_rain_end_time(None) is None
```

- [ ] **Step 3: 写测试 `test_normalize_station_adds_rain_end_time`**

```python
def test_normalize_station_adds_rain_end_time():
    """_normalize_station 输出含 rain_end_time 字段。"""
    import fixed_rainfall_impact_tool as frit
    station = {"station_id": "A", "name": "站A", "lon": 117.0, "lat": 39.0, "rainfall": 60.0}
    result = frit._normalize_station(station, "暴雨", rain_end_time="2026-07-28T07:30:00Z")
    assert result.get("rain_end_time") == "2026-07-28T07:30:00Z"
```

- [ ] **Step 4: 写测试 `test_normalize_station_defaults_rain_end_time_to_none`**

```python
def test_normalize_station_defaults_rain_end_time_to_none():
    """老调用不传 rain_end_time 时，输出 rain_end_time 为 None。"""
    import fixed_rainfall_impact_tool as frit
    station = {"station_id": "A", "name": "站A", "lon": 117.0, "lat": 39.0, "rainfall": 60.0}
    result = frit._normalize_station(station, "暴雨")  # 不传 rain_end_time
    assert result.get("rain_end_time") is None
```

- [ ] **Step 5: 运行确认 FAIL**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py::test_derive_rain_end_time_from_time_range_readable -v 2>&1
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py::test_derive_rain_end_time_returns_none_when_missing -v 2>&1
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py::test_normalize_station_adds_rain_end_time -v 2>&1
```

预期：全部 FAIL。

- [ ] **Step 6: 实现 `_derive_rain_end_time`**

在 `fixed_rainfall_impact_tool.py` 中，`_normalize_station` 函数之前（约 line 80 前）插入：

```python
import re
from typing import Any

def _derive_rain_end_time(rainfall_result: dict | None) -> str | None:
    """从 rainfall_result 顶层派生 rain_end_time ISO 字符串。

    尝试顺序：
    1. time_range 字段（"[start,end]" 格式，取 end 部分）
    2. time_range_readable 字段（"X 至 Y" 格式，取 Y）
    若都缺失或无法解析，返回 None（不抛异常）。
    """
    if not isinstance(rainfall_result, dict):
        return None

    # 优先 time_range 结构化字段
    tr = rainfall_result.get("time_range")
    if isinstance(tr, str) and "," in tr:
        # 格式 "[YYYYMMDDHHMMSS,YYYYMMDDHHMMSS]" 或 "[YYYY-MM-DD HH:MM,YYYY-MM-DD HH:MM]"
        parts = tr.strip("[]").split(",", 1)
        if len(parts) == 2:
            end_raw = parts[1].strip()
            if end_raw and len(end_raw) >= 8:
                # 尝试解析并转 ISO
                try:
                    from dateutil import parser as dateparser
                    dt = dateparser.parse(end_raw)
                    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                except Exception:
                    logger.warning("解析 time_range 失败: %s", end_raw)
                    return end_raw  # 返回原始字符串，builder 侧 _normalize_end_time 会再处理

    # 次选 time_range_readable（中文格式）
    trr = rainfall_result.get("time_range_readable")
    if isinstance(trr, str):
        # 格式 "X 至 Y" 或 "X ~ Y"
        m = re.search(r"[至~]\s*(\S+(?:\s+\S+)?)\s*$", trr)
        if m:
            end_raw = m.group(1).strip()
            try:
                from dateutil import parser as dateparser
                dt = dateparser.parse(end_raw)
                return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                logger.warning("解析 time_range_readable 失败: %s", end_raw)
                return end_raw

    return None
```

- [ ] **Step 7: 修改 `_normalize_station` 签名和输出**

第 81 行签名：
```python
def _normalize_station(station: dict, level: str, rain_end_time: str | None = None) -> dict:
```

第 82-92 行，在 `return` 字典中增加 `rain_end_time`：
```python
def _normalize_station(station: dict, level: str, rain_end_time: str | None = None) -> dict:
    name = station.get("name") or station.get("station_name")
    return {
        "station_id": station.get("station_id"),
        "station_name": name,
        "name": name,
        "lon": station.get("lon"),
        "lat": station.get("lat"),
        "rainfall": station.get("rainfall"),
        "rain_24h": station.get("rainfall"),
        "level": level,
        "rain_end_time": rain_end_time,
    }
```

- [ ] **Step 8: 修改 `_extract_rainstorm_stations` 传递 rain_end_time**

在第 99 行函数体中，level_to_threshold 之后 immediately 调用 `_derive_rain_end_time`：

```python
def _extract_rainstorm_stations(
    rainfall_result: dict,
    threshold_mm: float,
    rain_levels: list[tuple[str, float, float]],
) -> tuple[list[dict], set[str], set[str]]:
    level_to_threshold = {name: low for name, low, _high in rain_levels}
    # 派生 rain_end_time（所有站点共用查询时段结束时刻）
    derived_rain_end_time = _derive_rain_end_time(rainfall_result)
    ...
```

然后将 `_normalize_station(station, level)` 改为：
```python
_normalize_station(station, level, rain_end_time=derived_rain_end_time)
```

- [ ] **Step 9: 运行确认 PASS**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py -v 2>&1 | tail -10
```

预期：`13 passed`（基线 7 + 2 + 4 = 13）。

- [ ] **Step 10: Commit**

```bash
git add -A haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py
git commit -m "feat(qa-agent): _derive_rain_end_time + _normalize_station rain_end_time field"
```

---

### Task 4: Phase 3 — reference_time 顶层透传

**Files:**
- Modify: `haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py`
- Modify: `haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py`

**Interfaces:**
- Consumes: `_base_response_fields` 签名新增 `reference_time: str | None = None`
- Consumes: `_empty_response` 传 `reference_time=None`
- Consumes: `_format_mcp_response` 从 `result.get("params", {}).get("reference_time")` 取
- Produces: 顶层 dict 新增 `reference_time: str | None`

- [ ] **Step 1: 写测试 `test_base_response_fields_includes_reference_time`**

```python
def test_base_response_fields_includes_reference_time():
    """_base_response_fields 传 reference_time 时，顶层 dict 包含该字段。"""
    import fixed_rainfall_impact_tool as frit
    from types import SimpleNamespace
    # 构造最小参数
    resp = frit._base_response_fields(
        rainfall_result={}, threshold_mm=50.0, zones=set(), admins=set(),
        stations=[], segments=[], river_geojson=None,
        start_stats={}, summary="test",
        reference_time="2026-07-28T07:30:00Z",
    )
    assert resp.get("reference_time") == "2026-07-28T07:30:00Z"
```

- [ ] **Step 2: 写测试 `test_base_response_fields_reference_time_defaults_none`**

```python
def test_base_response_fields_reference_time_defaults_none():
    """不传 reference_time 时，顶层 dict 有 reference_time: None。"""
    import fixed_rainfall_impact_tool as frit
    resp = frit._base_response_fields(
        rainfall_result={}, threshold_mm=50.0, zones=set(), admins=set(),
        stations=[], segments=[], river_geojson=None,
        start_stats={}, summary="test",
    )
    assert resp.get("reference_time") is None
```

- [ ] **Step 3: 写测试 `test_format_mcp_response_extracts_reference_time_from_builder_result`**

```python
def test_format_mcp_response_extracts_reference_time_from_builder_result():
    """_format_mcp_response 从 builder result.params.reference_time 提取到顶层。"""
    import fixed_rainfall_impact_tool as frit
    # 构造模拟 builder result
    mock_result = {
        "segments": [],
        "river_geojson": {"type": "FeatureCollection", "features": []},
        "downstream_start_stats": {},
        "impact_stations": [],
        "river_summary": {"downstream_edge_count": 0},
        "params": {"reference_time": "2026-07-28T07:30:00Z"},
        "river_propagation": {"flow_velocity_mps": 2.0, "rivers": []},
    }
    rainfall_result = {"time_range_readable": "test"}
    resp = frit._format_mcp_response(
        mock_result, rainfall_result, 50.0, set(), set()
    )
    assert resp.get("reference_time") == "2026-07-28T07:30:00Z"
```

- [ ] **Step 4: 写测试 `test_format_mcp_response_reference_time_none_when_params_missing`**

```python
def test_format_mcp_response_reference_time_none_when_params_missing():
    """老 builder 无 params 时，reference_time 降级为 None。"""
    import fixed_rainfall_impact_tool as frit
    mock_result = {
        "segments": [],
        "river_geojson": {"type": "FeatureCollection", "features": []},
        "downstream_start_stats": {},
        "impact_stations": [],
        "river_summary": {"downstream_edge_count": 0},
        # 故意缺失 params
        "river_propagation": {"flow_velocity_mps": 2.0, "rivers": []},
    }
    rainfall_result = {"time_range_readable": "test"}
    resp = frit._format_mcp_response(
        mock_result, rainfall_result, 50.0, set(), set()
    )
    assert resp.get("reference_time") is None
```

- [ ] **Step 5: 运行确认 FAIL**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py::test_base_response_fields_includes_reference_time -v 2>&1
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py::test_base_response_fields_reference_time_defaults_none -v 2>&1
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py::test_format_mcp_response_extracts_reference_time_from_builder_result -v 2>&1
```

预期：全部 FAIL。

- [ ] **Step 6: 修改 `_base_response_fields` 签名和返回**

第 145-158 行签名，新增可选形参：
```python
def _base_response_fields(
    rainfall_result: dict,
    threshold_mm: float,
    zones: set[str],
    admins: set[str],
    stations: list[dict],
    segments: list[dict],
    river_geojson: dict | None,
    start_stats: dict,
    summary: str,
    affected_rivers: list[str] | None = None,
    rules: dict | None = None,
    river_propagation: dict | None = None,
    reference_time: str | None = None,  # 新增
) -> dict:
```

第 160-178 行返回 dict 中新增 `reference_time`：
```python
response = {
    ...
    "river_propagation": river_propagation or _empty_propagation(),
    "reference_time": reference_time,
}
```

- [ ] **Step 7: 修改 `_empty_response` 传 reference_time=None**

第 195-227 行 `_empty_response` 调用 `_base_response_fields` 时，在末尾传 `reference_time=None`。由于 `_base_response_fields` 默认就是 None，**实际上不需要改参数传递**（默认值就行）。但为了清晰，可以在调用处显式加 `reference_time=None`。

- [ ] **Step 8: 修改 `_format_mcp_response` 提取 reference_time**

第 230-260 行 `_format_mcp_response` 函数，在调用 `_base_response_fields` 之前，提取 `reference_time`：

```python
def _format_mcp_response(
    result: dict, rainfall_result: dict, threshold_mm: float, zones: set[str], admins: set[str]
) -> dict:
    segments = result.get("segments", [])
    river_geojson = result.get("river_geojson")
    downstream_start_stats = result.get("downstream_start_stats", {})
    affected_rivers = result.get("affected_rivers")
    if affected_rivers is None:
        affected_rivers = sorted(
            {str(s.get("rivername") or "").strip() for s in segments if s.get("rivername")}
        )
    # 提取 reference_time（builder 侧 params 中）
    reference_time = result.get("params", {}).get("reference_time")
    return _base_response_fields(
        ...
        reference_time=reference_time,
    )
```

- [ ] **Step 9: 运行确认 PASS**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py -v 2>&1 | tail -10
```

预期：`17 passed`（基线 7 + 2 + 4 + 4 = 17）。

- [ ] **Step 10: Commit**

```bash
git add -A haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py
git commit -m "feat(qa-agent): _base_response_fields + _format_mcp_response reference_time透传"
```

---

### Task 5: Phase 4 — IMPACT_RULES 增补 arrival 描述

**Files:**
- Modify: `haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py`
- Modify: `haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py`

**Interfaces:**
- Consumes: `IMPACT_RULES` dict
- Produces: `IMPACT_RULES["arrival"]` 描述文字

- [ ] **Step 1: 写测试 `test_impact_rules_contains_arrival_description`**

```python
def test_impact_rules_contains_arrival_description():
    """IMPACT_RULES 应包含 arrival 键且含 'estimated_arrival_time'。"""
    import fixed_rainfall_impact_tool as frit
    rules = frit.IMPACT_RULES
    assert "arrival" in rules, "IMPACT_RULES 应包含 arrival 键"
    text = rules["arrival"]
    assert "estimated_arrival_time" in text, "arrival 描述应含 estimated_arrival_time"
    assert "t0_source_time" in text, "arrival 描述应含 t0_source_time"
    assert "reference_time" in text, "arrival 描述应含 reference_time"
```

- [ ] **Step 2: 运行确认 FAIL**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py::test_impact_rules_contains_arrival_description -v 2>&1
```

预期：FAILED。

- [ ] **Step 3: 在 `IMPACT_RULES` 末尾新增 `arrival` 键**

在 `IMPACT_RULES` 字典（第 22-31 行）末尾增加：

```python
"arrival": (
    "GeoJSON feature.properties.estimated_arrival_time（UTC ISO 8601 Z 格式）"
    "= t0_source_time + propagation_time_hours。直接段 T0 = 该边所有 trigger 站点中最早 "
    "rain_end_time；下游段 T0 = 上游 direct 段中最早 rain_end_time（沿 BFS 路径传播）。"
    "顶层 reference_time = 所有 rainstorm_stations 中最早 rain_end_time（UTC ISO Z）。"
    "站点 rain_end_time 从 rainfall_result 顶层 time_range 末端派生，"
    "所有站点共用查询时段结束时刻。"
),
```

- [ ] **Step 4: 运行确认 PASS**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py -v 2>&1 | tail -10
```

预期：`18 passed`。

- [ ] **Step 5: Commit**

```bash
git add -A haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py
git commit -m "feat(qa-agent): IMPACT_RULES add arrival description"
```

---

### Task 6: Phase 5 — CLAUDE.md 追加契约说明

**Files:**
- Modify: `haiheliuyubaoyuagent-master/CLAUDE.md`

- [ ] **Step 1: 确认当前 CLAUDE.md 中 MCP 层相关行**

```bash
grep -n "fixed_rainfall_impact\|station_buffer\|get_affected_river" haiheliuyubaoyuagent-master/CLAUDE.md 2>&1
```

- [ ] **Step 2: 在 CLAUDE.md 中追加 MCP 层契约同步说明**

在 `CLAUDE.md` 末尾（或 `rainfall_impact_geojson.py` 相关段落下方）追加：

```markdown
- `fixed_rainfall_impact_tool.py` 的 IMPACT_RULES["direct"] 文字、_empty_response 中 station_buffer_km 硬编码、server.py 工具描述中的 "30km" 均已同步为 20km（2026-07-27 feat/qa-agent-rain-impact-sync）。`_base_response_fields` 顶层包含 `reference_time`（builder result.params.reference_time 透传）。`_normalize_station` 输出含 `rain_end_time`（从 rainfall_result 顶层 time_range 末端派生，所有站点共用）。`IMPACT_RULES["arrival"]` 描述 estimated_arrival_time / t0_source_time / reference_time 语义。GeoJSON feature.properties 的 t0_source_time / estimated_arrival_time 由 builder 直接嵌入，MCP 层原样透传。
```

- [ ] **Step 3: Commit**

```bash
git add -A haiheliuyubaoyuagent-master/CLAUDE.md
git commit -m "docs(qa-agent): CLAUDE.md add 20km + arrival contract sync notes"
```

---

### Task 7: Phase 6 — code-simplifier + Pro 最终审查

**Files:**
- 读取所有已修改文件

- [ ] **Step 1: 运行完整 pytest 确认无回归**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py -v 2>&1 | tail -5
```

预期：`18 passed`。

- [ ] **Step 2: 使用 `superpowers:requesting-code-review` 请求最终审查**

（由 DeepSeek v4 Pro 行为约束的 agent 执行，重点检查：MCP 层与牵引层契约一致性、向后兼容性、_derive_rain_end_time 字符串解析鲁棒性）

- [ ] **Step 3: 根据审查意见修改并 re-run 测试**

---

### Task 8: Phase 7 — finishing (Push + PR + main merge)

**Files:**
- 无代码改动

- [ ] **Step 1: 确认最终测试全绿**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py -v 2>&1 | tail -3
```

- [ ] **Step 2: 清理 venv 记录、确认 `git status` 干净**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master
git status 2>&1
```

- [ ] **Step 3: Push 并创建 PR**

```bash
git push -u origin feat/qa-agent-rain-impact-sync
```

- [ ] **Step 4: 合入 main（用户确认后）**

```bash
git checkout main
git merge --ff-only feat/qa-agent-rain-impact-sync
git push origin main
git branch -d feat/qa-agent-rain-impact-sync
git push origin --delete feat/qa-agent-rain-impact-sync
```

- [ ] **Step 5: 落 claude-mem 新记忆**

使用 `claude-mem` skill 记录 `[[qa-agent-rain-impact-sync]]`，内容参考 `docs/superpowers/specs/2026-07-27-qa-agent-rain-impact-sync-design.md`。

- [ ] **Step 6: 更新 MEMORY.md 索引**

追加 `- [qa-agent-rain-impact-sync](qa-agent-rain-impact-sync.md)` 到 `C:\Users\Xiao\.claude\projects\D--PythonProject-haiheliuyubaoyuagent-master\memory\MEMORY.md`。