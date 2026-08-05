# 问答排版 + 决策天气延迟优化 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ①修复网页端工具 step 出现在回答下方的排版问题（挂到思考过程下）；②优化 `query_decision_weather_for_poi` 31 秒延迟（槽位规则抽取省 1 次 LLM 调用）；③候选工具召回影子模式（只记录不启用）。

**Architecture:** ①`_run_tool_round` 加 `parent_step_id` 参数，把工具 step 挂到 `reasoning.step.id` 下；②`_extract_decision_slots_rule_based` 纯规则抽位置名+问题类型，规则失败回退 LLM；③`ToolCandidateIndex` 启动构建一次，影子记录候选工具是否包含 Planner 实际调用工具。

**Tech Stack:** Python 3.10+, Chainlit（兼容 2.9.6 内网 + 2.11.0 本地）, pytest.

## Global Constraints

- 内网 Chainlit **2.9.6**，不重装。不引入 `auto_collapse`（2.10+ 才有）等新特性。
- 只改 `_run_tool_round` 父子挂接与决策天气槽位抽取，**不改回答逻辑、工具调用、消息顺序**。
- 测试必须从 `chainlitexam/` 运行；用 venv `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe`。
- 全量套件预期 1 个既有 flaky 失败 `tests/test_message_orchestrator.py::test_process_message_skips_fast_paths_when_disabled`。
- 规则抽取失败必须回退 LLM（保底，不破坏回答）。
- 分支：`perf/qa-step-layout-latency`（已建）。提交 `git add` 只加改动文件。

---

### Task 1: 工具 step 挂到思考过程下（排版）

**Files:**
- Modify: `chainlitexam/message_orchestrator.py`（`_run_tool_round` 约 1850 行 + 调用点约 4629 行）
- Test: `chainlitexam/tests/test_message_orchestrator.py`

**Interfaces:**
- Consumes: `process_message` 内 `reasoning: ReasoningStep`（有 `.step.id` 属性）；`_run_tool_round` 现有 6 参数。
- Produces: `_run_tool_round(planner_msg, tools, messages, user_text, iteration, callbacks, parent_step_id: str | None = None)`。"第 N 轮数据查询" step 的 `parent_id` 设为 `parent_step_id`（若提供）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_message_orchestrator.py` 新增：

```python
def test_run_tool_round_uses_parent_step_id():
    """_run_tool_round 传入 parent_step_id 时，工具 step 应挂到该父 step 下。"""
    created = []

    class FakeStep:
        _id_counter = 0
        def __init__(self, **kwargs):
            type(self)._id_counter += 1
            self.id = f"step-{type(self)._id_counter}"
            self.name = kwargs.get("name", "")
            self.parent_id = kwargs.get("parent_id")
            self.type = kwargs.get("type", "tool")
            self.show_input = True
            self.output = ""
            self.input = ""
            self.default_open = None
            created.append(self)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def update(self):
            return None

    class FakePlannerMsg:
        tool_calls = []

    import chainlit as cl
    # 用 monkeypatch 注入 FakeStep，但这里以函数内直接 patch 为例：
    import message_orchestrator as mo

    async def _run():
        callbacks = {"tool_observation_to_text": lambda obs: str(obs)}
        messages = []
        await mo._run_tool_round(
            FakePlannerMsg(), [], messages, "测试", 1, callbacks, parent_step_id="reasoning-step-1"
        )
        return messages

    asyncio.get_event_loop().run_until_complete(_run())

    round_step = next((s for s in created if s.name.startswith("第 1 轮")), None)
    assert round_step is not None, f"应创建第 1 轮 step，实际 created={[s.name for s in created]}"
    assert round_step.parent_id == "reasoning-step-1", f"第 1 轮 step 应挂到 reasoning-step-1，实际 {round_step.parent_id}"
