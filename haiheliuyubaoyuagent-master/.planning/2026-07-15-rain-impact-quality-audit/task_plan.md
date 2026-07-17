# Task Plan: 暴雨影响河流代码质量全面审计

**PLAN_ID:** 2026-07-15-rain-impact-quality-audit  
**创建时间:** 2026-07-15  
**Goal:** 全面审计 `rainfall_impact_geojson.py` 及相关调用链的逻辑正确性、代码质量、重复/遗漏/孤立河段问题，并修复根因。

## Current Phase
Phase 1: Requirements & Discovery

## Phases

### Phase 1: Requirements & Discovery
- [x] 收集用户观察到的所有异常现象（重复河段、缺失下游、孤立河段）
- [x] 梳理暴雨影响河流的完整数据流：站点 → 直接河段 → pkl 图边 → 下游追踪 → GeoJSON
- [x] 明确每个异常现象的判断标准和期望行为
- **Status:** complete

### Phase 2: Root Cause Analysis
- [x] 使用 `superpowers:systematic-debugging` 系统分析三类异常
- [x] 检查 `_query_direct_rows`、`_query_downstream_rows`、`_build_river_geojson`、`_drop_downstream_covered_by_direct`、`_find_direct_graph_starts` 等核心函数
- [x] 确认重复、遗漏、孤立的产生机制
- **Status:** complete

### Phase 3: Fix Implementation
- [x] 对根因实施最小、可追溯的修复
- [x] 保持现有测试通过，补充回归测试
- **Status:** complete

### Phase 4: Code Review & Simplification
- [x] 使用 `code-review` 技能扫描改动
- [x] 使用 `code-simplifier` 清理代码
- **Status:** complete

### Phase 5: Verification
- [x] 使用 `superpowers:verification-before-completion` 运行全量测试
- [ ] 使用内网样本验证异常是否消除（需用户配合重新运行）
- **Status:** complete (pending user re-run)

### Phase 6: Documentation & Memory
- [x] 使用 `claude-md-management:revise-claude-md` 更新 CLAUDE.md
- [x] 使用 `claude-mem` 记录关键决策
- **Status:** complete

## Key Questions
1. 重复河段是在哪个阶段产生的（SQL 查询、GeoJSON 构建、去重逻辑）？
2. 直接影响河段有下游但没生成，是起点选择问题还是下游匹配被过滤？
3. 孤立河段是数据对齐问题还是裁剪/去重逻辑问题？
4. `is_luan` 相关改动是否引入了新的边界情况？
5. 现有单元测试是否覆盖了真实 PostgreSQL 的执行路径？

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| 待填写 | 待填写 |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| 待填写 | 待填写 | 待填写 |
