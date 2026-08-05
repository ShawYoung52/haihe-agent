# 问答智能体排版修复 + 决策天气速度优化 设计

**日期**：2026-08-05
**状态**：设计（待用户审阅）
**范围**：`chainlitexam` 问答智能体（Chainlit 网页端）

## 背景与用户反馈

甲方反馈两个问题：
1. **排版**：网页端"第 1 轮数据查询/查询决策天气点位"等工具 step 独立显示在回答下方，而非像思考过程一样在上方/可折叠。
2. **速度**：`query_decision_weather_for_poi`（如"梅江会展中心明日天气如何？"）单工具耗时 31 秒。

**关键约束**：内网离线 Linux 服务器是 **Chainlit 2.9.6**（非本地 2.11.0），不能重装。修复必须兼容 2.9.6。2.9.6 上思考过程**能手动折叠但不自动折叠**（`auto_collapse` 是 2.10+ 才有的参数）。

## 现状与根因

### 1. 排版问题

`_run_tool_round`（`message_orchestrator.py:1850`）创建的两个 step 未挂到思考过程下：
- 外层"第 N 轮数据查询" step（1859 行）：`cl.Step(name=..., type="tool")`，无 `parent_id` → root 级。
- 工具 step（1821/1883 行）：`parent_id=step.id`（挂在"第 N 轮"下）。

而 `ReasoningStep`（思考过程）挂到 `current_run.id`（root 级）。因此工具 step 与思考过程、回答消息并列显示，工具 step 出现在回答下方。

**用户诉求**：工具 step 要么不放，要么和思考过程一样放上面。

### 2. 速度问题（31 秒）

`query_decision_weather_for_poi`（`tools/decision_weather.py:32`）内部**串行执行 2 次 LLM 调用**：
1. `_extract_decision_weather_slots`（`decision_weather_core.py:312`）：LLM 抽位置名 + 问题类型。
2. `_generate_decision_weather_answer`（`decision_weather_core.py:437`）：LLM 生成结论。

加外层 Planner LLM = **3 次 LLM 串行**，每次 5-10s → 30s+。

`_decision_weather_prefilter`（`decision_weather_core.py:126`）已是纯规则（关键词 + `institution_suffixes` 后缀判断）。位置名和问题类型可以**规则抽取**。

## 设计

### 改动 1：工具 step 挂到思考过程下（排版）

`_run_tool_round` 增加可选参数 `parent_step_id: str | None = None`。`process_message` 调用时传 `reasoning.step.id`。`_run_tool_round` 的"第 N 轮数据查询" step 用 `parent_id=parent_step_id`（若提供）。

效果：工具 step（含"第 N 轮数据查询"和所有工具）成为思考过程的子节点，前端渲染在思考容器内，不再独立出现在回答下方。

**安全性**：只改 step 父子挂接，不改回答逻辑、工具调用、消息顺序。`parent_step_id=None` 默认值保证其他调用不受影响。

### 改动 2：决策天气槽位规则抽取 + LLM 兜底（速度）

新增 `_extract_decision_slots_rule_based(user_text) -> dict | None`（纯规则）：
- **位置名**：匹配 `institution_suffixes`（中心/大学/医院/学校/公园/酒店/大厦/机场/车站/景区/园区/小区等）的前缀名词短语。如"梅江会展中心"→"梅江会展中心"、"天津大学"→"天津大学"。
- **问题类型**：关键词规则——
  - 下雨/有雨/降雨/降水 → `rain_now` 或 `rain_next_hours`
  - 气温/温度/热/冷 → `temperature`
  - 风 → `wind`
  - 能见度/雾/霾 → `visibility`
  - 适合/活动/户外 → `activity`
  - 未来N小时/接下来N小时 → `rain_next_hours`
  - 其余 → `general_weather`
- 返回 `{"is_decision_weather": True, "location_name": ..., "question_type": ..., "need_clarification": False}`。

`_extract_decision_weather_slots` 改为**规则优先，规则失败回退 LLM**：
```python
rule_slots = _extract_decision_slots_rule_based(user_text)
if rule_slots:
    return rule_slots
# 回退：现有 LLM 抽取（保底，不破坏回答）
```

**安全性**：规则抽不出（如无明确后缀）→ 回退 LLM，回答不受影响。规则抽出的位置名交给 `search_poi` 检索，与 LLM 抽取路径相同。问题类型只作为业务事实传给 LLM 生成结论。

## 载体

| 文件 | 改动 |
|------|------|
| `chainlitexam/message_orchestrator.py` | `_run_tool_round` 加 `parent_step_id` 参数；`process_message` 传 `reasoning.step.id` |
| `chainlitexam/tools/decision_weather_core.py` | 新增 `_extract_decision_slots_rule_based`；`_extract_decision_weather_slots` 规则优先 + LLM 兜底 |

## 测试

- `_run_tool_round` 挂父：验证工具 step 的 `parent_id` 为传入值；默认 None 时不改行为。
- 规则抽取：验证"梅江会展中心/天津大学/未来N小时下雨/适合户外"等用例位置名与问题类型正确；无后缀的模糊问题（如"学校明天天气"）回退 LLM。
- 现有 `tests/test_decision_weather_tool.py`、`tests/test_reasoning_step.py` 全量回归。

## 风险

- **改动 2**：规则抽取若误判位置名，POI 检索可能失败 → 工具返回"未检索到可用经纬度"，与 LLM 抽取失败路径一致（不崩溃、可换问法）。LLM 兜底保证不改变原有行为。
- **改动 1**：仅 UI 层级调整，不涉及回答正确性。
- **内网 2.9.6 兼容**：不引入 `auto_collapse`，不依赖 2.10+ 特性。思考过程保持"能手动折叠"，工具 step 挂父后自然折叠在思考容器内。