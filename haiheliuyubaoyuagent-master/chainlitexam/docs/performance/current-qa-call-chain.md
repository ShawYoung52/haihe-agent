# 当前 QA 调用链（非 fast path）观测文档

> 目标：为尾部延迟观测（tail-latency observability）提供**当前**调用链的基线。
> 本文只描述 `ENABLE_FAST_PATHS=false`（默认）时，非 fast path 的完整 QA 调用链，
> 每个节点标注：是否调 LLM、输入大小、超时、重试、Semaphore、提前 return、敏感输出。
>
> 代码依据（阅读到的真实实现）：
> - `chainlitexam/qa_http_api.py` — `QARuntime.ask` / `_run_once` / `_get_runtime` / `merge_answers` / `reasoning_texts`
> - `chainlitexam/message_orchestrator.py` — `process_message` / `_run_tool_round` / `_invoke_tools_in_parallel` / `_invoke_tool_with_tolerance` / `_route_simple_weather_query`
> - `chainlitexam/chain_gzt.py` — `_build_orchestrator_callbacks` / `astream_planner_think` / `astream_answer_chain_to_message` / `ainvoke_chain` / `astream_thinking_to_reasoning` / `stream_text_to_message`

---

## 1. 调用链总览

```
HTTP 请求 POST /api/v1/qa/ask
  └─ qa_http_api.runtime.ask（QARuntime.ask）
      ├─ [1] 排队：self._semaphore（并发 4） + store.lock_for(cid)
      ├─ [2] 运行时获取：_get_runtime()（进程级，load_sse_tools 首次连内网 MCP）
      └─ [3] 执行：asyncio.wait_for(_run_once(...), timeout=TIMEOUT_SECONDS=180)
          └─ init_http_context + CapturingEmitter（拦截输出）
              └─ process_message(...)
                  ├─ [4] is_current_rolling_weather_query 专用路径（LLM 1 次 summary，超时 130s）
                  ├─ [5] ReasoningStep 创建 + stream_msg 发送
                  ├─ [6] 简单天气规则路由（_route_simple_weather_query，无 LLM）
                  ├─ [7] THINKING_PLANNER（ENABLE_LLM_THINKING=false 默认跳过；开启时 LLM）
                  ├─ [8] Planner 第 1 次（astream_planner_think，LLM，60s/连接错误重试）
                  ├─ [9] _run_tool_round（并行纯数据工具 + 串行副作用工具）
                  │     ├─ 并行：_PARALLEL_TOOL_SEMAPHORE（并发 4）+ 60s 容错重试
                  │     └─ 串行：单工具 _invoke_tool_with_tolerance（60s，get_city_rainfall_time_range 小时纠偏重试）
                  ├─ [10] Fix A：滚动预报数据完整 → 跳过第 2 次 Planner → Answer
                  ├─ [11] Fix C：Planner 超时 + 有数据 → 回退 Answer
                  ├─ [12] 否则 Planner 第 2 次（LLM）+ 工具轮（至多 MAX_PLANNER_ROUNDS=5）
                  └─ [13] Answer（astream_answer_chain_to_message：chainlit 逐 chunk / http 一次；60s）
  └─ 结果归并：merge_answers / reasoning_texts / _scrub 脱敏
```

---

## 2. 节点明细

### [1] HTTP 排队：`QARuntime.ask`（qa_http_api.py:574）

- **调 LLM**：否
- **输入大小**：`question` 去空白后校验，`len(text) > MAX_QUESTION_LENGTH=2000` 抛 `ValueError`（qa_http_api.py:590）。
- **超时**：整体 `asyncio.wait_for(..., timeout=TIMEOUT_SECONDS=180)`（qa_http_api.py:628）。
- **重试**：无。超时抛 `asyncio.TimeoutError` 由调用方处理。
- **Semaphore**：`self._semaphore = asyncio.Semaphore(MAX_CONCURRENCY)`，`MAX_CONCURRENCY = QA_API_MAX_CONCURRENCY`（默认 4，qa_http_api.py:62/534）。**等待计数 = 排队长度**（埋点目标）。
- **锁**：`self.store.lock_for(cid)` 同会话串行，防读改写竞态（qa_http_api.py:626）。
- **缓存**：单轮请求（无 `conversation_id`）命中 `_response_cache`（`QA_API_RESPONSE_CACHE_TTL`，默认 300s）直接返回，不跑完整链路（qa_http_api.py:599-621）。多轮请求不缓存。
- **提前 return**：命中响应缓存、`question` 为空、超长、`conversation_id` 非法。
- **敏感输出**：无。

