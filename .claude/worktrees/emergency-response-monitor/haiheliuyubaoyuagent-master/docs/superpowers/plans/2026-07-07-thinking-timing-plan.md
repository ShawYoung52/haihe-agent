# 问答智能体思考过程与工具耗时优化 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Chainlit 问答智能体的思考过程对业务人员可读，并在后端控制台以统一格式输出每个工具耗时与每个问题的端到端总耗时。

**Architecture:** 新增独立的 `timing_logger.py` 负责统一格式输出；复用并扩展现有的 `ReasoningStep` 类以支持业务化子阶段；在 `process_message()` 与所有 fast paths 中按统一阶段展示思考过程；通过 `_build_thinking_summary()` 在最终回答前加一句业务化总结。

**Tech Stack:** Python 3.10+, Chainlit, LangChain, asyncio

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `chainlitexam/timing_logger.py` | 新增。统一输出 `[TOOL_TIMING]` 和 `[QUERY_TIMING]` 到控制台。 |
| `chainlitexam/message_orchestrator.py` | 修改。扩展 `ReasoningStep`、埋点计时、改造 planner 思考过程、为 fast paths 补思考过程、生成回答开头总结。 |

---

## Task 1: 计时日志模块 `timing_logger.py`

**Files:**
- Create: `chainlitexam/timing_logger.py`
- Test: `chainlitexam/tests/test_timing_logger.py`（如 tests 目录不存在则创建）

### Step 1.1: 创建 `timing_logger.py`

```python
# chainlitexam/timing_logger.py
import re


class TimingLogger:
    """统一输出问答耗时日志，便于后续接入日志采集器做慢查询分析。"""

    @staticmethod
    def _safe_summary(text: str | None, max_len: int = 40) -> str:
        if not text:
            return ""
        summary = re.sub(r"\s+", " ", str(text)).strip()
        if len(summary) > max_len:
            summary = summary[:max_len] + "..."
        return summary

    @staticmethod
    def log_tool(session_id: str, query_summary: str, tool_name: str,
                 elapsed: float, status: str = "ok") -> None:
        """记录单个工具调用耗时。"""
        if not session_id:
            session_id = "unknown"
        summary = TimingLogger._safe_summary(query_summary)
        print(f"[TOOL_TIMING] "
              f"session={session_id} "
              f"query=\"{summary}\" "
              f"tool={tool_name} "
              f"elapsed={elapsed:.2f}s "
              f"status={status}")

    @staticmethod
    def log_query(session_id: str, query_summary: str,
                  total_elapsed: float, status: str = "ok") -> None:
        """记录整个问题的端到端耗时。"""
        if not session_id:
            session_id = "unknown"
        summary = TimingLogger._safe_summary(query_summary)
        print(f"[QUERY_TIMING] "
              f"session={session_id} "
              f"query=\"{summary}\" "
              f"total_elapsed={total_elapsed:.2f}s "
              f"status={status}")
```

### Step 1.2: 创建测试

```python
# chainlitexam/tests/test_timing_logger.py
import sys
from io import StringIO

sys.path.insert(0, "..")

from timing_logger import TimingLogger


def test_log_tool_format():
    out = StringIO()
    old_stdout = sys.stdout
    sys.stdout = out
    try:
        TimingLogger.log_tool("sess-123", "未来三天天津降雨如何", "get_city_rainfall", 2.345)
    finally:
        sys.stdout = old_stdout
    line = out.getvalue().strip()
    assert line.startswith("[TOOL_TIMING]")
    assert "session=sess-123" in line
    assert "tool=get_city_rainfall" in line
    assert "elapsed=2.35s" in line
    assert "status=ok" in line


def test_log_query_format():
    out = StringIO()
    old_stdout = sys.stdout
    sys.stdout = out
    try:
        TimingLogger.log_query("sess-123", "未来三天天津降雨如何", 8.123)
    finally:
        sys.stdout = old_stdout
    line = out.getvalue().strip()
    assert line.startswith("[QUERY_TIMING]")
    assert "total_elapsed=8.12s" in line


def test_summary_truncation():
    assert TimingLogger._safe_summary("a" * 100, 40).endswith("...")
    assert len(TimingLogger._safe_summary("a" * 100, 40)) == 43
```

