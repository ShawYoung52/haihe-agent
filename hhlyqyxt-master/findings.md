# 调研发现

## 项目结构与现有数据流

- `ScheduledTask/stationProcessMin.py` 是现有 5 分钟牵引调度脚本：
  - 每 5 分钟读取 `SURF_CHN_MUL_MIN` 分钟降水数据；
  - 重采样为 5 分钟累计雨量 `PRE`；
  - 计算海河流域/天津市 1h 滑动、24h 累计、面雨量、天津暴雨等级、水库超限等；
  - 写入 `qy_minute_monitor` 主表及 `qy_minute_1h_max_station`、`qy_minute_24h_max_station`、`qy_minute_tj_rain_cnty`、`qy_minute_zone_pre`、`qy_shuike` 等子表。
- 调度器使用 APScheduler `BlockingScheduler`，`process_task()` 已可处理追补（落后多个 5 分钟时循环执行）。
- 数据库访问通过 `utils/db.py` 的 SQLAlchemy `Session/Engine`（PostgreSQL），默认 `expire_on_commit=True`。
- 当前后台为 FastAPI (`main.py`)，已提供 `/selectqybytimerangedataminute` 等查询接口供前端/问答智能体消费。

## 与问答智能体的职责边界

- **牵引智能体（本仓库）**：数据采集、清洗、阈值判定、结构化落库、对外提供只读查询接口。
- **问答智能体（外部）**：读取落库结果，承载“应急响应逻辑”（如生成预案、调度建议、影响范围说明等）。

## 应急响应条件（仅实况监测部分）

| 级别 | 实况条件 |
|------|----------|
| Ⅳ级 | 过去 24h 暴雨（50.0–99.9mm）国家站占比 ≥ 20% |
| Ⅲ级 | 过去 12h 暴雨（50.0–99.9mm）国家站占比 ≥ 20% |
| Ⅱ级 | 过去 24h 大暴雨（100.0–249.9mm）国家站占比 ≥ 15% |
| Ⅰ级 | 过去 24h 特大暴雨（≥250.0mm）国家站占比 ≥ 15% |

> 预报、台风、相邻条件均不在牵引智能体处理范围。

## 实现要点

- 表名：`qy_emergency_response_monitor`，`datatime` 有 UNIQUE 约束（结构性幂等）。
- 国家气象观测站 `Station_levl` 取值为 `"011"`、`"012"`、`"013"`、`"016"`。
- 按站点在 12h/24h 窗口内对 5 分钟累计降水 `PRE` **求和**，再按阈值分级。
- 缺测哨兵值（`PRE > 99988`）按 0 处理；无法解析为数值的行直接剔除。
- 新模块：`ScheduledTask/emergency_response_monitor.py`。
- 查询接口：`GET /tool/emergency-response/latest?limit=N`。
- 调度集成：在 `calcmaxdataseg5min()` 关闭分钟监测 session 后调用。

## 全面审查发现并修复的问题（2026-07-17）

| 级别 | 问题 | 根因 | 修复 |
|------|------|------|------|
| P0 | `circleadd5min` 列筛选丢弃 `Station_levl` | 10 列白名单未含该列，concat 后历史行全 NaN，应急响应永久失效 | 白名单加入 `Station_levl`，旧 CSV 无该列时补空串兼容 |
| P0 | `session.close()` 后访问 `qmm.id` | `expire_on_commit=True`，close 后访问过期属性抛 `DetachedInstanceError` | close 前先取 `minute_monitor_id = qmm.id` |
| P1 | CSV 每周期重复读取 2–3 次 | run 与 compute 各自 `read_csv` | compute 内只读一次；`_read_max_datetime` 删除 |
| P1 | 数据延迟/追补时同 datatime 重复写入 | 无幂等机制 | 同事务先删后插 + datatime UNIQUE 约束 |
| P1 | 返回 detached ORM 对象 | commit 后属性过期、close 后访问即炸 | 返回前 `session.refresh(record)` |
| P2 | 缺 `Station_levl` 列时 KeyError | 只保护了 circleadd5min，未保护消费方 | compute 缺列时补空串 + warning |
| P2 | 0 字节 CSV 抛 `EmptyDataError` | `df.empty` 检查之前就已抛异常 | compute 捕获 `EmptyDataError` 返回 None |
| P2 | 幂等测试只验证调用次数 | MagicMock 未钉住 filter 条件 | 断言 filter 参数含 `datatime` |

## 遗留事项

- CSV 每周期仍被 `circleadd5min`、`calcmaxdataseg5min`、应急响应模块各读一次（跨模块签名改动大，成本可接受，未动）。
- `utils/rainfall_impact_geojson.py` 及其测试是用户另一项 WIP，9 个测试失败与本次无关，未触碰。
