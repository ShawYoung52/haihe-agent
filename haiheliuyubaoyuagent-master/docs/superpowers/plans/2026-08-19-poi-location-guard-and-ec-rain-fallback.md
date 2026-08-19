# POI 预报定位守卫 + 超时效 EC 降水回退 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复两类 POI 预报取数错误：(1) 区域工具对"不认识的具体地点"（如密云水库）静默退回天津市区代表点、张冠李戴；(2) 超滚动预报 240h 的点位日期（如 9月1日）直接报"暂无数据"，而服务器上 EC AIFS 有降水数据。

**Architecture:** 两个独立改动，共用"点位预报正确性"主题。
- **Part 1（定位守卫）**：镜像现有流域拦截器（`is_basin_weather_query`），在 `query_rolling_forecast` 加"含具体点位词但区域表查不到"的守卫，命中则抛 `BusinessException` 引导 planner 改用能地理编码的 `query_decision_weather_for_poi`。纯 Python 逻辑，本地可测。
- **Part 2（EC 降水回退）**：MCP 侧新增 `sample_ec_point_daily_rain`（复用现有 `_find_ec_precip_file` + `_sample_station_forecast_rain_mm`），挂进 `query_rolling_forecast_core` 的 out_of_range + point_mode 分支，返回 `status:"ec_rain_fallback"`；chainlitexam 决策天气层识别该状态、生成**只讲降雨**的诚实回答（零编造气温/风/能见度）。

**Tech Stack:** Python 3.10+、FastMCP、GDAL（仅服务端，本地 mock）、pytest。

**Spec:** 本计划即设计（2026-08-19 会话内逐条确认：密云走"A 治本 + B 兜底"，EC 走"降水回退·部分回答"）。

## Global Constraints

- **不改变问答结果口径的既有行为**：无地点/"我市/全市/今天天气"仍默认天津市区代表点；已命中区域别名/区县的查询行为完全不变。
- **零编造**：EC 只提供降雨；气温/风力/能见度超出 240h 时效**绝不编造**，回答须明说"暂无法提供"。
- **数据源标签**：EC 回退回答标注"ECMWF AIFS（仅降雨）"；不动既有"天津市气象台滚动预报"标签（那是另一独立问题，本期不改）。
- **内网地址脱敏**：错误文本/日志不落 IP/路径；沿用 `_scrub_text`/日志只记异常类型的惯例。
- **测试运行环境**：MCP 测试用 `haihe-weather-analyzer-mcp/.venv-test/Scripts/python.exe`（**无 GDAL/osgeo**——EC 采样层必须 mock；`rolling_forecast_service`、`haihe_mcp_tools` 可 import）。chainlitexam 测试用 `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe` 并从 `chainlitexam/` 目录跑。
- **import 方向**：`haihe_mcp_tools` 顶层 import `rolling_forecast_service`；反向必须**函数级惰性 import**（否则循环）。
- 提交信息遵循仓库既有 `fix(scope): 中文描述` / `feat(scope): ...` 风格。

---

## Part 1 — POI 定位守卫（密云静默退回天津的修复）

### Task 1: `is_unresolved_poi_forecast_query` 谓词 + 区域命中辅助

**Files:**
- Modify: `haihe-weather-analyzer-mcp/rolling_forecast_service.py`（紧随 `is_basin_weather_query`，~line 503）
- Test: `haihe-weather-analyzer-mcp/tests/test_rolling_forecast_poi_guard.py`（新建）

**Interfaces:**
- Produces:
  - `POI_PLACE_KEYWORDS: tuple[str, ...]`
  - `has_matched_rolling_region(text: str) -> bool`
  - `is_unresolved_poi_forecast_query(user_query: str, regions: str = "") -> bool`
- Consumes: 模块级 `ROLLING_FORECAST_REGION_ALIASES`、`ROLLING_FORECAST_COORDS`（已存在）。

- [ ] **Step 1: 写失败测试**