### Step 1.3: 运行测试

Run: `cd chainlitexam && python -m pytest tests/test_timing_logger.py -v`

Expected: 3 tests PASS

### Step 1.4: Commit

```bash
git add chainlitexam/timing_logger.py chainlitexam/tests/test_timing_logger.py
git commit -m "feat: add unified timing logger for tool and query elapsed time"
```

---

## Task 2: 扩展 `ReasoningStep` 支持业务化子阶段

**Files:**
- Modify: `chainlitexam/message_orchestrator.py:21-61`

### Step 2.1: 修改 `ReasoningStep` 类

把原 `ReasoningStep` 替换为：

```python
class ReasoningStep:
    """
    DeepSeek 式实时思考步骤：在 Chainlit 界面展示可展开的推理过程。
    通过 append/update 实时刷新，让业务人员看到系统每一步在做什么。
    支持业务化子阶段（stage），便于按"理解问题-查询数据-评估结果-生成结论"组织。
    """

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

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def stage(self, title: str, detail: str = ""):
        """开启一个业务化子阶段，前一个阶段会自动保留。"""
        if self.step is None:
            return None
        if self._current_stage is not None:
            await self._current_stage.update()
        self._current_stage = cl.Step(name=title, parent_id=self.step.id, type="tool")
        self._current_stage.show_input = "markdown"
        self._current_stage.input = ""
        self._current_stage.output = detail or ""
        await self._current_stage.send()
        return self._current_stage

    async def append(self, text: str):
        if not text:
            return
        if self._current_stage is not None:
            self._current_stage.output += text
            await self._current_stage.update()
        elif self.step is not None:
            self._buffer += text
            self.step.output = self._buffer
            await self.step.update()

    async def line(self, text: str):
        await self.append(text + "\n")

    async def close(self):
        if self._current_stage is not None:
            await self._current_stage.update()
            self._current_stage = None
        if self.step is not None and not self._closed:
            self._closed = True
            self.step.output = self._buffer or "思考完成"
            await self.step.update()
```

### Step 2.2: Commit

```bash
git add chainlitexam/message_orchestrator.py
git commit -m "feat: add stage-based reasoning step for business users"
```

---

## Task 3: 工具调用计时埋点

**Files:**
- Modify: `chainlitexam/message_orchestrator.py:1-18`（imports）
- Modify: `chainlitexam/message_orchestrator.py:1592-1619`（`_invoke_tool_with_tolerance`）

### Step 3.1: 导入 `TimingLogger`

在 `message_orchestrator.py` 顶部添加：

```python
try:
    from timing_logger import TimingLogger
except Exception:
    class TimingLogger:
        @staticmethod
        def log_tool(*args, **kwargs):
            pass

        @staticmethod
        def log_query(*args, **kwargs):
            pass
```

### Step 3.2: 修改 `_invoke_tool_with_tolerance`

原函数中 `print(f"[工具耗时] ...")` 的 4 处替换为 `TimingLogger.log_tool`，并保留原 `print` 作为兼容调试输出：

```python
async def _invoke_tool_with_tolerance(tool_name: str, tool, tool_args, step):
    session_id = cl.user_session.get("id") or ""
    query_summary = cl.user_session.get("last_query") or ""

    start_time = time.time()
    try:
        result = await tool.ainvoke(tool_args)
        elapsed = time.time() - start_time
        print(f"[工具耗时] {tool_name}: {elapsed:.2f}s")
        TimingLogger.log_tool(session_id, query_summary, tool_name, elapsed, status="ok")
        return result, elapsed
    except Exception as e:
        elapsed = time.time() - start_time
        print(f"[工具耗时] {tool_name}: {elapsed:.2f}s (失败)")
        TimingLogger.log_tool(session_id, query_summary, tool_name, elapsed, status="fail")
        err_text = str(e)
        if tool_name != "get_city_rainfall_time_range" or "hour%6==2" not in err_text:
            raise

        retry_args, old_hour, new_hour = _build_hour_tolerant_args(tool_args)
        if not retry_args:
            raise

        step.input += (
            f"⚠️ 检测到小时参数不合法：{old_hour}，"
            f"已自动纠偏为 {new_hour} 并重试。\n"
        )
        print(f"[容错重试] {tool_name}: hour {old_hour} -> {new_hour}")
        retry_start = time.time()
        result = await tool.ainvoke(retry_args)
        retry_elapsed = time.time() - retry_start
        print(f"[工具耗时] {tool_name}(重试): {retry_elapsed:.2f}s")
        TimingLogger.log_tool(session_id, query_summary, f"{tool_name}(retry)", retry_elapsed, status="ok")
        return result, retry_elapsed
```

