# Findings & Decisions: 工具失败通用报错与测试导入问题

**PLAN_ID:** 2026-07-13-fix-tool-error-handling  
**日期:** 2026-07-13

## Requirements
- 用户询问“今天要启动应急响应吗”时，问答智能体不应在已生成最终答案后仍展示“数据查询遇到问题，请稍后重试。”
- 修复用户后续提供的生产日志中暴露的 `safe_emergency_response` 与 `evaluate_haihe_forecast_emergency_response` 底层异常

## Research Findings
- 错误文案位于 `chainlitexam/message_orchestrator.py:2529`，在 `_run_tool_round` 的通用 `except Exception` 块中
- 当 `evaluate_haihe_forecast_emergency_response` 因 EC 预报文件缺失/无国家站抛出 `BusinessException` 时，该通用块会：
  1. 在控制台打印 `[工具错误] {tool_name}: {e}`
  2. 向用户发送 `cl.Message(content="数据查询遇到问题，请稍后重试。")`
  3. 将 `observation_text` 设为失败信息并加入 messages
- `safe_evaluate_haihe_emergency_response` 被调用时 `times` 可能为空字符串，直接触发 `times 不能为空`
- `evaluate_emergency_response_core` 调用 `_observation_fetch_core` 时传入 `DEFAULT_OBS_ELEMENTS`（含 `PRE_1h`），但 `_observation_fetch_core` 默认使用分钟降水资料码 `SURF_CHN_PRE_MIN`，导致 MUSIC API 返回 `PRE_1h is not config`
- `get_city_rainfall_time_range` 对 `hour%6==2` 的校验已在 `_invoke_tool_with_tolerance` 中做容错重试，但其他异常仍会透传
- 测试导入问题：按 CLAUDE.md 从 `chainlitexam/` 目录运行测试即可规避，本次未改动模块导入

## Root Cause
1. **通用错误弹窗**：`_run_tool_round` 对任何工具异常都向用户发送固定错误消息，即使该工具失败不影响最终回答
2. **实况应急响应工具元素不匹配**：`evaluate_emergency_response_core` 向分钟降水接口传入了小时要素 `PRE_1h`
3. **safe_emergency_response 空 times 未兜底**：工具入口未处理 LLM 传入空 `times` 的情况
4. **应急响应问题被预警专用答案抢占**：`process_message` 中一旦 `warning_bundles` 非空就生成预警专用答案并退出，导致同时调用的应急响应工具结果被忽略

## Fix Plan
1. `chainlitexam/message_orchestrator.py`：
   - 改进 `_run_tool_round` 异常处理：保留控制台日志，不再发送 `cl.Message`，改为结构化 `ToolMessage`
   - 控制台日志与 `tool_step.output` 均使用 `_scrub_internal_data` 脱敏
   - 新增 `EMERGENCY_RESPONSE_TOOL_NAMES`；当本轮 tool_calls 包含应急响应工具时，跳过 `warning_bundles` 专用答案分支
2. `haihe-weather-analyzer-mcp/haihe_mcp_tools.py`：
   - `evaluate_emergency_response_core` 调用 `_observation_fetch_core` 时传入 `DEFAULT_MIN_PRE_ELEMENTS`
3. `haihe-weather-analyzer-mcp/custom_tools/safe_emergency_response_tool.py`：
   - 新增 `_default_observation_times`，空 `times` 时默认取当前整点
4. `chainlitexam/tests/test_message_orchestrator.py`：
   - 新增单元测试验证工具失败时不发送通用错误消息

## Verification
- `python -m pytest tests/ -v`：51 passed
- `python tests/test_fast_paths.py`：18/18 passed
- `python -m py_compile` 所有改动文件：OK

## Technical Decisions
| Decision | Rationale |
|----------|-----------|
| 不在工具失败时单独发送 `cl.Message` | 避免用户收到重复/冲突信息；让 LLM 在最终回答中统一说明 |
| 保留失败 observation 进入 messages | 让 planner 知晓工具失败，可基于其他工具结果生成回答或说明 |
| `_scrub_internal_data` 继续用于清理异常文本 | 防止内部 IP、路径、账号信息泄露到 LLM 上下文 |
| 未改动 `from utils.tool_result` 导入 | 生产环境通过 `cd chainlitexam` 启动，CLAUDE.md 已规定测试命令从该目录执行；强行改相对导入会破坏生产 |
| 分钟降水接口使用 `DEFAULT_MIN_PRE_ELEMENTS` | 与 `SURF_CHN_PRE_MIN` 资料码匹配，消除 `PRE_1h is not config` |
| safe 工具入口对空 times 兜底 | LLM 可能遗漏必填参数，兜底到当前整点可提高容错 |
