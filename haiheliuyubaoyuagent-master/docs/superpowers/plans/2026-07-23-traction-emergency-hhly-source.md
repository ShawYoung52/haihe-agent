# 牵引应急响应 HHLY 数据源改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 牵引智能体应急响应改用 HHLY 流域数据源 + 国家站 2 位口径 11/12/13/16，新增进程内 HHLY 拉取函数，旧 CSV 链路保留向下兼容。

**Architecture:** 仅改 `hhlyqyxt-master/ScheduledTask/emergency_response_monitor.py`：新增 `_fetch_hhly_rainfall_for_emergency` 复用牵引侧 `utils.MusicTool.MusicClient` 拉 HHLY 5 分钟降水；`NATIONAL_STATION_LEVELS` 改 2 位、`_normalize_station_level` 去前导零；`compute_emergency_response_stats`/`run_emergency_response_monitor` 扩容接受 DataFrame/timerange。阈值与响应级别规则、入库逻辑、表结构完全不动。

**Tech Stack:** Python 3.10+、pandas、pytest、牵引侧 utils.MusicTool.MusicClient（天擎 MUSIC API）。

**Spec:** `docs/superpowers/specs/2026-07-23-traction-emergency-hhly-source-design.md`

## Global Constraints

- **强约束：代码必须写进 `hhlyqyxt-master/ScheduledTask/emergency_response_monitor.py` 本身**；只复用牵引侧 `utils.MusicTool.MusicClient/MusicConfig`（同在 hhlyqyxt-master 仓库，内网同部署）；**严禁跨仓库 import 问答侧（haihe-weather-analyzer-mcp）模块**——内网服务器上两项目不并排部署，跨仓库 import 生产必崩。
- 改造范围仅应急响应；`stationProcessMin.py`/`stationProcess.py` 的 `HHLY_JUECE` 与共享 `yangxiao.csv` 一律不动（同事代码）。
- 阈值常量（50/100/250mm）、占比阈值（0.20/0.15）、响应级别 1-4 规则、`QyEmergencyResponseMonitor` 表结构与字段一律不动。
- 旧 CSV 链路保留向下兼容（现有 `stationProcessMin.py:444` 调用不改、不崩）；timerange 与 csv_path 同传时 timerange 优先并 WARNING；都不传时 ValueError。
- 测试运行从 `hhlyqyxt-master` 目录用 `python -m pytest utils/tests/ -v`（本机隔离 venv `D:\PythonProject\.venv-haihe-tests\Scripts\python.exe` 已装 pandas/pytest）。
- git 提交只按文件精确 add，严禁 `git add -A`（工作区有大量无关删除）；直接在 main 上提交。
- 归一化对 None/非数字宽容（返回 `"0"`，不命中国家站集，作非国家站处理）。
- 错误文本不含内网 IP/路径（沿用脱敏约定）。

---

### Task 1: 国家站口径改为 2 位 11/12/13/16（去前导零）

**Files:**
- Modify: `hhlyqyxt-master/ScheduledTask/emergency_response_monitor.py:20-34`（`NATIONAL_STATION_LEVELS` 与 `_normalize_station_level`）
- Test: `hhlyqyxt-master/utils/tests/test_emergency_response_monitor.py:174-188`（`test_station_levl_normalization`）

**Interfaces:**
- Consumes: 无（基础常量）。
- Produces: `NATIONAL_STATION_LEVELS = {"11","12","13","16"}`；`_normalize_station_level(value) -> str` 去前导零（011→11、11→11、'016'→16、None→'0'）。后续 Task 的拉取与判定都用它筛国家站。

- [ ] **Step 1: 写失败测试（更新归一化断言）**

将 `test_emergency_response_monitor.py` 的 `test_station_levl_normalization` 替换为：