### Step 3.3: Commit

```bash
git add chainlitexam/message_orchestrator.py
git commit -m "feat: instrument tool calls with unified timing logger"
```

---

## Task 4: 问题端到端计时

**Files:**
- Modify: `chainlitexam/message_orchestrator.py:4294`（`process_message` 入口）
- Modify: `chainlitexam/message_orchestrator.py:4642`（`process_message` 正常出口）
- Modify: `chainlitexam/message_orchestrator.py:4389,4451,4532,4607,4634`（异常/提前出口）
- Modify: `chainlitexam/message_orchestrator.py:4369`（保存 `last_query` 到 session）

### Step 4.1: 在 session 中保存当前问题摘要

在 `process_message` 中 `messages.append(HumanMessage(...))` 后添加：

```python
messages.append(HumanMessage(content=message.content))
cl.user_session.set("last_query", message.content)
```

### Step 4.2: 在入口记录开始时间

在 `process_message` 最开头添加：

```python
async def process_message(message: cl.Message, planner_chain, answer_chain, tools, messages, callbacks):
    query_start_time = time.time()
    session_id = cl.user_session.get("id") or ""
    query_summary = message.content
```

### Step 4.3: 封装一个安全的计时出口函数

在 `process_message` 函数体内（紧接变量定义后）添加：

```python
    def _log_query_exit(status: str = "ok"):
        try:
            total_elapsed = time.time() - query_start_time
            TimingLogger.log_query(session_id, query_summary, total_elapsed, status=status)
        except Exception:
            pass
```

### Step 4.4: 在所有出口调用 `_log_query_exit`

搜索 `process_message` 内所有 `return` 的位置，在 return 前调用：

- 正常出口：`await reasoning.close(); _log_query_exit(); return`
- planner 首轮失败：`await reasoning.__aexit__...; _log_query_exit("fail"); ...; return`
- answer chain 失败：`await cl.Message(...).send(); _log_query_exit("fail"); ...; return`
- 预警生成失败但最终有 fallback：`...; _log_query_exit(); return`
- LLM 调用失败 break 后：循环外兜底处 `_log_query_exit("fail")`
- 兜底回答失败：`await cl.Message(...).send(); _log_query_exit("fail"); return`
- 兜底回答成功：循环外 `_log_query_exit(); return`

需要保证每个 `return` 前都调用。可在函数末尾统一处理：由于 `process_message` 最后有 `await reasoning.close(); return`，可在此处记录正常 `QUERY_TIMING`；对于异常分支，在每个异常 return 前显式记录。

### Step 4.5: Commit

```bash
git add chainlitexam/message_orchestrator.py
git commit -m "feat: log per-query end-to-end elapsed time"
```

---

## Task 5: Planner 路径思考过程业务化

**Files:**
- Modify: `chainlitexam/message_orchestrator.py:4376-4642`

### Step 5.1: 用 `stage()` 替换现有 `line()` 组织

在 `process_message()` 的 planner 路径中：

原代码：
```python
    reasoning = ReasoningStep("🤔 思考过程")
    await reasoning.__aenter__()
    await reasoning.line("正在分析您的问题，识别需要查询的气象、水文数据...")
```

改为：
```python
    reasoning = ReasoningStep("🤔 思考过程")
    await reasoning.__aenter__()
    await reasoning.stage("🔍 理解问题", "正在分析您的问题，识别需要关注的时间、区域和气象要素...")
```

### Step 5.2: 识别到工具调用时切换到"查询数据"阶段

原代码：
```python
    if planner_msg.tool_calls:
        ...
        await reasoning.line(f"**需要查询以下数据：{tool_names_display}（共 {tool_count} 项）**")
```

改为：
```python
    if planner_msg.tool_calls:
        ...
        await reasoning.stage("📡 查询数据", f"需要查询以下数据：{tool_names_display}（共 {tool_count} 项）")
```