### [2] 运行时获取：`_get_runtime`（qa_http_api.py:552）

- **调 LLM**：否。
- **输入大小**：无（进程级单例，首次构造）。
- **超时**：`asyncio.wait_for(self._factory(), timeout=TIMEOUT_SECONDS=180)`（qa_http_api.py:566）。`_factory` 内含 `load_sse_tools()` 连内网 MCP，内网抖动时可能长时间挂住，超时兜底。
- **重试**：失败后 `self._runtime = None`，下一个请求可重试（qa_http_api.py:569-570）。
- **Semaphore**：`self._init_lock`（asyncio.Lock），并发首次初始化只进行一次。
- **提前 return**：已初始化直接返回。
- **敏感输出**：`load_sse_tools()` 连接内网 MCP 地址（日志脱敏，不外泄）。

### [3] 执行：`_run_once`（qa_http_api.py:636）

- **调 LLM**：间接（调 `process_message`）。
- **输入大小**：`question`（≤2000 字）+ `history`（`store.get(cid)`，`QA_API_MAX_HISTORY_TURNS=10` 轮精简历史，`prune_history` 丢弃 ToolMessage/SystemMessage/tool_calls 空壳）。
- **超时**：继承 [1] 的 180s 整体超时；超时触发 `CancelledError`，`finally` 用 `asyncio.shield(store.save(...))` 保证历史落盘（qa_http_api.py:675-678）。
- **重试**：无。
- **Semaphore**：无（[1] 已持）。
- **提前 return**：无。
- **会话回收**：`_release_chainlit_session(ctx.session.id)` 手动 pop，防止 user_sessions 无界增长 OOM（qa_http_api.py:686）。
- **敏感输出**：历史内容含用户原始提问，`_scrub` 出口脱敏。

### [4] `is_current_rolling_weather_query` 专用路径（message_orchestrator.py:4211）

- **调 LLM**：是，1 次（`build_current_weather_observation_summary_prompt` → `callbacks["ainvoke_chain"](answer_chain, ...)`，message_orchestrator.py:4269-4276）。
- **输入大小**：`message.content`（用户问题）+ 实况 payload（`payload` dict，天擎聚合结果）。
- **超时**：工具调用 `asyncio.wait_for(..., timeout=130)`（message_orchestrator.py:4229）；LLM summary 无显式超时（走 `ainvoke_chain` 的 60s + 连接重试，见 [8]）。
- **重试**：工具无；LLM summary 失败 → 代码兜底（message_orchestrator.py:4284-4286）。
- **Semaphore**：无。
- **提前 return**：命中即整条链路 return（含超时/异常降级 return，message_orchestrator.py:4312/4328）。
- **敏感输出**：实况 payload 含站点数据、时间戳（脱敏后进 `final_text`）。

### [5] ReasoningStep + stream_msg（message_orchestrator.py:4453-4457）

- **调 LLM**：否。
- **输入大小**：无。
- **超时/重试/Semaphore**：无。
- **提前 return**：无。
- **敏感输出**：`ReasoningStep("🤔 思考过程")` 与空 `stream_msg` 对前端可见；HTTP 模式由 `CapturingEmitter` 拦截，`reasoning_texts()` 过滤 `tool`（原始 JSON 数十 KB）与 `user_message` 类型。

### [6] 简单天气规则路由：`_route_simple_weather_query`（message_orchestrator.py:759 / 4463-4466）

