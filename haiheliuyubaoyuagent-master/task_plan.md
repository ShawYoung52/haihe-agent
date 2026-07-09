# Task Plan: 修复 Chainlit 实时思考前端不可见

**创建时间:** 2026-07-08
**当前阶段:** Phase 2

## Goal
让海河流域气象智能体在 Chainlit 前端像 DeepSeek 一样实时展示“思考过程”可折叠块，而不是只输出最终答案。

## Current Phase
Phase 2: Planning & Structure

## Phases

### Phase 1: Requirements & Discovery
- [x] 复现用户问题：日志显示思考生成成功，但前端无展示
- [x] 检查 `ReasoningStep` 与 Chainlit `cl.Step` 的使用方式
- [x] 检查 `.chainlit/config.toml` 中 `cot` 配置
- [x] 确认 Chainlit 默认 UI 对 `type="llm"` step 及嵌套 stage 的展示规则
- **Status:** complete

### Phase 2: Planning & Structure
- [x] 确定根因：`ReasoningStep.append` 把 token 写进嵌套子 stage，父 step output 为空
- [x] 确定修复方案：让 `append` 始终刷新父 step output；`stage` 仅作为业务阶段标题追加到父 output
- [x] 更新测试断言以匹配新行为
- **Status:** complete

### Phase 3: Implementation
- [x] 修改 `message_orchestrator.py` 中的 `ReasoningStep`
- [x] 调整 `stage()` / `append()` / `close()` 行为
- [x] 确保 planner 与 fast path 的思考流都落到可见 output
- **Status:** complete

### Phase 4: Testing & Verification
- [x] 运行 `test_reasoning_step.py`
- [x] 运行 `test_thinking.py` / `test_thinking_summary.py` / `test_fast_paths.py`
- [x] 红绿回归验证：还原旧代码后新测试失败，恢复修复后通过
- [x] 启动 Chainlit 最小示例 + Playwright 验证 UI：可看到“已使用 🤔 思考过程”折叠块及思考内容
- **Status:** complete

### Phase 5: Code Review & Completion
- [x] 使用 `code-review` 技能检查改动（9 角度扫描 + 关键问题已处理）
- [x] 使用 `superpowers:verification-before-completion` 确认验证结果
- [x] 提交/总结
- **Status:** complete

## Key Questions
1. Chainlit 默认 UI 是否能展示 `type="llm"` 且 output 非空的 step？ → `cot=full` 时应可展示
2. 为什么当前前端看不到？ → token 被写入嵌套子 stage，父 step output 为空
3. 是否需要切换 step type 为 `"tool"`？ → 先保持 `"llm"`，`cot=full` 已启用；若仍不可见再评估

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| 让父 `cl.Step.output` 始终承载完整思考文本 | Chainlit UI 默认只渲染父 step 的 output；子 stage 在当前 CoT 视图下不可见 |
| `stage()` 改为向父 output 追加阶段标题 | 保留业务阶段语义，同时让用户在可折叠块里看到阶段切换 |
| 保留 `type="llm"` | 语义正确，且 `.chainlit/config.toml` 已设置 `cot="full"` |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| - | - | - |

## Notes
- 根因文件：`chainlitexam/message_orchestrator.py` 中 `ReasoningStep` 类
- 关键方法：`append()` 在 `_current_stage` 存在时只更新子 stage；`close()` 时父 output 可能为 "思考完成"
- 下游回调：`chainlitexam/chain_gzt.py` 的 `_process_thinking_stream` / `_process_planner_stream` 调用 `reasoning_step.append(token)`
