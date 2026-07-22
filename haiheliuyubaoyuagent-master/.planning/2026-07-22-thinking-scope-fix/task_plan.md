# Task Plan: 思考过程地域口径修复

**PLAN_ID:** 2026-07-22-thinking-scope-fix
**创建时间:** 2026-07-22
**Goal:** “🤔 思考过程”叙述的地域范围与用户问题一致；用户未提流域/河系时不得出现“海河流域”。

## 根因

- 思考过程不是模型真实思维链，而是 `THINKING_PROMPT` / `FAST_PATH_THINKING_PROMPT` 让 LLM 扮演"思考助手"单独生成的叙述（planner 规划之前生成）。
- 两个 prompt 开头都是“你是**海河流域**气象问答智能体的思考助手”，且可用数据类型列举偏流域口径，导致任何问题的叙述都往“海河流域”上靠（如“最近会有大暴雨吗”被叙述成“了解海河流域近期是否会出现大暴雨”）。

## Phases

### Phase 1: 根因定位（完成）
### Phase 2: TDD 修复
- [ ] 静态测试：两个 thinking prompt 必须包含“地域范围与用户问题一致/未提流域不得出现海河流域”的约束
- [ ] 修改 `prompts.py` 两个 prompt
### Phase 3: 验证 → code-review → 简化
### Phase 4: CLAUDE.md / claude-mem / 提交

## Decisions

| Decision | Rationale |
|----------|-----------|
| 保留"海河流域气象问答智能体"身份 | 产品名称，无需回避 |
| 新增显式地域一致性规则 | 直接约束叙述范围，比删掉身份信息更精准 |
| 两个 prompt 同步修改 | planner 路径与 fast path 路径同一问题 |
