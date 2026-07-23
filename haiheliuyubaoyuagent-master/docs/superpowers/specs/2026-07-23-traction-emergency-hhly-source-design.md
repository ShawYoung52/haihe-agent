# 牵引智能体应急响应 — 改用 HHLY 数据源 + 11/12/13/16 口径 设计文档

- 日期：2026-07-23
- 状态：已获用户批准（方案 A + 进程内函数查询接口）
- 需求来源：牵引智能体应急响应此前读 HHLY_JUECE 站点（3 位 Station_levl 口径 011/012/013/016）；需改为独立拉一份 HHLY 站点、业务口径改用 2 位 11/12/13/16

## 1. 业务需求（已与用户确认）

| 决策点 | 结论 |
| --- | --- |
| 改造范围 | **仅应急响应相关**；其它（河系降水量入库、水库信息、circleadd5min 等同事代码）不碰 |
| 数据源 | 从 HHLY_JUECE 改为 **HHLY**；且应急响应**独立拉取**——不动共享 CSV，stationProcessMin/stationProcess（同事负责）的 basinCodes 保持 HHLY_JUECE |
| 站级别口径 | 外部口径为 **2 位 11/12/13/16**（CSV/接口实返为 3 位 011/012/013/016），归一化去前导零后匹配 |
| 判定逻辑 | **保留不动**——暴雨 50mm 12h/24h、大暴雨 100mm 24h、特大暴雨 250mm 24h；占比阈值 0.20（暴雨）/0.15（大暴雨+特大暴雨）；响应级别 1/2/3/4 与现 `_determine_response_level` 完全一致 |
| 拉取方式 | 在牵引智能体 `emergency_response_monitor.py` 新增进程内函数查询接口（复用 `utils/MusicTool.MusicClient`），应急响应内部直接消费；不新增 HTTP/MCP、不落地新 CSV |
| 表写入 | `qy_emergency_response_monitor` 表结构不变 |

## 2. 架构与数据流

```
stationProcessMin 每 5 分钟触发（同事代码，不改）
   ├─ 拉 HHLY_JUECE → 生成 yangxiao.csv（共享，同事河系/水库代码继续用，不改）
   └─ run_emergency_response_monitor(...)   ← 应急响应入口签名扩容但向下兼容
        ├─ _fetch_hhly_rainfall_for_emergency(timerange, client=None)   ← 新增
        │     复用 MusicClient.get_surf_pre_in_basin_timerange(basin_codes="HHLY", ...)
        │     data_code="SURF_CHN_MUL_MIN"，elements 含 Station_levl
        │     返回 DataFrame（列：Station_Id_C,Datetime,PRE,Station_levl,Lon,Lat,...）
        ├─ compute_emergency_response_stats(df_or_path, datatime)   ← 扩容：吃 DataFrame 或 CSV 路径
        │     新增 _normalize_station_level 去前导零 → 匹配 {"11","12","13","16"}
        │     阈值/级别规则不变
        └─ 写 qy_emergency_response_monitor（先删后插，不变）
```

应急响应从此不再依赖共享 `yangxiao.csv`，自带 HHLY 拉取；HHLY_JUECE 链路与同事代码零改动。

## 3. 组件改动

### 3.1 `emergency_response_monitor.py`

**新增常量与归一化（去前导零而非补零）**：
```python
NATIONAL_STATION_LEVELS = {"11", "12", "13", "16"}  # 业务口径：2 位

def _normalize_station_level(value) -> str:
    """归一化为去前导零的 2 位字符串：011→11、11→11、'016'→16。"""
    try:
        return str(int(value))
    except (ValueError, TypeError):
        text = str(value or "").strip()
        return text.lstrip("0") or "0"
```
- 现有 `NATIONAL_STATION_LEVELS={"011","012","013","016"}` + `zfill(3)` 补零 → 改为 2 位去零。`MusicTool.normalize_station_level` 同款逻辑（已存在），保证 CSV 3 位与接口 2 位输入都能命中 11/12/13/16。
- 阈值常量 `BAOYU_LOWER=50.0/DABAOYU_LOWER=100.0/TEDABAOYU_LOWER=250.0` 不动。
- `_determine_response_level`、`compute_emergency_response_stats` 内部判定流程不动。

