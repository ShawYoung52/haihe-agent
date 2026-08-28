# 河流沿线与河系降雨预报 Implementation Plan

> 执行状态（2026-08-28）：河流/河系实现和离线回归完成，独立最终审查待执行，真实PostGIS/GDAL与内网栅格待验证；最终状态、56题清单和命令见[验收记录](../2026-08-27-priority-acceptance.md)。下方保留原始提案步骤，不以未勾选复选框表示尚未实现。原known-system-first与EPSG:3857示例已被替代：裸河名先查全量表，仅未找到才回退河系；采用geography真实米制5000米缓冲，明确河系由统一入口复用既有核心。新区域风险no_data为不可用，Chainlit/MCP必须分目录分进程使用系统Python测试。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让“明天泃河有雨吗”“今天晚上滦河有雨吗”等问题稳定使用河道或河系专用降雨数据，并按动态自然日/时段返回真实统计。

**Architecture:** 新建无 FastMCP 依赖的河流预报核心模块。模块从用户原问解析河名和时间窗口，已知九分区河系复用 `river_system_forecast`，具体河流从 `haihe_river_directed_full_v6` 读取真实几何、构造两侧各 5 公里缓冲后叠加现有降雨栅格。MCP 只注册一个统一入口，主动工具路由把所有河流未来降雨问题限制到该入口。

**Tech Stack:** Python 3.10+、PostgreSQL/PostGIS、psycopg2、GDAL/OGR、pytest、FastMCP

**Spec:** `chainlitexam/docs/superpowers/specs/2026-08-26-tianhe-river-risk-priority-design.md`

## Global Constraints

- 使用配置项 `postgres.river_table_full`，默认 `haihe_river_directed_full_v6`。
- 河道缓冲固定为两侧各 5 公里，并在米制坐标系中计算。
- SQL 值全部参数化，schema/table 使用安全标识符组合。
- “明天”按下一自然日；“今天晚上”按 18:00—24:00，18 时后从当前有效整点起算。
- 多日查询逐日统计，不重复使用整个时段总量。
- 数据缺失、河流未找到和无雨必须是三个不同状态。
- 不得回退到天津 `query_rolling_forecast`。
- `ENABLE_FAST_PATHS` 保持 `False`。

---

### Task 1: 实现河名和时间窗口解析

**Files:**
- Create: `haihe-weather-analyzer-mcp/river_query_forecast.py`
- Create: `haihe-weather-analyzer-mcp/tests/test_river_query_forecast.py`

**Interfaces:**
- Produces: `KNOWN_RIVER_SYSTEMS: frozenset[str]`
- Produces: `ForecastPeriod(label: str, target_start: datetime, target_end: datetime)`
- Produces: `extract_river_target(user_query: str) -> str`
- Produces: `resolve_river_forecast_periods(user_query: str, now: datetime | None = None) -> list[ForecastPeriod]`

- [ ] **Step 1: 写河名和时间窗口失败测试**

```python
from datetime import datetime
from zoneinfo import ZoneInfo

import river_query_forecast as rqf

TZ = ZoneInfo("Asia/Shanghai")


def test_extracts_river_after_leading_time_words():
    assert rqf.extract_river_target("明天泃河有雨吗？") == "泃河"
    assert rqf.extract_river_target("今天晚上滦河有雨吗？") == "滦河"


def test_tomorrow_is_a_natural_day():
    periods = rqf.resolve_river_forecast_periods(
        "明天泃河有雨吗？", datetime(2026, 8, 27, 10, 15, tzinfo=TZ)
    )
    assert [(p.target_start.hour, p.target_end.hour) for p in periods] == [(0, 0)]
    assert periods[0].target_start.date().isoformat() == "2026-08-28"
    assert periods[0].target_end.date().isoformat() == "2026-08-29"


def test_tonight_starts_at_18_before_evening_and_current_hour_after_18():
    before = rqf.resolve_river_forecast_periods(
        "今天晚上滦河有雨吗？", datetime(2026, 8, 27, 15, 20, tzinfo=TZ)
    )[0]
    after = rqf.resolve_river_forecast_periods(
        "今天晚上滦河有雨吗？", datetime(2026, 8, 27, 20, 35, tzinfo=TZ)
    )[0]
    assert before.target_start.hour == 18
    assert after.target_start.hour == 20
    assert before.target_end.date().isoformat() == "2026-08-28"


def test_future_three_days_returns_three_non_overlapping_periods():
    periods = rqf.resolve_river_forecast_periods(
        "泃河未来三天降雨", datetime(2026, 8, 27, 8, 0, tzinfo=TZ)
    )
    assert len(periods) == 3
    assert all(a.target_end == b.target_start for a, b in zip(periods, periods[1:]))
```

