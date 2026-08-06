# 问答智能体 P95/P99 尾延迟观测 + 气象专业化基础 设计

**日期**：2026-08-06
**状态**：设计（待用户审阅）
**范围**：`chainlitexam` 问答智能体

## 背景与目标

GPT 方案要求：①定位并降低 P95/P99 尾延迟（当前平均已改善，但偶发很慢）；②气象专业化不能继续堆长 Prompt。本设计为**第一批**——纯观测/文档/测试数据，零业务风险，不改变回答逻辑、工具选择、接口契约。

**硬约束**：`ENABLE_FAST_PATHS=false` 不动 fast path；`/api/v1/qa/ask` 契约冻结（可加 `request_id`/`X-Request-ID`）；分支 `perf/qa-tail-latency-meteo-domain`；高风险功能默认关。

## 第一批范围（4 项）

### ⓪ 黄金基线（前置，GPT 原则 1、2）

修改任何代码前，先锁定基线：

1. **基线报告** `docs/performance/baseline-before-optimization.md`：记录当前通过的测试（全量 252 passed、1 既有 flaky、5 skipped）、代表性问答结果、工具路由、HTTP 响应样例、当前 p50/p95（从既有 `[QUERY_TIMING]` 日志）、Planner 轮数分布。
2. **基线测试集** `tests/fixtures/meteo_qa_cases.json`：26 类问题各 1-2 例，每例定义 `question/expected_tools{allowed,required,forbidden}/key_facts/time_scope/spatial_scope/units/forbidden_phrases/should_image/should_gis`。作为后续 Prompt 拆分、候选工具启用、DeepSeek 评估的对比基准。

### ① 调用链文档 `docs/performance/current-qa-call-chain.md`

画非 fast path 调用链：HTTP 排队 → 获取运行时 → Planner → 工具调用 → 再次 Planner → Answer → HTTP 结果归并。每节点标注：是否调 LLM、输入大小、超时、重试、Semaphore、提前 return、敏感输出。

**现状**：无此文档。之前多轮优化（跳过第 2 次 Planner、超时不重试、并行工具、HTTP 轻量）已改变调用链，需要成文。

### ② TimingContext 观测增强

**现状**（`timing_logger.py`）：`TimingContext` 有 `mark/record_planner_round/record_tool_call/log`，输出文本 `[PERF] request_id=... planner_round_1=... total_ms=...`。`timing.log()` 只在 `process_message` 主循环正常路径调用一次（约 4965 行），**20+ 个提前 return 路径不记录**。

**改动**：
1. **统一出口**：把 `timing.log()` 从主循环移到 `_log_query_exit` 的 `finally`（`message_orchestrator.py`），用 `cl.user_session` 存 timing 对象。所有退出路径（return/except/timeout/forced_final_text/warning fallback/手动绘图）都经 `_log_query_exit`，记录一次且只记录一次。`_log_query_exit` 已有 `query_timing_logged` 去重标志，复用。
2. **JSON Lines 格式**：`TimingContext.log()` 改为输出 `[PERF] {json}`，字段含 `request_id/execution_mode/planner_rounds/tool_call_count/stages{thinking,planner_round_N,tool_round_N,answer}/total_ms/status`。不记录用户问题全文、工具原始结果、内网地址。
3. **字符数指标**：`planner_input_chars`（planner 调用时 messages 拼接后字符数）、`answer_input_chars`、`planner_output_chars`、`answer_output_chars`。在 `process_message` 的 planner/answer 调用点记录。无 token 接口时不伪造，只记字符数。
4. **排队/等待时间**：HTTP Semaphore 等待（`qa_http_api.py` 的 `_semaphore`）与工具并发 Semaphore 等待（`_run_tool_round` 的 `_PARALLEL_TOOL_SEMAPHORE`）分别记录 `http_queue_wait_ms`/`tool_queue_wait_ms`，不能把排队算成工具接口慢。
5. **统计脚本** `scripts/perf_stats.py`：读 `[PERF]` JSON Lines（从 stdin 或文件），输出 p50/p90/p95/p99、Planner 轮数分布、工具耗时排行、排队时间排行。日志不含完整用户问题，可含 `query_type/query_length/query_hash`。

### ③ 气象专业回归问题集框架 `tests/fixtures/meteo_qa_cases.json`

30 类问题各 1-2 例，每例定义：`question`、`expected_tools{allowed,required,forbidden}`、`key_facts`、`time_scope`、`spatial_scope`、`units`、`forbidden_phrases`、`should_image`、`should_gis`。

覆盖：当前实况/降雨/过去1-6小时雨量/未来小时/1-7天/降水起止/气温/大风/能见度/强对流/生效预警/今日发布/历史预警/国家级/无预警/水位/河网/上下游/暴雨区划/应急响应/点位决策/流域面雨量/预报检验/缺测/工具超时/多轮追问/图片/GIS/同会话连续/异会话并发。

作为后续 Prompt 拆分、候选工具启用、DeepSeek 评估的对比基准。

## 载体

| 文件 | 改动 |
|------|------|
| `docs/performance/baseline-before-optimization.md` | 基线报告 |
| `tests/fixtures/meteo_qa_cases.json` | 黄金基线测试集框架 |
| `docs/performance/current-qa-call-chain.md` | 新增调用链文档 |
| `chainlitexam/timing_logger.py` | `TimingContext.log()` 改 JSON Lines；加字符数/排队字段 |
| `chainlitexam/message_orchestrator.py` | 统一出口（timing 移入 `_log_query_exit` finally）；字符数埋点；工具排队记录 |
| `chainlitexam/qa_http_api.py` | HTTP Semaphore 排队记录 |
| `chainlitexam/scripts/perf_stats.py` | 统计脚本 |

## 遵守 GPT 13 条黄金原则

- **小批量独立提交**（原则 12）：每项一个提交，说明范围、影响文件、测试、回滚。
- **禁止大规模重写核心流程**（原则 3）：只提取小函数、加埋点、加可关闭适配层。
- **Shadow 不改变回答**（原则 6）：观测项只记录，不改工具调用/最终答案/GIS。
- **不以提速减数据**（原则 7）：埋点只记耗时，不跳过任何必要查询。
- **回归失败立即停**（原则 13）：每个提交后跑全量测试，异常即停。

## 测试

- `TimingContext.log()` JSON 格式可解析、字段完整、不含敏感信息。
- `_log_query_exit` 统一出口：正常路径/提前 return/异常都记录一次。
- 字符数埋点：planner_input_chars 正确。
- 统计脚本：对样例 JSON Lines 输出正确 p50/p90/p95/p99。
- 问题集 JSON 结构校验（每例含必需字段）。
- 全量测试回归。

## 风险

- **全为观测性改动**，不改回答逻辑、工具选择、接口契约。
- 统一出口把 `timing.log()` 从主循环移走，需确认 `_log_query_exit` 能访问 timing 对象（经 `cl.user_session`）。
- 字符数埋点仅记录，不伪造 token。

## 后续批次（本批不做）

Prompt 拆分（shadow）、MeteoEvidence 设计、候选工具正式启用、工具结果压缩、LLM 预热、DeepSeek 评估（合规待确认）。