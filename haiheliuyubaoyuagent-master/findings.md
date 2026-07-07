# Findings: 路由问题诊断

**日期:** 2026-07-07

## R1: 周末快速路径过严的前置条件

`_try_weekend_activity_fast_path` 三个前置条件（AND 逻辑）：

1. **周末关键词** (line 3262): `["周末", "周六", "周日", "星期日", "星期天", "周六日"]`
2. **流域范围** (line 3268): `["海河流域", "海河", "流域"]` — **过于严格**
3. **活动意图** (line 3273): `["户外", "活动", "出行", ...]` OR regex `(适合|能|可以|好不好|行不行|好).{0,6}(吗|么|呢)`

"本周末天气如何" → 条件2不满足 → 返回 False → 跌落 Planner LLM → 误调 POI 工具

## R2: 快速路径执行顺序

当前顺序 (line 4340-4370):
```
rainfall_img → river_plot → rainfall_analysis → city_avg_rainfall → 
rain_duration → today_rainfall → weekly_forecast → heavy_rain_check → 
subbasin_forecast → basin_areal_rainfall → weekend_activity → 
basin_weather → general_weather → water_level → emergency_response → 
poi → risk_warning → rainstorm_impact_time → planner_LLM
```

"本周末天气如何" 在所有快速路径均失败后落入 planner LLM。

## R3: 用户期望

用户问"本周末天气如何"，期望得到：
- 周六、周日两天的天气预报
- 温度、降雨情况
- 类似 `_try_weekend_activity_fast_path` 的输出格式

不应返回：单一观测站实况数据