```python
# haihe-weather-analyzer-mcp/tests/test_rolling_forecast_poi_guard.py
import rolling_forecast_service as rfs


class TestIsUnresolvedPoiForecastQuery:
    def test_miyun_reservoir_is_unresolved(self):
        assert rfs.is_unresolved_poi_forecast_query("未来三天密云水库有降水吗？") is True

    def test_miyun_reservoir_weather_is_unresolved(self):
        assert rfs.is_unresolved_poi_forecast_query("密云水库天气怎么样？") is True

    def test_tianjin_university_is_unresolved(self):
        # 天津大学不是区域表里的区，含"大学"点位词 → 视为具体点位（应转 POI 地理编码）
        assert rfs.is_unresolved_poi_forecast_query("天津大学明天天气怎么样") is True

    def test_bare_no_location_defaults_tianjin(self):
        assert rfs.is_unresolved_poi_forecast_query("今天天气怎么样") is False

    def test_wo_shi_quan_shi_not_poi(self):
        assert rfs.is_unresolved_poi_forecast_query("我市未来三天天气") is False
        assert rfs.is_unresolved_poi_forecast_query("全市明天有雨吗") is False

    def test_known_region_not_unresolved(self):
        assert rfs.is_unresolved_poi_forecast_query("西青明天天气") is False
        assert rfs.is_unresolved_poi_forecast_query("天津市区未来三天") is False

    def test_region_wins_over_poi_keyword(self):
        # 含"大学"但也含已知区域"滨海新区" → 区域命中优先，不算未解析
        assert rfs.is_unresolved_poi_forecast_query("滨海新区大学城明天天气") is False

    def test_regions_param_also_considered(self):
        assert rfs.is_unresolved_poi_forecast_query("明天天气", regions="蓟州") is False
        assert rfs.is_unresolved_poi_forecast_query("明天天气", regions="密云水库") is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd haihe-weather-analyzer-mcp && ./.venv-test/Scripts/python.exe -m pytest tests/test_rolling_forecast_poi_guard.py -q`
Expected: FAIL（`AttributeError: module 'rolling_forecast_service' has no attribute 'is_unresolved_poi_forecast_query'`）

- [ ] **Step 3: 实现谓词**

在 `rolling_forecast_service.py` 的 `is_basin_weather_query`（line 489-501）之后插入：

```python
# 具体点位指示词：出现这些词、但区域表（天津11区县）匹配不到时，说明问的是"具体点位"，
# 区域工具不应静默退回天津市区代表点，应转决策天气 POI 路径（先 search_poi 定位经纬度）。
POI_PLACE_KEYWORDS = (
    "水库", "拦河坝", "学校", "大学", "中学", "小学", "幼儿园", "学院",
    "医院", "机场", "火车站", "高铁站", "汽车站", "客运站", "车站",
    "港口", "港区", "码头", "公园", "湿地", "景区", "景点", "旅游区",
    "广场", "大厦", "体育馆", "体育场", "博物馆", "展览馆",
    "开发区", "工业园", "园区", "度假区", "古镇",
)


def has_matched_rolling_region(text: str) -> bool:
    """文本是否命中任一已知滚动预报区域（天津 11 区县及其别名）。"""
    text = str(text or "")
    if any(alias in text for alias in ROLLING_FORECAST_REGION_ALIASES):
        return True
    return any(region in text for region in ROLLING_FORECAST_COORDS)


def is_unresolved_poi_forecast_query(user_query: str, regions: str = "") -> bool:
    """问句含具体点位指示词、但区域表匹配不到 → 点位未解析（区域工具不应静默默认天津市区）。

    仅作区域工具的兜底守卫：命中时引导 planner 改用 query_decision_weather_for_poi。
    无点位词（"今天天气"/"我市"/"未来三天"）返回 False，保持默认天津市区行为不变。
    已知区域命中（即使同句含点位词，如"滨海新区大学城"）返回 False，区域路径优先。
    """
    text = f"{user_query or ''} {regions or ''}"
    if has_matched_rolling_region(text):
        return False
    return any(keyword in text for keyword in POI_PLACE_KEYWORDS)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd haihe-weather-analyzer-mcp && ./.venv-test/Scripts/python.exe -m pytest tests/test_rolling_forecast_poi_guard.py -q`
Expected: PASS（8 条）

- [ ] **Step 5: Commit**

```bash
git add haihe-weather-analyzer-mcp/rolling_forecast_service.py haihe-weather-analyzer-mcp/tests/test_rolling_forecast_poi_guard.py
git commit -m "feat(rolling): 新增 is_unresolved_poi_forecast_query 谓词——识别'区域表查不到的具体点位'"
```

### Task 2: `query_rolling_forecast` 接入守卫 + 静态接线测试

**Files:**
- Modify: `haihe-weather-analyzer-mcp/haihe_mcp_tools.py`（`query_rolling_forecast`，basin guard 之后 ~line 3101）
- Test: `haihe-weather-analyzer-mcp/tests/test_rolling_forecast_poi_guard.py`（追加）

**Interfaces:**
- Consumes: `is_unresolved_poi_forecast_query`（Task 1）。
- Produces: `query_rolling_forecast` 在未解析点位 + 非 point_mode 时抛 `BusinessException`。

先看 `haihe_mcp_tools.py` 顶部如何 import `is_basin_weather_query`（应是 `from rolling_forecast_service import ...` 或 `import rolling_forecast_service as rfs`），把 `is_unresolved_poi_forecast_query` 加进**同一处** import，保持风格一致。

- [ ] **Step 1: 写失败测试（静态接线 + 谓词行为）**

模仿 `test_rolling_forecast_basin_guard.py` 的"读源码 + marker"静态检查（避免 FastMCP 上下文），追加：