```python
def test_station_levl_normalization(make_csv):
    """Station_levl 应支持 '11'、'011'、11 等多种输入形式，归一为去前导零的 2 位口径 11/12/13/16。"""
    rows = [
        {"Station_Id_C": "A", "Datetime": "2026-07-15 09:00:00", "PRE": 60.0, "Station_levl": "11"},
        {"Station_Id_C": "B", "Datetime": "2026-07-15 09:00:00", "PRE": 60.0, "Station_levl": "011"},
        {"Station_Id_C": "C", "Datetime": "2026-07-15 09:00:00", "PRE": 60.0, "Station_levl": 11},
        {"Station_Id_C": "D", "Datetime": "2026-07-15 09:00:00", "PRE": 60.0, "Station_levl": "013"},
        {"Station_Id_C": "E", "Datetime": "2026-07-15 09:00:00", "PRE": 60.0, "Station_levl": "016"},
        {"Station_Id_C": "F", "Datetime": "2026-07-15 09:00:00", "PRE": 60.0, "Station_levl": "014"},
    ]
    csv_path = make_csv(rows, datatime="2026-07-15 10:00:00")
    # 归一为 2 位：11/11/11/13/16/14；国家级口径 {11,12,13,16} 命中前 5 个
    assert erm._normalize_station_level("011") == "11"
    assert erm._normalize_station_level("11") == "11"
    assert erm._normalize_station_level(11) == "11"
    assert erm._normalize_station_level("016") == "16"
    assert erm._normalize_station_level("014") == "14"
    assert erm._normalize_station_level(None) == "0"

    result = erm.compute_emergency_response_stats(csv_path, "2026-07-15 10:00:00")
    assert result["total_national_stations"] == 5
    assert result["station_12h_baoyu"] == 5
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /d D:\PythonProject\haiheliuyubaoyuagent-master\hhlyqyxt-master && python -m pytest utils/tests/test_emergency_response_monitor.py::test_station_levl_normalization -v`
Expected: FAIL（`assert '011' == '11'` —— 现有 `_normalize_station_level` 返回补零的 3 位）

- [ ] **Step 3: 实现去前导零归一化**

`emergency_response_monitor.py:20` 处常量与 28-34 行函数替换为：

```python
NATIONAL_STATION_LEVELS = {"11", "12", "13", "16"}


def _normalize_station_level(value) -> str:
    """归一为去前导零的 2 位字符串：011→11、11→11、'016'→16；None/空→'0'。"""
    try:
        return str(int(value))
    except (ValueError, TypeError):
        text = str(value or "").strip()
        return text.lstrip("0") or "0"
```

- [ ] **Step 4: 运行全文件测试确认通过**

Run: `cd /d D:\PythonProject\haiheliuyubaoyuagent-master\hhlyqyxt-master && python -m pytest utils/tests/test_emergency_response_monitor.py -v`
Expected: 全部 PASS（所有用 011/012/013/016 输入的既有用例因去前导零仍命中 2 位集合，行为不变；`test_station_levl_normalization` 新断言通过）

- [ ] **Step 5: 提交**

```bash
git add hhlyqyxt-master/ScheduledTask/emergency_response_monitor.py hhlyqyxt-master/utils/tests/test_emergency_response_monitor.py
git commit -m "feat(emergency): use 2-digit national station level 11/12/13/16 (strip leading zeros)"
```

---

### Task 2: 新增 HHLY 拉取函数 `_fetch_hhly_rainfall_for_emergency`

**Files:**
- Modify: `hhlyqyxt-master/ScheduledTask/emergency_response_monitor.py`（文件顶部 import 区 + 新增函数）
- Test: `hhlyqyxt-master/utils/tests/test_emergency_response_monitor.py`（新增 fetch 测试）

**Interfaces:**
- Consumes: `utils.MusicTool.MusicClient`、`utils.MusicTool.MusicConfig`（牵引侧，同仓库）；Task 1 的 `_normalize_station_level`。
- Produces:
  - `_fetch_hhly_rainfall_for_emergency(timerange: str, client: Optional[Any] = None) -> pd.DataFrame`
  - 常量 `HHLY_BASIN_CODES = "HHLY"`、`HHLY_MIN_DATA_CODE = "SURF_CHN_MUL_MIN"`、`HHLY_MIN_ELEMENTS`（含 Station_levl）
  - 返回 DataFrame 列：`Station_Id_C,Datetime,PRE,Station_levl,Lon,Lat,City,Station_Name,Cnty,Province,Town`；空/失败→空 DataFrame。

- [ ] **Step 1: 写失败测试**

在 `test_emergency_response_monitor.py` 末尾追加：

