# Task Plan: 修复暴雨区域GIS受影响河流显示缺失

**PLAN_ID:** 2026-07-13-rain-impact-rivers  
**创建时间:** 2026-07-13  
**Goal:** 修复牵引智能体中暴雨区域未在十四所GIS显示受影响河流的问题，并同步修复问答智能体对该能力的调用与结果展示。

## Current Phase
Phase 9: Downstream Tracking Distance Investigation

## Phases

### Phase 1: Requirements & Discovery
- [x] 读取牵引智能体工具目录 `hhlyqyxt-master/utils` 中的相关模块
- [x] 定位“暴雨影响河流”/“暴雨区域GIS展示”相关函数与数据流
- [x] 复现/确认问题：东边暴雨区域存在，但十四所GIS无受影响河流
- [x] 识别问答智能体中调用牵引智能体结果的入口
- **Status:** complete

### Phase 2: Root Cause Analysis
- [x] 追溯暴雨区域到受影响河流的计算逻辑
- [x] 对比正常场景与异常场景的数据差异
- [x] 形成明确假设：`_find_direct_graph_starts` 过于严格，pkl/full_v5 对齐偏差导致无下游起点，直接河段稀疏时整体空白
- **Status:** in_progress

### Phase 3: Fix Traction Agent
- [x] 在 `hhlyqyxt-master/utils` 中实施最小修复
- [x] 确保暴雨区域能正确关联到受影响河流
- **Status:** complete

### Phase 4: Verify Traction Agent
- [x] 运行牵引智能体相关测试/脚本验证修复
- [x] 确认东边暴雨区域示例能返回受影响河流
- **Status:** complete

### Phase 5: Fix Q&A Agent Integration
- [x] 检查问答智能体 `chainlitexam/message_orchestrator.py` / `chain_gzt.py` 中对牵引能力的调用
- [x] 修复因牵引智能体改动导致的接口不匹配或结果解析问题
- [x] 确保问答智能体能正确展示受影响河流信息
- **Status:** complete

### Phase 6: Verify Q&A Agent
- [x] 运行问答智能体测试套件
- [x] 验证暴雨影响河流相关 fast path / planner 路径
- **Status:** complete

### Phase 7: Code Review & Simplification
- [x] 使用 `code-review` 技能扫描改动
- [x] 修复 code-review 发现的 4 个问题（兜底边隔离、空响应结构、direct_match_km 读取、测试 `_fn` 修正）
- [x] 使用 `code-simplifier` 简化代码
- **Status:** complete

### Phase 8: Final Verification & Documentation
- [x] 使用 `superpowers:verification-before-completion` 完成最终验证
- [x] 更新 `CLAUDE.md` 相关说明（添加暴雨影响河流关键文件与跨仓库同步提醒）
- [x] 使用 `claude-mem` 记录关键决策（worker 模式下写入 memory 文件并更新 MEMORY.md 索引）
- **Status:** complete

### Phase 9: Downstream Tracking Distance Investigation
- [ ] 复现/理解用户感知的下游追踪距离问题
- [ ] 分析实际输出数据，定位具体问题点
- [ ] 若确认存在 bug，实施修复并验证
- **Status:** in_progress

## Key Questions
1. 牵引智能体中“暴雨影响河流”的逻辑在哪几个文件？
2. 十四所GIS需要什么样的输入才能渲染受影响河流？
3. 东边暴雨区域示例中，是完全没有返回河流，还是返回了但GIS未渲染？
4. 问答智能体如何调用牵引智能体的该能力？接口是否需要同步调整？

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| 先修牵引智能体，再修问答智能体 | 问答智能体依赖牵引智能体输出，根因在牵引侧 |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| - | - | - |