### Step 5.3: 补充查询阶段

原代码：
```python
        await reasoning.line(f"**补充查询第 {iteration} 轮：{tool_names_display}**")
```

改为：
```python
        await reasoning.stage("📡 查询数据", f"补充查询更多数据：{tool_names_display}")
```

### Step 5.4: 评估结果阶段

在 `_run_tool_round` 之后、下一轮 planner 调用之前：

```python
        await reasoning.stage("✅ 评估结果", "已获取数据，正在判断能否完整回答您的问题...")
```

原代码：
```python
        await reasoning.line("**正在评估已获取的数据是否足够回答您的问题...**")
```

改为保留该行作为 detail，但用 stage 包裹。

### Step 5.5: 生成结论阶段

所有"正在为您生成分析结论..."、"正在整理回答..."统一归入 `✍️ 生成结论` stage：

```python
await reasoning.stage("✍️ 生成结论", "正在整理分析结论并用自然语言呈现...")
```

### Step 5.6: Commit

```bash
git add chainlitexam/message_orchestrator.py
git commit -m "feat: phase-based business-readable reasoning for planner path"
```

---

## Task 6: Fast paths 补思考过程

**Files:**
- Modify: `chainlitexam/message_orchestrator.py:1631-1635` 附近（新增 helper）
- Modify: `chainlitexam/message_orchestrator.py:1766-1882` 等 fast path 函数

### Step 6.1: 新增 `_show_business_reasoning` helper

在 `_show_thinking()` 函数后添加：

```python
async def _show_business_reasoning(intent_text: str, data_sources: list[str],
                                   conclusion_hint: str) -> ReasoningStep:
    """为 fast path 创建一段业务化的思考过程，包含理解问题、查询数据、生成结论三个阶段。"""
    reasoning = ReasoningStep("🤔 思考过程")
    await reasoning.__aenter__()
    await reasoning.stage("🔍 理解问题", f"识别到您的问题意图：{intent_text}")
    await reasoning.stage("📡 查询数据", "将查询以下数据：" + "、".join(data_sources))
    await reasoning.stage("✍️ 生成结论", conclusion_hint)
    return reasoning
```

### Step 6.2: 修改 `_try_affected_river_network_by_rainfall_fast_path`

在 `thinking_msg = None` 之后、进入 try 前创建 reasoning：

```python
    reasoning = await _show_business_reasoning(
        intent_text="分析暴雨影响河系并绘制专题图",
        data_sources=["降雨实况数据", "河网水系数据"],
        conclusion_hint="将绘制暴雨影响河系专题图并给出文字分析"
    )
```

在该函数所有 return/except 出口处 `await reasoning.close()`。

### Step 6.3: 修改 `_try_rainfall_img_fast_path`

在 `thinking_msg = await _show_thinking(...)` 后添加：

```python
    reasoning = await _show_business_reasoning(
        intent_text="生成海河流域降水实况分布图",
        data_sources=["实况降雨站点数据"],
        conclusion_hint="将生成降雨分布图并简要说明时间范围和分区"
    )
```

在所有 return 前 `await reasoning.close()`。

### Step 6.4: 修改 `_try_warning_fact_fast_path`

```python
    reasoning = await _show_business_reasoning(
        intent_text="查询天津气象预警信息",
        data_sources=["生效预警", "历史预警", "今日预警", "国家预警"]（根据实际调用的接口动态选择）,
        conclusion_hint="将整理预警清单、核心结论与防范建议"
    )
```

注意：预警 fast path 会动态决定调用哪些接口，可在确定接口后更新 `reasoning._current_stage.output` 或重新调用 `stage()`。

### Step 6.5: 修改 `_try_rainfall_analysis_fast_path`

```python
    reasoning = await _show_business_reasoning(
        intent_text="分析指定时段降雨特征",
        data_sources=["实况降雨站点数据"],
        conclusion_hint="将统计降雨分布、极值、持续时间等特征"
    )
```

### Step 6.6: 修改 `_try_city_avg_rainfall_fast_path`

```python
    reasoning = await _show_business_reasoning(
        intent_text="查询城市平均降雨量",
        data_sources=["城市面雨量数据"],
        conclusion_hint="将给出各城市平均降雨量排名或对比"
    )
```

