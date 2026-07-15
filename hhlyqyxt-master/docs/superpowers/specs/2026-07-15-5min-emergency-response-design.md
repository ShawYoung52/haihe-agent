# 5 分钟降雨应急响应监测设计文档

## 1. 目标与范围

在牵引智能体侧，每 5 分钟基于已有的 `SURF_CHN_MUL_MIN` 分钟降水数据，计算海河流域国家气象观测站在过去 12 小时、24 小时内的暴雨/大暴雨/特大暴雨站点数量与占比，并持久化到 `qy_emergency_response_monitor` 表。应急响应等级判断逻辑由问答智能体消费数据后自行处理，本系统只输出**结构化实况监测事实**。

## 2. 触发条件（仅实况部分）

| 应急响应级别 | 实况条件 |
|--------------|----------|
| Ⅳ级 | 过去 24h 暴雨（50.0–99.9mm）国家站占比 ≥ 20% 且强降水持续 |
| Ⅲ级 | 过去 12h 暴雨（50.0–99.9mm）国家站占比 ≥ 20% 且强降水持续 |
| Ⅱ级 | 过去 24h 大暴雨（100.0–249.9mm）国家站占比 ≥ 15% 且强降水持续 |
| Ⅰ级 | 过去 24h 特大暴雨（≥250.0mm）国家站占比 ≥ 15% 且强降水持续 |

本模块负责计算占比，**“强降水持续”**由问答智能体根据连续多个 5 分钟周期的监测结果自行判断。

## 3. 数据源

- 复用 `ScheduledTask/stationProcessMin.py` 中已生成的 5 分钟累计降水 CSV（`tempfile = "./yangxiao.csv"`）。
- 字段：`Station_Id_C`, `Datetime`, `PRE`, `Lat`, `Lon`, `City`, `Station_Name`, `Cnty`, `Province`, `Town`, `Station_levl`。
- `Station_levl` 取值为 `"011"`、`"012"`、`"013"`、`"016"` 时视为国家气象观测站。

## 4. 计算逻辑

### 4.1 统计窗口

- 12 小时：`(end_time - 12h, end_time]`
- 24 小时：`(end_time - 24h, end_time]`

### 4.2 分级标准

```text
暴雨       50.0 <= PRE < 100.0
大暴雨     100.0 <= PRE < 250.0
特大暴雨   PRE >= 250.0
```

### 4.3 指标

对每个窗口计算：

- `total_national_stations`：窗口内出现过的国家站总数（去重）。
- `count_baoyu` / `count_dabaoyu` / `count_tedabaoyu`：分别达到暴雨/大暴雨/特大暴雨的国家站数量。
- `ratio_baoyu` / `ratio_dabaoyu` / `ratio_tedabaoyu`：占比 = count / total_national_stations，保留 4 位小数。

### 4.4 当前周期应急等级推导

按最高级别优先原则：

```text
IF ratio_tedabaoyu_24h >= 15%  -> level = I
ELIF ratio_dabaoyu_24h >= 15%  -> level = II
ELIF ratio_baoyu_12h >= 20%    -> level = III
ELIF ratio_baoyu_24h >= 20%    -> level = IV
ELSE                            -> level = 0（无）
```

> 仅作为当前周期的监测指标写入，不强加“持续”判断。

## 5. 表结构

表名：`qy_emergency_response_monitor`

| 字段 | 类型 | 说明 |
|------|------|------|
| id | SERIAL PRIMARY KEY | 主键 |
| datatime | TIMESTAMP | 本周期结束时间（监测时间） |
| minute_monitor_id | INTEGER | 关联 `qy_minute_monitor.id`（可选，用于关联 5 分钟主表） |
| total_national_stations | INTEGER | 窗口内国家站总数 |
| station_12h_baoyu | INTEGER | 12h 暴雨站数 |
| ratio_12h_baoyu | NUMERIC(6,4) | 12h 暴雨占比 |
| station_24h_baoyu | INTEGER | 24h 暴雨站数 |
| ratio_24h_baoyu | NUMERIC(6,4) | 24h 暴雨占比 |
| station_24h_dabaoyu | INTEGER | 24h 大暴雨站数 |
| ratio_24h_dabaoyu | NUMERIC(6,4) | 24h 大暴雨占比 |
| station_24h_tedabaoyu | INTEGER | 24h 特大暴雨站数 |
| ratio_24h_tedabaoyu | NUMERIC(6,4) | 24h 特大暴雨占比 |
| response_level | SMALLINT | 当前周期推导等级 0/4/3/2/1（0=无，1=Ⅰ级...4=Ⅳ级） |
| create_time | TIMESTAMP DEFAULT now() | 入库时间 |