```python
def _fake_records(pres=("60.0",), station_levels=("011",)):
    rows = []
    for sid, pre, lev in zip(["A", "B", "C"], pres, station_levels):
        rows.append({
            "Station_Id_C": sid, "Datetime": "2026-07-15 09:00:00", "PRE": pre,
            "Station_levl": lev, "Lat": "39.0", "Lon": "117.0",
            "City": "天津市", "Station_Name": f"站{sid}", "Cnty": "测试区",
            "Province": "天津市", "Town": "测试镇",
        })
    return rows


def test_fetch_hhly_rainfall_uses_hhly_basin_and_min_data_code(monkeypatch):
    """拉取必须 basin_codes=HHLY、data_code=SURF_CHN_MUL_MIN，并含 Station_levl。"""
    captured = {}

    class FakeClient:
        def get_surf_pre_in_basin_timerange(self, basin_codes, timeRange, elements=None,
                                            data_code=None, **kwargs):
            captured.update(basin_codes=basin_codes, timeRange=timeRange, elements=elements,
                            data_code=data_code)
            return _fake_records()

    df = erm._fetch_hhly_rainfall_for_emergency("[20260715000000,20260715100000]", client=FakeClient())
    assert captured["basin_codes"] == "HHLY"
    assert captured["data_code"] == "SURF_CHN_MUL_MIN"
    assert "Station_levl" in captured["elements"]
    assert list(df["Station_Id_C"]) == ["A", "B", "C"]
    assert "Station_levl" in df.columns and "PRE" in df.columns


def test_fetch_hhly_rainfall_instantiates_client_when_none(monkeypatch):
    """client=None 时应现场实例化牵引侧 MusicClient(MusicConfig())。"""
    sentinel = object()
    created = {}

    class FakeConfig:
        pass

    class FakeClient:
        def __init__(self, cfg):
            created["cfg"] = cfg
        def get_surf_pre_in_basin_timerange(self, basin_codes, timeRange, elements=None,
                                           data_code=None, **kwargs):
            return _fake_records()

    monkeypatch.setattr(erm, "MusicConfig", FakeConfig)
    monkeypatch.setattr(erm, "MusicClient", FakeClient)
    df = erm._fetch_hhly_rainfall_for_emergency("[20260715000000,20260715100000]")
    assert "cfg" in created
    assert len(df) == 3


def test_fetch_hhly_rainfall_empty_returns_empty_dataframe(monkeypatch):
    class FakeClient:
        def get_surf_pre_in_basin_timerange(self, basin_codes, timeRange, **kwargs):
            return []
    df = erm._fetch_hhly_rainfall_for_emergency("[20260715000000,20260715100000]", client=FakeClient())
    assert df.empty
    for col in ("Station_Id_C", "Datetime", "PRE", "Station_levl"):
        assert col in df.columns


def test_fetch_hhly_rainfall_propagates_api_error(monkeypatch):
    class FakeClient:
        def get_surf_pre_in_basin_timerange(self, basin_codes, timeRange, **kwargs):
            raise RuntimeError("天擎超时")
    with pytest.raises(RuntimeError, match="天擎超时"):
        erm._fetch_hhly_rainfall_for_emergency("[20260715000000,20260715100000]", client=FakeClient())
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /d D:\PythonProject\haiheliuyubaoyuagent-master\hhlyqyxt-master && python -m pytest utils/tests/test_emergency_response_monitor.py -v -k fetch_hhly`
Expected: FAIL（`AttributeError: module ... has no attribute '_fetch_hhly_rainfall_for_emergency'`）

- [ ] **Step 3: 实现拉取函数**

`emergency_response_monitor.py` 顶部 import 区（在 `from utils.db import Session` 附近）新增牵引侧导入：

```python
from utils.MusicTool import MusicClient, MusicConfig
```

在 `NATIONAL_STATION_LEVELS` 常量区下方新增：

```python
# 应急响应独立数据源：海河流域（HHLY）分钟降水
HHLY_BASIN_CODES = "HHLY"
HHLY_MIN_DATA_CODE = "SURF_CHN_MUL_MIN"
HHLY_MIN_ELEMENTS = (
    "Station_levl,Lat,Lon,Alti,Station_Id_C,Datetime,IYMDHM,RYMDHM,UPDATE_TIME,"
    "City,Station_Name,Cnty,NetCode,Province,REGIONCODE,Town,Year,Mon,Day,Hour,Min,PRE"
)
```

