# Task Plan: 将暴雨影响河流专题图工具转化为问答智能体内容并提升代码质量

**PLAN_ID:** 2026-07-09-convert-rainfall-impact-tool-to-qna-agen  
**Goal:** 把 `haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py` 的能力接入 `chainlitexam` 本地工具集，减少牵引智能体依赖；同时针对问答智能体前后端和 MCP 做一次聚焦的代码质量治理（减少冗余与嵌套）。

## Current Phase
Phase 3 — Implementation

## Phases

### Phase 1: Requirements & Discovery
- [x] 用户要求把牵引智能体相关的 `fixed_rainfall_impact_tool.py` 转化为问答智能体内容
- [x] 用户要求遍历 `chainlitexam` 前后端 + `haihe-weather-analyzer-mcp`，控制代码质量、减少冗余与嵌套
- [x] 使用 Agent 扫描两个子系统，得到高优先级质量问题清单
- **Status:** complete

### Phase 2: Planning & Structure
- [x] 确定本次聚焦范围：
  1. 新增 `chainlitexam/tools/rainfall_river_impact.py` 本地工具，复用 MCP 参数解析与返回格式，调用外部 `hhlyqyxt-master/utils/rainfall_impact_geojson.py`  builder（dev 环境缺失时给出友好提示）。
  2. 重构 `haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py` 自身：抽离响应格式化、简化注销逻辑、降低嵌套。
  3. 治理 `chainlitexam` 中 `_unwrap_tool_result` 的四处重复实现，提取到 `chainlitexam/utils/tool_result.py`。
- [x] 明确不处理：巨型 `register_tools` / `register_haihe_tools` 拆分、fast path 大重构、前端深度重构。这些作为后续迭代项。
- **Status:** complete

### Phase 3: Implementation
- [ ] **Task 1** 新增 `chainlitexam/tools/rainfall_river_impact.py` 并在 `chain_gzt.py` 注册
- [ ] **Task 2** 更新 `chainlitexam/prompts.py` 路由指引
- [ ] **Task 3** 重构 `haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py`
- [ ] **Task 4** 提取 `chainlitexam/utils/tool_result.py` 并替换重复实现
- **Status:** in_progress

### Phase 4: Testing & Verification
- [ ] `python -m py_compile` 所有改动文件
- [ ] 运行 `chainlitexam` 回归套件
- [ ] 运行 `superpowers:verification-before-completion`
- **Status:** pending

### Phase 5: Delivery
- [ ] `code-review` + `code-simplifier` 扫描修复
- [ ] 提交并 push 到 GitHub
- [ ] 更新 claude-mem 与 planning 文件
- **Status:** pending

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| 本地工具仍调用外部 `hhlyqyxt-master/utils/rainfall_impact_geojson.py` | 真正的河网拓扑计算不在当前仓库，无法短期自包含；先把“壳”和调用逻辑迁到 chainlitexam，降低对 MCP 的依赖 |
| 保留 MCP 版本作为过渡期 fallback | 避免生产环境直接切换风险 |
| 本地工具名使用 `local_get_affected_river_network_by_rainfall` | 与 `local_analyze_rainfall_by_time` 命名风格一致，避免与 MCP 工具名冲突 |
| 仅治理 `_unwrap_tool_result` 重复 | 影响面可控、收益明确；更大的 fast path / register_tools 重构单独规划 |

## Errors Encountered
| Error | Resolution |
|-------|------------|
| 外部 builder 在 dev 仓库不存在 | 本地工具运行时给出清晰提示，不阻塞 import/语法检查 |