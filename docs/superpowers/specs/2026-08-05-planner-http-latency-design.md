# Planner/HTTP 问答链路延迟优化 — 安全批设计

**日期**：2026-08-05
**状态**：设计（待用户审阅）
**范围**：`chainlitexam` 问答智能体（Chainlit 网页端 + `/api/v1/qa/ask` HTTP 接口）

## 背景与目标

甲方反馈问答智能体响应慢。经评估，GPT 提出的完整性能方案中部分改动可能影响回答正确性，因此**分阶段实施**。本次只做**安全批**——基本不影响回答决策逻辑的改动：

1. **关闭额外 Thinking LLM**（`ENABLE_LLM_THINKING=false`）：通用链路 Planner 前的 `thinking_chain` 输出仅展示、不喂 Planner，删除它只省等待时间。
2. **阶段耗时日志**：结构化记录各阶段耗时，为后续优化提供数据。
3. **Planner/Answer 独立环境变量**：硬编码改可配置，但**默认值保持不变**（不改变回答）。
4. **HTTP 轻量模式**：HTTP 接口不逐 chunk 刷新 Chainlit，内存累积后一次性写入。
5. **HTTP emitter 不落库**：删进程级 `_qa_persist_blocked`，改 per-emitter 内存记录。

**逐步实施的后续批次**（本次不做，另行规划）：Planner 轮数降低、候选工具过滤、工具结果压缩、换小 Planner 模型、无脑工具并行——均需影子验证 + 灰度。

## 硬约束

- 生产 `ENABLE_FAST_PATHS=false`，只优化通用 Planner 问答链路，**不动/不启用/不重构任何 fast path**。
- `/api/v1/qa/ask` 是外部公司正式接口，**必须向后兼容**：不改请求字段、不删响应字段、不改字段类型、保留 `code/data/message` 外层 + `answer/conversation_id/images/gis/reasoning/elapsed_seconds` 六字段、可加可选字段/响应头但不能破坏旧客户端、不要求 SSE。
- **禁止向外部返回**：内部思考过程/CoT、模型名/地址、MCP 工具名、内网 IP/库地址/路径、原始异常/Traceback/连接串、未脱敏工具响应。
- **不以牺牲回答正确性为代价**，所有优化有测试 + 耗时埋点证明。
- 新建分支 `perf/planner-http-latency`，不直接改 main。
- 无法连接内网模型/MCP 时不得伪造性能数字，用 fake chain/tool 写延迟测试并明确标注"真实数据需内网验证"。

## 现状

### 1. Thinking LLM
`message_orchestrator.py` `process_message`（约 4337-4354 行）：非简单天气路由时调用 `thinking_chain` → `astream_thinking_to_reasoning`，输出追加到 `ReasoningStep` 展示，**不写入 Planner 的 `messages`**。删除它不影响 Planner 输入/工具参数/Answer 输入/最终回答。

### 2. 耗时日志
`timing_logger.py` 有 `TimingLogger.log_tool`（`[TOOL_TIMING]`）与 `log_query`（`[QUERY_TIMING]`），记录工具与整查询耗时，**缺分阶段**（thinking/planner/tool/answer）。

### 3. LLM 配置
`chain_gzt.py` `_build_orchestrator_runtime`（约 2469-2482 行）：`planner_llm` 与 `answer_llm` 硬编码 `model="Qwen3.6-27B"`, `temperature=0.7`, `openai_api_base="http://10.226.188.156:8000/v1/"`。单一配置，不可独立调。

### 4. HTTP 逐 token 刷新
`chain_gzt.py` `astream_answer_chain_to_message`（约 868-904 行）：`answer_chain.astream` 逐 chunk `stream_msg.update()` 刷新前端。HTTP 接口复用同一路径，逐 chunk 事件是浪费。

### 5. HTTP emitter 落库
`qa_http_api.py` `CapturingEmitter`（约 333-396 行）：`send_step/update_step/delete_step/send_element/send_window_message` 记录内存 **同时调 `super()` 进 Chainlit 数据层**。靠进程级 `_qa_persist_blocked` 全局标志 + `_ensure_data_layer_filter` 数据层代理（约 423-466 行）在 HTTP 期间拦截 DB 写。该全局标志与多 HTTP 请求并发存在冲突隐患。

## 设计

### Item ① 关闭 Thinking LLM（`ENABLE_LLM_THINKING=false`）

**改动**：`message_orchestrator.py` 模块级新增 `ENABLE_LLM_THINKING = os.getenv("ENABLE_LLM_THINKING", "false").lower() == "true"`（默认 false）。`process_message` 中 `thinking_chain` 调用改为 `if ENABLE_LLM_THINKING and not simple_route:`。为 false 时：
- 不调用 `thinking_chain`、不调用 `astream_thinking_to_reasoning`。
- 保留确定性业务阶段（`reasoning.stage` 的"理解问题/查询数据/生成结论"）。
- 不影响 fast path（fast path 不经过此段）。
- HTTP `reasoning` 字段保留（为 `include_reasoning=true` 时返回业务阶段摘要，不返回模型思考过程）。

**测试**：`ENABLE_FAST_PATHS=false` 且 `ENABLE_LLM_THINKING=false` 时，`astream_thinking_to_reasoning` 调用次数严格为 0。

### Item ② 阶段耗时日志

**改动**：`timing_logger.py` 新增 `TimingContext` 类，持有 `request_id` 与各阶段起始时间，`process_message` 内记录：
- `queue_wait_ms`（HTTP 请求排队到开始）
- `thinking_ms`（Thinking LLM，若启用）
- `planner_round_1_ms`、`planner_round_2_ms`（各轮 Planner）
- `tool_total_ms` + 每工具 `tool_ms`
- `answer_ms`
- `total_ms`
- `planner_rounds`、`tool_call_count`
- `request_id`