建表 SQL：

```sql
CREATE TABLE IF NOT EXISTS qy_emergency_response_monitor (
    id SERIAL PRIMARY KEY,
    datatime TIMESTAMP NOT NULL,
    minute_monitor_id INTEGER,
    total_national_stations INTEGER NOT NULL DEFAULT 0,
    station_12h_baoyu INTEGER NOT NULL DEFAULT 0,
    ratio_12h_baoyu NUMERIC(6,4) NOT NULL DEFAULT 0,
    station_24h_baoyu INTEGER NOT NULL DEFAULT 0,
    ratio_24h_baoyu NUMERIC(6,4) NOT NULL DEFAULT 0,
    station_24h_dabaoyu INTEGER NOT NULL DEFAULT 0,
    ratio_24h_dabaoyu NUMERIC(6,4) NOT NULL DEFAULT 0,
    station_24h_tedabaoyu INTEGER NOT NULL DEFAULT 0,
    ratio_24h_tedabaoyu NUMERIC(6,4) NOT NULL DEFAULT 0,
    response_level SMALLINT NOT NULL DEFAULT 0,
    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_qy_emergency_response_monitor_datatime
    ON qy_emergency_response_monitor(datatime DESC);
```

## 6. 模块设计

新建 `ScheduledTask/emergency_response_monitor.py`，职责单一：

- `load_minute_rainfall(csv_path)`：读取 5 分钟 CSV。
- `filter_national_stations(df)`：按 `Station_levl` 过滤国家站。
- `compute_window_stats(df, end_time, window_hours)`：计算单个窗口的分级站数/占比。
- `compute_emergency_response_stats(df, end_time)`：组合 12h/24h 计算，返回指标字典。
- `resolve_response_level(stats)`：按最高级原则推导等级。
- `save_emergency_response_monitor(session, stats, datatime, minute_monitor_id=None)`：写入数据库。
- `run_emergency_response_monitor(csv_path, datatime=None, minute_monitor_id=None)`：一次完整流程。

新增模型 `Models/QyEmergencyResponseMonitor.py`（SQLAlchemy ORM）。

## 7. 与调度器集成

在 `ScheduledTask/stationProcessMin.py` 的 `calcmaxdataseg5min()` 末尾调用：

```python
from ScheduledTask.emergency_response_monitor import run_emergency_response_monitor

run_emergency_response_monitor(
    csv_path=tempfile,
    datatime=end_time,
    minute_monitor_id=qmm.id,
)
```

保证同一个 5 分钟周期内，先有 `qy_minute_monitor` 主记录，再写入应急响应监测记录，便于关联。

## 8. 对外接口

在 `Controller/tool_router.py` 增加：

```python
@toolrouter.get("/emergency-response/latest")
def get_latest_emergency_response(limit: int = 1):
    ...
```

返回最新的应急响应监测记录，供问答智能体查询。

## 9. 异常处理

- CSV 文件不存在或为空：记录 warning，跳过写入，不阻塞主调度流程。
- 窗口内国家站数为 0：写入全 0 记录，`response_level = 0`。
- 数据库异常：捕获后打印日志，关闭 session，抛出由上层决定是否需要重试。

## 10. 测试

- 单元测试：`utils/tests/test_emergency_response_monitor.py`，覆盖：
  - 国家站过滤；
  - 12h/24h 分级统计；
  - 等级推导；
  - 空数据/无国家站边界；
- 集成测试：手动跑一次 `process_task()`，验证数据库有记录。