- **调 LLM**：否（纯规则，跳过 planner LLM，省 5-10s）。
- **输入大小**：`message.content`（用户问题文本）。
- **超时/重试/Semaphore**：无。
- **命中条件**：明确时间词（`_SIMPLE_WEATHER_TIME_WORDS`）+ 明确天气词（`_SIMPLE_WEATHER_KEYWORD_WORDS`），排除流域/河系（`_is_basin_or_river_query`）、决策类词（`_SIMPLE_WEATHER_EXCLUDE_WORDS`）。
- **提前 return**：命中 → `_enforce_simple_weather_route` 构造伪 planner_msg，跳过 [7]/[8] 直接进 [9]。`_is_future_hour_weather_query` 时不走此路由（走 `_enforce_initial_future_hour_weather_route`）。
- **敏感输出**：无。

### [7] THINKING_PLANNER（message_orchestrator.py:4469）

- **调 LLM**：`ENABLE_LLM_THINKING=false`（默认）**跳过**；开启时 1 次（`callbacks["astream_thinking_to_reasoning"]`）。
- **输入大小**：`message.content` + `THINKING_PROMPT.format(current_time, user_query)`。
- **超时**：`astream_thinking_to_reasoning` 30s（chain_gzt.py:1000-1002）。
- **重试**：无；超时/异常 → 追加提示行并继续（chain_gzt.py:1004-1011）。
- **Semaphore**：无。
- **提前 return**：规则路由命中时跳过（`and not simple_route`，message_orchestrator.py:4469）。
- **敏感输出**：思考文本流式进 ReasoningStep。

### [8] Planner 第 1 次：`astream_planner_think`（message_orchestrator.py:4503 / chain_gzt.py:964）

- **调 LLM**：是（`PLANNER_MODEL`，默认 Qwen3.6-27B）。
- **输入大小**：`messages`（用户问题 + 历史），`_compress_messages` 前压缩：更早历史 ToolMessage 截 500 字符、AIMessage 截 1500 字符，最近一轮保持完整（message_orchestrator.py:213-239）。
- **超时**：`PLANNER_TIMEOUT_SECONDS`（默认 60s，chain_gzt.py:966）。
- **重试**：`PLANNER_MAX_RETRIES`（默认 2）。推理超时（TimeoutError）**不重试**直接抛；连接/限流错误（ConnectionError/httpx.ConnectError/httpx.ReadTimeout）重试，间隔 1s（chain_gzt.py:969-981）。
- **Semaphore**：无（planner LLM 调用无信号量限制）。
- **提前 return**：简单天气路由/未来小时天气路由命中时跳过本节点（见 [6]）。planner 首轮异常 → 报错 return（message_orchestrator.py:4507-4516）。
- **输出**：`AIMessage`（`tool_calls` + `content`）。`_ensure_tool_calls_from_content` 兜底。
- **敏感输出**：planner content 可能含业务中间结论；tool_calls 参数含用户地点/时间。

### [9] `_run_tool_round`（message_orchestrator.py:1860）

- **调 LLM**：否（纯工具执行；分支处理里预警/决策天气走专用装配器会再调 answer LLM，见下）。
- **输入大小**：`planner_msg.tool_calls`（工具名 + args）。
- **阶段一 并行**：`_invoke_tools_in_parallel`（message_orchestrator.py:1806）。
  - 仅白名单 `_PARALLEL_SAFE_TOOLS`（纯数据、无副作用工具，message_orchestrator.py:1785-1798）。
  - **Semaphore**：`_PARALLEL_TOOL_SEMAPHORE`，`_PARALLEL_TOOL_CONCURRENCY=4`（进程内共享，message_orchestrator.py:1801-1803）。
  - `asyncio.gather` 并行；单工具失败转失败观测文本，不中断整轮。