在 `compute_emergency_response_stats` 函数定义之前新增：

```python
def _fetch_hhly_rainfall_for_emergency(
    timerange: str,
    client: Optional[Any] = None,
) -> pd.DataFrame:
    """独立拉取 HHLY 流域 5 分钟降水，供应急响应内部消费。

    timerange 形如 "[20260722080000,20260723080000]"。client 为 None 时
    现场实例化牵引侧 MusicClient(MusicConfig())。返回含
    Station_Id_C/Datetime/PRE/Station_levl 等列的 DataFrame；
    空结果返回带列的空 DataFrame；天擎异常原样向上传播由调用方处理。
    """
    own_client = client
    if own_client is None:
        own_client = MusicClient(MusicConfig())
    records = own_client.get_surf_pre_in_basin_timerange(
        basin_codes=HHLY_BASIN_CODES,
        timeRange=timerange,
        elements=HHLY_MIN_ELEMENTS,
        data_code=HHLY_MIN_DATA_CODE,
    )
    columns = HHLY_MIN_ELEMENTS.split(",")
    if not records:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(records, columns=columns)
```

**注**：`Optional`/`Any` 已在文件顶部 `from typing import Optional, Union` 中导入；若该 import 不含 `Any`，将其改为 `from typing import Any, Optional, Union`。pandas 已 `import pandas as pd`。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /d D:\PythonProject\haiheliuyubaoyuagent-master\hhlyqyxt-master && python -m pytest utils/tests/test_emergency_response_monitor.py -v -k fetch_hhly`
Expected: 4 PASS

- [ ] **Step 5: 提交**

```bash
git add hhlyqyxt-master/ScheduledTask/emergency_response_monitor.py hhlyqyxt-master/utils/tests/test_emergency_response_monitor.py
git commit -m "feat(emergency): add HHLY rainfall fetch for emergency response"
```

---

### Task 3: `compute_emergency_response_stats` 扩容接受 DataFrame

**Files:**
- Modify: `hhlyqyxt-master/ScheduledTask/emergency_response_monitor.py:86-170`（`compute_emergency_response_stats`）
- Test: `hhlyqyxt-master/utils/tests/test_emergency_response_monitor.py`

**Interfaces:**
- Consumes: Task 1 国家站集合；现 `compute_emergency_response_stats(csv_path, datatime)`。
- Produces: `compute_emergency_response_stats(source, datatime=None)`，`source` 接受 CSV 路径（str）或 DataFrame；判定结果字典结构不变。

- [ ] **Step 1: 写失败测试**

在末尾追加：

```python
def test_compute_stats_accepts_dataframe_equivalent_to_csv(make_csv):
    """compute_emergency_response_stats 应接受 DataFrame，结果与等价 CSV 一致。"""
    rows = [
        {"Station_Id_C": "A", "Datetime": "2026-07-15 08:00:00", "PRE": 40.0, "Station_levl": "011"},
        {"Station_Id_C": "A", "Datetime": "2026-07-15 09:00:00", "PRE": 40.0, "Station_levl": "011"},
        {"Station_Id_C": "B", "Datetime": "2026-07-15 09:00:00", "PRE": 10.0, "Station_levl": "011"},
    ]
    csv_path = make_csv(rows, datatime="2026-07-15 10:00:00")
    csv_result = erm.compute_emergency_response_stats(csv_path, "2026-07-15 10:00:00")

    df = pd.DataFrame(rows)
    df["Datetime"] = pd.to_datetime(df["Datetime"])
    df["PRE"] = pd.to_numeric(df["PRE"], errors="coerce")
    df_result = erm.compute_emergency_response_stats(df, "2026-07-15 10:00:00")

    assert df_result["total_national_stations"] == csv_result["total_national_stations"]
    assert df_result["station_12h_baoyu"] == csv_result["station_12h_baoyu"]
    assert df_result["response_level"] == csv_result["response_level"]


