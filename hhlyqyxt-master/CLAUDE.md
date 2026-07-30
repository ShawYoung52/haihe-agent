# CLAUDE.md — 牵引智能体（hhlyqyxt-master）

牵引智能体：海河流域水文预警定时任务 + 应急响应 + 河流影响图。

## 关键约定

- **不跨仓库 import 问答智能体的模块**——内网服务器上问答仓库不在牵引旁边（[[traction-emergency-hhly-source]]）
- 只修牵引侧代码，同事的代码不要动（[[traction-review-scope-rule]]）
- Python venv 用 `D:\PythonProject\haiheliuyubaoyuagent-master\.venv\Scripts\python.exe` 绝对路径，别用系统 `python` / `py`（[[haihe-project-env-quirks]]）

## stationProcessMin 定时任务（每 5 分钟）

流程链路：`stationProcessMin.main()` → `BlockingScheduler` cron `*/5` → `process_task()` → `circleadd5min()` + `calcmaxdataseg5min()`

### 两套独立 CSV（**不要合并**）

| CSV | Basin | 用途 |
|---|---|---|
| `./24hourmindata.csv` | `HHLY_JUECE` | 河流影响图、天津分县暴雨等级、最大站统计入库（同事代码依赖） |
| `./hhly_24hourmindata.csv` | `HHLY` | 应急响应级别判定（`compute_emergency_response_stats`） |

**为什么分开**：两个 basin 的站点集合不同，应急响应对国家站口径要求严格。合并会破坏 12h/24h 占比分母。

### HHLY 数据源：SURF_CHN_PRE_MIN + Q_PRE 过滤 + 内存聚合

**从 2026-07-30 开始**（commits `5c250e9`-`80c2477`）：
- HHLY 数据源用 **`SURF_CHN_PRE_MIN`**（含 `Q_PRE` 质量标志），不是 `SURF_CHN_MUL_MIN`
- `hhly_24hourmindata.csv` 存**原始 1min 记录**（不 resample！）
- 应急响应判定时调 `ScheduledTask.precipitation_aggregator.aggregate_minute_precipitation`：
  - Q_PRE 过滤：默认可信 `{"0","3","4"}`
  - 内存里按 `Station_Id_C` 聚合 12h/24h 累计
- 分子统计用**区间语义**（`baoyu ∈ [50,100)` / `dabaoyu ∈ [100,250)` / `tedabaoyu ∈ [250,∞)`）
- 参考问答智能体 `haihe-weather-analyzer-mcp/haihe_mcp_tools.aggregate_minute_precipitation`，牵引侧**独立实现**（不跨仓库 import）

**不要重新引入 5min resample**——那是老架构，会导致 `PRE="000"` 字符串拼接、`Station_levl` 跨时刻不一致等 bug。详见 `[[traction-hhly-pre-min-refactor]]` 记忆。

### 24h 滚动窗口边界（用 `>=` 不是 `>`）

```python
start_time = end_time - pd.Timedelta(hours=24)
df = df_5min[
    (df_5min["Datetime"] >= start_time) &   # ← 用 >=
    (df_5min["Datetime"] <= end_time)
].copy()
```

**用 `>` 会每 tick 丢失 start_time 那一个 5min 边界**，24h 后 CSV 缩到 23h55min，无限收缩到 0。commit `fc4f802`。

**例外**：`calctianjinrainlevel` 里的 1h/6h/24h **统计窗口**继续用 `>`（避免边界站重复计入求最大值时）。

### MUSIC 拉取空档（不推进 end_time，下 tick 自动重试）

```python
try:
    res = readmindatabytimerange(...)
except MusicApiError as e:
    logger.warning(...)
    return (end_time - pd.Timedelta(minutes=5)).to_pydatetime()  # 不推进
```

MUSIC 入库延时（数据 18:30 但要 18:33 才入库）常见。**必须允许下 tick 重试同一时段**，不能一律"跳过 + 推进"（那样会永久丢 5min 数据）。commit `ee7da45`。

### process_task 追赶循环预算

```python
tick_start = datetime.now()
max_tick_seconds = 240  # 单 tick 最多 4 分钟
datatime = circleadd5min()
calcmaxdataseg5min()
while datetime.now() > datatime:
    if (datetime.now() - tick_start).total_seconds() > max_tick_seconds:
        break
    prev_datatime = datatime
    datatime = circleadd5min()
    if datatime <= prev_datatime:  # MUSIC 空档
        break  # 下 tick 隔 5 分钟再试，避免同 tick 高频打 MUSIC
    calcmaxdataseg5min()
```

commit `5b2cca4`。

### 5min 聚合（**一次 agg 搞定所有列**）

