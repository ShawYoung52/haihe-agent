# Task Plan: 流域未来天气按河系回答

**PLAN_ID:** 2026-07-22-basin-weather-by-river-system  
**创建时间:** 2026-07-22  
**Goal:** 为海河流域气象问答智能体新增按河系（九分区）回答流域/子流域未来天气的能力，代表城市仅作为补充。

## Current Phase

Phase 1: Planning（进行中）

## Phases

### Phase 1: Planning
- [x] 需求澄清：确认以河系为主、城市为辅，覆盖九大分区
- [x] 撰写设计文档 `docs/superpowers/specs/2026-07-22-basin-weather-by-river-system-design.md`
- [x] 用户审查并批准设计文档
- [x] 创建 task_plan.md / findings.md / progress.md
- [x] 明确需要修改的文件和接口
- [ ] 列出详细测试用例与验证步骤
- **Status:** in_progress

### Phase 2: MCP 新工具实现
- [ ] 在 `haihe-weather-analyzer-mcp/tools.py` 新增 `get_river_system_rainfall_forecast` 工具
- [ ] 实现 `_load_zone_boundaries_from_db` 从 `haihe_zone_9` 读取分区边界
- [ ] 复用/抽取栅格裁剪统计逻辑，支持滚动预报 `.nc` 与 EC AIFS tif
- [ ] 处理错误路径：数据库异常、无边界、无预报文件、部分河系统计失败
- [ ] 在 `haihe-weather-analyzer-mcp/server.py` 注册新工具
- [ ] 编写单元测试 `test_river_system_rainfall_forecast.py`
- **Status:** pending

### Phase 3: Chainlit 集成
- [ ] 在 `chainlitexam/chain_gzt.py` 中注册并暴露新 MCP 工具
- [ ] 更新 `chainlitexam/prompts.py`：
  - [ ] 子流域未来天气查询规范改为优先调用新工具
  - [ ] 流域预报规则明确禁止 `query_rolling_forecast`，指定新工具
  - [ ] 给出河系主表 + 城市补充的回答格式示例
- [ ] 更新 `chainlitexam/tests/test_thinking.py` 或新增 prompt 规则静态检查
- **Status:** pending

### Phase 4: Testing & Verification
- [ ] MCP 层单元测试通过
- [ ] Chainlit 层 pytest 通过
- [ ] 手动验证典型问题：
  - [ ] “海河流域明天天气怎么样” → 返回九大分区河系主表
  - [ ] “大清河流域未来三天天气” → 返回大清河河系数据
  - [ ] “海河流域未来一周天气” → 支持 1-7 天河系预报
- [ ] 运行 `superpowers:verification-before-completion`
- **Status:** pending

### Phase 5: Code Review & Simplification
- [ ] 运行 `code-review` 审查变更
- [ ] 运行 `code-simplifier` 清理重复与冗余
- [ ] 修复审查发现的问题
- **Status:** pending

### Phase 6: Documentation & Memory
- [ ] 更新 `CLAUDE.md` 中关于流域/子流域未来天气的规则
- [ ] 更新 planning 文件（task_plan / progress / findings）
- [ ] 写入 claude-mem：流域未来天气以河系级预报为主、城市为辅
- [ ] 使用 `superpowers:finishing-a-development-branch` 收尾
- **Status:** pending

## Key Questions

1. 九分区的名称在 `haihe_zone_9` 表中是否包含“海河”“北三河”“永定河”等业务常用名称？（需实现时确认）
2. `get_city_rainfall_time_range` 的栅格统计逻辑是否应抽取为共享函数，还是直接复制后调整？（实现时评估重复度）
3. 子流域问题（如“大清河”）应只返回对应分区，还是返回全部分区但高亮该分区？（设计：只返回对应分区，更聚焦）

## Decisions Made

| Decision | Rationale |
|----------|-----------|
| 新增独立 MCP 工具 | 与现有 `get_city_rainfall_time_range` 架构对称，planner 可直接调用 |
| 使用 `haihe_zone_9` 数据库边界 | 已确认边界在数据库中，结果比城市聚合更精确 |
| 九大分区为默认展示维度 | 与面雨量口径一致，业务认知统一 |
| 城市明细作为补充 | 满足领导要求的同时保留城市细节 |
| 不重点改造 fast path | `ENABLE_FAST_PATHS=false` 为默认，planner-only 为主路径 |
| 用户侧只输出业务口径 | 不暴露数据库表名、工具参数、文件名等后端细节 |

## Risks

| Risk | Mitigation |
|------|------------|
| `haihe_zone_9` 表无 geometry 或名称不匹配 | 实现时先查询表结构，必要时降级到城市聚合 |
| 栅格计算逻辑复用引入回归 | 保持 `get_city_rainfall_time_range` 不变，新增代码独立测试 |
| Prompt 更新后 planner 仍调旧工具 | 静态检查 + 手动验证典型问题 |
| 新增工具超时 | 设置合理超时，部分失败时返回成功河系 |

## Files to Modify

- `haihe-weather-analyzer-mcp/tools.py`
- `haihe-weather-analyzer-mcp/server.py`
- `haihe-weather-analyzer-mcp/tests/test_river_system_rainfall_forecast.py`（新增）
- `chainlitexam/chain_gzt.py`
- `chainlitexam/prompts.py`
- `chainlitexam/tests/test_thinking.py`（或新增测试）
- `CLAUDE.md`
- `.planning/2026-07-22-basin-weather-by-river-system/*`
- `memory/*.md`（claude-mem）
