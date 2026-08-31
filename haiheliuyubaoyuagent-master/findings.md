# 路由摸底发现（2026-08-31）

## 探针方法

对 docx 50 条 + 天河补充逐条跑 `_route_simple_weather_query` / `_route_tianhe_knowledge_query`（含 `_route_tianhe_fixed_catalog_query` 固定目录前置）。

## 关键事实

1. **天河固定目录已覆盖用户粘贴的全部 48 条**（`chainlitexam/tools/tianhe_fixed_qa_catalog.py` 的 `TIANHE_FIXED_QA_QUESTIONS`）——包括洗车/穿衣/晾晒/带伞/副高/面雨量/短时强降水/雷电防御/高温危害/冰雹/双偏振雷达/自动站/卫星/雾霾/夏天多雨/打雷下雨/预警发布流程/天气会商/面雨量计算/MICAPS/系统问答 6 条/降雨对交通影响/当前湿度/今日雨情。**天河侧（需求 2）已接线，无需新增目录。**
2. docx 与天河列表的重复对应：科普类（暴雨防范/短时强降水/暴雨形成/雷电防御/自动站/卫星/雾霾）、统计气象 2 条、系统问答 5/6 条、降雨对交通影响 —— 已全部在天河目录。
3. docx 有但天河目录没有的：
   - "天津当前天气实况"（目录里是"天津当前的天气情况"——字面不同，走 PLANNER→本地实况工具，合理）
   - "天津未来三天天气？/未来一周我市天气怎么样？"→ SIMPLE 滚动预报 ✅
   - "今天晚上蓟州的天气怎么样？"→ SIMPLE ✅
   - "当前有哪些预警？"→ PLANNER→本地预警工具 ✅
   - "有什么使用小技巧？"→ PLANNER（系统问答变体，目录只有"我该怎么向你提问？"）

## 标黄 5 条的路由现状

| 问题 | 路由 | 风险点 |
|---|---|---|
| 明天泃河有雨吗？ | PLANNER | 泃河是蓟运河支流，需确认 planner 选 get_river_system_rainfall_forecast 且河系映射覆盖 |
| 今天晚上滦河有雨吗？ | PLANNER | 滦河在九大河系内，同上待验证；时段化"晚上" |
| 今天蓟州可能有哪些风险？ | PLANNER | 需确认 planner 选 query_risk_warning/区域风险，且答案是"风险"不是"天气" |
| 未来三天于桥水库降雨预报？ | SIMPLE→decision_weather POI | 于桥水库能否被 search_poi 命中（自然地物可能不入库，参照密云水库踩坑） |
| 盘山景区未来两天天气？ | SIMPLE→decision_weather POI | 盘山 POI 命中与景区类注意事项 |

## 待验证

- 九大河系（get_river_system_rainfall_forecast）覆盖范围：泃河/滦河是否在分区表内
- 于桥水库 POI 可命中性
- planner 对"X河有雨吗"的工具选择（prompt 规则是否引导到河系工具）
