# 区域综合风险与水库景区验收 Implementation Plan

> 执行状态（2026-08-28）：区域风险、标黄POI和本轮全量回归检查完成，独立最终审查待执行，完整依赖环境复测和内网联调待完成；最终56题和真实测试状态见[验收记录](../2026-08-27-priority-acceptance.md)。下方保留原始提案步骤，不以未勾选复选框表示尚未实现。no_data→no_risk示例已被替代为unavailable；缺水情且无雨不得预测水位平稳。关联河流裁定为九分区名称直接按分区统计，下一级具体河流查全量表并使用geography真实米制缓冲。Chainlit/MCP分目录分进程使用系统Python测试；原跨项目命令不能代表最终验证方式。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 稳定回答“今天蓟州可能有哪些风险”，并锁定于桥水库未来三天、盘山景区未来两天的现有 POI 决策天气链路和事实驱动注意事项。

**Architecture:** 在滚动预报服务中抽取区域综合风险核心入口，复用现有区域坐标、静态隐患和三灾种风险等级查询，不重复访问接口。FastMCP 注册风险专用工具，主动工具路由只对“已知区域 + 时间 + 泛风险”问题开放它；水库与景区继续走 `query_decision_weather_for_poi`，只增加验收和事实门控回归。

**Tech Stack:** Python 3.10+、pytest、FastMCP、Chainlit、现有风险 HTTP 接口与 POI 检索

**Spec:** `chainlitexam/docs/superpowers/specs/2026-08-26-tianhe-river-risk-priority-design.md`

## Global Constraints

- `ENABLE_FAST_PATHS` 必须保持 `False`。
- 风险查询必须一次汇总地质灾害、山洪和中小河流风险。
- 风险接口有效返回空集合时写“无风险”。
- 风险接口失败时写“接口暂不可用”或查询失败，不得冒充无风险。
- 最终回答不得出现“暂无对应时次风险资料”。
- 于桥水库和盘山景区继续复用 `query_decision_weather_for_poi`，不复制 POI 查询实现。
- 注意事项只能引用工具返回的天气现象、降雨、雷电、风、能见度、高温和风险事实。
- 不改变现有登录、权限和其他问答格式。

---

### Task 1: 提供区域综合风险核心查询

**Files:**
- Modify: `haihe-weather-analyzer-mcp/rolling_forecast_service.py:59-320,1142-1200`
- Create: `haihe-weather-analyzer-mcp/tests/test_region_weather_risks.py`

**Interfaces:**
- Produces: `query_region_weather_risks_core(user_query: str, regions: str = "", now: datetime | None = None) -> dict`
- Consumes: `parse_rolling_forecast_regions(...)`
- Consumes: `_region_or_city_coord(...)`
- Consumes: `_risk_fcst_times_from_window(...)`
- Consumes: `_query_region_hazards(...)`

- [ ] **Step 1: 写成功、无风险、部分失败和全失败测试**

```python
from datetime import datetime
from zoneinfo import ZoneInfo

import rolling_forecast_service as rfs

FIXED_NOW = datetime(2026, 8, 27, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def hazard_payload(*, risk_levels, risk_levels_available=True):
    return {
        "total_found": 298,
        "radius_km": 25.0,
        "categories": [
            {"key": "dzzh", "label": "地质灾害", "count": 257},
            {"key": "sh", "label": "山洪", "count": 27},
            {"key": "zxhl", "label": "中小河流", "count": 14},
        ],
        "hazards_available": True,
        "risk_levels": risk_levels,
        "risk_levels_available": risk_levels_available,
    }


def test_jizhou_risk_query_returns_all_three_categories(monkeypatch):
    monkeypatch.setattr(rfs, "_query_region_hazards", lambda lon, lat, times: {
        "total_found": 298,
        "radius_km": 25.0,
        "categories": [
            {"key": "dzzh", "label": "地质灾害", "count": 257},
            {"key": "sh", "label": "山洪", "count": 27},
            {"key": "zxhl", "label": "中小河流", "count": 14},
        ],
        "hazards_available": True,
        "risk_levels": {
            "dzzh": {"levels": {"三级": 1}, "total": 1, "level_advice": ["关注地质灾害风险"]},
            "sh": {"levels": {"四级": 2}, "total": 2, "level_advice": ["远离沟谷河道"]},
        },
        "risk_levels_available": True,
    })
    result = rfs.query_region_weather_risks_core(
        "今天蓟州可能有哪些风险？", now=FIXED_NOW
    )
    assert result["status"] == "ok"
    assert result["regions"][0]["region"] == "蓟州"
    assert {item["key"] for item in result["regions"][0]["risks"]} == {"dzzh", "sh", "zxhl"}


def test_empty_reachable_levels_are_no_risk(monkeypatch):
    monkeypatch.setattr(rfs, "_query_region_hazards", lambda *a: hazard_payload(risk_levels={}))
    result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)
    assert all(item["risk_status"] == "no_risk" for item in result["regions"][0]["risks"])


def test_explicit_failed_kind_is_unavailable_not_no_risk(monkeypatch):
    monkeypatch.setattr(rfs, "_query_region_hazards", lambda *a: hazard_payload(risk_levels={"dzzh": None}))
    result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)
    dzzh = next(item for item in result["regions"][0]["risks"] if item["key"] == "dzzh")
    assert dzzh["risk_status"] == "unavailable"


def test_all_risk_interfaces_failed_is_not_no_risk(monkeypatch):
    monkeypatch.setattr(rfs, "_query_region_hazards", lambda *a: hazard_payload(
        risk_levels=None, risk_levels_available=False
    ))
    result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)
    assert result["status"] == "risk_service_unavailable"
```

