# 牵引应急响应改用 SURF_CHN_PRE_MIN + Q_PRE 过滤 + 内存聚合 设计

- **状态**：草案（brainstorming 已完成，待用户 review）
- **日期**：2026-07-30
- **作用域**：`hhlyqyxt-master/ScheduledTask/emergency_response_monitor.py` + `stationProcessMin.py`
- **前置**：问答智能体应急响应用 `SURF_CHN_PRE_MIN` + Q_PRE 过滤 + `aggregate_minute_precipitation` 内存聚合，工作稳定
- **相关记忆**：`[[traction-emergency-hhly-source]]`、`[[traction-station-process-min]]`、`[[traction-review-scope-rule]]`

## 1. 目标

修复牵引侧 `hhly_24hourmindata.csv` 的两类 bug：
- `PRE` 列出现 `"000"` / `"00000"` 字符串拼接（原因：MUSIC 返回字符串，resample.sum 拼接）
- `Station_levl` 跨时刻不稳定（原因：MUSIC 脏数据，resample.first 每 5min 拿到不同值）

**做法**：换数据源 `SURF_CHN_MUL_MIN` → `SURF_CHN_PRE_MIN`（含 `Q_PRE` 质量标志），不再自己 5min resample，改为**存原始分钟数据 + 应急判定时内存聚合**。参考问答智能体 `aggregate_minute_precipitation`。

## 2. 关键决策

### 2.1 数据源

`SURF_CHN_PRE_MIN`（分钟降水专用，含 `Q_PRE`）替换 `SURF_CHN_MUL_MIN`。elements 同问答侧：

```
Station_Id_C,Station_levl,Lat,Lon,Alti,Admin_Code_CHN,V_ACODE_4SEARCH,Town_code,
City,Station_Name,Cnty,NetCode,Province,REGIONCODE,Town,Country,COUNTRYCODE,
Year,Mon,Day,Hour,Min,Datetime,PRE,Q_PRE,PRE_Sensor_Heigh,Station_Type,
REP_CORR_ID,UPDATE_TIME,D_RETAIN_ID,DATA_ID,D_SOURCE_ID,V08010,RYMDHM,IYMDHM
```

### 2.2 CSV 存储改为"原始分钟数据"（不 resample）

`hhly_24hourmindata.csv` 存 MUSIC 返回的**原始 1min 记录**，含 `Q_PRE` 字段：
- 每条记录一行（同站同分钟一条）
- 24h 窗口滚动逻辑不变（`Datetime >= end_time - 24h`）
- **不做 5min 聚合**——聚合搬到应急判定时做

### 2.3 应急判定时内存聚合

新增 `hhlyqyxt-master/ScheduledTask/precipitation_aggregator.py`（**牵引侧独立实现，不跨仓库 import**）：

```python
def aggregate_minute_precipitation(
    records: list[dict],
    end_time: datetime,
    windows_hours: tuple = (1, 12, 24),
    trusted_q_pre: set = frozenset({"0", "3", "4"}),
) -> list[dict]:
    """按站聚合分钟降水累计。
    
    - Q_PRE 过滤：只保留可信标志（默认 {"0","3","4"}）
    - 每站最新元信息 + PRE_1h/PRE_12h/PRE_24h 累计
    - 返回类似问答智能体 aggregate_minute_precipitation 的结构
    """
```

在 `emergency_response_monitor.compute_emergency_response_stats` 中：
1. 读 `hhly_24hourmindata.csv`（1min 记录）
2. 调 `aggregate_minute_precipitation` 内存聚合
3. 按现有 `_determine_response_level` 阈值判定级别（1-4）
4. 入库

### 2.4 CSV 追加逻辑保持

`_append_hhly_5min_to_rolling_csv` 只做：
1. 拉最近 5min HHLY 原始数据（1min 粒度）
2. 转 float PRE、去脏数据（保留 Q_PRE 列，不过滤——由应急判定时过滤）
3. 追加到 CSV，滚动 24h 窗口
4. **不 resample**、**不 groupby.agg**

## 3. 数据流

```
每 5min tick (circleadd5min):
  ├─ HHLY_JUECE 5min → resample → 24hourmindata.csv (不变)
  └─ HHLY (SURF_CHN_PRE_MIN) 5min → 直接追加 → hhly_24hourmindata.csv (改)

calcmaxdataseg5min:
  └─ run_emergency_response_monitor(csv_path=hhly_tempfile)
       ├─ 读 CSV → list[dict]
       ├─ aggregate_minute_precipitation(records, end_time, Q_PRE 过滤)
       │    → 每站 PRE_1h / PRE_12h / PRE_24h
       ├─ 按现有阈值判定 response_level (1-4)
       └─ 入库 QyEmergencyResponseMonitor
```