def test_compute_stats_empty_dataframe_returns_none():
    df = pd.DataFrame(columns=["Station_Id_C", "Datetime", "PRE", "Station_levl"])
    assert erm.compute_emergency_response_stats(df, "2026-07-15 10:00:00") is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /d D:\PythonProject\haiheliuyubaoyuagent-master\hhlyqyxt-master && python -m pytest utils/tests/test_emergency_response_monitor.py -v -k "accepts_dataframe or empty_dataframe"`
Expected: FAIL（传 DataFrame 会撞到 `pd.read_csv(df)` ——现有实现假设 str 路径）

- [ ] **Step 3: 重构 `compute_emergency_response_stats` 支持双入口**

将现有 `compute_emergency_response_stats(csv_path, datatime=None)`（86-170 行）整体替换为（保留全部判定逻辑、只改数据来源加载段）：

```python
def compute_emergency_response_stats(
    source: Union[str, "_PathLikeAndDf"] = None,
    datatime: Union[str, datetime, None] = None,
) -> Optional[dict]:
    """从 CSV 路径或 DataFrame 计算应急响应统计指标。

    Args:
        source: 5 分钟降水 CSV 文件路径，或已含 Station_Id_C/Datetime/PRE/Station_levl 等列的 DataFrame。
        datatime: 统计结束时间，窗口为 (datatime - 12h/24h, datatime]；None 时用数据最大时间。

    Returns:
        各阈值站点数、占比和响应级别字典；数据为空或无可解析内容时返回 None。
    """
    if isinstance(source, pd.DataFrame):
        df = source.copy()
    else:
        try:
            df = pd.read_csv(source)
        except pd.errors.EmptyDataError:
            return None

    if df is None or df.empty:
        return None

    if "Station_levl" not in df.columns:
        logger.warning("数据缺少 Station_levl 列，全部站点按非国家站处理")
        df["Station_levl"] = ""

    df["Datetime"] = pd.to_datetime(df["Datetime"], errors="coerce")
    df = df.dropna(subset=["Datetime"])
    if df.empty:
        return None
    if datatime is None:
        datatime = df["Datetime"].max()

    end_time = _parse_datatime(datatime)
    start_12h = end_time - timedelta(hours=12)
    start_24h = end_time - timedelta(hours=24)

    df["PRE"] = pd.to_numeric(df["PRE"], errors="coerce")
    df.loc[df["PRE"] > 99988, "PRE"] = 0.0
    df = df.dropna(subset=["PRE"])
    if df.empty:
        return None
    df["Station_levl_norm"] = df["Station_levl"].apply(_normalize_station_level)

    national_df = df[df["Station_levl_norm"].isin(NATIONAL_STATION_LEVELS)].copy()
    if national_df.empty:
        return None

    window_24h = national_df[(national_df["Datetime"] > start_24h) & (national_df["Datetime"] <= end_time)]
    window_12h = national_df[(national_df["Datetime"] > start_12h) & (national_df["Datetime"] <= end_time)]

    sum_pre_24h = _sum_precip_by_station(window_24h)
    sum_pre_12h = _sum_precip_by_station(window_12h)

    total = len(sum_pre_24h)

    station_12h_baoyu = _count_by_threshold(sum_pre_12h["PRE"], BAOYU_LOWER, DABAOYU_LOWER)
    station_24h_baoyu = _count_by_threshold(sum_pre_24h["PRE"], BAOYU_LOWER, DABAOYU_LOWER)
    station_24h_dabaoyu = _count_by_threshold(sum_pre_24h["PRE"], DABAOYU_LOWER, TEDABAOYU_LOWER)
    station_24h_tedabaoyu = _count_by_threshold(sum_pre_24h["PRE"], TEDABAOYU_LOWER)

    ratio_12h_baoyu = _ratio(station_12h_baoyu, total)
    ratio_24h_baoyu = _ratio(station_24h_baoyu, total)
    ratio_24h_dabaoyu = _ratio(station_24h_dabaoyu, total)
    ratio_24h_tedabaoyu = _ratio(station_24h_tedabaoyu, total)

    response_level = _determine_response_level(
        ratio_12h_baoyu, ratio_24h_baoyu, ratio_24h_dabaoyu, ratio_24h_tedabaoyu
    )

    return {
        "datatime": end_time,
        "total_national_stations": total,
        "station_12h_baoyu": station_12h_baoyu,
        "ratio_12h_baoyu": ratio_12h_baoyu,
        "station_24h_baoyu": station_24h_baoyu,
        "ratio_24h_baoyu": ratio_24h_baoyu,
        "station_24h_dabaoyu": station_24h_dabaoyu,
        "ratio_24h_dabaoyu": ratio_24h_dabaoyu,
        "station_24h_tedabaoyu": station_24h_tedabaoyu,
        "ratio_24h_tedabaoyu": ratio_24h_tedabaoyu,
        "response_level": response_level,
    }