- [ ] **Step 2: 运行测试并确认核心入口不存在**

Run:

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest haihe-weather-analyzer-mcp/tests/test_region_weather_risks.py -q
```

Expected: FAIL，`query_region_weather_risks_core` 尚不存在。

- [ ] **Step 3: 实现风险状态归一化**

固定三类元数据：

```python
REGION_RISK_CATEGORIES = (
    ("dzzh", "地质灾害"),
    ("sh", "山洪"),
    ("zxhl", "中小河流"),
)
```

核心函数从 `regions or user_query` 解析区域，使用 `_region_or_city_coord` 获取经纬度，使用 `resolve_requested_calendar_window` 与 `_risk_fcst_times_from_window` 生成风险时次，再对每个区域仅调用一次 `_query_region_hazards`。

每个灾种统一输出：

```python
{
    "key": "sh",
    "label": "山洪",
    "hidden_point_count": 27,
    "risk_status": "risk" | "no_risk" | "unavailable",
    "levels": {"四级": 2},
    "risk_point_count": 2,
    "advice": ["远离沟谷河道"],
}
```

规则：`risk_levels_available is False` 时所有类型 `unavailable`；字典中显式 `key: None` 时该类型 `unavailable`；字典缺少该 key 或值为 `no_data` 时按已确认业务口径归为 `no_risk`；存在等级时为 `risk`。

- [ ] **Step 4: 运行风险核心和既有风险回归**

Run:

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest haihe-weather-analyzer-mcp/tests/test_region_weather_risks.py haihe-weather-analyzer-mcp/tests/test_risk_warning_hazard_match.py haihe-weather-analyzer-mcp/tests/test_rolling_forecast_region_hazards.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交本任务**

```powershell
git add -- haihe-weather-analyzer-mcp/rolling_forecast_service.py haihe-weather-analyzer-mcp/tests/test_region_weather_risks.py
git commit -m "feat(risk): add region-wide weather risk query core"
```

---

### Task 2: 注册风险工具并增加高置信主动路由

**Files:**
- Modify: `haihe-weather-analyzer-mcp/haihe_mcp_tools.py:20-35,3015-3210`
- Modify: `haihe-weather-analyzer-mcp/server.py:95-115`
- Modify: `chainlitexam/tools/active_tool_router.py:31-145`
- Modify: `chainlitexam/prompts.py:188-275`
- Modify: `chainlitexam/tests/test_active_tool_router.py`
- Modify: `chainlitexam/tests/fixtures/meteo_qa_cases.json`
- Create: `haihe-weather-analyzer-mcp/tests/test_region_risk_tool_registration.py`

**Interfaces:**
- Produces MCP tool: `query_region_weather_risks(user_query: str, regions: str = "") -> dict`
- Produces active route domain: `region_risk`

- [ ] **Step 1: 写注册和路由失败测试**

```python
def test_generic_region_risk_question_uses_dedicated_tool():
    router, _ = _router()
    decision = router.select("今天蓟州可能有哪些风险？")
    assert decision.mode == "filtered"
    assert decision.query_type == "region_risk"
    assert decision.tool_names == ("query_region_weather_risks",)


def test_non_weather_business_risk_does_not_use_weather_risk_tool():
    router, _ = _router()
    assert router.select("项目上线有哪些风险？").query_type != "region_risk"
```

在 `meteo_qa_cases.json` 增加“今天蓟州可能有哪些风险？”用例，`required` 只含 `query_region_weather_risks`。注册测试使用 `FakeMCP` 记录 `register_haihe_tools` 装饰的函数名，断言包含 `query_region_weather_risks`。

- [ ] **Step 2: 运行测试并确认新工具尚未注册**

Run:

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest chainlitexam/tests/test_active_tool_router.py haihe-weather-analyzer-mcp/tests/test_region_risk_tool_registration.py -q
```