### Step 6.7: 修改 `_try_today_rain_duration_fast_path`

```python
    reasoning = await _show_business_reasoning(
        intent_text="统计今日降雨时长",
        data_sources=["实况降雨站点数据"],
        conclusion_hint="将统计今日各站累计降雨时长"
    )
```

### Step 6.8: 修改 `_try_today_rainfall_fast_path`

```python
    reasoning = await _show_business_reasoning(
        intent_text="查询今日降雨情况",
        data_sources=["实况降雨数据", "预报降雨数据"],
        conclusion_hint="将分时段说明今日已下和将下的降雨"
    )
```

### Step 6.9: 修改 `_try_weekly_forecast_fast_path`

```python
    reasoning = await _show_business_reasoning(
        intent_text="查询未来一周天气预报",
        data_sources=["ECMWF AIFS 预报数据"],
        conclusion_hint="将给出未来一周天气趋势与重点关注"
    )
```

### Step 6.10: 修改 `_try_heavy_rain_check_fast_path`

```python
    reasoning = await _show_business_reasoning(
        intent_text="检查未来是否会出现强降雨",
        data_sources=["预报降雨数据"],
        conclusion_hint="将给出强降雨出现时段、区域与强度判断"
    )
```

### Step 6.11: 修改 `_try_subbasin_forecast_fast_path`

```python
    reasoning = await _show_business_reasoning(
        intent_text="查询子流域未来天气预报",
        data_sources=["子流域预报数据"],
        conclusion_hint="将给出指定子流域未来几天天气预报"
    )
```

### Step 6.12: 修改 `_try_basin_areal_rainfall_fast_path`

```python
    reasoning = await _show_business_reasoning(
        intent_text="查询流域面雨量",
        data_sources=["面雨量数据"],
        conclusion_hint="将给出流域面雨量统计与对比"
    )
```

### Step 6.13: 修改 `_try_weekend_activity_fast_path`

```python
    reasoning = await _show_business_reasoning(
        intent_text="获取周末户外活动天气建议",
        data_sources=["周末天气预报数据"],
        conclusion_hint="将给出周末天气适合度与活动建议"
    )
```

### Step 6.14: 修改 `_try_basin_weather_fast_path`

```python
    reasoning = await _show_business_reasoning(
        intent_text="查询海河流域整体天气",
        data_sources=["流域天气预报数据"],
        conclusion_hint="将给出海河流域今明后天气概况"
    )
```

### Step 6.15: 修改 `_try_water_level_fast_path`

```python
    reasoning = await _show_business_reasoning(
        intent_text="查询河网水位",
        data_sources=["河网水位数据"],
        conclusion_hint="将给出关键站点水位信息"
    )
```

### Step 6.16: 修改 `_try_general_weather_fast_path`

```python
    reasoning = await _show_business_reasoning(
        intent_text="查询通用天气",
        data_sources=["天气预报数据"],
        conclusion_hint="将给出天气概况与变化趋势"
    )
```

### Step 6.17: 修改 `_try_decision_weather_fast_path`

```python
    reasoning = await _show_business_reasoning(
        intent_text="查询具体点位决策天气",
        data_sources=["点位天气预报数据"],
        conclusion_hint="将给出该点位的天气影响评估"
    )
```

### Step 6.18: 修改 `_try_emergency_response_fast_path`

```python
    reasoning = await _show_business_reasoning(
        intent_text="查询防汛应急响应信息",
        data_sources=["防汛应急响应数据"],
        conclusion_hint="将给出应急响应级别与相关信息"
    )
```

### Step 6.19: Commit

```bash
git add chainlitexam/message_orchestrator.py
git commit -m "feat: add business-readable reasoning to all fast paths"
```

---

## Task 7: 回答开头一句总结

**Files:**
- Modify: `chainlitexam/message_orchestrator.py`

### Step 7.1: 新增 `_build_thinking_summary`

在 `_show_business_reasoning` 附近添加：