```

> 注：`FakeStep` 需要在调用前 patch 到 `mo.cl.Step`。若用 `monkeypatch.setattr(mo.cl, "Step", FakeStep)` 更干净，测试里用 monkeypatch fixture。确保 `FakePlannerMsg.tool_calls=[]` 时 `_run_tool_round` 不调用 `_invoke_tools_in_parallel`（或该函数对空列表返回空 dict）。

- [ ] **Step 2: 运行测试确认失败**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_message_orchestrator.py::test_run_tool_round_uses_parent_step_id -v`
Expected: FAIL（当前无 `parent_step_id` 参数）。

- [ ] **Step 3: 实现**

`_run_tool_round` 签名加 `parent_step_id: str | None = None`（1850 行）。"第 N 轮数据查询" step（1859 行）改为：

```python
    async with cl.Step(name=f"第 {iteration} 轮数据查询（共 {len(planner_msg.tool_calls)} 项）", type="tool",
                       parent_id=parent_step_id) as step:
```

`process_message` 调用点（4629 行）加传参：

```python
        forced_final_text, ree, warning_bundles, round_rolling_forecast_bundles = await _run_tool_round(
            planner_msg, tools, messages, message.content, iteration, callbacks,
            parent_step_id=reasoning.step.id if reasoning and reasoning.step else None,
        )
```

> 说明：`reasoning` 在 `process_message` 内始终已创建（`reasoning = ReasoningStep("🤔 思考过程")` + `__aenter__`），`.step` 非 None。默认 None 时行为不变。

- [ ] **Step 4: 运行测试确认通过**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_message_orchestrator.py -v`
Expected: PASS（含新增测试与既有测试）。

- [ ] **Step 5: 运行全量测试确认无回归**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_decision_weather_tool.py -v`
Expected: 全量 PASS，仅 1 个既有 flaky 失败。

- [ ] **Step 6: 提交**

```bash
git add chainlitexam/message_orchestrator.py chainlitexam/tests/test_message_orchestrator.py
git commit -m "fix(qa): attach tool round steps under reasoning step (fix step layout)"
```

---

### Task 2: 决策天气槽位规则抽取 + LLM 兜底（速度）

**Files:**
- Modify: `chainlitexam/tools/decision_weather_core.py`（新增 `_extract_decision_slots_rule_based`；改 `_extract_decision_weather_slots`）
- Test: `chainlitexam/tests/test_decision_weather_tool.py`

**Interfaces:**
- Consumes: `_decision_weather_prefilter` 的 `institution_suffixes` 列表逻辑。
- Produces: `_extract_decision_slots_rule_based(user_text: str) -> dict | None`（返回 `{"is_decision_weather", "location_name", "question_type", "need_clarification", "clarification_question"}` 或 None）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_decision_weather_tool.py` 新增：

```python
def test_rule_based_slot_extraction_locations():
    """规则抽取能识别常见点位名称。"""
    import dw  # 现有 import 别名（检查现有文件头部 import）
    assert dw._extract_decision_slots_rule_based("梅江会展中心明天天气怎么样")["location_name"] == "梅江会展中心"
    assert dw._extract_decision_slots_rule_based("天津大学未来24小时会下雨吗")["location_name"] == "天津大学"
    assert dw._extract_decision_slots_rule_based("梅江会展中心适合户外活动吗")["question_type"] == "activity"


def test_rule_based_slot_extraction_falls_back_on_ambiguous():
    """无明确后缀的模糊问题应返回 None（回退 LLM）。"""
    assert dw._extract_decision_slots_rule_based("学校明天天气怎么样") is None  # 后缀"学校"前无具体名词
    assert dw._extract_decision_slots_rule_based("天气怎么样") is None
```

> 注：检查现有测试文件的 import（是 `import dw` 还是 `from tools import decision_weather as dw`），适配实际别名。

- [ ] **Step 2: 运行测试确认失败**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_decision_weather_tool.py::test_rule_based_slot_extraction_locations -v`
Expected: FAIL（`_extract_decision_slots_rule_based` 不存在）。

- [ ] **Step 3: 实现**

