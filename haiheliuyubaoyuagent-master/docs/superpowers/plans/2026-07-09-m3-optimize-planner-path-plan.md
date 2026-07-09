# M3 优化 Planner-Only 路径 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `query_decision_weather_for_poi` tool that encapsulates the original fast-path logic, and enhance `WEATHER_ASSISTANT_PROMPT` so the planner routes POI-specific weather questions to it.

**Architecture:** A new local LangChain tool in `chainlitexam/tools/decision_weather.py` reuses the slot extraction, POI lookup, station matching, and answer generation logic from `DecisionWeatherQAService` but returns plain Markdown instead of sending UI messages. The tool is registered alongside existing local tools, and the planner prompt is updated with a clear routing rule.

**Tech Stack:** Python 3.11, LangChain tools, Chainlit, pytest, pytest-asyncio

---

## File Map

| File | Responsibility |
|------|---------------|
| `chainlitexam/tools/decision_weather.py` (new) | Contains `query_decision_weather_for_poi` tool and helper functions extracted from `DecisionWeatherQAService` |
| `chainlitexam/message_orchestrator.py` (modify) | Source of `_extract_slots`, `_normalize_slots`, POI helpers, station matching, and `_generate_answer` to be reused/refactored |
| `chainlitexam/chain_gzt.py` (modify) | Registers the new tool by calling `build_decision_weather_tools()` |
| `chainlitexam/prompts.py` (modify) | Adds decision-weather POI routing guidance to `WEATHER_ASSISTANT_PROMPT` |
| `chainlitexam/tests/test_decision_weather_tool.py` (new) | Tests for the new tool |

---

## Task 1: Extract Decision Weather Logic into a Reusable Module

**Files:**
- Create: `chainlitexam/tools/decision_weather.py`
- Modify: `chainlitexam/message_orchestrator.py` (helper functions may become re-exports or shared)

**Context:** The original `DecisionWeatherQAService` in `message_orchestrator.py` mixes business logic with Chainlit UI side effects. We need a pure function version that takes `user_text`, `tools`, `answer_chain`, `callbacks` and returns Markdown.

- [ ] **Step 1: Create the new file skeleton**

Create `chainlitexam/tools/decision_weather.py`:

```python
"""决策天气 POI 查询工具：封装原 fast path 的多步逻辑，供 planner 调用。"""
from __future__ import annotations

import json
import traceback
from datetime import datetime, timedelta
from typing import Any

from langchain_core.tools import tool

# These functions will be imported from message_orchestrator.py in a later step
# _extract_first_json_object
# _decision_weather_prefilter
# _decision_pick_first_poi
# _nearest_decision_station
# _decision_period_args
# _select_decision_fcst_time
# _compact_decision_forecast_facts
# _clean_table_cell
# _sanitize_display_text


def build_decision_weather_tools():
    return [query_decision_weather_for_poi]
```

- [ ] **Step 2: Copy `_extract_slots` and `_normalize_slots` from `DecisionWeatherQAService`**

Read `chainlitexam/message_orchestrator.py` lines 628-700 (approximately). Copy `_extract_slots` and `_normalize_slots` into `chainlitexam/tools/decision_weather.py` as module-level async functions. Remove any dependency on `self` by passing required objects as arguments.

For example, `_extract_slots` originally calls `self.callbacks["ainvoke_chain"](self.answer_chain, ...)`. Change the signature to:

```python
async def _extract_slots(user_text: str, answer_chain, ainvoke_chain_callback) -> dict:
    ...
```

- [ ] **Step 3: Copy the answer generation prompt logic**

Copy `_generate_answer` from `DecisionWeatherQAService` (around lines 670-700) into the new file as a module-level async function:

```python
async def _generate_answer(user_text: str, facts: dict, answer_chain, ainvoke_chain_callback) -> str:
    prompt = (
        "请仅依据下面 JSON 中的业务天气事实回答用户问题。不要编造未返回的天气、雨量、温度、风力或能见度。\n"
        "严禁输出点位定位过程、经纬度、代表点、工具名、接口名、URL、参数名、query_mode、fcst_time、startPeriod、endPeriod、interval 等技术信息。\n"
        "回答统一采用业务口径：\n"
        "1. 必须先输出【核心结论】，用一句话直接回答天气是否良好、是否有降雨、是否有灾害性天气或是否适合活动。\n"
        "2. 综合天气/活动/考试/会展/节假日类：第二模块用【XX逐日预报】或【XX明日预报】，表格列为：日期｜天气现象｜气温(℃)｜风力（级）｜风向。\n"
        "3. 未来N小时是否下雨类：第二模块用【XX逐小时预报】，表格列为：时段｜天气现象｜气温(℃)｜风力（级）｜风向。\n"
        "4. 当前是否下雨类：核心结论写“当前无降雨/当前正在降雨”；第二模块用【降雨情况】，列出已返回的累计雨量或时段降水，缺失的1小时/3小时/6小时雨量不要编造。\n"
        "5. 风况字段中若同时包含风向和风力，请拆成“风力（级）”和“风向”；无法拆分时可在对应列写原始风况中的可识别部分。\n"
        "6. 末尾只写：数据来源：天津市气象台滚动预报。\n\n"
        f"用户问题：{user_text}\n\n"
        f"业务天气事实 JSON：{json.dumps(facts, ensure_ascii=False, default=str)}"
    )
    result = await ainvoke_chain_callback(answer_chain, {"messages": [HumanMessage(content=prompt)]})
    return getattr(result, "content", None) or str(result)
```

Add `from langchain_core.messages import HumanMessage` at the top of the file.

- [ ] **Step 4: Implement `query_decision_weather_for_poi`**

Add the tool function. It must accept `user_text` plus the runtime dependencies (`tools`, `answer_chain`, `callbacks`). Because LangChain `@tool` only exposes the `user_text` parameter to the LLM, we use a closure factory:

```python
def _build_query_decision_weather_for_poi(answer_chain, tools, callbacks):
    @tool
    async def query_decision_weather_for_poi(user_text: str) -> str:
        """
        回答关于具体地点、场馆、学校、医院、设施附近的未来天气或当前天气决策服务问题。
        适用于"XX地方明天天气怎么样""XX场馆未来24小时有雨吗""XX学校适合户外活动吗"等查询。
        内部会自动完成 POI 定位、代表站匹配、滚动预报查询和格式化回答。
        参数 user_text：用户原始问题文本。
        返回：已经格式化好的 Markdown 文本，可直接展示给用户。
        """
        if not user_text or not _decision_weather_prefilter(user_text):
            return "该问题不属于具体点位的决策天气查询，请使用其他工具或通用天气查询。"

        poi_tool = _find_tool(tools, "search_poi")
        forecast_tool = _find_tool(tools, "query_rolling_forecast")
        if not poi_tool or not forecast_tool:
            return "当前缺少 POI 定位或滚动预报工具，无法回答点位天气问题。"

        try:
            slots = await _extract_slots(user_text, answer_chain, callbacks["ainvoke_chain"])
        except Exception as exc:
            print(f"[DecisionWeatherTool] LLM 抽取失败：{exc}")
            return "无法识别点位天气查询意图，请补充具体地点和查询时段。"

        if not bool(slots.get("is_decision_weather")):
            return "该问题不属于具体点位的决策天气查询。"

        if bool(slots.get("need_clarification")):
            return str(slots.get("clarification_question") or "请补充具体位置和查询时段。").strip()

        normalized = _normalize_slots(slots)
        if normalized.get("error"):
            return normalized["error"]

        location_name = normalized["location_name"]
        target_start = normalized["target_start"]
        target_end = normalized["target_end"]
        interval = normalized["interval"]
        fcst_time = _select_decision_fcst_time()
        start_period, end_period = _decision_period_args(fcst_time, target_start, target_end)

        try:
            poi_raw = await poi_tool.ainvoke({"keyword": location_name, "size": 5})
            poi_payload = _unwrap_tool_observation(poi_raw)
            poi = _decision_pick_first_poi(poi_payload if isinstance(poi_payload, dict) else {})
            if not poi:
                return f"未检索到“{_clean_table_cell(location_name)}”的可用经纬度信息，请换一个更明确的位置名称。"

            poi_lon = float(poi["longitude"])
            poi_lat = float(poi["latitude"])
            nearest = _nearest_decision_station(poi_lon, poi_lat)
            point_name = str(poi.get("name") or location_name)
            poi_address = str(poi.get("address") or "")

            forecast_args = {
                "user_query": user_text,
                "regions": "",
                "lon": nearest["lon"],
                "lat": nearest["lat"],
                "point_name": f"{point_name}附近（{nearest['region']}代表点）",
                "matched_region": nearest["region"],
                "fcst_time": fcst_time,
                "start_period": start_period,
                "end_period": end_period,
                "interval": interval,
            }

            forecast_raw = await forecast_tool.ainvoke(forecast_args)
            forecast_payload = _unwrap_tool_observation(forecast_raw)

            facts = _compact_decision_forecast_facts(
                forecast_payload if isinstance(forecast_payload, dict) else {},
                target_start,
                target_end,
            )
            facts["poi"] = {
                "name": point_name,
                "address": poi_address,
                "lon": poi_lon,
                "lat": poi_lat,
            }
            facts["matched_station"] = nearest
            facts["question_type"] = slots.get("question_type") or "general_weather"

            final_text = await _generate_answer(user_text, facts, answer_chain, callbacks["ainvoke_chain"])
            return _sanitize_display_text(final_text or "")
        except Exception as exc:
            print(f"[DecisionWeatherTool] 查询失败：{exc}")
            traceback.print_exc()
            return "点位天气查询遇到异常，请稍后重试。"

    return query_decision_weather_for_poi
```

- [ ] **Step 5: Update `build_decision_weather_tools` to use the factory**

```python
def build_decision_weather_tools(answer_chain, tools, callbacks):
    """返回决策天气 POI 工具列表，供主模型绑定。"""
    return [_build_query_decision_weather_for_poi(answer_chain, tools, callbacks)]
```

- [ ] **Step 6: Add required imports and helpers**

Add imports at the top of `chainlitexam/tools/decision_weather.py`:

```python
from langchain_core.messages import HumanMessage
```

Implement or import from `message_orchestrator.py`:
- `_find_tool`
- `_unwrap_tool_observation`
- `_decision_weather_prefilter`
- `_decision_pick_first_poi`
- `_nearest_decision_station`
- `_decision_period_args`
- `_select_decision_fcst_time`
- `_compact_decision_forecast_facts`
- `_clean_table_cell`
- `_sanitize_display_text`
- `_extract_first_json_object`

**Decision:** For M3, import these helpers from `message_orchestrator.py` to avoid duplication. If circular imports arise, move the helpers to a shared module in a future cleanup.

- [ ] **Step 7: Run syntax check**

```bash
cd chainlitexam
python -m py_compile tools/decision_weather.py
```

Expected: no output (success)

- [ ] **Step 8: Commit**

```bash
git add chainlitexam/tools/decision_weather.py
git commit -m "feat: add query_decision_weather_for_poi tool skeleton"
```

---

## Task 2: Register the New Tool in chain_gzt.py

**Files:**
- Modify: `chainlitexam/chain_gzt.py:2382-2384`

**Context:** Tools are loaded in `on_chat_start` and bound to the planner LLM.

- [ ] **Step 1: Import the builder**

Add near the top of `chainlitexam/chain_gzt.py` where other tool builders are imported:

```python
from tools.decision_weather import build_decision_weather_tools
```

- [ ] **Step 2: Merge the new tool into the tools list**