```

签名去掉不存在的 `_PathLikeAndDf` 类型别名，直接用 `Union[str, pd.DataFrame]`：

```python
def compute_emergency_response_stats(
    source: Union[str, pd.DataFrame] = None,
    datatime: Union[str, datetime, None] = None,
) -> Optional[dict]:
```

- [ ] **Step 4: 运行全测试确认通过**

Run: `cd /d D:\PythonProject\haiheliuyubaoyuagent-master\hhlyqyxt-master && python -m pytest utils/tests/test_emergency_response_monitor.py -v`
Expected: 全部 PASS（既有 CSV 用例 + 新 DataFrame 用例；缺失 Station_levl 列用例经 `dropna(Datetime)` 仍走原分支）

- [ ] **Step 5: 提交**

```bash
git add hhlyqyxt-master/ScheduledTask/emergency_response_monitor.py hhlyqyxt-master/utils/tests/test_emergency_response_monitor.py
git commit -m "feat(emergency): accept DataFrame in compute_emergency_response_stats"
```

---

### Task 4: `run_emergency_response_monitor` 扩容支持 timerange HHLY 自拉链路

**Files:**
- Modify: `hhlyqyxt-master/ScheduledTask/emergency_response_monitor.py:173-216`（`run_emergency_response_monitor`）
- Test: `hhlyqyxt-master/utils/tests/test_emergency_response_monitor.py`

**Interfaces:**
- Consumes: Task 2 `_fetch_hhly_rainfall_for_emergency`；Task 3 `compute_emergency_response_stats(source)`。
- Produces: `run_emergency_response_monitor(csv_path: Optional[str]=None, datatime=None, minute_monitor_id=None, timerange: Optional[str]=None, client: Optional[Any]=None)`；timerange 走 HHLY 自拉→compute(DataFrame)→入库。

- [ ] **Step 1: 写失败测试**

在末尾追加：

```python
def test_run_emergency_response_monitor_timerange_uses_hhly_fetch(monkeypatch):
    """传 timerange 时走 HHLY 自拉链路并入库。"""
    fetched = {"called": False}

    def fake_fetch(timerange, client=None):
        fetched["called"] = True
        fetched["timerange"] = timerange
        return pd.DataFrame([
            {"Station_Id_C": "A", "Datetime": "2026-07-15 09:00:00",
             "PRE": 60.0, "Station_levl": "011", "Lat": "39.0", "Lon": "117.0",
             "City": "天津市", "Station_Name": "站A", "Cnty": "测试区",
             "Province": "天津市", "Town": "测试镇"},
        ])

    monkeypatch.setattr(erm, "_fetch_hhly_rainfall_for_emergency", fake_fetch)
    mock_session = MagicMock()
    monkeypatch.setattr(erm, "Session", MagicMock(return_value=mock_session))

    result = erm.run_emergency_response_monitor(
        timerange="[20260715000000,20260715100000]",
        datatime="2026-07-15 10:00:00",
        minute_monitor_id=7,
    )
    assert fetched["called"] is True
    assert fetched["timerange"] == "[20260715000000,20260715100000]"
    assert isinstance(result, QyEmergencyResponseMonitor)
    assert result.minute_monitor_id == 7
    assert result.total_national_stations == 1
    mock_session.add.assert_called_once_with(result)
    mock_session.commit.assert_called_once()


