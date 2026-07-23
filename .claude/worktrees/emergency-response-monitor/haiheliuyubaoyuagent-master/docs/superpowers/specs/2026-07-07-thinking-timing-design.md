# 问答智能体思考过程与工具耗时优化设计

## 背景

海河流域暴雨洪水预报智能体的 Chainlit 前端已能通过 `ReasoningStep` 展示 planner 路径的思考过程，后端也通过 `print` 输出工具耗时。但存在以下问题：

1. **思考过程不够业务化**：现有文字偏技术（如"调用 tool"、"补充查询第 N 轮"），fast paths 更是只有单行"正在查询…"提示，业务人员难以看懂系统当前在做什么。
2. **工具耗时散落在控制台**：`_invoke_tool_with_tolerance()` 直接 `print`，没有统一格式，也没有 session ID 和问题摘要，不利于后续接入日志采集器做慢查询分析。
3. **缺少端到端耗时记录**：每个问题从用户发问到最终回答输出的总时间没有记录，无法定位"慢"发生在工具层还是 LLM 层。
4. **缺少回答开头总结**：思考过程是可折叠 Step，业务人员若不想展开，无法快速知道系统做了什么。

## 目标

1. 让思考过程对业务人员可读：阶段化、业务化语言，覆盖 fast paths 和 planner 路径。
2. 在后端控制台以统一格式输出每个工具耗时 + 每个问题的端到端总耗时，便于后续优化。
3. 在最终回答开头加一句业务化总结，与思考 Step 互补。

## 方案概述

采用**最小侵入式改造**：复用现有 `ReasoningStep` 和 `_invoke_tool_with_tolerance`，补充阶段化展示、统一计时日志、fast path 思考过程、回答开头总结。不引入额外 LLM 调用，避免增加延迟。

## 详细设计

### 1. 计时日志模块

新增 `chainlitexam/timing_logger.py`：

```python
class TimingLogger:
    @staticmethod
    def log_tool(session_id: str, query_summary: str, tool_name: str,
                 elapsed: float, status: str = "ok"):
        print(f"[TOOL_TIMING] "
              f"session={session_id} "
              f"query=\"{query_summary}\" "
              f"tool={tool_name} "
              f"elapsed={elapsed:.2f}s "
              f"status={status}")

    @staticmethod
    def log_query(session_id: str, query_summary: str,
                  total_elapsed: float, status: str = "ok"):
        print(f"[QUERY_TIMING] "
              f"session={session_id} "
              f"query=\"{query_summary}\" "
              f"total_elapsed={total_elapsed:.2f}s "
              f"status={status}")
```

- `session_id`：取自 `cl.user_session.get("id")`，若不存在则生成 UUID。
- `query_summary`：用户问题截断到 40 字，保留关键地名/时间/气象要素。
- `tool_name`：使用工具原始名，便于后端统一分析。
- `status`：`ok` 或 `fail`。

埋点位置：
- `process_message()` 入口记录 `query_start_time`，所有出口（含异常）调用 `log_query()`。
- `_invoke_tool_with_tolerance()` 在成功和失败时均调用 `log_tool()`。

### 2. 思考过程阶段化

改造 `ReasoningStep` 类，支持业务化子阶段：

```python
class ReasoningStep:
    def __init__(self, name: str = "🤔 思考过程"):
        self.name = name
        self.step: cl.Step | None = None
        self._buffer: str = ""
        self._closed: bool = False
        self._current_stage: cl.Step | None = None

    async def __aenter__(self):
        self.step = cl.Step(name=self.name, type="llm")
        self.step.show_input = "markdown"
        self.step.input = ""
        self.step.output = ""
        await self.step.send()
        return self

    async def stage(self, title: str, detail: str = ""):
        """开启一个业务化子阶段，前一个阶段自动关闭。"""
        if self._current_stage is not None:
            await self._current_stage.update()
        self._current_stage = cl.Step(name=title, parent_id=self.step.id, type="tool")
        self._current_stage.show_input = "markdown"
        self._current_stage.input = ""
        self._current_stage.output = detail
        await self._current_stage.send()
        return self._current_stage

    async def line(self, text: str):
        if self._current_stage is not None:
            self._current_stage.output += ("\n" + text)
            await self._current_stage.update()
        else:
            self._buffer += text + "\n"
            if self.step is not None:
                self.step.output = self._buffer
                await self.step.update()

    async def close(self):
        if self._current_stage is not None:
            await self._current_stage.update()
            self._current_stage = None
        if self.step is not None and not self._closed:
            self._closed = True
            self.step.output = self._buffer or "思考完成"
            await self.step.update()
```

planner 路径的阶段映射：

