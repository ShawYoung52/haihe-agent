# 问答智能体 114 秒响应优化 设计

**日期**：2026-08-05
**状态**：设计（待用户审阅）
**范围**：`chainlitexam` 问答智能体（Chainlit 网页端）

## 背景与用户反馈

甲方线上日志显示一个查询耗时 **114 秒**（`total_ms=114515ms`），其中 `answer=107928ms`。用户问"这问题能回答两分钟的吗"，要求遍历问答智能体代码优化速度。

## 根因（遍历代码 + 日志分析）

**时间线**（问题："周末适合搞户外活动吗？"）：
- `planner_round_1=5858ms` — 第 1 次 Planner 选 `query_rolling_forecast`（5.8s，正常）
- `tool_round_1=720ms` — 工具查询（0.7s，数据已压缩，正常）
- `answer=107928ms` — **第 2 次 Planner 花了 107.9s**：`[astream_planner_think] 第 1 次调用超时，准备重试...` = 60s 超时 + 47s 重试
- `total=114515ms` — 总计 114.5s，其中 94% 花在第 2 次 Planner

**关键根因 1（主因）**：`query_rolling_forecast` 已返回完整 `daily_summary`/`code_section`，但代码仍调用第 2 次 Planner 判断"是否够回答"。这次 Planner 超时又重试 = 107s。匹配 GPT 方案第五节："工具成功且结果完整时，不出现无意义的第 3 次 Planner"。

**关键根因 2（放大）**：`astream_planner_think`（`chain_gzt.py:958`）`timeout=60` 且重试。推理超时还重试一次 → 最坏 120s+。GPT 方案 P0 要求"Planner 超时不能自动再等待一个完整的 60 秒""推理超时不自动重复完整请求，仅连接/限流错误重试"。

## 设计

### Fix A：滚动预报数据完整时跳过第 2 次 Planner（核心，省 107s）

**插入点**：`process_message` 主循环（`message_orchestrator.py:4659` 的 `forced_final_text` 分支之后、`while` 末尾第 2 次 Planner 调用之前）。

**逻辑**：
- 本轮工具调用后 `rolling_forecast_bundles` 非空，且最后一个 bundle 有 `code_section`（代码生成的表格）→ 数据完整，无需 Planner 二次决策。
- 直接调用 `answer_chain` 生成【核心结论】（LLM 只写一句话结论，表格/数据来源由 `assemble_rolling_forecast_answer` 代码组装），然后走与"复用 Planner 回答"相同的收口路径。
- 若本轮同时调用了应急响应工具（`has_emergency_response_tool`），**不跳过**（需 planner 综合应急判定）。

**效果**：单滚动预报工具查询从"Planner① → Tool → Planner② → Answer"变为"Planner① → Tool → Answer"，省掉整次第 2 次 Planner（理想链路，匹配 GPT 方案验收 2）。

**安全性**：只跳过一次 Planner 决策，Answer LLM 仍生成结论，`assemble_rolling_forecast_answer` 组装表格/数据来源。不改工具调用、消息顺序、最终回答结构。

### Fix B：推理超时不重试（防最坏 120s）

**改动**：`astream_planner_think` 与 `ainvoke_chain`（`chain_gzt.py`）：
- 超时（`TimeoutError`/`asyncio.TimeoutError`）**不重试**，直接抛错。
- 仅对连接失败/限流等瞬时错误重试（GPT 方案明确要求）。
- 新增 `PLANNER_TIMEOUT_SECONDS`/`ANSWER_TIMEOUT_SECONDS` 环境变量（默认 60，保持现状，可配置）。

**效果**：Planner 超时最坏从 120s 降到 60s。

**安全性**：默认超时值不变（60s），只去掉"超时还重试"的冗余。超时后回退到现有错误处理路径。

### Fix C：超时后回退 answer 生成（兜底）

**现状**：`astream_planner_think` 超时抛错 → `process_message` 捕获打印 traceback → 报"大模型调用失败"。用户得不到回答。

**改动**：`process_message` 第 2 次 Planner 调用超时后，若已有 `rolling_forecast_bundles` 数据，回退到 Answer LLM 生成结论（与 Fix A 相同路径），而不是直接报错。

**效果**：Planner 超时也能给出基于已查数据的回答。

**安全性**：仅超时兜底路径，不改正常流程。

## 载体

| 文件 | 改动 |
|------|------|
| `chainlitexam/message_orchestrator.py` | Fix A：滚动预报数据完整跳过第 2 次 Planner；Fix C：超时回退 |
| `chainlitexam/chain_gzt.py` | Fix B：超时不重试 + 超时环境变量 |

## 测试

- Fix A：单滚动预报工具查询，验证不调用第 2 次 Planner、Answer 正常生成、`assemble_rolling_forecast_answer` 组装正确。
- Fix B：`astream_planner_think`/`ainvoke_chain` 超时抛错不重试；连接错误仍重试。
- Fix C：Planner 超时 + 已有滚动预报数据 → 回退 Answer 生成而非报错。
- 全量测试回归（现有 237 passed + 新测试）。

## 风险与取舍

- **Fix A**：跳过第 2 次 Planner 后，Answer LLM 只生成一句话结论。若某查询需 Planner 综合多个工具结果才答，不适用（有 `has_emergency_response_tool` 守卫 + `rolling_forecast_bundles` 完整才跳）。保守兜底：`code_section` 为空时不跳。
- **Fix B**：超时不重试可能让瞬时网络抖动时 Planner 失败更频繁，但有 Fix C 兜底（回退 answer）。
- 不改回答逻辑、工具调用、消息顺序、最终回答结构。