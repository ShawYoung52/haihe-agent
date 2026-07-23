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

## 2026-07-23 牵引智能体全量代码审查（四维度）

审查范围：`hhlyqyxt-master` 全量生产代码 ~6500 行。D1 正确性 / D2 业务口径 / D3 代码质量 / D4 部署合规。先前的 P0（circleadd5min 丢 Station_levl、DetachedInstanceError、get_edge_length_km NaN、应急响应 session 生命周期）已验证修复，无回归。

### P0（业务关键，需用户确认口径）

| ID | 维度 | 位置 | 问题 | 待确认 |
|----|------|------|------|--------|
| P0-1 | D2 | `test_rain_impact_internal.py:89-104`、`rainstorm_impact_map_service.py:232-259` | `river_propagation`（用户专门要的传播时间）算了但在所有用户入口都没输出/落盘 | 是否补输出？补哪些入口？ |
| P0-2 | D2 | `stationProcessMin.py:444-448` | HHLY 独立拉取链路（B1-B4 新增的 `timerange` 路径）**未接入生产**，生产应急响应仍读 `yangxiao.csv`（HHLY_JUECE）。HHLY 改造在生产是死代码 | 是否把生产调用切到 `timerange`？还是维持 CSV 路径只改 2 位口径？ |

### P1（明确 bug 或业务口径偏差）

| ID | 维度 | 位置 | 问题 | 修复方向 |
|----|------|------|------|----------|
| P1-1 | D1 | `stationProcessMin.py:467-471` | `circleadd5min` 在 MUSIC 返回空 DS（无列空 DataFrame）时 `res["Datetime"]` 抛 KeyError，5 分钟任务整体崩溃，CSV 不更新，后续周期重复崩 | 空 DS 守卫，写 CSV 后 return |
| P1-2 | D1/D4 | `stationProcessMin.py:526,503` | `getreservoir` `requests.post` 无 timeout，API 挂起则 `max_instances=1` 丢所有后续 5 分钟任务；返回异常载荷时 `df['overLimit']` KeyError | 加 `timeout=(5,30)` + try/except |
| P1-3 | D1 | `stationProcessMin.py:311-441` | `calcmaxdataseg5min` session 无 try/finally，NaT datetime 或 DB 错时 qmm 已提交但明细丢失、连接泄漏 | 包 try/except/finally |
| P1-4 | D1 | `stationProcess.py:115` | `stationmonitorbyhour` session 从不 close，每小时泄漏连接 | try/finally close |
| P1-5 | D1 | `Controller/wx_router.py:34-35` | `raise SendResponse(...)` 把 Pydantic 模型当异常抛，每次微信发送失败都 500；且 `success=True` 语义错 | 改 `return SendResponse(success=False, ...)` |
| P1-6 | D1 | `main.py:60-173` | `selectqybytimerange` session 从不 close，每次调用泄漏连接 | try/finally close |
| P1-7 | D2 | `stationProcessMin.py:761` | `qy_minute_zone_pre` 只存最大面雨量的 1 个河系，非全部 9 个河系 | 存全部 9 个 or 确认 max-only（需口径） |
| P1-8 | D2 | `stationProcessMin.py:686-688` | `qy_minute_tj_rain_cnty` 只存全局最大雨量级的区县，其它级被丢 | 存全部雨量级>0 or 确认 worst-only（需口径） |
| P1-9 | D2 | `rainfall_impact_geojson.py:1248` | `_build_river_propagation` 下游边无 `"row"` 键，命名回退 pkl 名，与 GeoJSON/affected_rivers 口径可能不一致（滦河单字尤甚） | 下游边补 full_v6 row 查询后再命名 |
| P1-10 | D2 | `stationProcessMin.py:574-579,613-618,650-655` | 天津预警 `pd.cut(right=True)` 区间 `(30,50]`，30.0mm 不触发；CMA "达30毫米以上"惯例应 ≥。同文件 `countstationnumbylevel` 用 `right=False`，自相矛盾 | `right=False` or 确认严格 `>`（需口径） |

### P2（次要/一致性/硬化）

