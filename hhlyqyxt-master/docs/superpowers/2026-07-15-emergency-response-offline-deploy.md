# 5 分钟降雨应急响应监测 — 离线服务器部署指南

## 一、代码同步

当前环境无法直接 `git push` 到 GitHub（TLS 连接失败），请按以下任一方式把 `main` 分支代码同步到离线服务器：

### 方式 A：在能联网的机器上 push

```bash
git push origin main
```

然后在离线服务器上：

```bash
cd /path/to/haiheliuyubaoyuagent-master
git pull origin main
```

### 方式 B：通过 U 盘/内网文件同步

1. 在开发机上把当前 `main` 分支打包：
   ```bash
   git archive --format=tar.gz --output=hhly-emergency-response.tar.gz main
   ```
2. 复制到离线服务器后解压覆盖项目目录。

---

## 二、数据库建表

进入 `hhlyqyxt-master` 目录，找到 `Models/QyEmergencyResponseMonitor.py`，文件底部已附 DDL：

```sql
CREATE TABLE qy_emergency_response_monitor (
    id SERIAL PRIMARY KEY,
    datatime TIMESTAMP NOT NULL UNIQUE,
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

CREATE INDEX idx_qy_emergency_response_monitor_datatime
    ON qy_emergency_response_monitor(datatime DESC);
```

`datatime` 的 UNIQUE 约束是结构性幂等保障——即使手动 dry-run 与调度重叠，也不会产生重复行。

### 已建表升级（之前部署过旧版表）

如果服务器上已有不含 UNIQUE 的旧表，执行以下迁移：

```sql
-- 先清理可能的重复历史数据（如有）
DELETE FROM qy_emergency_response_monitor a USING qy_emergency_response_monitor b
WHERE a.id > b.id AND a.datatime = b.datatime;

ALTER TABLE qy_emergency_response_monitor
    ADD CONSTRAINT qy_emergency_response_monitor_datatime_key UNIQUE (datatime);

CREATE INDEX IF NOT EXISTS idx_qy_emergency_response_monitor_datatime
    ON qy_emergency_response_monitor(datatime DESC);
```

---

## 三、旧 CSV 兼容（重要）

部署初期，服务器上的 `yangxiao.csv` 可能是旧版生成（无 `Station_levl` 列）。代码已加防御：

- `circleadd5min` 读入时若 CSV 无该列，自动补空串。
- `emergency_response_monitor.compute_emergency_response_stats` 同样补空串并告警，不抛 KeyError。

但旧数据行在补空串后会被当作非国家站过滤掉，导致 12h/24h 窗口内 `total_national_stations` 偏小。**两个选择**：

- **推荐**：部署后直接删除旧 CSV，让调度重建（首次 `unionmindataby10minuteto24h` 会重灌 24h 数据，需要等待约 24 小时滚满；或手动跑一次该函数重灌）。
- **可接受**：保留旧 CSV，接受 24 小时过渡期，过渡期内 `response_level` 偏保守。

---

## 四、重启 5 分钟调度

```bash
cd /path/to/haiheliuyubaoyuagent-master/hhlyqyxt-master

# 查找并停止旧的 stationProcessMin 进程
ps aux | grep "ScheduledTask.stationProcessMin" | grep -v grep
kill <pid>

# 重新启动
nohup python -u -m ScheduledTask.stationProcessMin > stationProcessMin.log 2>&1 &
```

---

## 五、验证

### 5.1 调度是否正常运行

查看日志：

```bash
tail -f stationProcessMin.log
```

应能看到每个自然 5 分钟周期（00/05/10...）执行一次，无异常。

### 5.2 数据库是否有记录

等待至少一个 5 分钟后查询：

```sql
SELECT *
FROM qy_emergency_response_monitor
ORDER BY datatime DESC
LIMIT 5;
```

### 5.3 查询接口是否可用

```bash
curl "http://<服务器IP>:7000/tool/emergency-response/latest?limit=5"
```

应返回最新应急响应监测记录 JSON。

---

## 五、回滚（如需）

如果部署后需要回滚，在离线服务器上执行：

```bash
cd /path/to/haiheliuyubaoyuagent-master/hhlyqyxt-master
git log --oneline -10

# 回退到合并前的 commit（37ec9c4 是合并前最后一个 commit）
git reset --hard 37ec9c4

# 重启调度
kill <stationProcessMin pid>
nohup python -u -m ScheduledTask.stationProcessMin > stationProcessMin.log 2>&1 &
```

> 注意：回退到 `37ec9c4` 会同时回退 `stationProcessMin.py` 上本次合并带进去的其它本地改动。如果只想回退应急响应模块，请手动 revert 相关文件。