在 `decision_weather_core.py` 新增（放在 `_decision_weather_prefilter` 后）：

```python
_DECISION_WEATHER_SUFFIXES = [
    "会展中心", "中心", "大学", "学院", "学校", "医院", "公园", "酒店",
    "大厦", "广场", "机场", "车站", "码头", "景区", "园区", "小区",
]
_DECISION_RAIN_WORDS = ["下雨", "有雨", "降雨", "降水", "暴雨", "雷阵雨", "雨"]


def _extract_decision_slots_rule_based(user_text: str) -> dict | None:
    """纯规则抽取点位决策天气槽位；无法可靠抽取时返回 None（调用方回退 LLM）。"""
    t = (user_text or "").strip()
    if not t:
        return None

    # 1) 位置名：匹配机构后缀前的最长名词短语
    location = None
    for suffix in sorted(_DECISION_WEATHER_SUFFIXES, key=len, reverse=True):
        idx = t.find(suffix)
        if idx < 0:
            continue
        # 取后缀前的一段（跳过标点/介词/时间词）
        head = t[:idx]
        head = re.split(r"[，。？?！!、\s，]|今天|明天|后天|周末|未来|现在|上午|下午|晚上|夜里", head)[-1]
        # 去掉结尾的"在/去/到/位于/附近"等
        head = re.sub(r"(在|去|到|位于|附近|周边|旁边|距|距离)$", "", head)
        candidate = (head + suffix).strip()
        if candidate and len(candidate) >= 2:
            location = candidate
            break

    if not location:
        return None

    # 2) 问题类型
    qtype = "general_weather"
    if any(w in t for w in ["适合", "活动", "户外", "露营", "出行"]):
        qtype = "activity"
    elif any(w in t for w in ["未来", "小时", "接下来"]):
        qtype = "rain_next_hours"
    elif any(w in t for w in _DECISION_RAIN_WORDS):
        qtype = "rain_now"
    elif any(w in t for w in ["能见度", "雾", "霾"]):
        qtype = "visibility"
    elif any(w in t for w in ["气温", "温度", "热", "冷"]):
        qtype = "temperature"
    elif any(w in t for w in ["风"]):
        qtype = "wind"

    return {
        "is_decision_weather": True,
        "location_name": location,
        "question_type": qtype,
        "need_clarification": False,
        "clarification_question": "",
    }
```

改 `_extract_decision_weather_slots` 开头（312 行）加规则优先：

```python
    rule_slots = _extract_decision_slots_rule_based(user_text)
    if rule_slots:
        return rule_slots
    # 回退：现有 LLM 抽取
```

- [ ] **Step 4: 运行测试确认通过**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_decision_weather_tool.py -v`
Expected: PASS（含新增测试与既有 27 条）。

- [ ] **Step 5: 运行全量测试确认无回归**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_decision_weather_tool.py -v`
Expected: 全量 PASS，仅 1 个既有 flaky 失败。

- [ ] **Step 6: 提交**

```bash
git add chainlitexam/tools/decision_weather_core.py chainlitexam/tests/test_decision_weather_tool.py
git commit -m "perf(qa): rule-based decision weather slot extraction with LLM fallback"
```

---

### Task 3: 候选工具召回影子模式（只记录不启用）— GPT 方案七

**Files:**
- Create: `chainlitexam/tools/tool_candidate_index.py`（新文件）
- Modify: `chainlitexam/chain_gzt.py`（`_build_orchestrator_runtime` 构建索引 + callbacks 传 `tool_candidate_index`）
- Modify: `chainlitexam/message_orchestrator.py`（`process_message` 影子记录候选 vs 实际工具）
- Test: `chainlitexam/tests/test_tool_candidate_index.py`（新建）

