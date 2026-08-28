# 天河新增目录接入 Implementation Plan

> 执行状态（2026-08-28）：51题实现和离线回归完成，独立最终审查待执行，内网联调待完成；最终状态、56题清单和命令见[验收记录](../2026-08-27-priority-acceptance.md)。下方保留原始提案步骤，不以未勾选复选框表示尚未实现。冲突示例已被执行裁定取代：新区域风险no_data为不可用、geography真实米制缓冲、裸河名先查全量表、Chainlit/MCP分目录分进程使用系统Python测试。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将甲方新增的 51 个天河问题稳定接入 `query_tianhe_fixed_qa`，同时保留此前 03/04/05 已验收问法并阻止非目录问题误调用天河。

**Architecture:** 新增一个无 Chainlit 依赖的目录模块，集中保存新增验收问题与轻量规范化规则。消息编排器先检查新增精确目录，再执行现有简单天气和旧天河兼容规则；所有工具调用继续经过既有天河边界校验并原样传递用户文本。

**Tech Stack:** Python 3.10+、pytest、Chainlit、LangChain tool calls

**Spec:** `chainlitexam/docs/superpowers/specs/2026-08-26-tianhe-river-risk-priority-design.md`

## Global Constraints

- `ENABLE_FAST_PATHS` 必须保持 `False`。
- 新增 51 题只做空白和句末标点规范化，不做模糊语义扩展。
- `query_tianhe_fixed_qa.query` 必须保留用户输入原文。
- 天河失败时不得调用本地工具或 LLM 代答。
- 此前已验收的 03/04/05 天河问法必须继续命中。
- “未来三天天气怎么样”不得命中天河。
- “高温预警期间最高会到多少度”必须继续调用 `get_effective_warning_info`。

---

### Task 1: 建立新增天河目录的单一事实来源

**Files:**
- Create: `chainlitexam/tools/tianhe_fixed_qa_catalog.py`
- Create: `chainlitexam/tests/test_tianhe_fixed_qa_catalog.py`

**Interfaces:**
- Produces: `TIANHE_FIXED_QA_QUESTIONS: tuple[str, ...]`
- Produces: `normalize_tianhe_catalog_question(value: str) -> str`
- Produces: `is_tianhe_fixed_qa_question(value: str) -> bool`

- [ ] **Step 1: 写目录完整性和规范化失败测试**

```python
from tools.tianhe_fixed_qa_catalog import (
    TIANHE_FIXED_QA_QUESTIONS,
    is_tianhe_fixed_qa_question,
)


def test_new_catalog_contains_all_51_unique_questions():
    assert len(TIANHE_FIXED_QA_QUESTIONS) == 51
    assert len(set(TIANHE_FIXED_QA_QUESTIONS)) == 51


def test_catalog_accepts_only_spacing_and_terminal_punctuation_variants():
    assert is_tianhe_fixed_qa_question("  当前湿度大不大？  ")
    assert is_tianhe_fixed_qa_question("当前 湿度 大不大?")
    assert not is_tianhe_fixed_qa_question("天津未来三天天气怎么样？")
    assert not is_tianhe_fixed_qa_question("明天蓟州湿度大不大？")
```

测试中的 51 题参数表必须逐字包含用户清单，从“今年7月蓟州区有多少天超过35℃”到“降雨对道路交通会带来什么影响？”，不得用从生产常量反向读取的方式构造预期值。

- [ ] **Step 2: 运行测试并确认因模块不存在而失败**

Run:

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest chainlitexam/tests/test_tianhe_fixed_qa_catalog.py -q
```

Expected: FAIL，错误为 `ModuleNotFoundError: tools.tianhe_fixed_qa_catalog`。

- [ ] **Step 3: 实现目录模块**

```python
from __future__ import annotations

import re


TIANHE_FIXED_QA_QUESTIONS = (
    "今年7月蓟州区有多少天超过35℃",
    "今年以来我市40℃以上高温出现过几次？",
    "现在市区风大吗？",
    "市区现在气温和风的实况",
    "全市现在下了多少雨",
    "今天雨都下在哪了",
    "暴雨天气的防范建议",
    "大风天气的防范建议",
    "高温天气的防范建议",
    "强对流天气怎么应对",
    "暴雨预警四个等级是什么",
    "高温怎么定义",
    "气温多高算是高温",
    "高温来了公众应该怎么办",
    "高温预警信号及应对措施",
    "降雨量怎么分等级",
    "台风等级",
    "暴雨预警发出后公众该怎么办",
    "暴雨是如何形成的",
    "暴雨等级是如何划分的",
    "暴雨的主要危害有哪些",
    "当前湿度大不大？",
    "今日雨情",
    "今天适合洗车吗？",
    "今天穿衣有什么建议？",
    "今天适不适合晾晒？",
    "什么是短时强降水？",
    "副高代表什么含义？",
    "什么是面雨量？",
    "雷电怎么防御？",
    "高温有哪些危害？",
    "冰雹产生原理？",
    "双偏振雷达干什么用？",
    "自动气象站如何观测？",
    "气象卫星有什么作用？",
    "雾和霾有什么区别？",
    "夏天为何多雨？",
    "为什么打雷下雨？",
    "天津当前的天气情况",
    "预警发布流程是什么？",
    "天气会商包含哪些内容？",
    "面雨量如何计算？",
    "双偏振雷达产品怎么看？",
    "MICAPS 产品怎么分析？",
    "你可以回答哪些问题？",
    "明天出门要不要带伞",
    "哪些问题你无法解答？",
    "你的气象数据来源是什么？",
    "预报可以支持多长时效？",
    "我该怎么向你提问？",
    "降雨对道路交通会带来什么影响？",
)