```python
def _build_thinking_summary(query: str, has_chart: bool = False) -> str:
    """根据用户问题和是否有图表，生成一句业务化前缀，放在最终回答开头。"""
    if not query:
        return ""

    q = query.strip()
    lowered = q.lower()

    # 意图关键词匹配
    if any(k in lowered for k in ["降雨分布图", "降水实况图", "面雨量分布图", "实况图", "降雨图"]):
        base = "已生成海河流域降水实况分布图，说明如下："
    elif any(k in lowered for k in ["预警", "警报"]):
        base = "已查询相关气象预警信息，整理结论如下："
    elif any(k in lowered for k in ["河网", "水系", "河流"]):
        base = "已绘制河网可视化并叠加行政区划底图，分析如下："
    elif any(k in lowered for k in ["水位", "水文"]):
        base = "已查询河网水位数据，整理如下："
    elif any(k in lowered for k in ["应急响应", "防汛"]):
        base = "已查询防汛应急响应信息，结论如下："
    elif any(k in lowered for k in ["未来", "预报", "明天", "后天", "周末"]):
        base = "已结合预报数据完成分析，为您整理结论如下："
    elif any(k in lowered for k in ["今天", "今日", "实况", "现在", "当前"]):
        base = "已结合实况观测数据完成分析，为您整理结论如下："
    else:
        base = "已理解您的问题，为您解答如下："

    # 如果有图表，补充说明
    if has_chart:
        base = base.replace("为您整理结论如下：", "并生成相关图表，结论如下：")

    return base
```

### Step 7.2: 在 fast paths 出口拼接总结

修改 `_emit_fast_path_result`：

```python
async def _emit_fast_path_result(
    text: str,
    thinking_msg: cl.Message,
    messages: list,
    user_text: str,
    images: list = None,
    append_followup: bool = True,
    reasoning: ReasoningStep | None = None,
    has_chart: bool = False,
):
    await thinking_msg.remove()
    summary = _build_thinking_summary(user_text, has_chart=has_chart)
    final_text = summary + "\n\n" + text if summary else text
    if images:
        await cl.Message(content=final_text, elements=images).send()
    else:
        await cl.Message(content=final_text).send()
    _save_to_history(user_text, final_text, messages)
    if reasoning is not None:
        await reasoning.close()
```

所有调用 `_emit_fast_path_result` 的地方需要传入 `reasoning` 和 `has_chart`。如果某些路径没有 reasoning 对象，可传 `None`。

### Step 7.3: 在 planner 路径出口拼接总结

在 `process_message()` 中所有发送最终回答前：

```python
summary = _build_thinking_summary(message.content, has_chart=是否有图表生成)
text = summary + "\n\n" + text if summary else text
await callbacks["stream_text_to_message"](text, stream_msg=stream_msg)
```

图表生成判断：在 `_run_tool_round` 中如果调用了 `get_station_rainfall_real_img` 或 `get_river_network_for_plot`，可设置一个标志 `has_chart_generated = True` 传给后续流程。简单做法是在 `process_message()` 中根据最终是否有图片元素判断，或在 `cl.user_session.set("has_chart_generated", True)` 并在回答前读取。

### Step 7.4: Commit

```bash
git add chainlitexam/message_orchestrator.py
git commit -m "feat: add one-sentence business summary before each answer"
```

---

## Task 8: 集成测试

**Files:**
- `chainlitexam/tests/test_timing_logger.py` — 计时日志格式与失败场景
- `chainlitexam/tests/test_thinking_summary.py` — 回答前缀生成
- `chainlitexam/tests/test_reasoning_step.py` — ReasoningStep 单元测试
- `chainlitexam/tests/test_fast_paths.py` — fast path 静态检查

### Step 8.1: 自动测试脚本

在仓库根目录执行：

```bash
cd chainlitexam
python tests/test_timing_logger.py
python tests/test_thinking_summary.py
python tests/test_reasoning_step.py
python tests/test_fast_paths.py
```

期望结果：
- `test_timing_logger.py`、`test_thinking_summary.py`、`test_reasoning_step.py` 输出 `All tests passed.`。
- `test_fast_paths.py` 输出 `Total: 18 fast paths, 18 passed.`。
- 任一脚本非零退出即为失败，需先修复再进入人工验证。

### Step 8.2: 本地启动服务（人工验证前置条件）