**Interfaces:**
- Produces: `ToolCandidateIndex` 类——`__init__(tools: list)` 构建关键词→工具映射；`candidates_for(user_text: str, limit: int = 12) -> list[str]` 返回候选工具名；`build` 一次（启动时）。
- 影子模式：`process_message` 在 Planner 返回 tool_calls 后，调用 `candidates_for(user_text)`，记录 `[TOOL_CAND]` 日志"候选是否包含实际调用工具"，**不改 Planner 绑定**。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_tool_candidate_index.py`：

```python
"""候选工具召回索引（影子模式）测试。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from chainlitexam.tests.stubs import ensure_stubs
ensure_stubs()

from tools.tool_candidate_index import ToolCandidateIndex


def _fake_tool(name, desc=""):
    class _T:
        def __init__(self):
            self.name = name
            self.description = desc
        @property
        def args_schema(self):
            class _S:
                properties = {}
            return _S()
    return _T()


def test_candidates_include_weather_tools_for_weather_query():
    tools = [
        _fake_tool("query_rolling_forecast", "查询天津滚动预报未来天气"),
        _fake_tool("query_decision_weather_for_poi", "查询具体点位附近天气"),
        _fake_tool("get_effective_warning_info", "查询当前生效预警"),
        _fake_tool("query_water_level", "查询水位"),
    ]
    idx = ToolCandidateIndex(tools)
    cands = idx.candidates_for("梅江会展中心明天天气怎么样", limit=12)
    assert "query_decision_weather_for_poi" in cands, f"天气+点位查询应召回决策天气工具，实际 {cands}"


def test_candidates_include_warning_tool_for_warning_query():
    tools = [_fake_tool("get_effective_warning_info", "查询当前生效预警"), _fake_tool("query_rolling_forecast", "预报")]
    idx = ToolCandidateIndex(tools)
    cands = idx.candidates_for("天津有暴雨预警吗", limit=12)
    assert "get_effective_warning_info" in cands, f"预警查询应召回预警工具，实际 {cands}"


def test_index_built_once_is_stable():
    tools = [_fake_tool("query_rolling_forecast", "预报天气")]
    idx = ToolCandidateIndex(tools)
    first = idx.candidates_for("明天天气", limit=12)
    second = idx.candidates_for("明天天气", limit=12)
    assert first == second  # 索引稳定，不随调用变化
```

- [ ] **Step 2: 运行测试确认失败**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_tool_candidate_index.py -v`
Expected: FAIL（`ToolCandidateIndex` 不存在）。

- [ ] **Step 3: 实现**

新建 `tools/tool_candidate_index.py`：

```python
"""候选工具召回索引（影子模式）。