## 4. 契约兼容性

| 消费者 | 影响 |
|---|---|
| `qy_emergency_response_monitor` 表 | 字段/口径不变（response_level、station_XXh_baoyu、ratio_XXh_baoyu 等） |
| `stationProcessMin.py` 主流程 | 只改 HHLY 累积部分（`_append_hhly_5min_to_rolling_csv`），其他不动 |
| HHLY_JUECE 链路（`24hourmindata.csv`） | **完全不变** |
| 天河报告触发 | 依赖 response_level 结果，仍能触发 |
| 现有测试 104 passed | 需要更新 HHLY 相关测试（test_hhly_rolling_csv.py） |

## 5. 测试策略

### 单元测试
- `test_aggregate_minute_precipitation`（新增）:
  - Q_PRE 过滤 (`"1"` `"9"` 应被过滤，`"0"` `"3"` `"4"` 保留)
  - 窗口累计正确（1h / 12h / 24h）
  - 空输入返回空
  - 同一站点多条记录合并到一条聚合结果
- `test_append_hhly_raw_min_to_csv`（改写现有 test_hhly_rolling_csv）:
  - 追加原始分钟数据到 CSV（不聚合）
  - 24h 窗口滚动
  - CSV 内 PRE 是 float，Station_levl 与源数据一致

### 内网验证
- `verify_emergency_scenario.py` 使用新数据源重跑 2023-07-30 场景
- CSV 内 `PRE` 值为标准数字（`0.0` / `1.2` 等，无 `"000"` `"00000"`）
- 同一 `Station_Id_C` 的 `Station_levl` 稳定不变
- 应急响应 `response_level` 正确判定

## 6. 执行计划

### Phase 1: 新增 precipitation_aggregator.py
- 参考问答侧 `haihe_mcp_tools.aggregate_minute_precipitation`
- 独立实现，不跨仓库 import
- 单元测试覆盖 Q_PRE 过滤 / 窗口累计 / 空输入

### Phase 2: 改造 `_fetch_hhly_rainfall_for_emergency`
- 数据源 `SURF_CHN_MUL_MIN` → `SURF_CHN_PRE_MIN`
- elements 换成含 `Q_PRE` 的字段列表
- 返回原始 DataFrame（不做聚合）

### Phase 3: 改造 `_append_hhly_5min_to_rolling_csv`
- 删除 resample + agg 逻辑
- 直接追加 1min 记录到 CSV
- 24h 窗口滚动逻辑保留

### Phase 4: 改造 `compute_emergency_response_stats`
- 读 CSV → list[dict]
- 调用 `aggregate_minute_precipitation`
- 按现有阈值判定 level (口径不变)

### Phase 5: 更新测试
- 改写 `test_hhly_rolling_csv.py`
- 新增 `test_precipitation_aggregator.py`

### Phase 6: 内网验证 + PR

## 7. 风险

| 风险 | 缓解 |
|---|---|
| `SURF_CHN_PRE_MIN` 在 HHLY 流域可能返回记录数比 `SURF_CHN_MUL_MIN` 多/少 | 首次上线用 `verify_emergency_scenario.py` 对比新老结果 |
| Q_PRE 过滤后触发站数变少，response_level 阈值可能需要调整 | 保留 15%/20% 阈值不变（口径与问答侧一致，问答已验证） |
| CSV 存原始 1min 数据体积比 5min 聚合大 5 倍 | 24h 窗口大约 200 站 × 60min × 24h = 288K 行；~30MB，可接受 |
| 应急判定时聚合耗时 | 每 tick 一次，200 站 × 1440 分钟 = 30 万行 pandas groupby，秒级 |

## 8. 交付

- 新增 `hhlyqyxt-master/ScheduledTask/precipitation_aggregator.py`
- 新增 `hhlyqyxt-master/ScheduledTask/tests/test_precipitation_aggregator.py`
- 改造 `stationProcessMin.py`（`_append_hhly_5min_to_rolling_csv`）
- 改造 `emergency_response_monitor.py`（`_fetch_hhly_rainfall_for_emergency` + `compute_emergency_response_stats`）
- 改写 `tests/test_hhly_rolling_csv.py`
- 新记忆：`[[traction-hhly-pre-min-refactor]]`

## 9. 模型分工

| 阶段 | 模型 | 职责 |
|---|---|---|
| brainstorming | Opus（本会话） | 业务口径对齐、参考问答侧 |
| writing-plans | 本会话 | Phase 编排 |
| TDD 执行 | DeepSeek v4 Flash | 分 phase 落地 |
| code-review | DeepSeek v4 Pro | PR 前审查 |
