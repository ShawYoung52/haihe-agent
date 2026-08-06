# Planner/Answer Prompt 拆分 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从 `WEATHER_ASSISTANT_PROMPT`（644 行）提取 `PLANNER_SYSTEM_PROMPT`（只含工具选择/参数规则/停止条件）和 `METEO_ANSWER_SYSTEM_PROMPT`（只含气象表达/格式/结论结构），双轨 binding，默认关闭，不删除旧 Prompt。

**Architecture:** ①从 `prompts.py` 现有 prompt 提取 Planner/Answer 两部分；②`chain_gzt.py` 加 `ENABLE_NEW_PLANNER_PROMPT`/`ENABLE_NEW_ANSWER_PROMPT` 开关，默认 false；③`_build_orchestrator_runtime` 双轨选择 prompt，planner 和 answer 独立可配。

**Tech Stack:** Python 3.10+, Chainlit（2.9.6/2.11.0）, pytest.

## Global Constraints

- 旧 Prompt 保留为默认，不删除。关开关后行为恢复原样。
- 纯提取不新增内容——新 Prompt 从旧 Prompt 提取，不写新规则。
- 不改变回答逻辑、工具选择、消息顺序、HTTP 契约。
- 默认 `ENABLE_NEW_PLANNER_PROMPT=false`、`ENABLE_NEW_ANSWER_PROMPT=false`。
- 分支：`perf/qa-prompt-split`（已建）。测试从 `chainlitexam/` 运行，venv `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe`。
- 全量套件预期 1 个既有 flaky `test_process_message_skips_fast_paths_when_disabled`。

---

### Task 1: Prompt 拆分 + 双轨绑定

**Files:**
- Modify: `chainlitexam/prompts.py`（新增 `PLANNER_SYSTEM_PROMPT`、`METEO_ANSWER_SYSTEM_PROMPT`；保留 `WEATHER_ASSISTANT_PROMPT` 不变）
- Modify: `chainlitexam/chain_gzt.py`（`_build_orchestrator_runtime` 加开关 + 双轨选择）
- Test: `chainlitexam/tests/test_prompts.py`

**Interfaces:**
- Consumes: `WEATHER_ASSISTANT_PROMPT`（现有 644 行）、`_build_orchestrator_runtime()`（chain_gzt.py）。
- Produces: `PLANNER_SYSTEM_PROMPT`、`METEO_ANSWER_SYSTEM_PROMPT`（两个新常量）；`ENABLE_NEW_PLANNER_PROMPT`、`ENABLE_NEW_ANSWER_PROMPT`（模块级 bool）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_prompts.py` 新增：

```python
def test_prompt_split_constants_exist():
    """新 Prompt 常量存在且与旧 Prompt 不同。"""
    import prompts
    assert hasattr(prompts, "PLANNER_SYSTEM_PROMPT")
    assert hasattr(prompts, "METEO_ANSWER_SYSTEM_PROMPT")
    assert isinstance(prompts.PLANNER_SYSTEM_PROMPT, str) and len(prompts.PLANNER_SYSTEM_PROMPT) > 100
    assert isinstance(prompts.METEO_ANSWER_SYSTEM_PROMPT, str) and len(prompts.METEO_ANSWER_SYSTEM_PROMPT) > 100
    # 旧 Prompt 不变
    assert len(prompts.WEATHER_ASSISTANT_PROMPT) > 500


def test_planner_prompt_has_no_formatting():
    """PLANNER_SYSTEM_PROMPT 不含 Markdown 格式/回答结构/语言风格。"""
    import prompts
    p = prompts.PLANNER_SYSTEM_PROMPT
    assert "| :---" not in p  # 无 Markdown 表格分隔符
    assert "核心结论" not in p  # 无结论格式
    assert "业务语言风格" not in p  # 无语言风格章节


def test_planner_prompt_has_tool_rules():
    """PLANNER_SYSTEM_PROMPT 含工具选择/参数/停止条件。"""
    import prompts
    p = prompts.PLANNER_SYSTEM_PROMPT
    assert "有工具必须用工具" in p or "调用工具获取真实数据" in p
    assert "决策天气" in p or "query_decision_weather" in p
    assert "单维度" in p  # 停止条件


def test_answer_prompt_has_expression_rules():
    """METEO_ANSWER_SYSTEM_PROMPT 含气象表达/格式/结论结构。"""
    import prompts
    p = prompts.METEO_ANSWER_SYSTEM_PROMPT
    assert "核心结论" in p
    assert "表格" in p or "| :---" in p
    assert "数据来源" in p


def test_answer_prompt_has_no_tool_selection():
    """METEO_ANSWER_SYSTEM_PROMPT 不含工具选择/路由规则。"""
    import prompts
    p = prompts.METEO_ANSWER_SYSTEM_PROMPT
    assert "有工具必须用工具" not in p
    assert "query_decision_weather_for_poi" not in p
```

- [ ] **Step 2: 运行测试确认失败**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_prompts.py -v`
Expected: 新增测试 FAIL（`PLANNER_SYSTEM_PROMPT` 不存在）。