- **阶段二 串行**：非白名单（副作用）工具逐一执行。
- **单工具超时/重试**：`_invoke_tool_with_tolerance`（message_orchestrator.py:858）。
  - `tool.ainvoke(args)` 无显式超时包一层——**超时由上层 `asyncio.wait_for` 控制**？不，`_invoke_tool_with_tolerance` 本身不设超时；但 `_run_tool_round` 调用处无 wait_for。**实际超时来源**：MCP 客户端/HTTP 客户端自身（内网 MCP SSE）超时。可观测性待补充（Task 3）。
  - 容错重试：仅 `get_city_rainfall_time_range` 且错误含 `hour%6==2` 时，`_build_hour_tolerant_args` 纠偏小时重试一次（message_orchestrator.py:874-897）。
- **分支处理**（message_orchestrator.py:1920-2055）：
  - `analyze_rainstorm_impact` → `enrich_with_impact_time_tool` + 强制收口文本（`forced_final_text`）。
  - `query_decision_weather_for_poi` → `forced_final_text`（不经 answer LLM）。
  - 预警工具 → `warning_bundles` 缓存；循环内 `finalize_warning_answer` 会再调 answer LLM。
  - `query_rolling_forecast` → `build_rolling_forecast_bundle` + `compact_rolling_forecast_facts`（压缩后进 ToolMessage）+ `rolling_forecast_llm_instruction`。
  - `get_station_rainfall_real_img` → base64 解码 → `cl.Image` 发送。
  - 其余 → `tool_observation_to_text`。
- **提前 return**：`forced_final_text`（决策天气/强制收口）→ 循环内 break（message_orchestrator.py:4669-4683）；`warning_bundles` → 专用装配器 break。
- **敏感输出**：工具观测含站点/河流/网格原始数据，`_scrub_internal_data` 处理失败摘要；`step.input`/`step.output` 展示耗时与状态，不泄原始坐标（river plot 明确要求不输出坐标数据）。

### [10] Fix A：滚动预报完整 → 跳过第 2 次 Planner（message_orchestrator.py:4709-4745）

- **调 LLM**：是，Answer 1 次（`astream_answer_chain_to_message`）。
- **触发**：`_has_complete_rolling_forecast(rolling_forecast_bundles)`（最后一个 bundle 有代码生成的表格）且无应急响应工具。
- **输入大小**：`messages`（含压缩后的滚动预报 ToolMessage）。
- **超时**：Answer 60s（message_orchestrator.py:4721）。
- **重试**：无；失败 → 用首个 bundle 的 `code_section` 代码收口兜底（text=""）。
- **Semaphore**：无。
- **提前 return**：是（break 后 `answer_generated=True`）。
- **输出**：`assemble_rolling_forecast_answer` 组装（代码表格 + LLM 结论）。

### [11] Fix C：Planner 超时 + 有数据 → 回退 Answer（message_orchestrator.py:4877-4904 及 4822-4850）

- **调 LLM**：是，Answer 1 次（回退重试）。
- **触发**：第 2 次（及后续轮）Planner `asyncio.TimeoutError`，且 `_has_complete_rolling_forecast`。
- **输入大小**：同 [10]。
- **超时**：Answer 60s。
- **重试**：无；Answer 也失败 → `text=""` 走代码收口。
- **Semaphore**：无。
- **提前 return**：是。

### [12] Planner 第 2 次及以上（message_orchestrator.py:4756-4871）

- **调 LLM**：是，每次 `astream_planner_think`（同 [8] 的超时/重试/Semaphore）。
- **输入大小**：`messages`（已含上一轮 tool 结果），`_compress_messages` 每次调用前压缩。
- **循环上限**：`MAX_PLANNER_ROUNDS`（默认 5），`while planner_msg.tool_calls and iteration < max_iterations`。
- **提前 return**：planner 无 tool_calls → 复用 planner content 或直接 Answer；未来小时天气路由已取预报 → `_set_tool_calls(planner_msg, [])` 忽略重复工具调用。
- **敏感输出**：同 [8]。

### [13] Answer：`astream_answer_chain_to_message`（chain_gzt.py:871）