结束时输出一条结构化日志（如 `[PERF] request_id=... thinking_ms=... planner_round_1_ms=... ...`）。**不记录**用户问题、工具原始结果、内网地址、绝对路径。日志分级为 info。

**测试**：fake chain/tool 下验证 TimingContext 各阶段累计正确、日志字段完整、不含敏感信息。

### Item ③ Planner/Answer 独立环境变量

**改动**：`chain_gzt.py` `_build_orchestrator_runtime` 改为读环境变量（`_env_str`/`_env_float`/`_env_int` 辅助，非法值回落默认）：
- `PLANNER_MODEL`（默认 Qwen3.6-27B）、`PLANNER_API_BASE`（默认现有）、`PLANNER_API_KEY`（默认 EMPTY）、`PLANNER_TEMPERATURE`（**默认 0.7**，保持现状）、`PLANNER_MAX_TOKENS`（默认 None）、`PLANNER_TIMEOUT_SECONDS`、`PLANNER_MAX_RETRIES`
- `ANSWER_MODEL`、`ANSWER_API_BASE`、`ANSWER_API_KEY`、`ANSWER_TEMPERATURE`（默认 0.7）、`ANSWER_TIMEOUT_SECONDS`、`ANSWER_MAX_RETRIES`

**要求**：默认值全部保持现状（temp 0.7，不按 GPT 方案改 0）；不新增真实 IP/密钥/连接串；Planner 与 Answer 可同模型或不同模型。

**测试**：默认配置下构造的 planner/answer 参数与现状一致；env 覆盖后参数改变。

### Item ④⑤ HTTP 轻量模式 + emitter 不落库（一起做）

**改动 A（item ⑤）**：`CapturingEmitter` 改为**纯内存记录**——`send_step/update_step/delete_step/send_element/send_window_message` 只记录 `_record`/`_deleted_ids`/`elements`/`gis_packets`，**不调 `super()`**。删除 `_qa_persist_blocked` 全局标志与 `_ensure_data_layer_filter` 数据层代理。网页 Chainlit 会话用默认 emitter，持久化不变。

**改动 B（item ④）**：新增 `execution_mode`：
- `chain_gzt.py` 的 `astream_answer_chain_to_message` 增加 `execution_mode="chainlit"` 参数。为 `"http"` 时：`answer_chain.astream` 流式接收但只在内存累积 `full_text`，**结束时一次性 `stream_msg.update()`**（不逐 chunk 刷），仍返回完整文本。
- callbacks 表增加 `execution_mode` 字段。网页 `_build_orchestrator_callbacks()` 默认 `"chainlit"`；HTTP 运行时显式传 `"http"`。
- `qa_http_api._run_once` 构造 emitter 仍是 `CapturingEmitter`；`process_message` 调用回调时走 HTTP 轻量路径。

**要求**：保留图片/GIS/最终答案捕获；`/api/v1/qa/ask` 响应 JSON 不变；不再依赖进程级布尔变量；同一 `conversation_id` 串行、不同 `conversation_id` 可并发，并发请求不串 answer/reasoning/images/gis。

**测试**：
1. 两个 HTTP 请求同时执行，结果不串。
2. HTTP 请求期间网页会话仍能正常持久化。
3. 同一 conversation_id 保持顺序。
4. 不再依赖 `_qa_persist_blocked`。
5. HTTP 模式不逐 chunk 调 `stream_msg.update()`（埋点计数）。

## 载体与影响

| 文件 | 改动 |
|------|------|
| `chainlitexam/message_orchestrator.py` | `ENABLE_LLM_THINKING` 开关；`TimingContext` 埋点 |
| `chainlitexam/timing_logger.py` | 新增 `TimingContext` 结构化耗时 |
| `chainlitexam/chain_gzt.py` | Planner/Answer 环境变量化；`astream_answer_chain_to_message` 加 `execution_mode`；callbacks 传 execution_mode |
| `chainlitexam/qa_http_api.py` | `CapturingEmitter` 纯内存；删 `_qa_persist_blocked`/`_ensure_data_layer_filter`；HTTP 传 execution_mode="http" |

## 验收标准

1. `ENABLE_FAST_PATHS=false` 且 `ENABLE_LLM_THINKING=false` 时，thinking chain 调用数为 0。
2. 理想链路：Planner 1 次 + Tool 1 轮 + Answer 1 次（不额外引入）。
3. HTTP 模式不逐 chunk 触发 `stream_msg.update()`。
4. HTTP 请求不写 Chainlit threads/steps 表。
5. 不再使用进程级 `_qa_persist_blocked`。
6. `/api/v1/qa/ask` 响应结构不变。
7. 所有现有及新增测试通过（全量套件预期 1 个既有 flaky 失败）。
8. 生成修改前后调用次数与耗时对比报告；无法连内网时区分"fake 测试结果"与"真实内网待验证项"。

## 风险与取舍

- **全部为低风险改动**：不改变 Planner 输入、工具选择、Answer 输入、最终回答逻辑。
- **Item ④ 的回归点**：`answer`/`images`/`gis`/`reasoning`/`conversation_id` 字段必须与原先一致（HTTP 一次性写入 vs 逐 chunk 写入，`merge_answers` 取最终态，语义等价）。
- **Item ⑤ 的回归点**：网页 Chainlit 会话持久化不受影响（用默认 emitter）。
- 温度默认 0.7 而非 GPT 方案的 0——符合"默认值不变"原则，温度调低归入风险批。