- [ ] **Step 2: 运行测试并确认模块不存在**

Run:

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest haihe-weather-analyzer-mcp/tests/test_river_query_forecast.py -q
```

Expected: FAIL，错误为 `ModuleNotFoundError: river_query_forecast`。

- [ ] **Step 3: 实现解析器**

实现不可变 `ForecastPeriod`，河名解析优先匹配 `KNOWN_RIVER_SYSTEMS`，再用河名正则提取 1—8 个汉字加“河”，并从候选前缀去掉“今天、今日、明天、明日、后天、未来、今晚、今天晚上”等时间词。若未提取到河名，抛出 `ValueError("未识别到河流或河系名称")`。

时间窗口实现使用 `time_source.now(ZoneInfo("Asia/Shanghai"))`，并返回带时区的 `datetime`：

```python
@dataclass(frozen=True)
class ForecastPeriod:
    label: str
    target_start: datetime
    target_end: datetime


def _day_period(day: date, label: str) -> ForecastPeriod:
    start = datetime.combine(day, time.min, tzinfo=TIANJIN_TIMEZONE)
    return ForecastPeriod(label, start, start + timedelta(days=1))
```

“未来 N 天”从明天开始拆成 N 个 `_day_period`；“今天晚上”使用 `[18:00, 24:00)`，当前时间超过 18:00 时起点下取当前整点但不得早于 18:00。

- [ ] **Step 4: 运行解析器测试**

Run:

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest haihe-weather-analyzer-mcp/tests/test_river_query_forecast.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交本任务**

```powershell
git add -- haihe-weather-analyzer-mcp/river_query_forecast.py haihe-weather-analyzer-mcp/tests/test_river_query_forecast.py
git commit -m "feat(river): parse river targets and forecast windows"
```

---

### Task 2: 从 full_v6 构造 5 公里河道缓冲区

**Files:**
- Modify: `haihe-weather-analyzer-mcp/river_query_forecast.py`
- Modify: `haihe-weather-analyzer-mcp/tests/test_river_query_forecast.py`

**Interfaces:**
- Produces: `RiverCorridor(river_name: str, matched_name: str, srid: int, geometry: Any, buffer_km: float)`
- Produces: `load_river_corridor(river_name: str, pg_conf: dict, buffer_km: float = 5.0) -> RiverCorridor`
- Raises: `RiverNotFoundError`, `RiverDatabaseError`

- [ ] **Step 1: 写 full 表、精确匹配和错误状态失败测试**

```python
class FakeCursor:
    def __init__(self, executed):
        self.executed = executed

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params):
        self.executed["sql"] = repr(statement)
        self.executed["params"] = params

    def fetchone(self):
        return {
            "matched_name": "泃河",
            "srid": 4326,
            "geom_wkb": b"valid-wkb",
        }


class FakeConnection:
    def __init__(self, executed):
        self.executed = executed

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self, **kwargs):
        return FakeCursor(self.executed)


class EmptyCursor(FakeCursor):
    def fetchone(self):
        return None


class EmptyConnection(FakeConnection):
    def __init__(self):
        super().__init__({})

    def cursor(self, **kwargs):
        return EmptyCursor(self.executed)


def test_corridor_query_uses_full_table_and_5000_metre_buffer(monkeypatch):
    executed = {}
    monkeypatch.setattr(rqf.psycopg2, "connect", lambda **kwargs: FakeConnection(executed))
    monkeypatch.setattr(rqf, "_geometry_from_wkb", lambda value: object())
    corridor = rqf.load_river_corridor(
        "泃河",
        {"schema": "public", "river_table_full": "haihe_river_directed_full_v6"},
    )
    assert "haihe_river_directed_full_v6" in executed["sql"]
    assert "ST_Buffer" in executed["sql"]
    assert executed["params"]["buffer_m"] == 5000.0
    assert corridor.buffer_km == 5.0


def test_missing_river_is_not_reported_as_no_rain(monkeypatch):
    monkeypatch.setattr(rqf.psycopg2, "connect", lambda **kwargs: EmptyConnection())
    with pytest.raises(rqf.RiverNotFoundError):
        rqf.load_river_corridor("不存在河", {"river_table_full": "haihe_river_directed_full_v6"})