**新增拉取接口**：
```python
def _fetch_hhly_rainfall_for_emergency(
    timerange: str,
    client: Optional[MusicClient] = None,
) -> pd.DataFrame:
    """独立拉取 HHLY 流域 5 分钟降水，供应急响应内部消费。

    timerange 形如 "[20260722080000,20260723080000]"。
    client 为 None 时现场实例化 MusicClient(MusicConfig())。
    返回含 Station_Id_C/Datetime/PRE/Station_levl/Lat/Lon 等列的 DataFrame；
    拉取失败/空时返回空 DataFrame（compute_emergency_response_stats 返回 None，不写表，只告警）。
    """
```
- `elements` 包含 `Station_levl,Lat,Lon,Station_Id_C,Datetime,PRE,City,Station_Name,Cnty,Province,Town`（与现有应急响应 CSV 列对齐）。
- `PRE` 异常值（>99988）按现有 `compute_emergency_response_stats` 内逻辑置 0，不在拉取层重复处理。
- 复用 `utils.MusicTool.MusicClient`（同 `stationProcessMin.py` 的 client 用法），不引入新依赖。

**`compute_emergency_response_stats` 扩容**：签名由 `(csv_path, datatime=None)` 改为 `(source, datatime=None)`，`source` 接受 CSV 路径字符串 **或** DataFrame：
- 字符串 → 沿用 `pd.read_csv`（旧路径，保持向下兼容，方便离线/测试用 CSV 复跑）；
- DataFrame → 直接用，跳过读文件。

**`run_emergency_response_monitor` 扩容**：新增可选 `client: Optional[MusicClient]=None` 与 `timerange: Optional[str]=None`：
- 传 `timerange` 时走新链路：内部 `_fetch_hhly_rainfall_for_emergency(timerange, client)` → `compute_emergency_response_stats(df, datatime)`；
- 仅传 CSV 路径（不传 `timerange`）时退回旧 CSV 链路（向下兼容现有 `stationProcessMin.py:444` 调用方，迁移期稳态）。
- 同时传 `timerange` 与 CSV 路径：`timerange` 优先，WARNING 日志提示忽略 CSV；
- 两者都不传：抛 `ValueError("run_emergency_response_monitor 需要提供 csv_path 或 timerange 之一")`（现有 `stationProcessMin.py:444` 始终传 csv_path，不会触发）。

### 3.2 调用方迁移（`stationProcessMin.py:444`，下个迭代）本次不动

本次只交付能力，不强制 `stationProcessMin.py` 切换。文档标注"切换等你确认后单独一个提交"，保持同事代码稳定。后续切换即把 `run_emergency_response_monitor(csv_path=tempfile, datatime=..., minute_monitor_id=...)` 改为传 `timerange="<HHLY 拉取时间窗>"` 即可。

## 4. 错误处理

- HHLY 拉取失败/空：`_fetch_hhly_rainfall_for_emergency` 返回空 DataFrame → `compute_emergency_response_stats` 返回 None → `run_emergency_response_monitor` 告警并返回 None，不写表（沿用现有 CSV-空分支语义）。
- 天擎网络异常由 `MusicClient` 内部重试覆盖（现有）；应急响应层不自行重试，异常向上传播由调用方决定。
- `datatime` 为 None 时用 DataFrame 内最大 `Datetime`（与现有 CSV 分支一致）。
- 归一化对非数字/None 宽容（返回 "0"，不命中 11/12/13/16，作非国家站处理）。

## 5. 测试

`hhlyqyxt-master/utils/tests/test_emergency_response_monitor.py`：
- 现有 CSV 用例全部保留并**更新归一化断言**：`'011'/'11'/11 → '11'`、`'016' → '16'`、`'014' → '14'`（非国家站）；`{"11","12","13","16"}` 集合生效，3 位与 2 位输入均命中国家站计数不变。
- 新增 DataFrame 输入口径测试：`compute_emergency_response_stats(df, datatime)` 结果与等价 CSV 一致。
- 新增 `_fetch_hhly_rainfall_for_emergency` 测试：mock `MusicClient.get_surf_pre_in_basin_timerange` 返回固定 records → DataFrame 列与 PRE 置 0 逻辑，断言 basin_codes 必为 "HHLY"、data_code 必为 "SURF_CHN_MUL_MIN"；空返回/拉取异常路径。
- 新增 `run_emergency_response_monitor` 新链路测试：传 `timerange` 时走 mock 拉取并入库（mock Session），断言先删后插、记录字段。

## 6. 兼容性

- 旧 CSV 链路（`stationProcessMin.py:444` 不传 timerange）完全保留，迁移期二者共存。
- `qy_emergency_response_monitor` 表结构与字段不变。
- 同事代码（共享 yangxiao.csv、HHLY_JUECE 拉取）零改动。

## 7. 不可做

- 不改 `stationProcessMin.py`/`stationProcess.py` 的 `HHLY_JUECE` 与共享 CSV。
- 不改 `compute_emergency_response_stats` 的阈值、占比阈值、响应级别规则。
- 不改 `QyEmergencyResponseMonitor` 表模型。