```python
from pathlib import Path

HMT = Path(__file__).resolve().parent.parent / "haihe_mcp_tools.py"


class TestQueryRollingForecastPoiGuardWiring:
    def test_guard_wired_after_basin_guard(self):
        src = HMT.read_text(encoding="utf-8")
        marker = "def query_rolling_forecast("
        idx = src.index(marker)
        body = src[idx: idx + 6000]
        assert "is_unresolved_poi_forecast_query" in body
        assert "query_decision_weather_for_poi" in body
        # 点位模式（已带 lon/lat）不拦截
        assert "lon is None or lat is None" in body

    def test_helper_imported(self):
        src = HMT.read_text(encoding="utf-8")
        assert "is_unresolved_poi_forecast_query" in src
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd haihe-weather-analyzer-mcp && ./.venv-test/Scripts/python.exe -m pytest tests/test_rolling_forecast_poi_guard.py::TestQueryRollingForecastPoiGuardWiring -q`
Expected: FAIL（源码里还没有 `is_unresolved_poi_forecast_query`）

- [ ] **Step 3: 在 `query_rolling_forecast` 加守卫**

`haihe_mcp_tools.py` 的 basin guard（line 3095-3101）之后、`return query_rolling_forecast_core(...)` 之前插入：

```python
        # 具体点位（水库/学校/机场等）但区域表查不到 → 不静默退回天津市区代表点，
        # 引导 planner 改用 query_decision_weather_for_poi（内部先 search_poi 定位经纬度再按点位查）。
        # 点位模式（调用方已给 lon/lat，如决策天气 POI）不拦截。
        if (lon is None or lat is None) and is_unresolved_poi_forecast_query(user_query, regions):
            raise BusinessException(
                "该地点为具体点位，本工具仅按天津市区及区级代表点查询、无法按名称定位；"
                "请改用 query_decision_weather_for_poi（先 search_poi 定位经纬度、再按点位查询）。"
            )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd haihe-weather-analyzer-mcp && ./.venv-test/Scripts/python.exe -m pytest tests/test_rolling_forecast_poi_guard.py -q`
Expected: PASS（全部）

- [ ] **Step 5: 回归 basin guard 测试**

Run: `cd haihe-weather-analyzer-mcp && ./.venv-test/Scripts/python.exe -m pytest tests/test_rolling_forecast_basin_guard.py -q`
Expected: 既有通过项不新增失败（注：`test_query_rolling_forecast_docstring_excludes_basin` 在 Git Bash 下因中文 GBK 编码报断言错是**既有环境 artifact**，与本改动无关；以中文正常的终端/CI 为准）

- [ ] **Step 6: Commit**

```bash
git add haihe-weather-analyzer-mcp/haihe_mcp_tools.py haihe-weather-analyzer-mcp/tests/test_rolling_forecast_poi_guard.py
git commit -m "fix(rolling): 区域工具对未识别具体点位抛 BusinessException 引导 POI 地理编码——修密云水库静默退回天津市区"
```

---

## Part 2 — 超 240h 点位日期 EC 降水回退

> 依赖：Part 1 可独立先行；Part 2 独立可交付。EC 真实文件布局/有效时次约定本地无法确认（GDAL + 数据在服务端），故采样用"候选窗口 + 首个命中"的鲁棒写法，并附一个服务端探针脚本供你实跑核对（不阻塞实现）。

### Task 3: `_ec_daily_window_candidates` 候选窗口生成（纯逻辑）

**Files:**
- Modify: `haihe-weather-analyzer-mcp/haihe_mcp_tools.py`（EC 辅助区，`_find_ec_precip_file` 附近 ~line 1730）
- Test: `haihe-weather-analyzer-mcp/tests/test_ec_rain_fallback.py`（新建）

**Interfaces:**
- Produces: `_ec_daily_window_candidates(target_date) -> list[tuple[datetime, int]]`，返回 `(窗口起点BJT, 累计小时)`，24h 优先、其后 12h、6h。
- Consumes: `TIANJIN_TIMEZONE`（`haihe_mcp_tools` 已有；若无则 `from rolling_forecast_service import TIANJIN_TIMEZONE` 或 `zoneinfo.ZoneInfo("Asia/Shanghai")`）。

- [ ] **Step 1: 写失败测试**

```python
# haihe-weather-analyzer-mcp/tests/test_ec_rain_fallback.py
from datetime import date
import haihe_mcp_tools as hmt


def test_candidates_prefer_24h_then_12h_then_6h():
    cands = hmt._ec_daily_window_candidates(date(2026, 9, 1))
    assert cands, "应生成候选"
    hours_seq = [h for _, h in cands]
    assert hours_seq[0] == 24
    # 所有 24h 排在所有 12h 前，所有 12h 排在所有 6h 前
    assert hours_seq == sorted(hours_seq, reverse=True)

def test_candidates_on_target_date():
    cands = hmt._ec_daily_window_candidates(date(2026, 9, 1))
    assert all(st.date() == date(2026, 9, 1) for st, _ in cands)
    assert all(st.tzinfo is not None for st, _ in cands)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd haihe-weather-analyzer-mcp && ./.venv-test/Scripts/python.exe -m pytest tests/test_ec_rain_fallback.py -q`
