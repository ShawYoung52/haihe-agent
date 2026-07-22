# Findings: 流域未来天气按河系回答

## Project Context Findings

### 当前流域天气回答实现
- `chainlitexam/message_orchestrator.py` 中 `_try_basin_weather_fast_path` 使用 `_BASIN_REP_CITIES = ["北京", "天津", "石家庄", "保定", "唐山", "沧州"]` 回答全流域天气。
- `_try_subbasin_forecast_fast_path` 使用 `_SUBBASIN_REP_CITIES` 将 11 个河系映射到代表城市。
- 两者均调用 MCP 工具 `get_city_rainfall_time_range` 获取城市级降雨预报。
- `ENABLE_FAST_PATHS=false` 为默认，当前主路径为 planner LLM + 工具循环。

### 现有数据来源
- `get_city_rainfall_time_range`（`haihe-weather-analyzer-mcp/analyzers/RainfallAnalyzer.py`）:
  - 读取 `city_boundary_shp` 城市边界 shapefile；
  - 通过 `rolling_forecast_grid.resolve_forecast_grid_source` 切换滚动预报 `.nc` 或 EC AIFS tif；
  - 使用 GDAL 栅格化城市边界，计算边界内平均/最大/最小雨量。
- `query_basin_areal_rainfall`（`haihe-weather-analyzer-mcp/tools.py`）:
  - 查询实况面雨量，支持九分区（`haihe_zone_9` 表）。

### 可用分区边界
- 数据库中存在 `haihe_zone_9`、`haihe_zone_11`、`haihe_zone_77`、`haihe_246_zone`、`haihe_zone_32` 等分区表。
- `tools.py:4292-4299` 已经使用这些表进行面雨量分区名 lookup。
- 用户确认九分区边界在数据库中，可直接用于栅格裁剪。

## Design Decisions

### 为什么新增 MCP 工具而不是在 Chainlit 层聚合城市结果？
1. 数据库中已有九分区边界，可直接裁剪降雨网格，结果更精确；
2. 与现有 `get_city_rainfall_time_range` 架构对称，便于维护和复用；
3. Planner 可直接调用，无需额外包装。

### 为什么以 planner-only 路径为主？
- `ENABLE_FAST_PATHS=false` 为默认配置；
- 领导需求面向的是智能体回答质量，靠 prompt 规则引导 planner 更可持续。

## Open Questions / 待确认

1. `haihe_zone_9` 表的 `zone_name` 字段是否包含业务常用名称（如“大清河”“北三河”），还是需要额外映射？
2. 九分区的 SRID 是否为 4326？是否需要坐标变换？
3. 滚动预报网格是否完整覆盖九大分区？若部分分区落在网格外，如何处理？

## Validation Criteria

- 新增工具 `get_river_system_rainfall_forecast` 可返回九大分区平均/最大/最小雨量；
- `prompts.py` 明确引导 planner 对流域/子流域未来天气调用新工具；
- 典型问题手动验证可返回河系主表；
- 单元测试覆盖正常路径、无数据路径、错误路径；
- 不破坏现有 `get_city_rainfall_time_range` 行为。
