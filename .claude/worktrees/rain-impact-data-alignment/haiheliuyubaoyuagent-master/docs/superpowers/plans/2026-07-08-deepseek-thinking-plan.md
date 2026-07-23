# DeepSeek 式深度思考过程展示 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把问答智能体的思考过程从阶段标题升级为 DeepSeek 式的自然语言深度思考，思考时展开、答案输出时折叠。

**Architecture:** 新增 thinking prompt 和轻量 thinking chain；改造 `ReasoningStep` 支持 `collapsed` 状态控制；在 planner 路径和 fast paths 中先调用 thinking chain 展示深度思考，再执行工具和答案生成。

**Tech Stack:** Python 3.10+, Chainlit, LangChain, Qwen3.6-27B via local proxy

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `chainlitexam/prompts.py` | 新增 `THINKING_PROMPT`、`FAST_PATH_THINKING_PROMPT`。 |
| `chainlitexam/chain_gzt.py` | 新增 `astream_thinking_to_reasoning()` callback，复用现有 LLM 调用基础设施。 |
| `chainlitexam/message_orchestrator.py` | 改造 `ReasoningStep` 支持折叠控制；在 fast paths / planner 路径中调用 thinking chain；最终答案独立发送。 |

---

## Task 1: 新增 Thinking Prompts

**Files:**
- Modify: `chainlitexam/prompts.py`

### Step 1.1: 在 `prompts.py` 末尾添加 prompts

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

### Step 1.2: Commit

```bash
git add chainlitexam/prompts.py
git commit -m "feat: add thinking prompts for DeepSeek-style reasoning"
```

---

## Task 2: 新增 `astream_thinking_to_reasoning` Callback

**Files:**
- Modify: `chainlitexam/chain_gzt.py`

### Step 2.1: 导入 thinking prompt

在 `chain_gzt.py` 顶部 import 区添加：

```python
try:
    from prompts import THINKING_PROMPT, FAST_PATH_THINKING_PROMPT
except Exception:
    THINKING_PROMPT = ""
    FAST_PATH_THINKING_PROMPT = ""
```

### Step 2.2: 新增 `astream_thinking_to_reasoning`

在 `_process_planner_stream` 附近添加：

```python
async def _process_thinking_stream(chain, input_dict, reasoning_step, config):
    """流式调用 thinking chain，将模型自然语言思考实时追加到 ReasoningStep。"""
    content_buf = ""
    async for chunk in chain.astream(input_dict, config=config):
        token = getattr(chunk, "content", None)
        if token:
            content_buf += token
            await reasoning_step.append(token)
    return content_buf.strip()


async def astream_thinking_to_reasoning(thinking_chain, input_dict, reasoning_step, config: RunnableConfig | None = None):
    """生成并流式展示深度思考，带 30 秒超时。"""
    try:
        return await asyncio.wait_for(
            _process_thinking_stream(thinking_chain, input_dict, reasoning_step, config),
            timeout=30,
        )
    except (asyncio.TimeoutError, TimeoutError):
        await reasoning_step.line("\n\n（思考生成超时，继续为您查询数据...）")
        return ""
    except Exception as e:
        await reasoning_step.line(f"\n\n（思考生成遇到异常：{str(e)[:100]}，继续为您查询数据...）")
        return ""
```

### Step 2.3: 注册 callback

在 `callbacks` 字典注册处（约 `chain_gzt.py:3564` 附近）添加：

```python
"astream_thinking_to_reasoning": astream_thinking_to_reasoning,
```

### Step 2.4: Commit

```bash
git add chainlitexam/chain_gzt.py
git commit -m "feat: add thinking stream callback for ReasoningStep"
```

---

## Task 3: 改造 `ReasoningStep` 支持折叠状态

**Files:**
- Modify: `chainlitexam/message_orchestrator.py`

### Step 3.1: 修改 `ReasoningStep.__aenter__` 和 `close`