Expected: FAIL。

- [ ] **Step 3: 注册 MCP 包装器**

从 `rolling_forecast_service` 导入核心函数，并在 `register_haihe_tools` 中注册：

```python
@mcp.tool()
def query_region_weather_risks(user_query: str, regions: str = "") -> dict:
    """查询天津已知区域在用户指定时段的地质灾害、山洪和中小河流综合风险。"""
    return query_region_weather_risks_core(user_query=user_query, regions=regions)
```

工具说明明确：只用于“某区今天/明天可能有哪些风险”等泛风险问题；单一灾种专业查询仍使用 `query_risk_warning`。

- [ ] **Step 4: 实现保守的 `region_risk` 路由**

在主动路由器中增加：

```python
"region_risk": ("query_region_weather_risks",),
```

命中必须同时满足：

1. 文本含天津已知区域或别名；
2. 文本含“风险”；
3. 文本含“今天、今日、明天、未来、当前、现在、可能”等至少一个时态/研判词；
4. 不含“项目、投资、合同、上线、账号”等非气象业务词；
5. 不含明确单灾种词“山洪、地质灾害、中小河流洪水”，避免抢占专业工具。

- [ ] **Step 5: 更新提示词**

提示词明确泛区域风险使用 `query_region_weather_risks`，必须完整列出三类状态；`no_risk` 写“无风险”，`unavailable` 写“接口暂不可用”，不得生成“暂无对应时次风险资料”。建议仅使用工具的 `advice` 和实际等级。

- [ ] **Step 6: 运行工具与路由回归**

Run:

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest chainlitexam/tests/test_active_tool_router.py haihe-weather-analyzer-mcp/tests/test_region_weather_risks.py haihe-weather-analyzer-mcp/tests/test_region_risk_tool_registration.py -q
```

Expected: PASS。

- [ ] **Step 7: 提交本任务**

```powershell
git add -- haihe-weather-analyzer-mcp/haihe_mcp_tools.py haihe-weather-analyzer-mcp/server.py haihe-weather-analyzer-mcp/tests/test_region_risk_tool_registration.py chainlitexam/tools/active_tool_router.py chainlitexam/prompts.py chainlitexam/tests/test_active_tool_router.py chainlitexam/tests/fixtures/meteo_qa_cases.json
git commit -m "feat(risk): route generic regional risk questions to composite tool"
```

---

### Task 3: 锁定于桥水库和盘山景区既有 POI 链路

**Files:**
- Modify: `chainlitexam/tests/test_decision_weather_tool.py`
- Modify: `chainlitexam/tests/test_active_tool_router.py`
- Review: `chainlitexam/tools/decision_weather_core.py`
- Review: `chainlitexam/tools/decision_weather.py`

**Interfaces:**
- Preserves: `query_decision_weather_for_poi(user_text: str) -> str`
- Preserves: `classify_poi_category("于桥水库") == "reservoir"`
- Preserves: `classify_poi_category("盘山景区") == "scenic"`

- [ ] **Step 1: 写两个标黄问题的路由验收测试**

```python
import chainlitexam.message_orchestrator as mo


@pytest.mark.parametrize("question", [
    "未来三天于桥水库降雨预报？",
    "盘山景区未来两天天气？",
])
def test_highlighted_poi_questions_use_decision_weather(question):
    router, _ = _router()
    decision = router.select(question)
    assert decision.mode == "filtered"
    assert decision.query_type == "decision_poi"
    assert decision.tool_names == ("query_decision_weather_for_poi",)


@pytest.mark.parametrize("question", [
    "未来三天于桥水库降雨预报？",
    "盘山景区未来两天天气？",
])
def test_highlighted_poi_questions_use_decision_weather_before_planner(question):
    assert mo._route_simple_weather_query(question) == (
        "query_decision_weather_for_poi",
        {"user_text": question},
    )
```

- [ ] **Step 2: 写逐日数量和事实门控验收测试**

使用现有代码生成表格与注意事项，构造三天水库事实和两天景区事实：

```python
def _fair_weather_facts(name, category, days):
    return {
        "poi": {"name": name},
        "poi_category": category,
        "has_rain_signal": False,
        "total_rain_mm": 0.0,
        "periods": [{
            "period_label": f"08月{28 + offset}日",
            "weather": "多云",
            "tmax": 28,
            "tmin": 21,
            "EDA": "西北风2-3级",
            "rain_1h": 0.0,
            "visibility_min_km": 8.0,
        } for offset in range(days)],
    }


