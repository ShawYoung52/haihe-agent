# Task Plan: M3 优化 Planner-Only 路径

**PLAN_ID:** 2026-07-09-m3-optimize-planner-path  
**Goal:** 完成 `query_decision_weather_for_poi` 工具的注册、提示词增强、测试补充与回归验证，使决策天气 POI 查询在 fast path 关闭时仍能被 planner 准确路由并高质量回答。

## Current Phase
Phase 6: Memory & Documentation — complete

## Phases

### Phase 1: Requirements & Discovery
- [x] 读取 M3 设计文档 `docs/superpowers/specs/2026-07-09-m3-optimize-planner-path-design.md`
- [x] 读取 M3 实施计划 `docs/superpowers/plans/2026-07-09-m3-optimize-planner-path-plan.md`
- [x] 确认骨架文件 `chainlitexam/tools/decision_weather.py` 已存在并了解其依赖
- **Status:** complete

### Phase 2: Planning & Structure
- [x] 复用现有 `docs/superpowers/plans/2026-07-09-m3-optimize-planner-path-plan.md` 作为执行基础
- [x] 识别剩余工作：注册工具、增强提示词、补充测试、回归验证
- **Status:** complete

### Phase 3: Implementation
- [x] **Task 2** 在 `chainlitexam/chain_gzt.py` 注册 `build_decision_weather_tools(answer_chain, tools, callbacks)`
- [x] **Task 3** 在 `chainlitexam/prompts.py` 增加决策天气 POI 查询规范并修正 `query_rolling_forecast`/`search_poi` 描述
- [x] 新增 `chainlitexam/tools/decision_weather_core.py` 存放共享 helper，简化 `message_orchestrator.py`
- [x] 修复 `_decision_weather_prefilter` 误过滤“地点+时间”查询的问题
- [x] 在 `TOOL_DISPLAY_NAMES` 中补充 `query_decision_weather_for_poi` 展示名
- **Status:** complete

### Phase 4: Testing & Verification
- [x] 运行新增测试 `tests/test_decision_weather_tool.py`
- [x] 运行完整回归套件（`ENABLE_FAST_PATHS=false` 与 `true` 两种模式）
- [x] 运行 fast path AST 检查 `tests/test_fast_paths.py`
- **Status:** complete

### Phase 5: Code Review & Completion
- [x] 使用本地 code review agent 扫描改动并修复问题（prefilter、prompt 编号、search_poi 描述、TOOL_DISPLAY_NAMES、测试覆盖）
- [x] 使用 `code-simplifier:code-simplifier` 提取 `decision_weather_core.py`
- [x] 使用 `superpowers:verification-before-completion` 完成最终验证
- [x] 提交改动
- **Status:** complete

### Phase 6: Memory & Documentation
- [x] 使用 `claude-mem` 记录 M3 关键决策到项目记忆
- [x] 更新 `.planning/2026-07-09-m3-optimize-planner-path/progress.md`
- **Status:** complete

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| 复用 `docs/superpowers/plans/2026-07-09-m3-optimize-planner-path-plan.md` | 计划已完整，无需重复制定 |
| 使用 isolated plan 目录 | 保留上一个已完成任务的 root planning 文件 |
| 不删除原 `DecisionWeatherQAService` | 设计文档要求保留，便于回退 |
| 将 decision weather helper 提取到 `tools/decision_weather_core.py` | 避免 tool 直接依赖 orchestrator 内部实现，减少循环依赖 |
| 修复 `_decision_weather_prefilter` 允许“地点+时间”查询 | 否则 prompt 示例会被自己的前置过滤拒绝 |
| Prompt 新增 section 编号为 `### 4`，原知识库 section 顺延为 `### 5` | 保持章节顺序清晰 |

## Errors Encountered
| Error | Resolution |
|-------|------------|
| 之前任务因 token 上限（262144）中断 | 本次拆分多步骤并使用子代理/任务列表控制上下文 |
| `_extract_slots` 在 tool 中缺少 `await` | 添加 `await`；测试 happy path 暴露 |
| `_decision_weather_prefilter` 拒绝 prompt 中的“明天/周末”示例 | 改为仅在“纯时间词+无地点/机构意图”时拒绝 |
| `langchain_core.tools` 未在测试 stubs 中覆盖 | 在测试文件内手动 stub `@tool` |
