# 调研发现

## 项目结构与现有数据流

- `ScheduledTask/stationProcessMin.py` 是现有 5 分钟牵引调度脚本：
  - 每 5 分钟读取 `SURF_CHN_MUL_MIN` 分钟降水数据；
  - 重采样为 5 分钟累计雨量 `PRE`；
  - 计算海河流域/天津市 1h 滑动、24h 累计、面雨量、天津暴雨等级、水库超限等；
  - 写入 `qy_minute_monitor` 主表及 `qy_minute_1h_max_station`、`qy_minute_24h_max_station`、`qy_minute_tj_rain_cnty`、`qy_minute_zone_pre`、`qy_shuike` 等子表。
- 调度器使用 APScheduler `BlockingScheduler`，`process_task()` 已可处理追补（落后多个 5 分钟时循环执行）。
- 数据库访问通过 `utils/db.py` 的 SQLAlchemy `Session/Engine`（PostgreSQL）。
- 当前后台为 FastAPI (`main.py`)，已提供 `/selectqybytimerangedataminute` 等查询接口供前端/问答智能体消费。

## 与问答智能体的职责边界

- **牵引智能体（本仓库）**：数据采集、清洗、阈值判定、结构化落库、对外提供只读查询接口。
- **问答智能体（外部）**：读取落库结果，承载“应急响应逻辑”（如生成预案、调度建议、影响范围说明等）。

## 应急响应条件（仅实况监测部分）

用户提供海河流域防汛应急响应条件，其中**监测（实况）部分**如下：

| 级别 | 实况条件 |
|------|----------|
| Ⅳ级 | 过去 24h 海河流域降水影响区域内，降水量达到暴雨（50.0–99.9mm）的国家气象观测站占比 ≥ 20%，且强降水持续 |
| Ⅲ级 | 过去 12h 海河流域降水影响区域内，降水量达到暴雨（50.0–99.9mm）的国家气象观测站占比 ≥ 20%，且强降水持续 |
| Ⅱ级 | 过去 24h 海河流域降水影响区域内，降水量达到大暴雨（100.0–249.9mm）的国家气象观测站占比 ≥ 15%，且强降水持续 |
| Ⅰ级 | 过去 24h 海河流域降水影响区域内，降水量达到特大暴雨（≥250.0mm）的国家气象观测站占比 ≥ 15%，且强降水持续 |

> 预报、台风部分不在牵引智能体处理范围。

## 实现要点

- 表名：`qy_emergency_response_monitor`。
- 国家气象观测站 `Station_levl` 取值为 `"011"`、`"012"`、`"013"`、`"016"`。
- 按站点在 12h/24h 窗口内对 5 分钟累计降水 `PRE` **求和**，再按阈值分级。
- 缺测哨兵值（`PRE > 99988`）按 0 处理；无法解析为数值的行直接剔除。
- 新模块：`ScheduledTask/emergency_response_monitor.py`。
- 查询接口：`GET /tool/emergency-response/latest?limit=N`。
- 调度集成：在 `calcmaxdataseg5min()` 关闭分钟监测 session 后调用，避免 session 泄漏。

## 已确认问题

- 国家站编码：`"011"`、`"012"`、`"013"`、`"016"`。
- “相邻”条件忽略，直接统计流域内国家站占比。
- 表名采用 `qy_emergency_response_monitor`。
