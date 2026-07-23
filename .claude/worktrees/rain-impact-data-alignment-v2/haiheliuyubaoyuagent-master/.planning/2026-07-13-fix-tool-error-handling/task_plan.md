# Task Plan: 修复工具失败时通用报错及测试导入问题

**PLAN_ID:** 2026-07-13-fix-tool-error-handling  
**创建时间:** 2026-07-13  
**Goal:** 定位并修复问答智能体在调用 evaluate_haihe_forecast_emergency_response / get_city_rainfall_time_range 等工具失败时向用户展示“数据查询遇到问题，请稍后重试。”通用报错的问题；同时修复 `message_orchestrator.py` 中 `from utils.tool_result` 相对导入导致测试无法收集的问题。

## Current Phase
All phases complete. Task ready for handoff / merge.

## Phases

### Phase 1: Root Cause Investigation
- [x] 定位错误文案来源：`message_orchestrator.py:2529` 的通用 except 块
- [x] 识别涉及工具：`evaluate_haihe_forecast_emergency_response`、`get_city_rainfall_time_range`
- [x] 分析 `evaluate_haihe_forecast_emergency_response_core` 抛出 BusinessException 的常见原因（EC 预报文件缺失、MUSIC 服务异常等）
- [x] 发现 `from utils.tool_result import _unwrap_tool_result` 在从仓库根运行测试时因 `utils` 模块不可达而失败
- **Status:** complete

### Phase 2: Implementation Planning
- [x] 确定修复范围：仅改进工具失败时的用户体验与可观测性，不改动业务判定逻辑
- [x] 决定异常处理策略：区分“数据/服务不可用”与“代码异常”，避免单工具失败直接弹通用错误
- [x] 决定导入修复方式：测试运行遵循 CLAUDE.md，从 `chainlitexam/` 目录启动；本次不改模块导入
- [x] 规划新增测试：工具失败时 observation_text 格式、测试可导入性
- **Status:** complete

### Phase 3: Implementation
- [x] 改进 `_run_tool_round` 异常处理：移除通用 `cl.Message`，改为 ToolMessage 交给 planner
- [x] 控制台日志与 `tool_step.output` 均使用 `_scrub_internal_data` 脱敏
- **Status:** complete

### Phase 4: Testing & Verification
- [x] 新增 `test_run_tool_round_failure_records_tool_message_without_generic_error` 单元测试
- [x] 运行 `python -m pytest tests/ -v`：51 passed
- [x] 运行 `python tests/test_fast_paths.py`：18/18 passed
- **Status:** complete

### Phase 5: Code Review & Simplification
- [x] 使用 `code-review` 技能扫描改动（Angle A-E）
- [x] 使用 `code-simplifier` 清理测试 mock：复用 `stubs.py` 的 `chainlit.Step`
- [x] 处理 review 发现的可操作问题：控制台日志脱敏
- **Status:** complete

### Phase 6: Final Verification & Documentation
- [x] 使用 `superpowers:verification-before-completion` 确认验证结果
- [x] 更新 `CLAUDE.md` 相关说明
- [x] 使用 `claude-mem` 记录关键决策
- **Status:** complete

### Phase 7: Additional MCP Tool Fixes (from production logs)
- [x] 分析用户提供的生产日志，识别 `safe_emergency_response` 和 `evaluate_haihe_forecast_emergency_response` 的真实异常
- [x] 修复 `evaluate_emergency_response_core` 向 `_observation_fetch_core` 传入错误 `elements`（`DEFAULT_OBS_ELEMENTS` 含 `PRE_1h`，但分钟降水资料码 `SURF_CHN_PRE_MIN` 不支持）导致 `PRE_1h is not config`
- [x] 修复 `safe_evaluate_haihe_emergency_response` 被传入空 `times` 时直接抛错：增加 `_default_observation_times` 兜底为当前整点
- [x] 运行 `py_compile` 验证修改文件语法
- **Status:** complete

### Phase 8: Additional Routing Fix (from user feedback)
- [x] 用户反馈：问应急响应时回答的是预警清单
- [x] 定位根因：planner 同时调用应急响应工具与预警工具时，`warning_bundles` 分支会直接生成预警专用答案并退出，忽略应急判定结果
- [x] 修复 `chainlitexam/message_orchestrator.py`：当本轮 tool_calls 包含应急响应工具时，跳过预警专用组装答案，让 planner 综合生成回答
- [x] 运行回归测试：51 passed
- **Status:** complete

### Phase 9: Forecast Station Fallback Fix (from user feedback)
- [x] 用户反馈：实况能查询成功，但预报工具报“没有可用于预报判定的国家站”
- [x] 定位根因：`_forecast_fetch_core` 只按起报时次单点查询站点，若该时次无实况数据则直接失败
- [x] 修复：单点无记录时兜底查询近 6 小时时间范围，并新增 `_deduplicate_latest_per_station` 按站点保留最新记录
- [x] code-review 复审判读通过
- [x] 运行回归测试与 py_compile：通过
- **Status:** complete

## Key Questions
1. 为什么单工具失败要弹通用错误给用户？当前设计是否过度打扰？
2. `BusinessException` 是否应该被特殊处理为“数据不可用”而非“系统错误”？
3. 从仓库根运行测试的导入问题是否也影响 CI？

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| 待补充 | 待补充 |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| `ModuleNotFoundError: No module named 'utils'` | 1 | 计划改为包内相对导入 |
| 工具失败时用户收到通用错误 | 1 | 计划改进异常处理与 ToolMessage 内容 |

## Notes
- 根因文件：`chainlitexam/message_orchestrator.py` 中 `_run_tool_round` 与模块导入
- 关键方法：`_invoke_tool_with_tolerance`、`_run_tool_round`
- 用户原始报错场景：询问“今天要启动应急响应吗”时，`evaluate_haihe_forecast_emergency_response` 查询失败，但最终答案仍能基于降雨数据生成