```

测试假连接器应实现上下文管理器、`cursor.execute` 和 `fetchone`，返回字段为 `matched_name`、`srid`、`geom_wkb`。

- [ ] **Step 2: 运行测试并确认接口不存在**

Run:

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest haihe-weather-analyzer-mcp/tests/test_river_query_forecast.py -q
```

Expected: FAIL，`load_river_corridor` 尚不存在。

- [ ] **Step 3: 实现安全的 PostGIS 查询**

使用 `psycopg2.sql.Identifier` 组合 schema/table，值使用命名参数。SQL 先计算匹配优先级，再只合并最佳级别河段：

```sql
WITH candidates AS (
    SELECT river_name, src_name, geom,
           CASE
             WHEN river_name = %(river_name)s OR src_name = %(river_name)s THEN 0
             ELSE 1
           END AS match_rank
    FROM {schema}.{table}
    WHERE river_name = %(river_name)s
       OR src_name = %(river_name)s
       OR river_name ILIKE %(contains)s
       OR src_name ILIKE %(contains)s
), best AS (
    SELECT * FROM candidates
    WHERE match_rank = (SELECT MIN(match_rank) FROM candidates)
), merged AS (
    SELECT COALESCE(MIN(NULLIF(river_name, '')), MIN(src_name)) AS matched_name,
           ST_UnaryUnion(ST_Collect(ST_MakeValid(geom))) AS geom
    FROM best
)
SELECT matched_name, 4326 AS srid,
       ST_AsBinary(
         ST_Transform(
           ST_Buffer(ST_Transform(ST_SetSRID(geom, %(source_srid)s), 3857), %(buffer_m)s),
           4326
         )
       ) AS geom_wkb
FROM merged
WHERE geom IS NOT NULL;
```

`contains` 取 `f"%{river_name}%"`，`source_srid` 来自配置默认 4326。无行或空 WKB 抛 `RiverNotFoundError`；连接/SQL/几何解析异常包装成 `RiverDatabaseError`，但不得吞掉 `RiverNotFoundError`。

- [ ] **Step 4: 运行空间查询测试**

Run:

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest haihe-weather-analyzer-mcp/tests/test_river_query_forecast.py -q
```

Expected: PASS；断言 full 表、5000 米缓冲和未找到状态。

- [ ] **Step 5: 提交本任务**

```powershell
git add -- haihe-weather-analyzer-mcp/river_query_forecast.py haihe-weather-analyzer-mcp/tests/test_river_query_forecast.py
git commit -m "feat(river): load five-kilometre river corridors from PostGIS"
```

---

### Task 3: 聚合具体河流和九分区河系降雨

**Files:**
- Modify: `haihe-weather-analyzer-mcp/river_query_forecast.py`
- Modify: `haihe-weather-analyzer-mcp/tests/test_river_query_forecast.py`

**Interfaces:**
- Produces: `query_river_rainfall_forecast_core(user_query: str, config: dict, ec_output_path: str = "", now: datetime | None = None) -> dict`
- Consumes: `river_system_forecast.get_river_system_rainfall_forecast(...)`
- Consumes: `river_system_forecast._resolve_forecast_file(...)`
- Consumes: `river_system_forecast._compute_rainfall_stats_for_geometry(...)`

- [ ] **Step 1: 写河道/河系分派和逐日统计失败测试**

```python
from datetime import datetime
from zoneinfo import ZoneInfo

