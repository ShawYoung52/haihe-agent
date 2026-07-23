# DeepSeek 式深度思考过程展示设计

## 背景

在 2026-07-07 的实现中，问答智能体已经具备：
- 业务化阶段展示（🔍 理解问题 / 📡 查询数据 / ✅ 评估结果 / ✍️ 生成结论）
- 工具耗时日志 `[TOOL_TIMING]` 和问题耗时日志 `[QUERY_TIMING]`
- 回答开头的业务化总结句

但用户反馈当前思考过程与期望仍有差距：希望像 DeepSeek 一样展现出**模型自然语言生成的深度思考过程**，而不是只有阶段标题或工具调用计划。

## 目标

1. 在最终答案输出前，由模型生成一段自然语言深度思考，实时展示给前端业务人员。
2. 思考过程初始状态**展开**，答案开始生成时思考过程**折叠**，业务人员焦点自然转移到答案。
3. 覆盖 planner 路径和所有 fast paths。
4. 保持原有计时日志功能不变。

## 方案概述

新增一个轻量的 **thinking chain / prompt**，在每次调用工具或生成答案前，先让模型根据用户问题和意图输出一段自然语言思考。`ReasoningStep` 负责：
- 初始创建时展开（`collapsed=False`）
- 实时流式追加思考内容
- 思考结束时折叠（`collapsed=True`）
- 随后输出最终答案

## 详细设计

### 1. Thinking Prompt

新增 prompt 常量，用于生成自然语言深度思考：

```python
THINKING_PROMPT = """你是海河流域气象问答智能体的"思考助手"。请根据用户问题，用第一人称、自然语言、业务化口吻，输出一段简短的分析思考过程（100~300 字）。

思考内容应包括：
1. 你理解用户想问什么；
2. 你需要查询哪些数据才能回答；
3. 你准备如何组织最终答案。

不要输出工具名、参数、接口名等技术细节。不要输出最终答案。只输出思考过程。

可用数据类型：实况降雨、预报降雨、河网水位、气象预警、行政区划底图、防汛应急响应、流域面雨量、城市平均降雨、滚动天气预报等。

当前时间：{current_time}
用户问题：{user_query}
"""
```

对于 fast paths，使用简化版 prompt：

```python
FAST_PATH_THINKING_PROMPT = """你是海河流域气象问答智能体的"思考助手"。用户问题已经识别为以下意图：{intent}。
请用第一人称、自然语言、业务化口吻，输出一段简短思考（50~150 字），说明：
1. 你理解用户想问什么；
2. 你将查询哪些数据；
3. 你将如何给出结论。

不要输出工具名、参数、接口名等技术细节。

当前时间：{current_time}
用户问题：{user_query}
意图：{intent}
数据来源：{data_sources}
"""
```

### 2. 新增 `ThinkingGenerator`

新增一个轻量模块或函数，负责调用 thinking chain：

```python
async def generate_thinking_text(user_text: str, intent: str = "", data_sources: list[str] | None = None) -> str:
    """调用轻量模型生成自然语言思考。"""
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    if intent:
        prompt = FAST_PATH_THINKING_PROMPT.format(
            current_time=current_time,
            user_query=user_text,
            intent=intent,
            data_sources="、".join(data_sources or []),
        )
    else:
        prompt = THINKING_PROMPT.format(
            current_time=current_time,
            user_query=user_text,
        )
    # 复用 callbacks["astream_thinking_to_reasoning"]
    ...
```

实际实现中，为了复用现有 LLM 调用基础设施，可以在 `chain_gzt.py` 中新增 `astream_thinking_to_reasoning(thinking_chain, input_dict, reasoning_step)`，类似现有的 `_process_planner_stream`。

### 3. 改造 `ReasoningStep`

在 `ReasoningStep` 中支持控制折叠状态：

```python
class ReasoningStep:
    ...

    async def __aenter__(self):
        self.step = cl.Step(name=self.name, type="llm")
        self.step.show_input = "markdown"
        self.step.input = ""
        self.step.output = ""
        self.step.collapsed = False  # 初始展开
        await self.step.send()
        return self

    async def close(self):
        if self._current_stage is not None:
            await self._current_stage.update()
            self._current_stage = None
        if self.step is not None and not self._closed:
            self._closed = True
            self.step.output = self._buffer or "思考完成"
            self.step.collapsed = True  # 思考结束时折叠
            await self.step.update()
```

同时保留 `stage()` 方法用于业务化阶段，但思考内容主要由模型生成。

### 4. Planner 路径流程

```
用户提问
  → 创建 ReasoningStep（展开）
  → 调用 thinking chain 生成并流式展示深度思考
  → 思考完成，ReasoningStep 折叠
  → 调用 planner_chain 决定工具
  → 执行工具（记录 TOOL_TIMING）
  → 生成答案并发送
```

在 `process_message()` 中：

```python
reasoning = ReasoningStep("🤔 思考过程")
await reasoning.__aenter__()

# 生成并展示深度思考
await callbacks["astream_thinking_to_reasoning"](
    thinking_chain, {"messages": messages}, reasoning
)

# 思考结束，折叠
await reasoning.stage("🔍 理解问题", "正在规划数据查询方案...")
```

### 5. Fast paths 流程

每个 fast path 在创建 `reasoning` 后：

```python
reasoning = await _show_business_reasoning(...)

# 生成并展示深度思考
thinking_text = await generate_thinking_text(
    user_text,
    intent="生成海河流域降水实况分布图",
    data_sources=["实况降雨站点数据"],
)
await reasoning.line(thinking_text)

# 思考结束，折叠
await reasoning.close()

# 调用工具 / 生成答案
...
```

### 6. 最终答案展示

最终答案作为独立消息发送，不再嵌套在 ReasoningStep 内。业务人员看到：
1. 一条可折叠的"🤔 思考过程" Step（已折叠）
2. 一条最终回答消息

### 7. 性能与降级

- thinking chain 应使用轻量模型或较低温度，避免增加过多延迟。
- thinking 生成失败时不阻塞主流程：直接继续工具调用/答案生成，思考 Step 显示"思考完成"。
- thinking 调用也应记录耗时，但不作为 `[TOOL_TIMING]`（避免混淆），可单独 `[THINK_TIMING]` 输出到控制台用于分析。

### 8. 错误处理

- thinking chain 超时/异常：`try/except` 捕获，调用 `await reasoning.line("思考生成遇到异常，继续为您查询数据...")`。
- `ReasoningStep.close()` 失败不影响答案输出。

## 影响范围

- 主要修改文件：
  - `chainlitexam/message_orchestrator.py`（ReasoningStep 改造、fast paths、process_message）
  - `chainlitexam/chain_gzt.py`（新增 thinking chain callback）
  - `chainlitexam/prompts.py`（新增 THINKING_PROMPT / FAST_PATH_THINKING_PROMPT）
- 新增依赖：无。
- 原有计时日志功能不受影响。

## 测试计划

1. Planner 路径：输入"未来三天海河流域降雨如何"，验证：
   - 思考 Step 初始展开，显示自然语言思考；
   - 思考结束后 Step 折叠；
   - 最终答案单独显示。
2. Fast path：输入"海河流域降雨分布图"，验证：
   - 思考 Step 展开并显示自然语言思考；
   - 思考结束后折叠；
   - 最终答案（含图片）单独显示。
3. Thinking 失败场景：模拟 thinking chain 超时，验证主流程不中断。

## 后续可选增强

- 对常用问题缓存思考文本，减少重复调用。
- 让 thinking chain 同时输出阶段标签，实现思考内容的自动分段。