- **调 LLM**：是（`ANSWER_MODEL`，默认 Qwen3.6-27B）。
- **输入大小**：`messages`（含所有 tool 结果 + planner 消息），`_compress_messages` 已压缩历史。
- **超时**：调用处统一 `asyncio.wait_for(..., timeout=60)`（如 message_orchestrator.py:4607、4718-4726、4818-4821、4943）。
- **重试**：无（`astream_answer_chain_to_message` 内部：流式失败 → 回退 `answer_chain.ainvoke` 非流式，chain_gzt.py:903-915）。
- **Semaphore**：无。
- **模式差异**：
  - `execution_mode="chainlit"`：逐 chunk `stream_msg.update()`（chain_gzt.py:892-894）。
  - `execution_mode="http"`：`_build_orchestrator_callbacks(execution_mode="http")`（chain_gzt.py:556/3766），仅在结尾 update 一次，减少 per-chunk 开销（chain_gzt.py:898-901）。
  - 含 Markdown 表格时 `stream_text_to_message` 直接完整发送避免切片破坏（chain_gzt.py:1026-1032）。
- **提前 return**：无（最后一步）。
- **敏感输出**：答案正文 `_sanitize_display_text` + `_scrub` 后进历史与 HTTP 响应。

### 出口归并：`merge_answers` / `reasoning_texts`（qa_http_api.py:163/393）

- **调 LLM**：否。
- **merge_answers**：按消息 id 取最终态 + 按首现顺序拼接；`drop_sideband=True` 过滤 `❌`/`⏱️`/`📊` 旁路消息和纯引导语；`deleted_ids` 处理被删消息（`CapturingEmitter` 触发 `delete_step` 时记录，qa_http_api.py:374-380）。
- **reasoning_texts**：按 id 归并取最终态，过滤 `tool`（原始 JSON 数十 KB）与 `user_message` 类型。
- **脱敏**：`_scrub`（IP/路径/数据库连接串）；日志只记异常类型不记 `exc_info`。
- **敏感输出**：最终 `answer` / `reasoning` 均过 `_scrub` 才返回。

---

## 3. 延迟关键点（埋点目标）

| 节点 | 延迟来源 | 超时 | 重试 | 可提前 return |
|---|---|---|---|---|
| [1] HTTP 排队 | `_semaphore` 等待（并发 4）+ `lock_for(cid)` | 180s 整体 | 无 | 缓存命中 |
| [2] 运行时获取 | `load_sse_tools()` 首连内网 MCP | 180s | 下请求重试 | 已初始化 |
| [4] 实况专用路径 | 天擎工具 + 1 次 summary LLM | 工具 130s / LLM 60s | 无 | 命中即 return |
| [7] THINKING_PLANNER | 默认关闭；开启时 1 次 LLM | 30s | 无 | 默认跳过 |
| [8]/[12] Planner | LLM 推理 | 60s | 连接错误 ×2（超时不重试） | 规则路由命中跳过 |
| [9] 工具轮 | MCP/HTTP 数据查询 | 无显式超时（依赖客户端） | 仅 hour 纠偏重试 | forced_final_text / 预警装配 |
| [10] Fix A | Answer LLM | 60s | 无 | 滚动预报完整即 break |
| [11] Fix C | Answer LLM（回退） | 60s | 无 | 有数据即 break |
| [13] Answer | LLM 推理 | 60s | 流式失败回退非流式 | 无 |

---

## 4. 敏感信息处理原则

- 内网服务地址（MUSIC `10.226.90.120`、PostgreSQL `10.226.107.130`、RAG `10.226.188.156:8033`、LLM 代理 `10.226.188.156:8000`）**不写进用户可见输出或文档**，用环境变量占位。
- 工具失败摘要经 `_scrub_internal_data` 脱敏；HTTP 响应出口经 `_scrub`。
- `reasoning_texts()` 过滤 `tool` 类型（原始 JSON 数十 KB），避免把工具原始观测透传给小程序。

---

## 5. 变更记录

- 2026-08-06：初始文档（Task 1 of qa-tail-latency-observability）。