| ID | 维度 | 位置 | 问题 |
|----|------|------|------|
| P2-1 | D1 | `emergency_response_monitor.py:182,191` | `ratio_12h_baoyu` 分母用 24h 站点数，12h 停报站点在分母不在分子，可能漏 Ⅲ 级触发（需口径确认是否 24h 共同基底） |
| P2-2 | D1 | `MusicTool.py:303-312` | `safe_float` 哨兵集不全（只认 999999/999990/-9999），999900 漏网；`float("nan")` 返回 NaN。仅影响 CLI judge 路径 |
| P2-3 | D1 | `emergency_response_monitor.py:96-123` | timerange 路径 HHLY Datetime 为 UTC，CSV 路径用 BJT，未来调用方传 BJT datatime 会算错 24h 窗（当前 timerange 仅测试用，潜伏） |
| P2-4 | D1 | `monitorservice.py:471-488` | `get_edge_length_km` 缺 NaN 守卫，滦河 len_km=NaN 会传播（仅注释 __main__ 调用，潜伏） |
| P2-5 | D1 | `stationProcess.py:34,154,209` | `stationmonitorbyhour` 空 MUSIC 响应 `df["SUM_PRE_1H"].astype` KeyError |
| P2-6 | D1 | `monitorservice.py:57,255` | `draw24hrainpic` 空响应崩 + f-string 拼 `IN(...)` SQL 注入隐患 |
| P2-7 | D1 | `stationProcessMin.py:85` | `unionmindataby10minuteto24h` 空 DataFrame 崩（仅 __main__） |
| P2-8 | D1 | `main.py:203-380` | `selectqybytimerangedataminute` session.close 无 try/finally，NaT strftime 泄漏 |
| P2-9 | D1 | `emergency_response_monitor.py:155,182,260` | compute 在 0 国家站时仍写无意义 0 记录；`to_datetime` 无 `errors="coerce"` 单条坏 datetime 崩 5 分钟任务 |
| P2-10 | D1 | `reportservice.py:8-20` | `checkreportexistbydate` 未知 report_type 时 `report_file_name` 未定义 NameError |
| P2-11 | D1 | `MusicTool.py:440,515-518,930` | CLI `evaluate_observation_response` 用累计阈值（≥50 含大暴雨/特大暴雨），与生产 exclusive band 不一致 |
| P2-12 | D1 | `MusicTool.py:70` | `MusicClient.session` 从不 close，5 分钟追补循环每次 new client 泄漏 socket |
| P2-13 | D2 | `monitorservice.py:403,441,240,255` | 停在 v4 图/表（rainfall_impact 已 v6），版本不同步；NaN 守卫缺失 |
| P2-14 | D2 | `main.py:330,286` vs `stationProcessMin.py:407,322` | 查询 SELECT admin_code/create_time/del_flag 但写入未填，恒 NULL |
| P2-15 | D2 | `main.py:366-374` vs `stationProcessMin.py:436` | `qy_shuike` 查询只暴露 stationname，QA 看不到超限水库位置/值 |
| P2-16 | D2 | `Models/QyMinuteMonitor.py:10` | `datatime` 无 unique/nullable 约束，重跑同 5 分钟槽位产生重复行（应急响应表有 unique+先删后插） |
| P2-17 | D2 | `rainfall_impact_geojson.py:110` | `aggregate_5min_station_pre_to_24h` 缺测 PRE 置 0 后累加，稀疏数据站可能假触发 50mm 河流影响 |
| P2-18 | D4 | `utils/db.py:6` | 硬编码 `postgres:postgres@10.226.107.130:5432/postgres` 连接串含密码，不可 env 覆盖 |
| P2-19 | D4 | `stationProcessMin.py:259,260` | graph_path 硬编码到 QA 仓库 test-data 目录、output_dir 硬编码 `/root/zm_code/`，不可 env 覆盖 |
| P2-20 | D4 | `es_preprocess.py`、`tool_router.py:48,215` | `ES_HOST` 硬编码 4+ 处不可 env 覆盖 |
| P2-21 | D4 | `stationProcessMin.py:503,261` | reservoir URL、public_base_url 硬编码不可 env 覆盖 |
| P2-22 | D4 | `MusicTool.py:28` | `service_ip` 硬编码（调用方有 env 覆盖，本体无） |
| P2-23 | D3 | 多处 | 重复的 safe_float 式强制转换、重复 DB session 模式、`test_rain_impact_internal.py` 名为 test 实为生产入口 |

### 部署合规正面结论
- 跨仓库 import QA 侧模块：**0 处**（用户硬约束兑现）。
- 应急响应 HHLY 改造（B1-B4）：合规代理 5 项硬约束全 PASS，旧调用方向下兼容。
- `rainstorm_impact_map_service.py`、`river_city_impact_tool.py`、`es_preprocess.py`、`db.py`、`Models/*` 业务逻辑/连接生命周期基本正确。