def test_run_emergency_response_monitor_timerange_priority_over_csv_warns(make_csv, monkeypatch, caplog):
    """同时传 timerange 与 csv 时 timerange 优先，并 WARNING 提示忽略 CSV。"""
    csv_path = make_csv([], datatime="2026-07-15 10:00:00")
    monkeypatch.setattr(erm, "_fetch_hhly_rainfall_for_emergency",
                        lambda timerange, client=None: pd.DataFrame(
                            columns=["Station_Id_C", "Datetime", "PRE", "Station_levl"]))
    monkeypatch.setattr(erm, "Session", MagicMock())

    with caplog.at_level("WARNING", logger="ScheduledTask.emergency_response_monitor"):
        erm.run_emergency_response_monitor(
            csv_path=csv_path,
            timerange="[20260715000000,20260715100000]",
            datatime="2026-07-15 10:00:00",
        )
    assert "timerange" in caplog.text and "csv" in caplog.text.lower()


def test_run_emergency_response_monitor_requires_source(monkeypatch):
    """csv_path 与 timerange 都不传时应抛 ValueError。"""
    monkeypatch.setattr(erm, "_fetch_hhly_rainfall_for_emergency",
                        lambda *a, **k: pd.DataFrame())
    with pytest.raises(ValueError, match="csv_path 或 timerange"):
        erm.run_emergency_response_monitor(datatime="2026-07-15 10:00:00")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /d D:\PythonProject\haiheliuyubaoyuagent-master\hhlyqyxt-master && python -m pytest utils/tests/test_emergency_response_monitor.py -v -k "timerange or requires_source"`
Expected: FAIL（`run_emergency_response_monitor() got unexpected keyword argument 'timerange'`）

- [ ] **Step 3: 扩容 `run_emergency_response_monitor`**

将现有签名与函数体（173-216 行）替换为（保留先删后插入库逻辑不变，只前置数据来源分支）：

```python
def run_emergency_response_monitor(
    csv_path: Optional[str] = None,
    datatime: Union[str, datetime, None] = None,
    minute_monitor_id: Optional[int] = None,
    timerange: Optional[str] = None,
    client: Optional[Any] = None,
) -> Optional[QyEmergencyResponseMonitor]:
    """计算应急响应指标并写入数据库。

    数据来源二选一：
    - timerange：独立拉取 HHLY 流域分钟降水后判定（推荐）；
    - csv_path：从 5 分钟降水 CSV 判定（旧链路，向下兼容）。
    同时提供 timerange 与 csv_path 时，timerange 优先并记录 WARNING 日志；
    都不传时抛 ValueError。同 datatime 的记录先删后插，保证重跑无重复行。

    Args:
        csv_path: 5 分钟降水 CSV 文件路径（旧链路）。
        datatime: 统计结束时间，默认为数据最大时间。
        minute_monitor_id: 关联的分钟监测记录 ID。
        timerange: HHLY 拉取时间窗，形如 "[20260722080000,20260723080000]"（新链路）。
        client: 已实例化的 MusicClient，None 时现场实例化。

    Returns:
        写入的 ORM 对象；数据不存在/为空时返回 None。
    """
    if timerange and csv_path:
        logger.warning("同时提供 timerange 与 csv_path，优先使用 timerange（HHLY），忽略 csv_path=%s", csv_path)
    if not timerange and not csv_path:
        raise ValueError("run_emergency_response_monitor 需要提供 csv_path 或 timerange 之一")

    if timerange:
        df = _fetch_hhly_rainfall_for_emergency(timerange, client=client)
        if df is None or df.empty:
            logger.warning("HHLY 拉取为空，不写表：timerange=%s", timerange)
            return None
        stats = compute_emergency_response_stats(df, datatime)
    else:
        if not Path(csv_path).exists():
            logger.warning("CSV 文件不存在: %s", csv_path)
            return None
        stats = compute_emergency_response_stats(csv_path, datatime)

    if stats is None:
        logger.warning("数据为空，未写入应急响应记录")
        return None

    record = QyEmergencyResponseMonitor(minute_monitor_id=minute_monitor_id, **stats)
    session = Session()
    try:
        session.query(QyEmergencyResponseMonitor).filter(
            QyEmergencyResponseMonitor.datatime == stats["datatime"]
        ).delete()
        session.add(record)
        session.commit()
        session.refresh(record)
        return record
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

**注**：`Any` 已在 Task 2 确认导入；`Path` 已 `from pathlib import Path`。

- [ ] **Step 4: 运行全测试确认通过**