FIXED_NOW = datetime(2026, 8, 27, 15, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
TEST_CONFIG = {
    "postgres": {
        "schema": "public",
        "river_table_full": "haihe_river_directed_full_v6",
        "srid": "4326",
    }
}


def fake_juhe_corridor(*args, **kwargs):
    return rqf.RiverCorridor("泃河", "泃河", 4326, object(), 5.0)


def fake_stats(*args, **kwargs):
    return {
        "average_rainfall_mm": 2.4,
        "max_rainfall_mm": 8.1,
        "min_rainfall_mm": 0.0,
        "valid_count": 12,
    }


def test_juhe_uses_corridor_and_reports_scope(monkeypatch):
    monkeypatch.setattr(rqf, "load_river_corridor", fake_juhe_corridor)
    monkeypatch.setattr(rqf.rsf, "_resolve_forecast_file", lambda *a: ("rain.tif", "滚动预报网格"))
    monkeypatch.setattr(rqf.rsf, "_compute_rainfall_stats_for_geometry", lambda *a, **k: {
        "average_rainfall_mm": 2.4,
        "max_rainfall_mm": 8.1,
        "min_rainfall_mm": 0.0,
        "valid_count": 12,
    })
    result = rqf.query_river_rainfall_forecast_core(
        "明天泃河有雨吗？", TEST_CONFIG, now=FIXED_NOW
    )
    assert result["status"] == "ok"
    assert result["scope_type"] == "river_corridor"
    assert result["scope_description"] == "泃河河道两侧约5公里沿线范围"
    assert result["periods"][0]["has_rain"] is True


def test_luanhe_uses_existing_nine_zone_tool(monkeypatch):
    calls = []
    monkeypatch.setattr(rqf.rsf, "get_river_system_rainfall_forecast", lambda **kwargs: calls.append(kwargs) or {
        "data_source": "滚动预报网格",
        "zones": [{"zone_name": "滦河", "average_rainfall_mm": 1.0, "max_rainfall_mm": 4.0, "min_rainfall_mm": 0.0}],
    })
    result = rqf.query_river_rainfall_forecast_core(
        "今天晚上滦河有雨吗？", TEST_CONFIG, now=FIXED_NOW
    )
    assert result["scope_type"] == "river_system"
    assert calls[0]["river_system"] == "滦河"
    assert calls[0]["forecast_hours"] == 6


def test_three_days_are_computed_independently(monkeypatch):
    resolved_starts = []
    monkeypatch.setattr(rqf, "load_river_corridor", fake_juhe_corridor)
    monkeypatch.setattr(rqf.rsf, "_resolve_forecast_file", lambda hours, start, path: resolved_starts.append(start) or ("rain.tif", "TEST"))
    monkeypatch.setattr(rqf.rsf, "_compute_rainfall_stats_for_geometry", fake_stats)
    result = rqf.query_river_rainfall_forecast_core(
        "泃河未来三天降雨", TEST_CONFIG, now=FIXED_NOW
    )
    assert len(result["periods"]) == 3
    assert len(set(resolved_starts)) == 3
```

- [ ] **Step 2: 运行测试并确认核心查询不存在**

Run:

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest haihe-weather-analyzer-mcp/tests/test_river_query_forecast.py -q
```

Expected: FAIL，`query_river_rainfall_forecast_core` 尚不存在。

- [ ] **Step 3: 实现统一查询核心**

具体河流每个 `ForecastPeriod` 独立调用预报文件解析与几何统计；`has_rain` 仅在 `valid_count > 0` 时按 `max_rainfall_mm > 0` 判断。`valid_count == 0` 返回周期状态 `no_coverage`，不得写成无雨。

已知九分区河系每个周期调用现有 `get_river_system_rainfall_forecast`，将 `zones` 原样放入该周期。统一返回：

```python
{
    "status": "ok",
    "river_name": target,
    "scope_type": "river_corridor" or "river_system",
    "scope_description": scope,
    "buffer_km": 5.0 or None,
    "periods": [{
        "label": period.label,
        "start_time": period.target_start.isoformat(),
        "end_time": period.target_end.isoformat(),
        "data_source": source,
        "has_rain": True or False,
        "average_rainfall_mm": value,
        "max_rainfall_mm": value,
        "min_rainfall_mm": value,
        "valid_count": count,
    }],
}
```

捕获 `RiverNotFoundError` 返回 `status="river_not_found"`；数据库、预报文件和栅格错误分别返回 `database_error`、`forecast_unavailable`、`calculation_error`。

- [ ] **Step 4: 运行核心测试和既有河系测试**

Run:

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest haihe-weather-analyzer-mcp/tests/test_river_query_forecast.py haihe-weather-analyzer-mcp/tests/test_river_system_rainfall_forecast.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交本任务**

```powershell
git add -- haihe-weather-analyzer-mcp/river_query_forecast.py haihe-weather-analyzer-mcp/tests/test_river_query_forecast.py
git commit -m "feat(river): aggregate corridor and river-system forecasts"
```

---

### Task 4: 注册 MCP 工具并锁定 Agent 工具选择

**Files:**
- Modify: `haihe-weather-analyzer-mcp/tools.py:1-30,2662-2710`
- Modify: `haihe-weather-analyzer-mcp/server.py:95-115`
- Modify: `chainlitexam/tools/active_tool_router.py:31-145`
- Modify: `chainlitexam/message_orchestrator.py:906-970`
- Modify: `chainlitexam/prompts.py:132-185,242-275`
- Modify: `chainlitexam/tests/test_active_tool_router.py`
- Modify: `chainlitexam/tests/fixtures/meteo_qa_cases.json`
- Create: `haihe-weather-analyzer-mcp/tests/test_river_query_tool_registration.py`

**Interfaces:**
- Produces MCP tool: `query_river_rainfall_forecast(user_query: str) -> dict`
- Produces active route domain: `river_forecast`

- [ ] **Step 1: 写注册和工具路由失败测试**

```python
def test_highlighted_river_questions_use_unified_river_tool():
    router, _ = _router()
    for question in ("明天泃河有雨吗？", "今天晚上滦河有雨吗？"):
        decision = router.select(question)
        assert decision.mode == "filtered"
        assert decision.query_type == "river_forecast"
        assert decision.tool_names == ("query_river_rainfall_forecast",)
        assert "query_rolling_forecast" not in decision.tool_names


def test_simple_weather_route_rejects_generic_river_names():
    assert mo._route_simple_weather_query("明天泃河有雨吗？") is None
```

在 `meteo_qa_cases.json` 增加两个用例，`required` 只含 `query_river_rainfall_forecast`，`forbidden` 包含 `query_rolling_forecast`。注册测试使用记录装饰器名称的 `FakeMCP` 调用 `tools.register_tools`，断言包含 `query_river_rainfall_forecast`。

- [ ] **Step 2: 运行测试并确认路由失败**

Run:

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest chainlitexam/tests/test_active_tool_router.py haihe-weather-analyzer-mcp/tests/test_river_query_tool_registration.py -q
```

Expected: FAIL，主动路由尚未提供 `river_forecast`。

- [ ] **Step 3: 注册统一 MCP 工具**

`tools.py` 导入 `river_query_forecast as rqf`，在河系工具附近注册：

```python
@mcp.tool()
def query_river_rainfall_forecast(user_query: str) -> dict:
    """查询具体河流沿线或九分区河系的未来降雨；参数必须传用户原始问题。"""
    ec_output_path = config.get("paths", "ecOutput") if config.has_option("paths", "ecOutput") else ""
    return rqf.query_river_rainfall_forecast_core(
        user_query=user_query,
        config=dict(config),
        ec_output_path=ec_output_path,
    )
```

在 `server.py` 的能力列表补充该工具名称，不修改服务地址或启动参数。

- [ ] **Step 4: 更新主动工具路由**

扩展河名正则，使“河”后接“有雨/会下雨/是否下雨”仍可命中；在 `_DOMAIN_TOOLS` 增加：

```python
"river_forecast": ("query_river_rainfall_forecast",),
```

在 `select` 的通用域分类之前增加高置信河流未来/今日时段判断：文本命中河流、包含降雨/天气词，并包含今天、今晚、明天、后天或未来时，直接返回 `river_forecast` 的过滤决策。河网关系、水位、暴雨影响范围等不进入此分支。

- [ ] **Step 5: 扩展简单天气的泛河名保护**

`message_orchestrator._is_basin_or_river_query` 除既有河名表外，再使用与主动路由同口径的泛河名正则，确保“泃河”不会被 `query_rolling_forecast` 抢占。

- [ ] **Step 6: 更新提示词工具边界**

提示词明确：具体河流和河系未来降雨统一调用 `query_river_rainfall_forecast`；具体河流按数据库真实河道两侧 5 公里，九分区河系复用分区预报；回答只引用工具返回的降雨字段和 `data_source`。

- [ ] **Step 7: 运行河流路由与 MCP 回归**

Run:

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest chainlitexam/tests/test_active_tool_router.py chainlitexam/tests/test_rolling_forecast_basin_guard.py haihe-weather-analyzer-mcp/tests/test_river_query_forecast.py haihe-weather-analyzer-mcp/tests/test_river_query_tool_registration.py haihe-weather-analyzer-mcp/tests/test_river_system_rainfall_forecast.py -q
```

Expected: PASS；两个标黄河流问题都只暴露统一河流工具。

- [ ] **Step 8: 提交本任务**

```powershell
git add -- haihe-weather-analyzer-mcp/tools.py haihe-weather-analyzer-mcp/server.py haihe-weather-analyzer-mcp/tests/test_river_query_tool_registration.py chainlitexam/tools/active_tool_router.py chainlitexam/message_orchestrator.py chainlitexam/prompts.py chainlitexam/tests/test_active_tool_router.py chainlitexam/tests/fixtures/meteo_qa_cases.json
git commit -m "feat(river): route river forecasts through spatial rainfall tool"
```