- [ ] **Step 3: 提取 PLANNER_SYSTEM_PROMPT**

从 `WEATHER_ASSISTANT_PROMPT` 提取以下部分到 `prompts.py` 新常量 `PLANNER_SYSTEM_PROMPT`（在 `WEATHER_ASSISTANT_PROMPT` 之前定义）：

**包含的章节**（纯提取，不新增）：
- 核心规则：有工具必须用工具（强制）（第 117-122 行）
- 服务范围声明（第 124 行）
- 知识库类问题：意图判定优先级（第 126-129 行）
- 天气预报服务（第 149-156 行）
- 降水预报与实况查询（第 163-199 行）
- 决策天气 POI 查询规范（第 202-212 行）
- 时间问题处理规范（第 246-266 行）
- 暴雨场景 · 分析范围（第 269-276 行）
- 单维度提问收口（第 273-276 行）
- 各工具路由规则（流域/子流域/预警/水位/河网/应急/面雨量/短临/预报检验/降雨图/风险预警等——从 WEATHER_ASSISTANT_PROMPT 第 365-510 行及相关段落提取）

**不包含**：语言风格、Markdown 格式、回答结构、结论模板、表格规范、风控建议、对话规范、领导简报、清单完整性、典型问题模板。

- [ ] **Step 4: 提取 METEO_ANSWER_SYSTEM_PROMPT**

从 `WEATHER_ASSISTANT_PROMPT` 提取到 `prompts.py` 新常量 `METEO_ANSWER_SYSTEM_PROMPT`：

**包含的章节**：
- 天津气象业务语言风格：核心结论优先（强制）（第 86-112 行）
- 回答规范（第 237-353 行）——数据准确性、时间问题、回答结构、表格格式、风险评估、对话规范、领导简报、完整明细、清单完整性、语言风格、特殊情况
- 典型问题回答模板（第 355-359 行）
- 重要提醒（第 360-364 行）
- 服务范围声明（第 124 行，重复但必要）

**不包含**：工具选择、路由优先级、参数规则、何时停止。

- [ ] **Step 5: 实现双轨绑定**

在 `chain_gzt.py` 的 `_build_orchestrator_runtime` 中（约 2512 行）：

```python
    from prompts import WEATHER_ASSISTANT_PROMPT, PLANNER_SYSTEM_PROMPT, METEO_ANSWER_SYSTEM_PROMPT
    ENABLE_NEW_PLANNER_PROMPT = os.environ.get("ENABLE_NEW_PLANNER_PROMPT", "false").strip().lower() in ("1", "true", "yes")
    ENABLE_NEW_ANSWER_PROMPT = os.environ.get("ENABLE_NEW_ANSWER_PROMPT", "false").strip().lower() in ("1", "true", "yes")

    planner_prompt = PLANNER_SYSTEM_PROMPT if ENABLE_NEW_PLANNER_PROMPT else WEATHER_ASSISTANT_PROMPT
    answer_prompt = METEO_ANSWER_SYSTEM_PROMPT if ENABLE_NEW_ANSWER_PROMPT else WEATHER_ASSISTANT_PROMPT

    planner_template = ChatPromptTemplate.from_messages([
        ("system", f"{prompt_prefix}{planner_prompt}"),
        MessagesPlaceholder(variable_name="messages"),
    ])
    answer_template = ChatPromptTemplate.from_messages([
        ("system", f"{prompt_prefix}{answer_prompt}"),
        MessagesPlaceholder(variable_name="messages"),
    ])
    planner_chain = planner_template | planner_llm.bind_tools(tools)
    answer_chain = answer_template | answer_llm
```

> 注意：`thinking_chain` 的 prompt 保持不变（它不参与决策，用 `{system_message}` 占位）。

- [ ] **Step 6: 运行测试确认通过**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_prompts.py tests/test_planner_answer_config.py tests/test_message_orchestrator.py -v`
Expected: PASS。

- [ ] **Step 7: 运行全量测试确认无回归**

Run: `D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_decision_weather_tool.py -v`
Expected: 全量 PASS，仅 1 个既有 flaky。

- [ ] **Step 8: 提交**

```bash
git add chainlitexam/prompts.py chainlitexam/chain_gzt.py chainlitexam/tests/test_prompts.py
git commit -m "feat(qa): split planner/answer prompts with dual-track gating (default off)"
```

---

## Self-Review

**1. Spec coverage**：Prompt 拆分（Task 1）✓；双轨绑定 ✓；默认关闭 ✓；旧 Prompt 保留 ✓。

**2. Placeholder scan**：提取 Prompt 的章节定位以实际行号为准（实现时按语义边界提取），非空占位。无 TBD。

**3. Type consistency**：`PLANNER_SYSTEM_PROMPT`/`METEO_ANSWER_SYSTEM_PROMPT` 均为 `str`；`ENABLE_NEW_PLANNER_PROMPT`/`ENABLE_NEW_ANSWER_PROMPT` 均为 `bool`。

**风险**：纯提取不新增——若遗漏某条规则，旧 Prompt 切换回即恢复。回归比较后启用。