def normalize_tianhe_catalog_question(value: str) -> str:
    text = re.sub(r"[\s\u3000]+", "", str(value or ""))
    return text.rstrip("？?。！!")


_NORMALIZED_TIANHE_FIXED_QA = frozenset(
    normalize_tianhe_catalog_question(item) for item in TIANHE_FIXED_QA_QUESTIONS
)


def is_tianhe_fixed_qa_question(value: str) -> bool:
    normalized = normalize_tianhe_catalog_question(value)
    return bool(normalized) and normalized in _NORMALIZED_TIANHE_FIXED_QA
```

- [ ] **Step 4: 运行目录测试并确认通过**

Run:

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest chainlitexam/tests/test_tianhe_fixed_qa_catalog.py -q
```

Expected: PASS，51 题唯一性和正反例全部通过。

- [ ] **Step 5: 提交本任务**

```powershell
git add -- chainlitexam/tools/tianhe_fixed_qa_catalog.py chainlitexam/tests/test_tianhe_fixed_qa_catalog.py
git commit -m "feat(tianhe): add fixed QA acceptance catalog"
```

---

### Task 2: 将新增目录置于本地天气路由之前

**Files:**
- Modify: `chainlitexam/message_orchestrator.py:931-1179,5330-5390`
- Modify: `chainlitexam/tests/test_tianhe_knowledge_route.py`

**Interfaces:**
- Consumes: `is_tianhe_fixed_qa_question(value: str) -> bool`
- Produces: `_route_tianhe_fixed_catalog_query(user_text: str) -> tuple[str, dict] | None`
- Preserves: `_route_tianhe_knowledge_query(user_text)` as the combined new-catalog plus legacy 03/04/05 boundary predicate

- [ ] **Step 1: 写优先级、原文透传和旧目录兼容失败测试**

```python
from tools.tianhe_fixed_qa_catalog import TIANHE_FIXED_QA_QUESTIONS


@pytest.mark.parametrize("question", TIANHE_FIXED_QA_QUESTIONS)
def test_all_new_questions_route_verbatim(question):
    assert mo._route_tianhe_fixed_catalog_query(question) == (
        "query_tianhe_fixed_qa",
        {"query": question},
    )


def test_original_text_is_not_trimmed_before_tool_call():
    raw = "  当前湿度大不大？  "
    assert mo._route_tianhe_fixed_catalog_query(raw)[1] == {"query": raw}


def test_catalog_wins_before_simple_weather_route():
    assert mo._route_simple_weather_query("今天适合洗车吗？") is not None
    assert mo._route_tianhe_fixed_catalog_query("今天适合洗车吗？")[0] == "query_tianhe_fixed_qa"


@pytest.mark.parametrize("question", [
    "昨天雨下得怎么样",
    "今天雨下了多长时间",
    "去年夏天全市最高气温达到多少度",
])
def test_previously_accepted_tianhe_questions_remain_supported(question):
    assert mo._route_tianhe_knowledge_query(question) is not None
```

- [ ] **Step 2: 运行定向测试并确认新增接口尚不存在或新问题未命中**

Run:

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest chainlitexam/tests/test_tianhe_knowledge_route.py -q
```

Expected: FAIL，新增 30 题至少有一题未命中，且 `_route_tianhe_fixed_catalog_query` 尚不存在。

- [ ] **Step 3: 接入目录并保留原文**

在 `message_orchestrator.py` 导入目录谓词并新增：

```python
from tools.tianhe_fixed_qa_catalog import is_tianhe_fixed_qa_question


def _route_tianhe_fixed_catalog_query(user_text: str) -> tuple[str, dict] | None:
    raw = str(user_text or "")
    if not is_tianhe_fixed_qa_question(raw):
        return None
    return "query_tianhe_fixed_qa", {"query": raw}
