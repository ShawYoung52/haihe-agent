# Progress: 流域未来天气按河系回答

## 2026-07-22

### 已完成
- 与领导/用户对齐需求：流域未来天气需新增按河系/流域维度回答，城市作为补充。
- 澄清关键问题：
  - 回答模式：河系主表 + 城市明细补充；
  - 覆盖分区：海河九分区（与面雨量口径一致）；
  - 应用范围：全流域 + 子流域未来天气问题；
  - 指标：目前只有降雨数据，仅回答降雨；
  - 时效：支持未来 10 天（240h），典型问题 1-7 天；
  - 边界来源：数据库 `haihe_zone_9` 表；
  - 实现方式：新增 MCP 工具，以 planner-only 路径为主。
- 撰写并批准设计文档：`docs/superpowers/specs/2026-07-22-basin-weather-by-river-system-design.md`
- 创建 planning 文件：task_plan.md、findings.md、progress.md
- Phase 2 实现：
  - 新增 `haihe-weather-analyzer-mcp/river_system_forecast.py`，含边界加载、栅格统计、EC/滚动预报切换；
  - 在 `haihe-weather-analyzer-mcp/tools.py` 注册 MCP 工具 `get_river_system_rainfall_forecast`；
  - 抽取共享栅格统计/数据源解析逻辑到 `analyzers/RainfallAnalyzer.py`，`get_city_rainfall_time_range` 同步复用；
  - 新增单元测试 `test_river_system_rainfall_forecast.py`。
- Phase 3 实现：
  - 更新 `chainlitexam/prompts.py`：子流域规范、流域预报规则、工具列表均指向新工具；
  - 更新 `chainlitexam/message_orchestrator.py`：`TOOL_DISPLAY_NAMES` 增加新工具；
  - 新增 `chainlitexam/tests/test_prompts.py` 静态检查。
- Phase 4 验证：
  - MCP 全量 pytest：39 通过，8 跳过（GDAL 未安装）；
  - Chainlit 全量 pytest：57 通过；
  - 修改文件 py_compile 语法检查通过。
- Phase 5 code-review：发现测试 mock、nodata 处理、坐标系、zone_type 透传等问题，已修复。
- Phase 6 文档与 memory：
  - 更新 `CLAUDE.md` 增加 basin/river-system future weather 约定；
  - 更新 claude-mem：`memory/basin-weather-by-river-system.md` + `MEMORY.md` 索引。

### 待完成
- 部署环境手动验证典型问题（"海河流域明天天气""大清河流域未来三天天气"）。

### 验证结果

| 检查项 | 状态 |
|--------|------|
| 设计文档完成 | ✓ |
| 规划文件创建 | ✓ |
| 用户审批 | ✓ |
| MCP 新工具实现 | ✓ |
| Chainlit prompt 更新 | ✓ |
| MCP 全量 pytest | ✓ 39/39 通过，8 跳过（GDAL） |
| Chainlit 全量 pytest | ✓ 57/57 通过 |
| code-review 问题修复 | ✓ |
| code-simplifier 复用抽取 | ✓ |
| CLAUDE.md 更新 | ✓ |
| auto-memory 更新 | ✓ |
| 部署环境手动验证 | ⏳ |

### 阻塞/风险
- 测试环境缺少 GDAL，栅格统计与 WKB 解析测试被跳过；需在部署环境验证。
- `get_river_system_rainfall_forecast` 返回最新可用滚动预报 cycle 的切片，若用户指定 `start_time` 与最新 cycle 不一致，`fcst_time` 字段仍显示用户请求时间；已按现有 `get_city_rainfall_time_range` 行为保持一致，部署后需确认业务是否接受。
