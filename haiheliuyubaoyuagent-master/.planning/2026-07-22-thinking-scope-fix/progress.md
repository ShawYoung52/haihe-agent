# Progress: 思考过程地域口径修复

## 2026-07-22

### 已完成
- 根因：思考过程是 THINKING_PROMPT / FAST_PATH_THINKING_PROMPT 让 LLM 扮演"思考助手"单独生成的叙述（非真实思维链）；两个 prompt 身份行硬编码"海河流域"，导致任何问题都被叙述成流域口径。
- TDD：先写静态测试（锚定正则 + 禁用条款断言），再改 prompt。
- 修复：身份行去掉"海河流域"，新增"地域口径必须与用户问题一致；未提流域/河系时思考中不得出现'海河流域'字样"。
- code-review：采纳 3 项（身份行引导违规、正则未锚定、断言空转）；第 4 项（WEATHER_ASSISTANT_PROMPT 同规则）超出本次范围——正式回答口径已由路由规则约束。
- 验证：prompt 相关测试全过；全套 63 通过 + 1 失败（同事并发修改 `_run_tool_round` 返回值 arity 4，与本修复无关，需其更新 test_message_orchestrator.py 解包）。
- 提交：98c1907。

### 注意
- 同事正在工作区并发开发：`tools/rolling_forecast_response.py`、`tools/warning_workflow.py`、`tools/decision_weather_fast_path.py` 新文件 + `_run_tool_round` 返回 4 元组。`tests/test_message_orchestrator.py::test_run_tool_round_failure_records_tool_message_without_generic_error` 需同步解包 4 个值。
