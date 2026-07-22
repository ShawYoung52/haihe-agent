# Progress: 流域天气回答两个 Bug 修复

## 2026-07-22

### 已完成
- 根因定位：
  - Bug 1：`message_orchestrator.py:1661` `_build_thinking_summary` 中关键词"河流"是"海河流域"的子串，流域天气问题误命中河网可视化前缀。
  - Bug 2：`analyzers/RainfallAnalyzer.resolve_forecast_raster_path` 滚动预报分支忽略 `start_time`，且只物化单时次 TP1H 切片（1 小时降水），未做窗口累计 → 今天/明天数据完全相同且口径与 EC rain_total 不一致。
- TDD 修复：
  - `河流(?!域)` 负向断言 + 预报分支补"天气"；新增 3 个 thinking_summary 测试。
  - `rolling_forecast_grid.compute_lead_hours`（半开区间）+ `materialize_rolling_forecast_accumulated`（`sum(min_count=1)`）+ `resolve_forecast_raster_path` 接入；新增 9 个 MCP 测试。
  - `_write_tp_to_geotiff` 抽取共用，`materialize_rolling_forecast_to_files` 复用。
- code-review 修复：闭区间 25 时次→半开 24 时次；NaN→0 改 min_count=1；banker's rounding 改 +0.5 取整；`driver.Create` None 守卫；`has_chart` 6 连 replace 简化为单次 replace（顺带修复预警分支漏网）；except 加 exc_info。
- 验证：MCP 48 通过 / 10 跳过（无 GDAL/样本），Chainlit 60 通过，py_compile 干净。
- 文档：CLAUDE.md 更新（lead/累计语义 + 关键词陷阱），claude-mem `basin-weather-by-river-system.md` 补充窗口语义。

### 待完成
- 提交；部署环境复测"今天/明天海河流域天气"。