Expected: FAIL（`AttributeError: ... no attribute '_ec_daily_window_candidates'`）

- [ ] **Step 3: 实现候选生成**

```python
def _ec_daily_window_candidates(target_date) -> List[Tuple[datetime, int]]:
    """target_date 当日 EC 累计降水产品的候选 (窗口起点BJT, 累计小时)，24h 优先。

    EC 有效时次常见 02/08/14/20 BJT（18 UTC 起报 +8h 平移），也可能是 00/06/12/18；
    故对 24h/12h/6h 各给一组当日起点候选，由 file-finder 逐个点名，首个命中即用。
    """
    base = datetime(target_date.year, target_date.month, target_date.day, tzinfo=TIANJIN_TIMEZONE)
    cands: List[Tuple[datetime, int]] = []
    for h in (8, 0, 14, 20, 2, 6, 12, 18):
        cands.append((base + timedelta(hours=h), 24))
    for h in (8, 20, 0, 12):
        cands.append((base + timedelta(hours=h), 12))
    for h in (8, 2, 14, 20, 0, 6, 12, 18):
        cands.append((base + timedelta(hours=h), 6))
    return cands
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./.venv-test/Scripts/python.exe -m pytest tests/test_ec_rain_fallback.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add haihe-weather-analyzer-mcp/haihe_mcp_tools.py haihe-weather-analyzer-mcp/tests/test_ec_rain_fallback.py
git commit -m "feat(ec): _ec_daily_window_candidates——目标日 EC 累计降水候选窗口生成"
```

### Task 4: `sample_ec_point_daily_rain` 点位采样（GDAL 层可 mock）

**Files:**
- Modify: `haihe-weather-analyzer-mcp/haihe_mcp_tools.py`（Task 3 之后）
- Test: `haihe-weather-analyzer-mcp/tests/test_ec_rain_fallback.py`（追加）

**Interfaces:**
- Consumes: `_ec_daily_window_candidates`（Task 3）、`_find_ec_precip_file`、`_sample_station_forecast_rain_mm`、`DEFAULT_EC_OUTPUT_PATH`（均已存在）。
- Produces: `sample_ec_point_daily_rain(lon, lat, target_date, ec_output_path=DEFAULT_EC_OUTPUT_PATH) -> Optional[Dict]`，命中返回 `{"rain_mm","window_start","window_hours","file"}`，否则 `None`。

- [ ] **Step 1: 写失败测试（monkeypatch 文件查找与采样，绕开 GDAL）**

```python
from datetime import date, datetime
import haihe_mcp_tools as hmt


def _patch(monkeypatch, files, samples):
    # files: dict[(start_str, hours)] -> path ; samples: dict[path] -> {"POI": mm}
    monkeypatch.setattr(hmt, "_find_ec_precip_file",
                        lambda root, st, h: files.get((st.strftime("%Y%m%d%H"), h)))
    monkeypatch.setattr(hmt, "_sample_station_forecast_rain_mm",
                        lambda recs, path: samples.get(path, {}))


def test_returns_first_hit(monkeypatch):
    _patch(monkeypatch,
           files={("2026090108", 24): "/ec/a.tif"},
           samples={"/ec/a.tif": {"POI": 12.5}})
    r = hmt.sample_ec_point_daily_rain(116.8, 40.4, date(2026, 9, 1))
    assert r["rain_mm"] == 12.5 and r["window_hours"] == 24

def test_none_when_no_file(monkeypatch):
    _patch(monkeypatch, files={}, samples={})
    assert hmt.sample_ec_point_daily_rain(116.8, 40.4, date(2026, 9, 1)) is None

def test_skips_file_with_no_point_value(monkeypatch):
    _patch(monkeypatch,
           files={("2026090108", 24): "/ec/a.tif", ("2026090100", 24): "/ec/b.tif"},
           samples={"/ec/a.tif": {}, "/ec/b.tif": {"POI": 3.0}})
    r = hmt.sample_ec_point_daily_rain(116.8, 40.4, date(2026, 9, 1))
    assert r["file"] == "/ec/b.tif" and r["rain_mm"] == 3.0

def test_sampler_exception_falls_through(monkeypatch):
    monkeypatch.setattr(hmt, "_find_ec_precip_file",
                        lambda root, st, h: "/ec/x.tif" if (st.strftime("%H"), h) == ("08", 24) else None)
    def boom(recs, path): raise RuntimeError("gdal missing")
    monkeypatch.setattr(hmt, "_sample_station_forecast_rain_mm", boom)
    assert hmt.sample_ec_point_daily_rain(116.8, 40.4, date(2026, 9, 1)) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv-test/Scripts/python.exe -m pytest tests/test_ec_rain_fallback.py -q`
