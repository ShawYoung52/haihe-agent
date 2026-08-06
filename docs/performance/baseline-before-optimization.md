# 问答智能体性能黄金基线（优化前）

> 目的：在开始 P95/P99 观测与优化之前，锁定当前系统的可复现状态作为黄金基线。
> 基线之后任何改动都必须对照本文件确认「无回归」或「有明确可量化的改善」。
> 本文件仅记录现状，不含任何业务逻辑改动。

- 日期：2026-08-06
- 分支：`perf/qa-tail-latency-meteo-domain`
- 基线上游 commit：`6736638`（docs: add observability implementation plan）
- 对应计划：`docs/superpowers/plans/2026-08-06-qa-tail-latency-observability.md`

---

## 1. 全量测试基线

运行命令（从 `chainlitexam/`）：

```bash
D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe \
  -m pytest tests/ --ignore=tests/test_decision_weather_tool.py -v
```

结果：

| 指标 | 值 | 说明 |
|---|---|---|
| passed | **252** | 全量通过 |
| failed | **1（既有 flaky）** | `test_process_message_skips_fast_paths_when_disabled`——真实创建 `ReasoningStep` 缺 Chainlit context 抛 `ChainlitContextException`，属既有问题，不计为本项目回归 |
| skipped | **5** | 多为 `tests/stubs.py` 假 chainlit 环境相关 |

排除说明：`tests/test_decision_weather_tool.py` 有既有 import 失败（`ModuleNotFoundError: No module named 'tools'`），与本项目无关，全量命令用 `--ignore` 排除。

新增基线产物（本任务）：
- `chainlitexam/tests/fixtures/meteo_qa_cases.json` —— 32 例气象问答问题集（28 类）
- `chainlitexam/tests/test_meteo_qa_cases.py` —— 结构校验（5 条，全部通过）

---

## 2. 已知响应延迟（P95/P99 观感）

从 `[QUERY_TIMING]` / `[PERF]` 日志观察到的代表性长尾案例（基线）：

| 案例 | 总响应耗时 | 主要构成 |
|---|---|---|
| 一般滚动预报问答（Fix A 已生效） | ~32s | Planner 第 1 次（5-10s）+ 滚动预报工具 + Answer |
| 综合/多工具问答（第 2 次 Planner 未跳过） | ~57s | 两次 Planner + 多轮工具串行 |
| 长尾（Planner 超时或工具慢，走超时/重试路径） | ~115s | 107s 超时 + 连接重试放大（Fix B/C 已部分缓解） |

已知主因（来自 2026-08-05 两个计划的根因分析）：
1. **第 2 次 Planner 冗余**——滚动预报数据已完整时仍再调一次 Planner（Fix A 跳过，但只覆盖 `code_section` 完整场景）。
2. **Planner/Answer 推理超时重试**——推理超时也被重试，等于连续等两个完整超时（Fix B 后仅连接/限流错误重试）。
3. **HTTP 逐 chunk 刷新**——Chainlit 模式逐 chunk `update()`，HTTP 场景浪费（`execution_mode="http"` 后一次更新）。
4. **排队**——HTTP 并发由 `QA_API_MAX_CONCURRENCY`（默认 4）信号量控制，超限排队等待。

这些数字为**日志观感基线**，后续 Task 2/3 补全 `[PERF]` JSON Lines + 排队/字符数埋点后，用 `perf_stats.py` 产出统计口径的 P50/P90/P95/P99。

---

## 3. 工具路由现状

| 查询域 | 路由方式 | 关键入口 |
|---|---|---|
| 预警（当前生效/历史/国家） | **规则路由** | `tools/warning_workflow.py::_route_warning_tools` → `_route_warning_tools_rule_based`（含"预警"关键词即命中，规则失败回退 LLM）；`WARNING_TOOL_NAMES = {get_effective_warning_info, get_history_warning_info, get_today_warning_summary, get_national_warning_info}` |
| 滚动预报（简单天气） | **Fix A + 简单天气规则路由** | `_route_simple_weather_query`（"今天/明天/后天/周末+天气"命中即跳过 Planner，省 5-10s）；`_has_complete_rolling_forecast` 数据完整 → 跳过第 2 次 Planner，直接 Answer（Fix A） |
| 点位决策天气 | **规则槽位抽取** | `tools/decision_weather_core.py::_extract_decision_slots_rule_based` 纯规则抽位置名+问题类型（省 1 次 LLM），失败回退 LLM；`_decision_weather_prefilter` 判定位点意图 |
| 流域/河系天气 | **河系工具 + 硬防护** | `get_river_system_rainfall_forecast` 优先；`query_rolling_forecast` 内部 `is_basin_weather_query` 硬防护拦截裸河名 |
| 候选工具召回 | **影子模式** | `tools/tool_candidate_index.py::ToolCandidateIndex` 只记录 `[TOOL_CAND]`，不改 Planner 绑定 |
| 其余 | Planner LLM + 工具循环 | `_run_tool_round` 并行纯数据工具（`_PARALLEL_TOOL_CONCURRENCY=4`）+ 串行副作用工具 |

