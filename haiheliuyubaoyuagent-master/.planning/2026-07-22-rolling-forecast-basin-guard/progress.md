# Progress: query_rolling_forecast 流域误路由硬防护

## 2026-07-22

### 已完成
- 根因：prompt 规则已禁止，但 `query_rolling_forecast` docstring 对"天气怎么样"类问题过于吸引（无流域排除、"未说明地点默认天津全部区域"），工具描述在工具选择中权重高于系统 prompt 禁令。
- TDD：先写 6 个测试（函数正/负例 + docstring 静态检查），确认 RED 后实现。
- 修复：
  - `rolling_forecast_service.is_basin_weather_query()`：强信号（海河流域/流域/河系）直接命中；裸河名需无 POI 语境；裸"海河"不算。
  - `haihe_mcp_tools.query_rolling_forecast`：docstring 顶部加排除条款；包装器抛 `BusinessException` 引导改调 `get_river_system_rainfall_forecast`；点位模式（lon/lat）跳过守卫。
  - `tools.py` 河系工具 docstring：补"今天"例句与"无论今天、明天还是未来"。
- code-review：采纳 2 项（点位模式误伤、裸河名 POI 误伤），补测试锁定；其余 3 项评估为可接受（关键词清单为兜底、双调用路径噪音轻微）。
- 验证：MCP 全套 54 通过 / 10 跳过（GDAL/样本）。
- 文档：CLAUDE.md basin-weather 条目补守卫说明；claude-mem `basin-weather-by-river-system.md` 补工具层防护记录。

### 待完成
- 提交；部署后复测"今天海河流域天气怎么样"。