Locate the lines in `chainlitexam/chain_gzt.py` where `tools` are loaded and bound to the planner. The new tool needs `answer_chain` and `callbacks`, so build it after those are available. Find the section similar to:

```python
tools = await load_sse_tools()
tools = tools + build_external_skill_tools() + build_rain_analysis_tools()
```

After `answer_chain` and `callbacks` are created in the same function, add:

```python
decision_weather_tools = build_decision_weather_tools(answer_chain, tools, callbacks)
tools = tools + decision_weather_tools
```

Make sure this happens before:

```python
planner_chain = prompt_template | planner_llm.bind_tools(tools)
```

- [ ] **Step 3: Verify tool loading**

Run:

```bash
cd chainlitexam
python -c "import chain_gzt; print('import ok')"
```

Expected: `import ok` (or any startup logs, no traceback)

- [ ] **Step 4: Commit**

```bash
git add chainlitexam/chain_gzt.py
git commit -m "feat: register decision weather POI tool in planner tool set"
```

---

## Task 3: Enhance WEATHER_ASSISTANT_PROMPT

**Files:**
- Modify: `chainlitexam/prompts.py`

**Context:** The planner prompt needs to explicitly tell the model when to call `query_decision_weather_for_poi` and when NOT to.

- [ ] **Step 1: Add decision weather POI section**

Find the section `### 4. 知识库类问题回答规范` in `chainlitexam/prompts.py`. Immediately before it, insert:

```markdown
### 5. 决策天气 POI 查询规范（强制）
- 当用户询问**具体地点、场馆、学校、医院、设施、单位**附近的未来天气或当前天气时，**必须**调用 `query_decision_weather_for_poi`。
- 典型问法：
  - "梅江会展中心明天天气怎么样"
  - "天津大学未来24小时会下雨吗"
  - "XX公园适合周末露营吗"
  - "XX机场现在能见度如何"
  - "XX医院附近未来三天天气"
- 该工具会自动完成 POI 定位、代表站匹配、滚动预报查询和格式化回答，**不要**自行拆分调用 `search_poi` 和 `query_rolling_forecast`。
- 如果用户问的是"天津天气""海河流域天气""西青区天气""滨海新区天气"等**宽泛区域**，**不调用**此工具，优先使用天津滚动预报或降雨工具。
```

- [ ] **Step 2: Clarify query_rolling_forecast usage**

Find where `query_rolling_forecast` is mentioned in the prompt. Update its description to:

```markdown
- `query_rolling_forecast`：查询天津及其区级区域（如西青区、滨海新区）的未来综合天气（气温、风力、降水、能见度等）。适用于区域级天气预报，**不适用于具体点位/场馆/设施查询**。
```

- [ ] **Step 3: Clarify search_poi usage**

Add near the precipitation or tool list section:

```markdown
- `search_poi`：仅用于需要精确经纬度的点位定位场景（如决策天气 POI 查询内部使用）。常规区域天气预报不要调用此工具。
```

- [ ] **Step 4: Run tests to ensure no syntax errors**

```bash
cd chainlitexam
python -m pytest tests/test_reasoning_step.py tests/test_thinking.py tests/test_thinking_summary.py tests/test_timing_logger.py tests/test_message_orchestrator.py -q
```

Expected: all tests pass

- [ ] **Step 5: Commit**

```bash
git add chainlitexam/prompts.py
git commit -m "docs: enhance planner prompt with decision-weather POI routing rules"
```

---

## Task 4: Add Tests for the New Tool

**Files:**
- Create: `chainlitexam/tests/test_decision_weather_tool.py`

**Context:** We need tests that verify the tool loads correctly and its internal logic works with mocked dependencies.

- [ ] **Step 1: Create test file with tool loading test**

Create `chainlitexam/tests/test_decision_weather_tool.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chainlitexam.tests.stubs import ensure_stubs

ensure_stubs()

import chainlitexam.tools.decision_weather as dw


def test_build_decision_weather_tools_returns_one_tool():
    tools = dw.build_decision_weather_tools(None, [], {})
    assert len(tools) == 1
    assert tools[0].name == "query_decision_weather_for_poi"
```