```

在 `_route_tianhe_knowledge_query` 的空文本判断后先调用该函数；未命中时继续执行原有 03/04/05 兼容规则。原有兼容分支的 `query` 行为保持不变，避免扩大本次修改面。

- [ ] **Step 4: 调整 `process_message` 的路由顺序**

将当前“简单天气 → 天河”的顺序改为：

```python
simple_route = _route_tianhe_fixed_catalog_query(message.content)
simple_route_label = "天河固定目录路由" if simple_route else "简单天气路由"
if simple_route is None and not _is_future_hour_weather_query(message.content):
    simple_route = _route_simple_weather_query(message.content)
if simple_route is None:
    simple_route = _route_tianhe_knowledge_query(message.content)
    if simple_route is not None:
        simple_route_label = "天河兼容目录路由"
```

保持 `_is_future_hour_weather_query` 的现有强制路由不变；新增目录中的“明天出门要不要带伞”不是“未来 N 小时”，会优先命中天河。

- [ ] **Step 5: 修正天河边界的原文参数**

`_enforce_tianhe_catalog_boundary` 校验时仍调用组合目录函数，但保留原始输入：

```python
normalized["args"] = {"query": str(user_text or "")}
```

不要在该赋值处调用 `.strip()`。

- [ ] **Step 6: 运行路由与边界测试**

Run:

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest chainlitexam/tests/test_tianhe_fixed_qa_catalog.py chainlitexam/tests/test_tianhe_knowledge_route.py chainlitexam/tests/test_warning_workflow.py -q
```

Expected: PASS；新增 51 题、旧目录兼容和高温预警本地路由同时通过。

- [ ] **Step 7: 提交本任务**

```powershell
git add -- chainlitexam/message_orchestrator.py chainlitexam/tests/test_tianhe_knowledge_route.py
git commit -m "feat(tianhe): prioritize the confirmed fixed QA catalog"
```

---

### Task 3: 锁定工具失败边界和非目录反例

**Files:**
- Modify: `chainlitexam/tests/test_tianhe_knowledge_route.py`
- Modify: `chainlitexam/prompts.py:253-270`

**Interfaces:**
- Preserves: `_enforce_tianhe_catalog_boundary(planner_msg, user_text)`
- Preserves: 天河工具失败后的直接失败说明，不向本地 Planner 回退

- [ ] **Step 1: 写非目录和 Planner 越权回归测试**

```python
from langchain_core.messages import AIMessage


@pytest.mark.parametrize("question", [
    "未来三天天气怎么样？",
    "明天蓟州天气怎么样？",
    "高温预警期间最高会到多少度？",
    "今天蓟州可能有哪些风险？",
    "明天泃河有雨吗？",
])
def test_new_catalog_does_not_capture_local_business_questions(question):
    assert mo._route_tianhe_fixed_catalog_query(question) is None


def test_planner_cannot_inject_tianhe_for_non_catalog_question():
    msg = AIMessage(content="", tool_calls=[{
        "name": "query_tianhe_fixed_qa",
        "args": {"query": "未来三天天气怎么样？"},
        "id": "bad-tianhe",
    }])
    guarded = mo._enforce_tianhe_catalog_boundary(msg, "未来三天天气怎么样？")
    assert guarded.tool_calls == []
```

- [ ] **Step 2: 运行测试并确认当前实现的真实状态**

Run:

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest chainlitexam/tests/test_tianhe_knowledge_route.py -q
```

Expected: PASS；如任何反例失败，先收紧目录匹配，不通过增加排除词掩盖错误。

- [ ] **Step 3: 更新提示词中的供应方边界说明**

将 `prompts.py` 的天河边界说明改为：新增 51 题优先按固定目录命中、此前 03/04/05 已验收问法继续兼容、目录外问题严禁调用天河、工具参数必须原文透传、天河失败不得本地代答。保留高温预警过程值走本地生效预警工具的现有规则。

- [ ] **Step 4: 运行 Chainlit 相关回归**

Run:

```powershell
& 'C:/Users/Xiao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest chainlitexam/tests/test_tianhe_fixed_qa_catalog.py chainlitexam/tests/test_tianhe_knowledge_route.py chainlitexam/tests/test_warning_workflow.py chainlitexam/tests/test_active_tool_router.py chainlitexam/tests/test_message_orchestrator.py::test_tianhe_tool_level_failure_still_stops_local_agent chainlitexam/tests/test_message_orchestrator.py::test_tianhe_unexpected_tool_exception_still_stops_local_agent chainlitexam/tests/test_message_orchestrator.py::test_tianhe_missing_tool_still_stops_local_agent -q
```

Expected: PASS，且无网络访问。

- [ ] **Step 5: 提交本任务**

```powershell
git add -- chainlitexam/prompts.py chainlitexam/tests/test_tianhe_knowledge_route.py
git commit -m "test(tianhe): lock catalog boundaries and local fallbacks"
```
