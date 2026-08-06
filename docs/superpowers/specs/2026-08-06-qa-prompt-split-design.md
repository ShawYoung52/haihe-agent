# Planner/Answer Prompt 拆分 设计

**日期**：2026-08-06
**状态**：设计（待用户审阅）
**范围**：`chainlitexam/prompts.py` + `chain_gzt.py`

## 背景

当前 `WEATHER_ASSISTANT_PROMPT`（644 行）同时承担 Planner 决策和 Answer 格式两个角色：
- Planner 需要：工具选择原则、参数规则、何时继续/停止、路由优先级
- Answer 需要：气象术语、时间/空间/单位规范、Markdown 表格、结论结构

**长 Prompt 使 Planner 第一个 token 延迟增加**（大量气象语言风格和格式规则在 Planner 决策时无关），且两个角色耦合在一起难以独立调优。

## 硬约束（GPT 13 原则）

- **双轨并存**：旧 Prompt 保留为默认，新 Prompt 经开关启用。不得删除旧 Prompt。
- **默认关闭**：`ENABLE_NEW_PLANNER_PROMPT=false`、`ENABLE_NEW_ANSWER_PROMPT=false`。关开关后行为恢复原样。
- **Shadow 比较**：启用新 Prompt 后，用固定问题集比较工具调用一致率，达标前不替代旧 Prompt。
- **禁止大规模重写 prompt_template 绑定逻辑**：只在 `_build_orchestrator_runtime` 增加 if/else 选择。

## 设计

### 1. PLANNER_SYSTEM_PROMPT（只负责决策）

从 `WEATHER_ASSISTANT_PROMPT` 提取以下部分，**不新增内容**：

- **核心规则：有工具必须用工具**（工具选择原则）
- **知识库类问题：意图判定优先级**（路由优先级）
- **天气预报服务：工具选择**（哪个工具对应哪种问法）
- **降水相关工具列表 + 时间设置规则**（参数规则）
- **决策天气 POI 查询规范**（路由到 POI 工具）
- **时间问题处理规范**（参数规则）
- **暴雨场景 · 分析范围**（参数规则）
- **单维度提问收口**（停止条件）
- 各工具路由规则（预警、水位、河网、应急响应、面雨量、流域、子流域、短临、预报检验等）

**不包含**：
- 气象语言风格、Markdown 表格格式、结论结构、防范建议、业务术语、单位规范、数据来源表述
- 回答结构、风险评估开关、领导简报模板、完整明细模式、清单完整性、语言风格

### 2. METEO_ANSWER_SYSTEM_PROMPT（只负责专业化表达）

从 `WEATHER_ASSISTANT_PROMPT` 提取：
- **天津气象业务语言风格：核心结论优先**
- **回答规范**（数据准确性、预报不确定性、数据来源）
- **回答结构**（核心结论 → 表格 → 数据来源）
- **表格格式规范**（左对齐、表头/分隔行/数据行）
- **对话规范**（只输出相关内容）
- **风险评估开关**、**领导简报默认模板**、**完整明细模式**
- **清单完整性**、**语言风格**、**典型问题回答模板**
- **重要提醒**

**不包含**：工具选择、路由优先级、参数规则、何时停止。

### 3. 双轨绑定

`chain_gzt.py` `_build_orchestrator_runtime`：

```python
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

### 4. 回归比较（Shadow）

启用新 Prompt 后，用 `meteo_qa_cases.json`（第一批 Task 0）比较：
- 工具名称一致率
- 工具参数一致率
- 工具调用数量
- 是否继续第二轮 Planner
- 是否空调用/误调用

在一致率未达标前，保持默认关闭。

## 载体

| 文件 | 改动 |
|------|------|
| `chainlitexam/prompts.py` | 新增 `PLANNER_SYSTEM_PROMPT`、`METEO_ANSWER_SYSTEM_PROMPT`；保留 `WEATHER_ASSISTANT_PROMPT` 不变 |
| `chainlitexam/chain_gzt.py` | `_build_orchestrator_runtime` 增加 `ENABLE_NEW_PLANNER_PROMPT`/`ENABLE_NEW_ANSWER_PROMPT` 开关，双轨选择 prompt |

## 测试

- 默认关闭时，`planner_chain` 和 `answer_chain` 仍使用 `WEATHER_ASSISTANT_PROMPT`（行为完全不变）。
- 开启 `ENABLE_NEW_PLANNER_PROMPT=true` 时，planner 使用 `PLANNER_SYSTEM_PROMPT`（不包含格式规则）。
- 开启 `ENABLE_NEW_ANSWER_PROMPT=true` 时，answer 使用 `METEO_ANSWER_SYSTEM_PROMPT`（不包含工具选择规则）。
- 全量测试回归。

## 风险

- **纯提取不新增**：新 Prompt 从旧 Prompt 提取，不新增内容，降低引入新错误的风险。
- **默认关闭**：旧 Prompt 保留为默认，不改变生产行为。
- **回归比较后再启用**：工具一致率达标前不替代。
- **GPT 原则 5**：新旧逻辑并存，不直接替换。