- [ ] **Step 2: Add a test for the happy path with mocked dependencies**

Append to the same file:

```python
import pytest
from datetime import datetime


class FakeChain:
    async def ainvoke(self, *args, **kwargs):
        class Result:
            content = '{"is_decision_weather": true, "location_name": "梅江会展中心", "target_start_time": "2026-07-09 08:00:00", "target_end_time": "2026-07-10 08:00:00", "interval_hours": 12, "question_type": "general_weather", "need_clarification": false}'
        return Result()


class FakePoiTool:
    name = "search_poi"

    async def ainvoke(self, args):
        return [{
            "text": json.dumps({
                "pois": [{
                    "name": "梅江会展中心",
                    "address": "天津市西青区",
                    "longitude": 117.2,
                    "latitude": 39.0,
                }]
            }, ensure_ascii=False)
        }]


class FakeForecastTool:
    name = "query_rolling_forecast"

    async def ainvoke(self, args):
        return [{
            "text": json.dumps({
                "periods": [{
                    "start_time": "2026-07-09 08:00:00",
                    "end_time": "2026-07-09 20:00:00",
                    "region": "西青区",
                    "WEA": "晴",
                    "TMAX": 32,
                    "TMIN": 24,
                    "EDA": "东南风3级",
                    "TP1H": 0,
                }]
            }, ensure_ascii=False)
        }]


@pytest.mark.asyncio
async def test_query_decision_weather_for_poi_happy_path():
    answer_chain = FakeChain()
    tools = [FakePoiTool(), FakeForecastTool()]
    callbacks = {
        "ainvoke_chain": lambda chain, inputs: answer_chain.ainvoke(),
    }

    poi_tools = dw.build_decision_weather_tools(answer_chain, tools, callbacks)
    tool = poi_tools[0]
    result = await tool.ainvoke({"user_text": "梅江会展中心明天天气怎么样"})

    assert isinstance(result, str)
    assert "梅江会展中心" in result or "核心结论" in result
```

- [ ] **Step 3: Run the new tests**

```bash
cd chainlitexam
python -m pytest tests/test_decision_weather_tool.py -v
```

Expected: tests pass. If the test reveals missing helpers or imports, fix them in `tools/decision_weather.py`.

- [ ] **Step 4: Commit**

```bash
git add chainlitexam/tests/test_decision_weather_tool.py
git commit -m "test: add decision weather POI tool tests"
```

---

## Task 5: Run Full Regression Suite

**Files:**
- N/A (verification step)

- [ ] **Step 1: Run all tests with fast paths disabled**

```bash
cd chainlitexam
ENABLE_FAST_PATHS=false python -m pytest tests/ -q
```

Expected: all tests pass

- [ ] **Step 2: Run all tests with fast paths enabled**

```bash
cd chainlitexam
ENABLE_FAST_PATHS=true python -m pytest tests/ -q
```

Expected: all tests pass

- [ ] **Step 3: Run fast path AST checks**

```bash
cd chainlitexam
python tests/test_fast_paths.py
```

Expected: `Total: 18 fast paths, 18 passed.`

- [ ] **Step 4: Commit verification**

```bash
git commit --allow-empty -m "chore: verify M3 changes pass regression suite in both modes"
```

---

## Self-Review Checklist

- [ ] Spec coverage: M3 design doc requirements (new tool, prompt enhancement, tests) are all covered by Tasks 1-5.
- [ ] Placeholder scan: No TBD/TODO; all code blocks contain real code; all commands exact.
- [ ] Type consistency: `build_decision_weather_tools` signature consistent across file, `chain_gzt.py`, and tests.
- [ ] Test coverage: Tool loading and happy path tested.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-09-m3-optimize-planner-path-plan.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach do you want?
