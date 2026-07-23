# Task Plan: 暴雨影响河流数据对齐（pkl/full_v6）

**PLAN_ID:** 2026-07-16-rain-impact-data-alignment  
**创建时间:** 2026-07-16  
**Goal:** 评估并选择数据侧修复方案，使 `river_directed_v6.pkl` 与 `haihe_river_directed_full_v6` 在几何/拓扑层面充分对齐，从根本上减少 rain impact 输出中的孤立河段、下游断裂和河段跳跃。

## Current Phase
Phase 1: 现状分析与根因确认

## Phases

### Phase 1: 现状分析与根因确认
- [ ] 读取并解析 `E:\fsdownload\rain_impact_result.json.river.geojson`，量化当前异常（孤立段、跳跃段、无下游直接段）
- [ ] 梳理 `E:\tj\line` 下数据生产脚本，理解 pkl 图与 full_v6 表的生成逻辑
- [ ] 明确 pkl edge 与 full_v6 `objectid` 不对齐的具体表现形式
- **Status:** in_progress

### Phase 2: 方案设计与比较
- [ ] 方案 A：清洗 `haihe_river_directed_full_v6`，使每个 `objectid` 对应一条连通河流
- [ ] 方案 B：重新生成 `river_directed_v6.pkl`，使 pkl edge 与 full_v6 子段一一对应
- [ ] 评估两种方案的工作量、风险、对现有代码的影响、可回滚性
- [ ] 给出推荐方案及备选方案
- **Status:** pending

### Phase 3: 详细设计
- [ ] 确定数据清洗/重生成流程、参数、输出格式
- [ ] 明确与现有 `rainfall_impact_geojson.py` 的兼容性要求
- [ ] 设计验证方法（拓扑检查、与 rain impact 输出对比、单元测试）
- **Status:** pending

### Phase 4: 实现（如用户批准）
- [ ] 按推荐方案生成清洗后的数据或新的 pkl
- [ ] 在 `hhlyqyxt-master` / `haihe-weather-analyzer-mcp` 中同步常量与路径
- [ ] 补充回归测试
- **Status:** pending

### Phase 5: 验证
- [ ] 运行现有测试套件
- [ ] 使用内网样本重新生成 rain impact GeoJSON 并对比异常数量
- [ ] 确认无回归
- **Status:** pending

### Phase 6: 文档与记忆
- [ ] 更新 CLAUDE.md 中关于数据对齐的说明
- [ ] 使用 claude-mem 记录最终决策
- **Status:** pending

## Key Questions
1. 当前 GeoJSON 中孤立河段、下游断裂、河段跳跃的精确数量与分布是什么？
2. pkl edge 与 full_v6 几何不对齐的根因是数据录入问题还是算法拆分问题？
3. 清洗 full_v6 与重生成 pkl，哪种方案更能在未来维护中避免复发？
4. 重新生成 pkl 是否需要同时更新 `DIRECTED_GRAPH_FILENAME` 常量及调用方？
5. 数据清洗后是否会影响其他依赖 full_v6 的业务（如水文模型、GIS 展示）？

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| 待填写 | 待填写 |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| 待填写 | 待填写 | 待填写 |