```python
df_5min = (
    df.set_index("Datetime")
    .groupby("Station_Id_C")
    .resample("5min", label="right", closed="right")
    .agg({
        "PRE": "sum",
        "Station_levl": "first", "Lat": "first", "Lon": "first",
        "City": "first", "Station_Name": "first",
        "Cnty": "first", "Province": "first", "Town": "first",
    })
    .reset_index()
)
```

**不要**先 agg PRE 再单独 `groupby(pd.Grouper(freq="5min", label="right"))` merge 其他列——`Grouper` 的 `closed` 默认是 `"left"`，与 `resample` 的显式 `"right"` 会导致 Datetime 键错位，merge 全部落 NaN → 元信息全丢 → 应急响应永远为 0 级 → 天河报告永远不触发（C1 血泪 `607ecfa`）。

### CSV encoding = utf-8-sig

**所有** `to_csv` / `read_csv` 加 `encoding="utf-8-sig"`。Windows Excel 打开中文站名不乱码。`read_csv` 加 `low_memory=False` 消除 `DtypeWarning`。

### MusicApiError 必须 catch

`circleadd5min` 两处 MUSIC 调用（HHLY_JUECE 主 + HHLY 累积）必须 catch `utils.MusicTool.MusicApiError`。不 catch 会逃逸到 APScheduler `EVENT_JOB_ERROR`，process_task 崩溃 → 应急响应不入库、天河报告不触发。

## 应急响应级别

`emergency_response_monitor._determine_response_level` 返回 0-4：
- **1 = I 级**（特大暴雨占比 ≥ 15%）
- **2 = II 级**（大暴雨占比 ≥ 15%）
- **3 = III 级**（12h 暴雨占比 ≥ 20%）
- **4 = IV 级**（24h 暴雨占比 ≥ 20%）
- **0 = 无预警**

**不是颜色！** 是罗马数字应急响应等级，数字越小级别越高。

**国家站口径**：2 位 `Station_levl in {"11","12","13","16"}`（不是 3 位 zfill）。

## 天河报告接口

POST `http://10.226.188.156:8001/api/report/generate`
body: `{"template": "haihe_weather_bulletin"}`（只传 template，其他默认）

在 `stationProcessMin.py:calcmaxdataseg5min()` 中 `run_emergency_response_monitor` 之后调用 `trigger_weather_bulletin_report(record.response_level)`。仅 I-IV 级触发。失败**不阻塞主流程**（只记 WARNING）。

## 场景重放脚本

`scripts/verify_emergency_scenario.py --date YYYY-MM-DD --hour HH` 用历史日期一键重现完整链路（拉数据 → 河流影响图 → 应急响应入库 → 报告触发）。用**验证专用 CSV**（`verify_juece_/verify_hhly_` 前缀），不冲突生产 CSV。

## 内网部署

```bash
sudo systemctl stop station-process-min
cd /root/zm_code
git pull origin main
sudo systemctl start station-process-min
sudo journalctl -u station-process-min -f
```

## 验证清单

- `24hourmindata.csv` / `hhly_24hourmindata.csv` 里 `Datetime.max()` 每 5 分钟前进（除非 MUSIC 真空档）
- 两个 CSV 的 `Datetime.min()` 距 `Datetime.max()` 正好 24h
- `hhly_24hourmindata.csv` 里 `Station_levl` 列**非 NaN**（C1 验证：如果全 NaN 说明 resample/Grouper closed 参数错配）
- 数据库 `qy_emergency_response_monitor` 每 5 分钟有新记录
- 应急响应 I-IV 级时 `qy_minute_monitor.geojsonurl` / `impact_city` 有值
- I-IV 级时天河报告日志有 `触发成功` 或 `4xx/5xx` 反馈

## 测试

```bash
cd hhlyqyxt-master
D:\PythonProject\haiheliuyubaoyuagent-master\.venv\Scripts\python.exe -m pytest ScheduledTask/tests/ utils/tests/
```

期望：104 passed（65 rainfall_impact + 30 emergency_response + 6 report_generator + 3 HHLY rolling CSV）

## 相关记忆

- `[[traction-emergency-hhly-source]]` — 应急响应 HHLY 数据源改造
- `[[traction-report-api]]` — 天河报告接口
- `[[traction-station-process-min]]` — 本文档所有约束的记忆汇总
- `[[traction-review-scope-rule]]` — 审查范围规则（只改我们的代码）
- `[[haihe-project-env-quirks]]` — Windows Store python 坑 + venv 路径 + git 精确 add
- `[[rain-impact-arrival-time-contract]]` — 暴雨影响河流 20km + arrival 契约
- `[[rain-impact-chain-arrival]]` — BFS 链式传播修复