```python
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

### Step 3.2: Commit

```bash
git add chainlitexam/message_orchestrator.py
git commit -m "feat: ReasoningStep expands while thinking and collapses on close"
```

---

## Task 4: Planner 路径接入深度思考

**Files:**
- Modify: `chainlitexam/message_orchestrator.py`

### Step 4.1: 创建 thinking chain 并传入 callbacks

在 `chain_gzt.py` 中，在构建 `callbacks` 字典前，创建一个轻量的 `thinking_chain`：

```python
thinking_chain = (
    ChatPromptTemplate.from_messages([
        ("system", THINKING_PROMPT),
        MessagesPlaceholder(variable_name="messages"),
    ])
    | answer_llm  # 复用 answer_llm，温度可微调
)
```

注意：由于 `THINKING_PROMPT` 包含 `{current_time}` 和 `{user_query}`，而 `from_messages` 的 prompt 可能无法直接替换这些字段。更简单的方式是在调用时手动 format prompt，然后传入 messages。

实际实现：在 `process_message()` 中构造 input：

```python
thinking_input = {
    "messages": [
        SystemMessage(content=THINKING_PROMPT.format(
            current_time=datetime.now().strftime("%Y-%m-%d %H:%M"),
            user_query=message.content,
        ))
    ]
}
```

### Step 4.2: 修改 `process_message()`

在创建 `ReasoningStep` 后、调用 planner 前，插入 thinking 生成：

```python
    reasoning = ReasoningStep("🤔 思考过程")
    await reasoning.__aenter__()

    # 生成并展示深度思考
    try:
        await callbacks["astream_thinking_to_reasoning"](
            thinking_chain,
            {
                "messages": [
                    SystemMessage(content=THINKING_PROMPT.format(
                        current_time=datetime.now().strftime("%Y-%m-%d %H:%M"),
                        user_query=message.content,
                    ))
                ]
            },
            reasoning,
        )
    except Exception:
        pass

    await reasoning.stage("🔍 理解问题", "正在规划数据查询方案...")
```

### Step 4.3: 在 `process_message()` 参数中传入 `thinking_chain`

修改 `process_message(message: cl.Message, planner_chain, answer_chain, tools, messages, callbacks)` 为：

```python
async def process_message(message: cl.Message, planner_chain, answer_chain, thinking_chain, tools, messages, callbacks):
```

并同步修改 `chain_gzt.py` 中调用 `process_message` 的地方。

### Step 4.4: Commit

```bash
git add chainlitexam/message_orchestrator.py chainlitexam/chain_gzt.py
git commit -m "feat: integrate deep thinking into planner path"
```

---

## Task 5: Fast Paths 接入深度思考

**Files:**
- Modify: `chainlitexam/message_orchestrator.py`

### Step 5.1: 新增 `generate_fast_path_thinking` helper

在 `_show_business_reasoning` 附近添加：

```python
async def generate_fast_path_thinking(
    thinking_chain,
    user_text: str,
    intent_text: str,
    data_sources: list[str],
) -> str:
    """为 fast path 生成一段自然语言深度思考。"""
    prompt = FAST_PATH_THINKING_PROMPT.format(
        current_time=datetime.now().strftime("%Y-%m-%d %H:%M"),
        user_query=user_text,
        intent=intent_text,
        data_sources="、".join(data_sources),
    )
    try:
        result = await thinking_chain.ainvoke({
            "messages": [SystemMessage(content=prompt)]
        })
        return getattr(result, "content", "") or ""
    except Exception:
        return ""
```

### Step 5.2: 修改所有 fast paths

在每个 fast path 创建 `reasoning` 后、调用工具前，生成并追加思考：

```python
    reasoning = await _show_business_reasoning(
        intent_text="生成海河流域降水实况分布图",
        data_sources=["实况降雨站点数据"],
        conclusion_hint="将生成降雨分布图并简要说明时间范围和分区"
    )

    thinking_text = await generate_fast_path_thinking(
        thinking_chain, user_text, "生成海河流域降水实况分布图", ["实况降雨站点数据"]
    )
    if thinking_text:
        await reasoning.line(thinking_text)