Run: `cd /d D:\PythonProject\haiheliuyubaoyuagent-master\hhlyqyxt-master && python -m pytest utils/tests/test_emergency_response_monitor.py -v`
Expected: 全部 PASS（既有 CSV 用例因 `csv_path` 仍作为首参、`timerange=None` 走旧链路；新增 timerange/priority/requires_source 三例通过）

- [ ] **Step 5: 提交**

```bash
git add hhlyqyxt-master/ScheduledTask/emergency_response_monitor.py hhlyqyxt-master/utils/tests/test_emergency_response_monitor.py
git commit -m "feat(emergency): support timerange HHLY fetch path in run_emergency_response_monitor"
```

---

### Task 5: 全链路回归 + 质量流程

**Files:** 无改动，仅验证与质量闭环。

- [ ] **Step 1: 牵引应急响应测试全量回归**

Run: `cd /d D:\PythonProject\haiheliuyubaoyuagent-master\hhlyqyxt-master && python -m pytest utils/tests/test_emergency_response_monitor.py -v`
Expected: 全部 PASS

- [ ] **Step 2: 牵引仓库其余测试无回归**

Run: `cd /d D:\PythonProject\haiheliuyubaoyuagent-master\hhlyqyxt-master && python -m pytest utils/tests/test_rainfall_impact_geojson.py -v`
Expected: 41/42 PASS（与上次一致，确认未相互影响）

- [ ] **Step 3: code-review（双代理：CLAUDE.md 合规 + 正确性）**

按 CLAUDE.md Superpowers Integration：`code-review` 工具审查 `git diff origin/main..HEAD` 未推送提交；或并行代理审 CLAUDE.md 合规、git 历史、bug。重点核对：
- 不得跨仓库 import 问答侧模块（强约束）；
- 旧 CSV 调用方 `stationProcessMin.py:444` 仍可工作（未传 timerange → 旧链路）；
- 国家站计数在 2 位口径下与旧值一致（去前导零等价）；
- 错误文本无内网 IP/路径。
修复确认的问题。

- [ ] **Step 4: code-simplifier**

审查本次改动可简化点（去重、过度防御、YAGNI），应用合理建议。

- [ ] **Step 5: superpowers:verification-before-completion**

全新重跑 Step 1-2 验证命令，确认无 FAIL 后再声明完成。

- [ ] **Step 6: claude-md-management:revise-claude-md**

把应急响应 HHLY 数据源约定（`_fetch_hhly_rainfall_for_emergency`、`HHLY_BASIN_CODES="HHLY"`、2 位国家站口径、timerange 新链路、旧 CSV 向下兼容）补入 `hhlyqyxt-master` 的 CLAUDE.md（若有）或主 CLAUDE.md 牵引智能体条目处。

- [ ] **Step 7: claude-mem 记忆**

写入本次应急响应改造的项目记忆（HHLY 数据源切换决策、2 位口径、独立拉取不碰同事代码、不得跨仓库 import 约束）。

- [ ] **Step 8: git push**

```bash
git push origin main
```

---

## Self-Review 记录

- **Spec 覆盖**：§3.1 常量/归一化→Task 1；§3.1 拉取接口→Task 2；§3.1 compute 扩容→Task 3；§3.1 run 扩容与二选一/优先/ValueError→Task 4；§4 错误处理→Task 2/4 空分支与异常传播；§5 测试→各 Task Step 1 + Task 5；§6/§7 兼容与不可做→Global Constraints + Task 4 优先级/保留旧链路 + 显式禁止 stationProcess 改动。
- **类型一致性**：`_fetch_hhly_rainfall_for_emergency(timerange, client=None) → DataFrame`、`compute_emergency_response_stats(source, datatime=None) → Optional[dict]`、`run_emergency_response_monitor(csv_path, datatime, minute_monitor_id, timerange, client)` 三处签名 Produces/Consumes 一致；`NATIONAL_STATION_LEVELS={"11","12","13","16"}`、`HHLY_BASIN_CODES="HHLY"` 常量名贯穿后续 Task。
- **占位符扫描**：无 TBD/TODO；每步含完整代码或精确命令。
- **强约束落实**：所有 import 限定 `utils.MusicTool`（同仓库），无 `haihe-weather-analyzer-mcp` 引用；Task 5 code-review 把"不得跨仓库 import"列为重点核对项。