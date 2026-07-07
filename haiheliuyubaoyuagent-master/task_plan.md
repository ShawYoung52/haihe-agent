# Task Plan: 问答智能体开发

**更新时间:** 2026-07-07
**状态:** complete

## 已完成任务

### Task 1: 快速路径冲突修复 ✓
- 周末快速路径放宽流域限制
- DecisionWeather 调度位置修正（pos 8 → pos 18）
- 17 路径触发条件审计，修复 3 个关键冲突
- prefilter 时间词排除、general_weather 周末关键词排除

### Task 2: 思考过程实时流式展示 ✓
- planner_llm streaming=False → True
- 新增 `astream_planner_think()` + `_process_planner_stream()`
- 所有 token 实时流到 ReasoningStep → DeepSeek 风格逐字展示
- 60s 超时 + 重试
- Pydantic v2 tool_calls=None 修复
- 清理 `<think>` 标签解析器死代码、`_extract_think_content` 死代码

### Task 3: 代码审查 + 清理 + 验证 ✓
- code-review（low）：3 发现 → 已修复
- simplify（--fix）：超时重试、死代码删除
- MCP server 导入路径修复（4 文件）
- UTF-8 BOM 全项目清理（19 文件）

### Task 4: Bug 修复 ✓
- AIMessage tool_calls=None → Pydantic v2 拒绝（def32c4）
- 流式思考空展示 → 去掉 `<think>` 标签依赖，全部流式输出（bbfcb5b）

## 最终调度顺序
```
 1. rainfall_img
 2. emergency_response
 3. affected_river_network
 4. river_plot
 5. rainfall_analysis
 6. city_avg_rainfall
 7. warning_fact
 8. rain_duration
 9. today_rainfall
10. weekly_forecast
11. heavy_rain_check
12. subbasin_forecast
13. basin_areal_rainfall
14. weekend_activity       ← 新增非流域分支
15. basin_weather
16. water_level
17. general_weather
18. decision_weather       ← 从 pos 8 移至此
```

## 提交历史
```
bbfcb5b fix: stream all tokens as thinking, remove broken think-tag parser
def32c4 fix: AIMessage tool_calls=None rejected by Pydantic v2
284b682 fix: MCP server import path corrections
7a4b98e fix: add timeout+retry to astream_planner_think, remove dead code
dfc57f7 feat: real-time streaming think process display (DeepSeek-style)
4d25ebb docs: finalize weekend routing fix task plan
be7ab4b fix: reorder fast path dispatch to prevent DecisionWeather from preempting weather queries
1a7cca0 docs: comprehensive fast path conflict audit results
b87cb8c fix: add '雨' to weekend weather intent keywords
70ba6ee fix: weekend fast path too restrictive, missed non-basin weather queries
8e272e9 fix: remove UTF-8 BOM from 19 Python files causing SyntaxError
```