---

## 4. 代表性问答结果（日志/实测样例）

### 4.1 简单天气（规则路由命中）

```
Q: 明天天津天气怎么样
路由: _route_simple_weather_query → query_rolling_forecast（跳过 Planner LLM）
数据: 滚动预报 bundle 含 code_section 表格
Fix A: _has_complete_rolling_forecast=True → 跳过第 2 次 Planner → Answer 生成
预期耗时: ~32s 档
```

### 4.2 预警（规则路由命中）

```
Q: 现在有什么暴雨预警
路由: _route_warning_tools_rule_based → get_effective_warning_info
输出: 生效预警清单表格 + 影响区域裁剪（_trim_warning_regions_for_scope）
```

### 4.3 点位决策天气（规则槽位）

```
Q: 梅江会展中心明天天气如何
路由: _extract_decision_slots_rule_based → location_name="梅江会展中心", question_type="general_weather"
数据: query_decision_weather_for_poi（POI 检索 + 逐小时决策预报）
```

### 4.4 HTTP 接口

```
POST /api/v1/qa/ask
{
  "question": "海河流域面雨量多少",
  "conversation_id": null,
  "include_reasoning": true,
  "include_gis": true
}
```

响应外层（`chain_gzt.py::_qa_ask`）：

```json
{
  "code": 200,
  "data": {
    "answer": "……（脱敏后正文，过滤 ❌/⏱️/📊 旁路消息）……",
    "conversation_id": "……",
    "images": [],
    "gis": [],
    "reasoning": ["……思考过程（过滤 tool/user_message）……"],
    "elapsed_seconds": 32.1
  },
  "message": "success"
}
```

- 单轮请求（无 `conversation_id`）命中响应缓存（`QA_API_RESPONSE_CACHE_TTL=300s`）时 `elapsed_seconds=0` 直接返回。
- 超时：`QA_API_TIMEOUT_SECONDS`（180s）→ HTTP 504；未配置 → 503。
- 多轮请求通过 `InMemoryConversationStore` 保留最近 10 轮（`QA_API_MAX_HISTORY_TURNS`）。

---

## 5. Planner 轮数分布（Fix A 后观感）

- **多数简单滚动预报问答：1 轮**（Fix A：数据完整即跳过第 2 次 Planner）。
- 综合/多工具/应急响应场景：仍走第 2 次 Planner 综合（2 轮）。
- Planner 轮数目前只能从 `[PERF]` 日志的 `planner_rounds=N` 观察，**无统计数据文件**——Task 2 统一出口后可用 `perf_stats.py` 量化分布。

---

## 6. 已知观测缺口（本基线未覆盖，后续任务补齐）

1. **无结构化统计**：P50/P90/P95/P99 只能人工看日志，无 `perf_stats.py`（Task 4）。
2. **无统一退出埋点**：`TimingContext.log()` 在主循环内，异常/提前 return 路径可能漏记（Task 2 统一到 `_log_query_exit` finally）。
3. **无排队计时**：HTTP 信号量排队时间未记录（Task 3）。
4. **无字符数埋点**：Planner/Answer 输入输出 token 量未记录（Task 3）。

---

## 7. 回归判定标准

任何后续改动后，对照本基线确认：

- [ ] 全量 `pytest tests/ --ignore=tests/test_decision_weather_tool.py`：252 passed、1 既有 flaky、5 skipped，**新增失败即停**。
- [ ] 简单天气 / 预警 / 决策点位三类路由行为不改变（fixtures 可作为回归样本）。
- [ ] 观测类改动（shadow 记录、埋点）不改变工具调用、最终答案、GIS。