| 原文字示例 | 归入 stage | 业务化表达示例 |
|---|---|---|
| 正在分析问题… | 🔍 理解问题 | 正在理解您的问题，识别需要关注的时间、区域和气象要素… |
| 需要查询以下数据… | 📡 查询数据 | 需要查询以下数据：城市面雨量、九分区降雨… |
| 补充查询第 N 轮… | 📡 查询数据 | 补充查询更多数据：河网水位、应急响应… |
| 正在评估已获取的数据… | ✅ 评估结果 | 已获取数据，正在判断能否完整回答您的问题… |
| 正在为您生成分析结论… | ✍️ 生成结论 | 正在整理分析结论并用自然语言呈现… |

### 3. fast paths 补思考过程

新增 helper：

```python
async def _show_business_reasoning(intent_text: str, data_sources: list[str],
                                   conclusion_hint: str) -> ReasoningStep:
    reasoning = ReasoningStep("🤔 思考过程")
    await reasoning.__aenter__()
    await reasoning.stage("🔍 理解问题",
                          f"识别到您的问题意图：{intent_text}")
    await reasoning.stage("📡 查询数据",
                          "将查询以下数据：" + "、".join(data_sources))
    await reasoning.stage("✍️ 生成结论",
                          conclusion_hint)
    return reasoning
```

在每条 fast path 中：
1. 开始时调用 `_show_business_reasoning()` 创建并发送思考 Step。
2. 保留原 `thinking_msg` 作为顶部"请稍候"提示。
3. 成功或失败出口处调用 `await reasoning.close()`。

示例（`_try_rainfall_img_fast_path`）：

```python
reasoning = await _show_business_reasoning(
    intent_text="生成海河流域降水实况分布图",
    data_sources=["实况降雨站点数据"],
    conclusion_hint="将生成降雨分布图并简要说明时间范围和分区"
)
```

### 4. 回答开头一句总结

新增 helper：

```python
def _build_thinking_summary(query: str, stages: list[str],
                            tool_display_names: list[str] | None = None,
                            has_chart: bool = False) -> str:
    """根据已走过的阶段和工具，生成一句业务化前缀。"""
    ...
```

生成规则：
- planner 路径且有工具调用："已结合降雨实况、河网水位等数据完成分析，为您整理结论如下："
- 降雨图 fast path："已生成海河流域降水实况分布图，说明如下："
- 直接回答（无工具）："已理解您的问题，直接为您解答："

实现位置：
- `process_message()` 最终发送回答前，统一拼接到 `text` 最前面。
- fast paths 在 `_emit_fast_path_result()` 之前拼接。

### 5. 数据流

```
用户提问
  ├── fast path
  │     ├── _show_business_reasoning() → 发送"🤔 思考过程" Step
  │     ├── 调用工具（_invoke_tool_with_tolerance 记录 TOOL_TIMING）
  │     ├── _build_thinking_summary() → 生成开头总结
  │     └── 发送最终回答
  └── planner 路径
        ├── ReasoningStep.stage("🔍 理解问题")
        ├── astream_planner_think() → 流式 token 进入当前 stage
        ├── 识别到工具 → stage("📡 查询数据")
        ├── _run_tool_round() → 每个工具记录 TOOL_TIMING
        ├── stage("✍️ 生成结论")
        ├── _build_thinking_summary()
        └── 发送最终回答
```

### 6. 错误处理

- `_invoke_tool_with_tolerance()` 失败时记录 `TOOL_TIMING status=fail`。
- `process_message()` 任何异常出口都记录 `QUERY_TIMING status=fail`。
- `ReasoningStep.close()` 在异常时通过 `__aexit__` 兜底关闭，避免 Step 挂起。
- 计时日志失败不阻塞主流程：所有 `TimingLogger` 调用包在 `try/except` 中。

### 7. 测试计划

本地启动 Chainlit 后验证：

1. fast path 问题（如"降雨分布图"）：
   - 前端出现"🤔 思考过程"且可展开；
   - Step 内有"理解问题 / 查询数据 / 生成结论"三个阶段；
   - 最终回答开头有一句总结；
   - 后端控制台出现 `[TOOL_TIMING]` 和 `[QUERY_TIMING]`。

2. planner 问题（如"未来三天海河流域降雨如何"）：
   - 思考 Step 出现 4 个阶段；
   - 每调用一个工具都打印 `[TOOL_TIMING]`；
   - 最终输出 `[QUERY_TIMING]`。

3. 失败/超时场景：
   - 工具失败时 `status=fail` 能打印；
   - 整体问答流程不中断，前端有友好提示。

## 影响范围

- 主要修改文件：
  - `chainlitexam/message_orchestrator.py`（`ReasoningStep`、fast paths、`process_message()`、`_invoke_tool_with_tolerance()`）
  - 新增 `chainlitexam/timing_logger.py`
- 不修改 LLM prompt、不修改工具实现、不引入新依赖。
- 对业务回答内容无影响，仅增加展示层和日志层。

## 后续可选增强

如果模板化总结上线后业务人员反馈不够自然，可升级到 LLM 辅助生成思考总结（方案 B），届时新增一个轻量 `thinking_summary_chain` 即可，本方案的结构可复用。