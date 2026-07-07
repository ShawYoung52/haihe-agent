# Task Plan: 问答智能体快速路径冲突修复

**创建时间:** 2026-07-07
**状态:** complete

## 已完成

### Phase 1: 周末快速路径放宽流域限制 ✓
- 非流域周末查询默认查天津，流域查询保持不变
- 新增"如何"/"怎么样"/"预报"/"雨"等天气意图关键词

### Phase 2: DecisionWeather 调度位置修正 ✓  
- 从位置 8 移到位置 17（general_weather 之后）
- prefilter 新增时间词排除（"周末"/"今天"/"明天"等）

### Phase 3: 快速路径冲突审查 ✓
- 审计 17 个快速路径触发条件
- 识别 7 个冲突点，修复 3 个关键问题
- general_weather 排除列表新增"周末"/"周六"/"周日"

### Phase 4: 验证 ✓
- Python 语法通过
- 调度顺序：weekend(13) → basin(14) → general(16) → decision_weather(17)

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
14. weekend_activity
15. basin_weather
16. water_level
17. general_weather
18. decision_weather    ← 从 pos 8 移至此
```