def test_yuqiao_reservoir_renders_three_days_without_invented_hazards():
    facts = _fair_weather_facts("于桥水库", "reservoir", 3)
    table = dw_core._build_decision_weather_table("未来三天于桥水库降雨预报？", facts)
    reminder = dw_core._build_poi_reminder_section(facts)
    assert all(f"08月{day}日" in table for day in (28, 29, 30))
    assert "低能见度" not in reminder
    assert "雷电" not in reminder
    assert "高温" not in reminder


def test_panshan_renders_two_days_and_real_weather_can_trigger_advice():
    facts = _fair_weather_facts("盘山景区", "scenic", 2)
    table = dw_core._build_decision_weather_table("盘山景区未来两天天气？", facts)
    assert "08月28日" in table and "08月29日" in table
    facts["periods"][0]["weather"] = "雷阵雨"
    facts["has_rain_signal"] = True
    facts["total_rain_mm"] = 12.0
    reminder = dw_core._build_poi_reminder_section(facts)
    assert "雷电" in reminder or "雷雨" in reminder
```

再沿用现有 `test_full_text_point_keeps_low_visibility_reminder` 和高温建议测试，确保门控不是一律删除建议。

- [ ] **Step 3: 运行验收测试并记录是否需要生产修改**

Run:

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest chainlitexam/tests/test_decision_weather_tool.py chainlitexam/tests/test_active_tool_router.py -q
```

Expected: PASS；本任务锁定现有 POI 分类、日期拆分和事实门控行为，不计划修改生产实现。

- [ ] **Step 4: 运行 POI 与滚动预报组合回归**

Run:

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest chainlitexam/tests/test_decision_weather_tool.py chainlitexam/tests/test_rolling_forecast_response.py haihe-weather-analyzer-mcp/tests/test_rolling_forecast_region_hazards.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交本任务**

```powershell
git add -- chainlitexam/tests/test_decision_weather_tool.py chainlitexam/tests/test_active_tool_router.py
git commit -m "test(poi): lock reservoir and scenic forecast acceptance"
```

---

### Task 4: 全量回归、代码审查和简化复核

**Files:**
- Review: all files changed by the three implementation plans
- Update: `current-progress.md`
- Update: `chainlitexam/docs/superpowers/specs/2026-08-26-tianhe-river-risk-priority-design.md` status line

**Interfaces:**
- Produces: 56 题内网验收清单（51 个天河问题 + 5 个标黄问题）
- Produces: final automated test evidence

- [ ] **Step 1: 运行所有新增定向测试**

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest chainlitexam/tests/test_tianhe_fixed_qa_catalog.py chainlitexam/tests/test_tianhe_knowledge_route.py chainlitexam/tests/test_active_tool_router.py chainlitexam/tests/test_decision_weather_tool.py haihe-weather-analyzer-mcp/tests/test_river_query_forecast.py haihe-weather-analyzer-mcp/tests/test_river_query_tool_registration.py haihe-weather-analyzer-mcp/tests/test_region_weather_risks.py haihe-weather-analyzer-mcp/tests/test_region_risk_tool_registration.py -q
```

Expected: PASS。

- [ ] **Step 2: 运行 Chainlit 全量测试**

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest chainlitexam/tests -q
```

Expected: 所有既有测试通过；环境依赖导致的 skip 数量需要记录，不得把失败改成 skip。

- [ ] **Step 3: 运行 MCP 相关全量测试**

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest haihe-weather-analyzer-mcp/tests -q
```

Expected: 所有离线单元测试通过；不得为绕过内网不可达而删除断言。

- [ ] **Step 4: 执行 code review**

逐文件检查：天河是否仍有非目录误路由；所有河流 SQL 是否参数化；具体河流是否只读 `river_table_full`；5 公里缓冲是否使用米制坐标；风险失败是否可能被转成无风险；POI 注意事项是否存在无事实触发；`ENABLE_FAST_PATHS` 是否仍为 `False`。发现问题先补失败测试，再修复。

- [ ] **Step 5: 执行 code simplifier**

只做等价简化：删除重复的目录列表、河名正则和风险状态映射，优先复用新模块公开函数；不得合并不同业务状态，不得改动现有回答标题、字段顺序或外部配置。每次简化后重跑 Step 1 定向测试。

- [ ] **Step 6: 更新进度和设计状态**

在 `current-progress.md` 记录：已接入的 51 题、5 个标黄问题、自动化测试数量、外网未执行的内网真实接口验收。将设计文档状态改为“实现完成，待内网联调”或实际状态。

- [ ] **Step 7: 最终提交**

```powershell
git add -- current-progress.md chainlitexam/docs/superpowers/specs/2026-08-26-tianhe-river-risk-priority-design.md
git commit -m "docs: record Tianhe and priority weather acceptance status"
```
