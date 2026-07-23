# Task Plan: query_rolling_forecast 流域误路由硬防护

**PLAN_ID:** 2026-07-22-rolling-forecast-basin-guard
**创建时间:** 2026-07-22
**Goal:** “今天海河流域天气怎么样”不再被路由到 `query_rolling_forecast`（天津 11 站点），保证走 `get_river_system_rainfall_forecast`。

## 根因

- prompt 规则（0999e51）已禁止，但 planner 的工具选择同时受**工具描述**影响。
- `query_rolling_forecast` docstring（`haihe_mcp_tools.py:2734`）写"适合未来天气…等预报类问题"、"未说明地点时默认查询天津全部区域"，无任何流域排除 → LLM 把流域天气问题当综合天气查。
- 系统 prompt 禁令 vs 工具描述邀请，后者在工具选择中权重更高，导致"以天津地区为代表"回答再次出现。
- `get_river_system_rainfall_forecast` docstring 例句只有"明天"，与之前 prompt 例句同样的缺口。

## 修复策略（纵深防御）

1. **工具描述层**：`query_rolling_forecast` docstring 顶部加显式排除条款；`get_river_system_rainfall_forecast` docstring 补"今天"例句与"无论今天、明天还是未来"。
2. **运行时硬防护**：`rolling_forecast_service.py` 新增 `is_basin_weather_query()`；`query_rolling_forecast` 包装器在命中流域/河系关键词时抛 `BusinessException`，提示改用 `get_river_system_rainfall_forecast`。planner 收到 ToolMessage 失败后会改调正确工具（tool-failure-handling 机制）。
3. 裸"海河"不纳入守卫（避免"海河边/海河夜景"点位问题误伤），只拦截"海河流域""流域""河系"及具体河系名。

## Phases

- [x] Phase 1 根因定位
- [ ] Phase 2 TDD：`is_basin_weather_query` 单测 + docstring 静态检查
- [ ] Phase 3 验证：MCP 全套 pytest
- [ ] Phase 4 code-review / 简化
- [ ] Phase 5 memory / CLAUDE.md / 提交