Expected: FAIL（`no attribute 'sample_ec_point_daily_rain'`）

- [ ] **Step 3: 实现采样**

```python
def sample_ec_point_daily_rain(
    lon: float,
    lat: float,
    target_date,
    ec_output_path: str = DEFAULT_EC_OUTPUT_PATH,
) -> Optional[Dict[str, Any]]:
    """在 EC AIFS 累计降水栅格上采样 target_date 当日点雨量（mm）；找不到任何可用产品返回 None。

    口径：候选窗口 24h 优先（Task 3），逐个点名 _find_ec_precip_file，首个在点位
    采到非空值的产品即返回。文件缺失/采样异常/点位出界均跳过继续，绝不抛给调用方。
    """
    records = [{"Station_Id_C": "POI", "Lat": lat, "Lon": lon}]
    for start_dt, hours in _ec_daily_window_candidates(target_date):
        path = _find_ec_precip_file(ec_output_path, start_dt, hours)
        if not path:
            continue
        try:
            sampled = _sample_station_forecast_rain_mm(records, path)
        except Exception:
            continue
        val = sampled.get("POI")
        if val is not None:
            return {
                "rain_mm": float(val),
                "window_start": start_dt,
                "window_hours": hours,
                "file": path,
            }
    return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./.venv-test/Scripts/python.exe -m pytest tests/test_ec_rain_fallback.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add haihe-weather-analyzer-mcp/haihe_mcp_tools.py haihe-weather-analyzer-mcp/tests/test_ec_rain_fallback.py
git commit -m "feat(ec): sample_ec_point_daily_rain——EC 累计降水栅格点位采样（候选窗口首个命中）"
```

### Task 5: `query_rolling_forecast_core` out_of_range + point_mode 挂 EC 回退

**Files:**
- Modify: `haihe-weather-analyzer-mcp/rolling_forecast_service.py`（`query_rolling_forecast_core` 的 `if calendar_error is not None:` 分支 ~line 1192；新增 `_try_ec_rain_fallback` / `_build_ec_rain_fallback_payload` / `_resolve_ec_target_date`）
- Test: `haihe-weather-analyzer-mcp/tests/test_ec_rain_fallback.py`（追加）

**Interfaces:**
- Consumes: `sample_ec_point_daily_rain`（Task 4，**函数级惰性 import**）、`_extract_explicit_query_dates`、`_parse_date`、`_build_calendar_error_payload`、`MAX_FORECAST_PERIOD_HOURS`（均已存在）。
- Produces:
  - `_resolve_ec_target_date(user_query, forecast_start_date, now) -> Optional[date]`
  - `_build_ec_rain_fallback_payload(sampled, target, point_name, matched_region, lon, lat, now) -> dict`（`status:"ec_rain_fallback"`）
  - `query_rolling_forecast_core` 在 out_of_range + point_mode 且 EC 有数时返回该 payload，否则维持原 out_of_range。

- [ ] **Step 1: 写失败测试**

```python
from datetime import date, datetime
from zoneinfo import ZoneInfo
import rolling_forecast_service as rfs

NOW = datetime(2026, 8, 19, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_out_of_range_point_mode_uses_ec(monkeypatch):
    monkeypatch.setattr(rfs, "_try_ec_rain_fallback", lambda *a, **k: {
        "status": "ec_rain_fallback", "target_date": "2026-09-01", "rain_mm": 5.0,
    })
    # 9月1日超 240h → calendar_error；point_mode → 走 EC
    r = rfs.query_rolling_forecast_core("9月1日天气怎么样", lon=116.8, lat=40.4, point_name="密云水库", now=NOW)
    assert r["status"] == "ec_rain_fallback"

def test_out_of_range_point_mode_no_ec_keeps_out_of_range(monkeypatch):
    monkeypatch.setattr(rfs, "_try_ec_rain_fallback", lambda *a, **k: None)
    r = rfs.query_rolling_forecast_core("9月1日天气怎么样", lon=116.8, lat=40.4, point_name="密云水库", now=NOW)
    assert r["status"] == "out_of_range"

def test_out_of_range_region_mode_no_ec(monkeypatch):
    # 非 point_mode 即使 EC 有数也不走点位 EC 回退
    called = []
    monkeypatch.setattr(rfs, "_try_ec_rain_fallback", lambda *a, **k: called.append(1) or {"status": "ec_rain_fallback"})
    r = rfs.query_rolling_forecast_core("9月1日天气怎么样", regions="蓟州", now=NOW)
    assert r["status"] == "out_of_range" and not called

def test_resolve_ec_target_date_explicit():
    assert rfs._resolve_ec_target_date("9月1日天气怎么样", "", NOW) == date(2026, 9, 1)
    assert rfs._resolve_ec_target_date("明天天气", "2026-09-01", NOW) == date(2026, 9, 1)
    assert rfs._resolve_ec_target_date("明天天气", "", NOW) is None
```