```

需要修改的 fast paths 列表：
- `_try_affected_river_network_by_rainfall_fast_path`
- `_try_rainfall_img_fast_path`
- `_try_warning_fact_fast_path`
- `_try_rainfall_analysis_fast_path`
- `_try_city_avg_rainfall_fast_path`
- `_try_today_rain_duration_fast_path`
- `_try_today_rainfall_fast_path`
- `_try_weekly_forecast_fast_path`
- `_try_heavy_rain_check_fast_path`
- `_try_subbasin_forecast_fast_path`
- `_try_basin_areal_rainfall_fast_path`
- `_try_weekend_activity_fast_path`
- `_try_basin_weather_fast_path`
- `_try_water_level_fast_path`
- `_try_general_weather_fast_path`
- `_try_decision_weather_fast_path`
- `_try_emergency_response_fast_path`
- `_try_river_plot_fast_path`

### Step 5.3: 把 `thinking_chain` 传入 fast path 函数

修改所有 fast path 函数签名，增加 `thinking_chain` 参数：

```python
async def _try_rainfall_img_fast_path(user_text: str, thinking_chain, tools, messages, callbacks) -> bool:
```

并同步修改 `process_message()` 中调用这些函数的地方，传入 `thinking_chain`。

### Step 5.4: Commit

```bash
git add chainlitexam/message_orchestrator.py
git commit -m "feat: add deep thinking to all fast paths"
```

---

## Task 6: 最终答案独立发送

**Files:**
- Modify: `chainlitexam/message_orchestrator.py`

### Step 6.1: 确保 `ReasoningStep.close()` 在答案生成前调用

在 `process_message()` 中，所有发送最终回答前，先调用 `await reasoning.close()`。

当前代码已经大部分如此，但需要检查：
- `forced_final_text` 分支
- `warning_bundles` 分支
- 循环内/外 answer_chain 分支
- fast paths 分支

确保 `close()` 在发送最终消息之前。

### Step 6.2: 最终答案作为独立消息

`process_message()` 中的最终答案已经通过 `stream_msg` 或 `cl.Message` 独立发送。fast paths 通过 `_emit_fast_path_result` 发送。这些保持不变。

### Step 6.3: Commit

```bash
git add chainlitexam/message_orchestrator.py
git commit -m "fix: ensure reasoning collapses before final answer is sent"
```

---

## Task 7: 新增/更新测试

**Files:**
- Modify: `chainlitexam/tests/test_reasoning_step.py`
- Modify: `chainlitexam/tests/test_fast_paths.py`
- Create: `chainlitexam/tests/test_thinking.py`（可选）

### Step 7.1: 更新 `test_reasoning_step.py`

添加测试：
- `ReasoningStep` 初始创建时 `collapsed=False`。
- `close()` 后 `collapsed=True`。

### Step 7.2: 更新 `test_fast_paths.py`

AST 检查增加：
- 每个 fast path 是否调用了 `generate_fast_path_thinking` 或把 `thinking_chain` 传入。

### Step 7.3: Commit

```bash
git add chainlitexam/tests/
git commit -m "test: update tests for deep thinking behavior"
```

---

## Task 8: 集成验证

**Files:** 无代码修改

### Step 8.1: 运行自动测试

```bash
cd chainlitexam
python tests/test_timing_logger.py
python tests/test_thinking_summary.py
python tests/test_reasoning_step.py
python tests/test_fast_paths.py
```

Expected: All tests passed.

### Step 8.2: 人工验证

启动服务：
```bash
cd haihe-weather-analyzer-mcp && python server.py
cd chainlitexam && chainlit run chain_gzt.py
```

测试用例：
1. Fast path：输入"海河流域降雨分布图"
   - 思考 Step 初始展开，显示自然语言思考
   - 思考结束后 Step 折叠
   - 最终答案（含图片）单独显示
2. Planner path：输入"未来三天海河流域降雨如何"
   - 思考 Step 初始展开，显示自然语言思考
   - 思考结束后折叠
   - 最终答案单独显示
3. Thinking 失败：模拟超时，验证主流程不中断

### Step 8.3: Commit

```bash
git commit --allow-empty -m "test: manual integration verification complete"
```

---

## 自我审查

- Spec coverage：每个需求都有对应 Task。
- Placeholder scan：无 TBD/TODO。
- Type consistency：`thinking_chain` 参数类型在 Task 4 和 Task 5 中一致。

---

## 执行交接

**Plan complete and saved to `docs/superpowers/plans/2026-07-08-deepseek-thinking-plan.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