启动时构建一次关键词→工具映射。Planner 仍绑定完整工具列表，
本模块只用于"记录候选工具是否包含 Planner 实际调用工具"的影子观测，
不改变 Planner 行为。
"""
from __future__ import annotations

import re
from typing import Any


class ToolCandidateIndex:
    """基于工具名/描述/参数名的关键词召回索引。"""

    def __init__(self, tools: list[Any]):
        self._tools = tools
        self._by_keyword: dict[str, list[str]] = {}
        self._default_candidates: list[str] = []
        self._build(tools)

    def _build(self, tools: list[Any]) -> None:
        """一次构建：提取每个工具的关键词并建立倒排。"""
        fallback_names = ["rag_search", "query_rolling_forecast"]  # 兜底工具
        for tool in tools:
            name = getattr(tool, "name", "") or ""
            if not name:
                continue
            desc = getattr(tool, "description", "") or ""
            keywords = self._keywords_for(name, desc)
            for kw in keywords:
                self._by_keyword.setdefault(kw, []).append(name)
            if name in fallback_names:
                self._default_candidates.append(name)
        # 兜底工具始终在候选里
        for fb in fallback_names:
            if fb not in self._default_candidates:
                self._default_candidates.append(fb)

    def _keywords_for(self, name: str, desc: str) -> list[str]:
        """从工具名+描述提取中文关键词。"""
        text = f"{name} {desc}"
        # 常见业务词
        biz = [
            "天气", "降雨", "降水", "雨", "预警", "水位", "河网", "河流",
            "行政区", "面雨量", "应急", "点位", "短临", "强对流", "雷暴",
            "冰雹", "气温", "风", "能见度", "雾", "霾", "站点", "实况",
        ]
        return [b for b in biz if b in text]

    def candidates_for(self, user_text: str, limit: int = 12) -> list[str]:
        """按用户问题关键词召回候选工具；无命中时返回兜底工具。"""
        matched: list[str] = []
        for kw, names in self._by_keyword.items():
            if kw in (user_text or ""):
                for n in names:
                    if n not in matched:
                        matched.append(n)
        for n in self._default_candidates:
            if n not in matched:
                matched.append(n)
        return matched[:limit]

    def contains(self, user_text: str, actual_tool: str, limit: int = 12) -> bool:
        """影子观测：候选是否包含实际调用的工具。"""
        return actual_tool in self.candidates_for(user_text, limit=limit)
```

`chain_gzt.py` `_build_orchestrator_runtime` 构建索引并传入 runtime：

```python
    from tools.tool_candidate_index import ToolCandidateIndex
    tool_candidate_index = ToolCandidateIndex(tools)
    # ... return 增加 "tool_candidate_index": tool_candidate_index
```

`message_orchestrator.py` `process_message` 影子记录（Planner 首次返回 tool_calls 后）：

```python
    if planner_msg.tool_calls and callbacks.get("tool_candidate_index"):
        try:
            idx = callbacks["tool_candidate_index"]
            actual = [tc["name"] for tc in planner_msg.tool_calls]
            hit = [t for t in actual if idx.contains(message.content, t)]
            print(f"[TOOL_CAND] request={session_id} actual={actual} "
                  f"recalled={len(hit)}/{len(actual)} candidates={idx.candidates_for(message.content, limit=12)}")
        except Exception:
            pass
```

> 说明：影子模式**只打印 `[TOOL_CAND]` 日志**，不改 Planner 绑定。`callbacks` 需包含 `tool_candidate_index`（`_build_orchestrator_callbacks` 加一项，默认 None 兼容）。

- [ ] **Step 4: 运行测试确认通过**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_tool_candidate_index.py -v`
Expected: PASS。

- [ ] **Step 5: 运行全量测试确认无回归**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_decision_weather_tool.py -v`
Expected: 全量 PASS，仅 1 个既有 flaky 失败。

- [ ] **Step 6: 提交**

```bash
git add chainlitexam/tools/tool_candidate_index.py chainlitexam/chain_gzt.py chainlitexam/message_orchestrator.py chainlitexam/tests/test_tool_candidate_index.py
git commit -m "feat(qa): shadow-mode tool candidate recall index (log-only, no binding change)"
```

---

## Self-Review

**1. Spec coverage**：
- 改动 1（排版：工具 step 挂思考下）→ Task 1 ✓
- 改动 2（决策天气槽位规则抽取 + LLM 兜底）→ Task 2 ✓
- 改动 3（候选工具召回影子）→ Task 3 ✓
- 兼容 2.9.6：不引入 auto_collapse ✓
- 改动 2（决策天气槽位规则抽取 + LLM 兜底）→ Task 2 ✓
- 兼容 2.9.6：不引入 auto_collapse ✓

**2. Placeholder scan**：Task 1 测试用 `FakeStep` patch `mo.cl.Step`，Task 2 测试用现有 `dw` 别名——均注明了适配方式。无 TBD/TODO。

**3. Type consistency**：`_run_tool_round(..., parent_step_id: str | None = None)` 在 Task 1 定义与调用一致；`_extract_decision_slots_rule_based(user_text) -> dict | None` 在 Task 2 定义与调用一致。

**风险**：Task 2 规则抽取若误判位置名，POI 检索失败 → 工具返回"未检索到可用经纬度"（与 LLM 失败路径一致）。LLM 兜底保证模糊问题不改变行为。内网 2.9.6 上工具 step 挂父后自然折叠在思考容器内。