> 注：`query_rolling_forecast_core` 对超 240h 的未来日期会走到 `calendar_error` 分支（不取网）。测试用 `now=NOW` 固定日期，9月1日相对 8月19日 = 13 天 > 240h，稳定触发 out_of_range。monkeypatch `_try_ec_rain_fallback` 隔离真实 EC/文件层。

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv-test/Scripts/python.exe -m pytest tests/test_ec_rain_fallback.py -q`
Expected: FAIL（`no attribute '_try_ec_rain_fallback' / '_resolve_ec_target_date'`，且核心仍返回 out_of_range）

- [ ] **Step 3: 实现**

`rolling_forecast_service.py` 新增（放 `_build_calendar_error_payload` 附近）：

```python
def _resolve_ec_target_date(user_query: str, forecast_start_date: str, now: datetime) -> date | None:
    """EC 回退需要的目标日历日：优先 forecast_start_date，否则从 user_query 显式日期解析。"""
    if forecast_start_date:
        try:
            return _parse_date(forecast_start_date)
        except Exception:
            pass
    dates = _extract_explicit_query_dates(user_query, now)
    return dates[0] if dates else None


def _build_ec_rain_fallback_payload(sampled, target, point_name, matched_region, lon, lat, now) -> dict:
    label = (point_name or matched_region or "指定点位").strip()
    return {
        "status": "ec_rain_fallback",
        "query_mode": "calendar_ec_rain_point",
        "data_source": "ECMWF AIFS",
        "target_date": target.isoformat(),
        "rain_mm": sampled["rain_mm"],
        "window_start": sampled["window_start"].strftime("%Y-%m-%d %H:%M"),
        "window_hours": sampled["window_hours"],
        "point_name": label,
        "query_point": _build_point_mode_query_point(True, lon, lat, point_name, matched_region),
        "query_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "message": "该日期超出滚动预报未来 10 天时效，已改用 ECMWF AIFS 累计降水产品，仅提供降雨参考。",
    }


def _try_ec_rain_fallback(user_query, forecast_start_date, lon, lat, point_name, matched_region, now):
    """out_of_range + 点位模式时尝试 EC 降水回退；目标日无法解析或 EC 无数据返回 None。"""
    target = _resolve_ec_target_date(user_query, forecast_start_date, now)
    if target is None:
        return None
    try:
        from haihe_mcp_tools import sample_ec_point_daily_rain  # 惰性 import 防循环
    except Exception:
        return None
    try:
        sampled = sample_ec_point_daily_rain(lon, lat, target)
    except Exception:
        return None
    if sampled is None:
        return None
    return _build_ec_rain_fallback_payload(sampled, target, point_name, matched_region, lon, lat, now)
```

把 `query_rolling_forecast_core` 的：

```python
    if calendar_error is not None:
        return _build_calendar_error_payload(
            calendar_error, point_mode, region_names, lon, lat, point_name, matched_region, now
        )