| 项目 | 要求 |
|---|---|
| 环境 | Python 3.10+，已安装项目依赖 |
| 启动 MCP server | `cd haihe-weather-analyzer-mcp && python server.py` |
| 启动 Chainlit | 另开终端：`cd chainlitexam && chainlit run chain_gzt.py` |
| 访问地址 | 浏览器打开 Chainlit 启动后输出的本地 URL |

### Step 8.3: 人工验证用例与签列表

| 编号 | 用例 | 输入/操作 | 期望结果 | 实际结果 | 签名 |
|---|---|---|---|---|---|
| 8.3.1 | Fast path 思考过程 | 输入：`海河流域降雨分布图` | 出现"🤔 思考过程"可折叠 Step；展开后看到"🔍 理解问题 / 📡 查询数据 / ✍️ 生成结论" | | |
| 8.3.2 | Fast path 回答前缀 | 输入：`海河流域降雨分布图` | 最终回答开头为"已生成海河流域降水实况分布图，说明如下：" | | |
| 8.3.3 | Fast path 计时日志 | 输入：`海河流域降雨分布图` | 后端控制台出现 `[TOOL_TIMING] ... elapsed=X.XXs status=ok` 和 `[QUERY_TIMING] ... total_elapsed=X.XXs status=ok` | | |
| 8.3.4 | Planner 路径阶段 | 输入：`未来三天海河流域降雨如何` | 思考 Step 中出现 4 个阶段（理解问题→查询数据→评估结果→生成结论） | | |
| 8.3.5 | Planner 路径计时 | 输入：`未来三天海河流域降雨如何` | 每个工具调用打印 `[TOOL_TIMING]`，最终打印 `[QUERY_TIMING]` | | |
| 8.3.6 | Planner 路径前缀 | 输入：`未来三天海河流域降雨如何` | 回答开头包含"预报数据"的总结句 | | |
| 8.3.7 | 失败/超时场景 | 1. 关闭或断开 MCP server（停止 `haihe-weather-analyzer-mcp/server.py`）。<br>2. 在 Chainlit 输入：`天津未来三天降雨如何`。<br>3. 观察控制台与前端响应。 | `[TOOL_TIMING] status=fail`、`[QUERY_TIMING] status=fail` 正常打印；前端不崩溃并给出友好提示；思考 Step 正常关闭 | | |
| 8.3.8 | 静态检查 | 运行 `python tests/test_fast_paths.py` | 18 个 fast path 全部 PASS | | |

### Step 8.4: 人工验证通过标准

- 自动测试脚本全部通过。
- 上述 8 条人工用例全部勾选"通过"。
- 未发现新的异常日志或前端报错。

### Step 8.5: Commit（仅当测试通过）

```bash
git add chainlitexam/tests/ docs/superpowers/plans/2026-07-07-thinking-timing-plan.md chainlitexam/timing_logger.py
git commit -m "test: enhance integration test coverage for reasoning and timing"
```

---

## 自我审查

### Spec 覆盖检查

| 设计需求 | 实现任务 |
|---|---|
| 统一控制台工具耗时日志 | Task 1, Task 3 |
| 端到端问题耗时日志 | Task 4 |
| ReasoningStep 阶段化 | Task 2, Task 5 |
| Planner 路径业务化思考 | Task 5 |
| Fast paths 业务化思考 | Task 6 |
| 回答开头总结 | Task 7 |
| 错误处理（计时失败不阻塞、Step 兜底关闭） | Task 2 `__aexit__`, Task 4 `_log_query_exit try/except`, Task 3 TimingLogger 调用嵌入 try 结构 |
| 测试 | Task 8 |

### Placeholder 检查

- 无 TBD/TODO。
- 所有代码块为可直接使用的 Python 代码或命令。
- 动态选择预警接口的描述在 Task 6.4 中保留为中文说明，需实现时根据 `_try_warning_fact_fast_path` 实际分支确定。

### 类型一致性检查

- `ReasoningStep.__aenter__` 返回 `self`，与现有用法一致。
- `_show_business_reasoning` 返回 `ReasoningStep`，与 `_emit_fast_path_result` 新增参数类型一致。
- `TimingLogger.log_tool` / `log_query` 参数类型在 Task 1 和后续调用中一致。

---

## 执行交接

**Plan complete and saved to `docs/superpowers/plans/2026-07-07-thinking-timing-plan.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**