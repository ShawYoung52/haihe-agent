# Task Plan: 有向图 pkl 与数据库表从 v5 升级到 v6

**创建时间:** 2026-07-14
**当前阶段:** Phase 5

## Goal
将项目代码中所有引用 `river_directed_v5.pkl` 及含 `v5` 版本号的数据库表/字段从 v5 更新为 v6，确保牵引智能体、问答智能体 MCP 工具、本地工具及测试使用一致的最新图版本。

## Current Phase
Phase 5: Code Review, Simplification, Memory Update

## Phases

### Phase 1: Requirements & Discovery
- [x] 全项目搜索 v5 相关引用（文件路径、字符串、配置键、表名）
- [x] 区分必须升级项与可保留项（如历史 diff 文件、git 提交哈希）
- [x] 识别跨仓库依赖：`../hhlyqyxt-master/utils/rainfall_impact_geojson.py`、`haihe-weather-analyzer-mcp/server.py`、`haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py` 等
- [x] 确认 v6 文件/表名命名规范（如 `river_directed_v6.pkl`、表名/字段模式）
- **Status:** complete

### Phase 2: Impact Analysis
- [x] 列出所有需修改文件及对应修改点
- [x] 评估是否有运行时代码动态构造 v5 字符串
- [x] 确认配置项、fallback 路径、默认路径是否需要同步
- [x] 识别测试数据/模拟对象是否需要更新
- **Status:** complete

### Phase 3: Implementation
- [x] 更新代码中的 v5 → v6 引用
- [x] 更新配置文件/默认路径
- [x] 更新测试与桩代码
- [x] 保持 `_resolve_graph_path` 等版本选择逻辑与 v6 命名一致
- **Status:** complete

### Phase 4: Testing & Verification
- [x] 运行 `pytest ../hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py`
- [x] 运行 `python tests/test_fast_paths.py`
- [x] 运行 `cd chainlitexam && pytest tests/ -v`
- [x] 运行 `py_compile` 检查修改文件语法
- [ ] 如果本地存在 v6 pkl/数据库，进行端到端冒烟测试
- **Status:** complete

### Phase 5: Code Review, Simplification, Memory Update
- [x] 使用 code-review / review 技能扫描改动
- [x] 使用 code-simplifier 清理重复版本号硬编码
- [x] 使用 superpowers:verification-before-completion 最终验证
- [x] 更新 CLAUDE.md 与 memory
- **Status:** complete

### Phase 6: Commit
- [x] 整理 git diff
- [x] 按仓库提交规范创建 commit
- [ ] 推送（如用户要求）
- **Status:** complete

## Key Questions
1. v6 pkl 文件实际路径是什么？是否仍放在 `Service/river_directed_v6.pkl`？
2. 数据库中受影响的表名/字段有哪些？是否有视图或存储过程引用 v5？
3. 是否有硬编码的 `v5` 用于非版本语义（如变量名 `full_v5` 是否为数据源标识而非可升级版本号）？
4. `server.py` 中覆盖的 `DEFAULT_DIRECT_GRAPH_MATCH_KM` 与版本升级是否有关？

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| 待补充 | 待搜索分析后填写 |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| - | - | - |

## Notes
- 起始线索：用户已打开 `haihe-weather-analyzer-mcp/server.py`，可能含 v5 硬编码。
- 相关记忆：[[rain-impact-river-defaults]] 提到 `_resolve_graph_path` 优先选择同目录 `river_directed_v5.pkl`。