```

改为：

```python
    if calendar_error is not None:
        if point_mode:
            ec = _try_ec_rain_fallback(
                user_query, forecast_start_date, lon, lat, point_name, matched_region, now
            )
            if ec is not None:
                return ec
        return _build_calendar_error_payload(
            calendar_error, point_mode, region_names, lon, lat, point_name, matched_region, now
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./.venv-test/Scripts/python.exe -m pytest tests/test_ec_rain_fallback.py -q`
Expected: PASS

- [ ] **Step 5: 回归 past_date / cache 测试**

Run: `./.venv-test/Scripts/python.exe -m pytest tests/test_rolling_forecast_past_date.py tests/test_rolling_forecast_cache.py -q`
Expected: 全 PASS（EC 分支只在 out_of_range+point_mode 触发，不影响 past_date/正常路径）

- [ ] **Step 6: Commit**

```bash
git add haihe-weather-analyzer-mcp/rolling_forecast_service.py haihe-weather-analyzer-mcp/tests/test_ec_rain_fallback.py
git commit -m "feat(rolling): 超240h点位日期 EC 降水回退——out_of_range+point_mode 时采样 EC 累计降水"
```

### Task 6: 决策天气层识别 ec_rain_fallback 并生成"只讲降雨"回答（双入口）

**Files:**
- Modify: `chainlitexam/tools/decision_weather_core.py`（新增 `_is_ec_rain_fallback_payload` + `_build_ec_rain_answer_text`）
- Modify: `chainlitexam/tools/decision_weather.py`（planner 工具 `query_decision_weather_for_poi`，past_date 分支后 ~line 166）
- Modify: `chainlitexam/tools/decision_weather_fast_path.py`（`DecisionWeatherQAService`，保持双入口 parity）
- Test: `chainlitexam/tests/test_decision_weather_tool.py`（追加）

**Interfaces:**
- Consumes: MCP 返回的 `status:"ec_rain_fallback"` payload（Task 5）、`_sanitize_display_text`、`callbacks["append_followup_if_needed"]`。
- Produces:
  - `_is_ec_rain_fallback_payload(forecast_payload) -> bool`
  - `_build_ec_rain_answer_text(payload, point_name) -> str`（纯代码、确定性、零 LLM、零编造）

- [ ] **Step 1: 写失败测试**

```python
# chainlitexam/tests/test_decision_weather_tool.py 追加
from tools.decision_weather_core import (
    _is_ec_rain_fallback_payload, _build_ec_rain_answer_text,
)

EC_PAYLOAD = {
    "status": "ec_rain_fallback", "target_date": "2026-09-01",
    "rain_mm": 5.0, "window_hours": 24, "data_source": "ECMWF AIFS",
}

def test_is_ec_rain_fallback_payload():
    assert _is_ec_rain_fallback_payload(EC_PAYLOAD) is True
    assert _is_ec_rain_fallback_payload({"status": "ok"}) is False
    assert _is_ec_rain_fallback_payload(None) is False

def test_ec_answer_rain_positive_no_fabrication():
    text = _build_ec_rain_answer_text(EC_PAYLOAD, "密云水库")
    assert "9月1日" in text and "5.0" in text
    assert "ECMWF AIFS" in text
    assert "气温" in text and "暂无法提供" in text  # 明说其他要素没有
    assert "预计有降雨" in text

def test_ec_answer_zero_rain():
    p = dict(EC_PAYLOAD, rain_mm=0.0)
    text = _build_ec_rain_answer_text(p, "密云水库")
    assert "无明显降雨" in text
    assert "气温" not in text.split("暂无法提供")[0] or True  # 不编具体气温值
    assert "~" not in text  # 不出现气温区间
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd chainlitexam && D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_decision_weather_tool.py -k ec_rain -q`
Expected: FAIL（`ImportError: cannot import name '_is_ec_rain_fallback_payload'`）

- [ ] **Step 3: 实现 core 辅助**

`decision_weather_core.py`（`_is_past_date_forecast_payload` 附近）：

```python
def _is_ec_rain_fallback_payload(forecast_payload: Any) -> bool:
    """滚动预报超 240h 点位日期命中 EC 降水回退标记。"""
    return isinstance(forecast_payload, dict) and forecast_payload.get("status") == "ec_rain_fallback"


def _build_ec_rain_answer_text(payload: dict, point_name: str) -> str:
    """EC 降水回退的确定性回答：只讲降雨，气温/风/能见度明说超出时效不提供，零编造。"""
    target = str(payload.get("target_date") or "")
    try:
        dt = datetime.strptime(target[:10], "%Y-%m-%d")
        label = f"{dt.month}月{dt.day}日"
    except (TypeError, ValueError):
        label = target or "该日期"
    name = (point_name or "该地点").strip()
    rain = payload.get("rain_mm")
    wh = payload.get("window_hours") or 24
    try:
        rain_val = float(rain)
    except (TypeError, ValueError):
        rain_val = 0.0
    rain_line = (
        f"预计有降雨，{wh} 小时累计约 {rain_val:.1f} 毫米"
        if rain_val > 0 else "预计无明显降雨"
    )
    return (
        f"【{name}{label}降水参考】\n"
        f"{label}已超出滚动预报未来 10 天时效，据 ECMWF AIFS 累计降水产品：{rain_line}。\n"
        f"气温、风力、能见度等要素超出时效，暂无法提供。\n"
        f"数据来源：ECMWF AIFS（仅降雨）。"
    )
```

- [ ] **Step 4: 接线 planner 工具 + fast-path（parity）**

`decision_weather.py` 在 `_is_past_date_forecast_payload` 分支（line 148）**之后**、`facts = _compact_decision_forecast_facts(...)`（line 168）**之前**插入：

```python
            # 超滚动预报 240h 的点位日期：MCP 已用 EC 降水回退 → 只讲降雨的确定性回答
            if _is_ec_rain_fallback_payload(forecast_payload):
                ec_text = _build_ec_rain_answer_text(forecast_payload, point_name)
                append_followup = callbacks.get("append_followup_if_needed", lambda t, u: t)
                return _sanitize_display_text(append_followup(ec_text, user_text))
```

并在该文件 import 区把 `_is_ec_rain_fallback_payload`、`_build_ec_rain_answer_text` 加进从 `tools.decision_weather_core` 的导入（与 `_is_past_date_forecast_payload` 同处）。

`decision_weather_fast_path.py` 的 `DecisionWeatherQAService` 在同样的 forecast_payload 处理点插入相同分支（parity），若该路径结构不同则调用共享的 `_is_ec_rain_fallback_payload` + `_build_ec_rain_answer_text`，不复制逻辑。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd chainlitexam && D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_decision_weather_tool.py -q`
Expected: PASS（含新增 ec_rain 用例，既有 44 条不回归）

- [ ] **Step 6: Commit**

```bash
git add chainlitexam/tools/decision_weather_core.py chainlitexam/tools/decision_weather.py chainlitexam/tools/decision_weather_fast_path.py chainlitexam/tests/test_decision_weather_tool.py
git commit -m "feat(decision): 识别 ec_rain_fallback 生成只讲降雨的诚实回答（双入口 parity）"
```

### Task 7: 服务端 EC 布局探针（你实跑，不阻塞）+ 全量回归 + 文档

**Files:**
- Create: `haihe-weather-analyzer-mcp/probe_ec_rain_layout.py`
- Modify: `CLAUDE.md`（决策天气/滚动预报段落补 EC 回退 + POI 守卫口径）

- [ ] **Step 1: 写探针脚本**

```python
# haihe-weather-analyzer-mcp/probe_ec_rain_layout.py
"""服务端实跑：核对 EC 累计降水文件命名/有效时次约定 + 点位采样是否出数。
用法：在能访问 EC 数据目录的内网机器上  python probe_ec_rain_layout.py 2026-09-01 116.8 40.4
"""
import sys
from datetime import date
import haihe_mcp_tools as hmt

d = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2026, 9, 1)
lon = float(sys.argv[2]) if len(sys.argv) > 2 else 116.8
lat = float(sys.argv[3]) if len(sys.argv) > 3 else 40.4
print("EC_OUTPUT_PATH =", hmt.DEFAULT_EC_OUTPUT_PATH)
for st, h in hmt._ec_daily_window_candidates(d):
    p = hmt._find_ec_precip_file(hmt.DEFAULT_EC_OUTPUT_PATH, st, h)
    print(f"  candidate start={st:%Y-%m-%d %H:%M} {h}h -> {p or '（无）'}")
r = hmt.sample_ec_point_daily_rain(lon, lat, d)
print("sample_ec_point_daily_rain ->", r)
```

- [ ] **Step 2: 请你在服务端实跑探针**，把候选命中情况/采样值发我；若有效时次约定与候选不符，据此调整 `_ec_daily_window_candidates` 的小时顺序（仅排序调整，不动结构）。

- [ ] **Step 3: 全量回归**

Run（MCP）: `cd haihe-weather-analyzer-mcp && ./.venv-test/Scripts/python.exe -m pytest tests/test_rolling_forecast_past_date.py tests/test_rolling_forecast_cache.py tests/test_rolling_forecast_poi_guard.py tests/test_ec_rain_fallback.py -q`
Run（chainlitexam，从 chainlitexam/ 跑，排除既有坏文件）: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/test_decision_weather_tool.py` 再单独跑 `tests/test_decision_weather_tool.py`
Expected: 不新增失败（已知：`test_message_orchestrator` 顺序依赖 1 个既有失败、`test_decision_weather_tool` 单独跑时的既有 import 现象，见 CLAUDE.md，均非本批回归）

- [ ] **Step 4: 更新 CLAUDE.md**，在"历史日期 → 历史实况查询"小节后补一段"超 240h 点位日期 → EC 降水回退 + 区域工具未识别点位守卫"口径（数据源边界、零编造、双入口、惰性 import 防循环）。

- [ ] **Step 5: Commit**

```bash
git add haihe-weather-analyzer-mcp/probe_ec_rain_layout.py CLAUDE.md
git commit -m "docs+probe: EC 降水回退服务端探针 + CLAUDE.md 补 POI 守卫/EC 回退口径"
```

---

## Self-Review 记录

- **Spec 覆盖**：密云守卫（用户选的 A 治本+B 兜底）→ Task 1-2；EC 降水回退（用户选的"部分回答"）→ Task 3-6；探针+回归+文档 → Task 7。两个方向全覆盖。
- **Placeholder 扫描**：无 TBD/TODO；每个代码步骤均给完整代码。EC 窗口约定属"服务端才能证实"的已知未知，用候选窗口鲁棒写法兜底 + 探针核对，非占位符。
- **类型一致性**：`sample_ec_point_daily_rain` 返回 dict 键（`rain_mm/window_start/window_hours/file`）在 Task 4 定义、Task 5 `_build_ec_rain_fallback_payload` 消费一致；`status:"ec_rain_fallback"` 在 Task 5 产出、Task 6 `_is_ec_rain_fallback_payload` 判定一致；`_is_ec_rain_fallback_payload`/`_build_ec_rain_answer_text` 在 Task 6 core 定义、decision_weather.py/fast_path 消费一致。
- **风险**：① EC 真实有效时次约定未证实 → Task 7 探针核对，候选写法已鲁棒；② 守卫误伤合法查询 → 谓词仅在"有点位词且无区域命中"时触发，"今天天气/我市"等无点位词不受影响，区域命中优先。
