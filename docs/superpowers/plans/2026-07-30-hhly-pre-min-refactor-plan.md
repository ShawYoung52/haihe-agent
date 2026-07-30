# HHLY SURF_CHN_PRE_MIN + Q_PRE 过滤 + 内存聚合 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** 牵引应急响应改用 `SURF_CHN_PRE_MIN` + `Q_PRE` 过滤 + 内存聚合，替代当前的 `SURF_CHN_MUL_MIN` + 5min resample。消除 `PRE="000"` 字符串拼接、`Station_levl` 跨时刻不一致等 bug。

**Architecture:** 新增 `precipitation_aggregator.py` 提供 `aggregate_minute_precipitation()`（参考问答智能体，独立实现）。`_append_hhly_5min_to_rolling_csv` 改为直接追加原始 1min 数据到 CSV，不做 resample。应急判定时读 CSV → 内存聚合 → 判定 → 入库。

**Tech Stack:** pandas / stdlib / pytest

## Global Constraints

- **不跨仓库 import 问答智能体模块**（[[traction-emergency-hhly-source]]），`aggregate_minute_precipitation` 独立实现在牵引侧
- 仅改牵引侧 3 个文件 + 新增 2 个文件（1 源码 + 1 测试）
- **HHLY_JUECE 链路 `24hourmindata.csv` 完全不动**
- `qy_emergency_response_monitor` 表字段/口径不变（response_level、station_XXh_baoyu、ratio_XXh_baoyu 等）
- `Q_PRE` 可信标志默认 `{"0","3","4"}`（与问答侧一致）
- 单元测试用 `.venv/Scripts/python.exe` 绝对路径执行

## Files

**新增**：
- `hhlyqyxt-master/ScheduledTask/precipitation_aggregator.py`（聚合工具，独立实现）
- `hhlyqyxt-master/ScheduledTask/tests/test_precipitation_aggregator.py`

**修改**：
- `hhlyqyxt-master/ScheduledTask/emergency_response_monitor.py`（换数据源 + 聚合方式）
- `hhlyqyxt-master/ScheduledTask/stationProcessMin.py`（`_append_hhly_5min_to_rolling_csv` 简化）
- `hhlyqyxt-master/ScheduledTask/tests/test_hhly_rolling_csv.py`（改写断言）

---

### Task 1: 新增 precipitation_aggregator.py

**Files:**
- Create: `hhlyqyxt-master/ScheduledTask/precipitation_aggregator.py`
- Create: `hhlyqyxt-master/ScheduledTask/tests/test_precipitation_aggregator.py`

**Interfaces:**
- Consumes: 无（stdlib + pandas 数据类型）
- Produces: `aggregate_minute_precipitation(records, end_time, windows_hours=(1,12,24), trusted_q_pre=frozenset({"0","3","4"})) -> list[dict]`

- [ ] **Step 1: 写测试文件 `test_precipitation_aggregator.py`**

```python
"""precipitation_aggregator 单元测试。

参考问答智能体 aggregate_minute_precipitation 的语义，牵引侧独立实现。
"""
from datetime import datetime, timedelta

import pytest

from ScheduledTask.precipitation_aggregator import aggregate_minute_precipitation


def _rec(sid: str, dt: datetime, pre, q_pre="0", station_levl="12", **extra):
    return {
        "Station_Id_C": sid,
        "Datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "Year": str(dt.year), "Mon": f"{dt.month:02d}", "Day": f"{dt.day:02d}",
        "Hour": f"{dt.hour:02d}", "Min": f"{dt.minute:02d}",
        "PRE": pre, "Q_PRE": q_pre,
        "Station_levl": station_levl, "Lat": "39.0", "Lon": "117.0",
        "City": "天津市", "Station_Name": "测试站", "Cnty": "南开",
        "Province": "天津", "Town": "",
        **extra,
    }


def test_aggregate_windows_correct():
    """1h/12h/24h 累计窗口正确。"""
    end = datetime(2026, 7, 30, 12, 0, 0)
    records = [
        _rec("A", end - timedelta(minutes=30), 1.0),   # 1h/12h/24h
        _rec("A", end - timedelta(hours=6), 2.0),      # 12h/24h
        _rec("A", end - timedelta(hours=20), 3.0),     # 24h
        _rec("A", end - timedelta(hours=30), 100.0),   # 超出 24h，排除
    ]
    out = aggregate_minute_precipitation(records, end_time=end)
    assert len(out) == 1
    row = out[0]
    assert row["Station_Id_C"] == "A"
    assert row["PRE_1h"] == pytest.approx(1.0)
    assert row["PRE_12h"] == pytest.approx(3.0)
    assert row["PRE_24h"] == pytest.approx(6.0)


def test_aggregate_q_pre_filter():
    """Q_PRE 不在 {0,3,4} 的记录被过滤。"""
    end = datetime(2026, 7, 30, 12, 0, 0)
    records = [
        _rec("A", end - timedelta(minutes=10), 5.0, q_pre="0"),
        _rec("A", end - timedelta(minutes=20), 100.0, q_pre="1"),  # 过滤
        _rec("A", end - timedelta(minutes=30), 2.0, q_pre="3"),
        _rec("A", end - timedelta(minutes=40), 100.0, q_pre="9"),  # 过滤
    ]
    out = aggregate_minute_precipitation(records, end_time=end)
    assert len(out) == 1
    assert out[0]["PRE_1h"] == pytest.approx(7.0)  # 只有 q_pre=0/3 保留


def test_aggregate_empty_returns_empty():
    """空输入返回空列表。"""
    assert aggregate_minute_precipitation([], end_time=datetime.now()) == []


def test_aggregate_multi_stations():
    """多站点独立累计。"""
    end = datetime(2026, 7, 30, 12, 0, 0)
    records = [
        _rec("A", end - timedelta(minutes=30), 1.0, station_levl="12"),
        _rec("A", end - timedelta(hours=5), 2.0, station_levl="12"),
        _rec("B", end - timedelta(minutes=30), 10.0, station_levl="16"),
    ]
    out = aggregate_minute_precipitation(records, end_time=end)
    by_id = {r["Station_Id_C"]: r for r in out}
    assert by_id["A"]["PRE_24h"] == pytest.approx(3.0)
    assert by_id["A"]["Station_levl"] == "12"
    assert by_id["B"]["PRE_24h"] == pytest.approx(10.0)
    assert by_id["B"]["Station_levl"] == "16"


def test_aggregate_negative_pre_ignored():
    """PRE < 0 被过滤。"""
    end = datetime(2026, 7, 30, 12, 0, 0)
    records = [
        _rec("A", end - timedelta(minutes=10), 5.0),
        _rec("A", end - timedelta(minutes=20), -999.0),  # 缺测哨兵
    ]
    out = aggregate_minute_precipitation(records, end_time=end)
    assert len(out) == 1
    assert out[0]["PRE_1h"] == pytest.approx(5.0)


def test_aggregate_datetime_object_input():
    """支持 datetime 对象作为 Datetime 字段。"""
    end = datetime(2026, 7, 30, 12, 0, 0)
    records = [
        {"Station_Id_C": "A", "Datetime": end - timedelta(minutes=10),
         "PRE": 3.0, "Q_PRE": "0", "Station_levl": "12", "Lat": 39, "Lon": 117},
    ]
    out = aggregate_minute_precipitation(records, end_time=end)
    assert len(out) == 1
    assert out[0]["PRE_1h"] == pytest.approx(3.0)


def test_aggregate_latest_meta_wins():
    """同站取最新时刻的元信息。"""
    end = datetime(2026, 7, 30, 12, 0, 0)
    records = [
        _rec("A", end - timedelta(minutes=20), 1.0, station_levl="12"),
        _rec("A", end - timedelta(minutes=5), 1.0, station_levl="13"),  # 更新的
        _rec("A", end - timedelta(minutes=10), 1.0, station_levl="14"),
    ]
    out = aggregate_minute_precipitation(records, end_time=end)
    assert out[0]["Station_levl"] == "13"
```

- [ ] **Step 2: 运行确认 FAIL**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/hhlyqyxt-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest ScheduledTask/tests/test_precipitation_aggregator.py -v
```

预期：ModuleNotFoundError（模块不存在）

- [ ] **Step 3: 实现 `precipitation_aggregator.py`**

```python
"""分钟降水聚合工具（牵引侧独立实现，不跨仓库 import 问答智能体）。

参考 haihe-weather-analyzer-mcp/haihe_mcp_tools.py 的
aggregate_minute_precipitation 语义与口径，独立实现在牵引侧
以避免跨仓库依赖（内网服务器不并排放两个仓库）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Optional, Sequence


TRUSTED_Q_PRE: frozenset = frozenset({"0", "3", "4"})


def _parse_datetime(r: dict) -> Optional[datetime]:
    """从记录中解析 Datetime，优先 Datetime 字段，其次 Year/Mon/Day/Hour/Min。"""
    dt = r.get("Datetime")
    if isinstance(dt, datetime):
        return dt
    if dt:
        text = str(dt).strip()
        for fmt in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    try:
        return datetime(
            int(r["Year"]), int(r["Mon"]), int(r["Day"]),
            int(r["Hour"]), int(r.get("Min", 0)), 0,
        )
    except (KeyError, ValueError, TypeError):
        return None


def _safe_float(v: Any, default: float = 0.0) -> float:
    if v is None or v == "" or v == "None":
        return default
    text = str(v).strip()
    if text in {"999999", "999999.0", "999990", "999990.0", "-9999", "-9999.0"}:
        return default
    try:
        return float(text)
    except (ValueError, TypeError):
        return default


def _q_pre_valid(q_pre: Any, trusted: frozenset) -> bool:
    if q_pre is None or str(q_pre).strip() == "":
        return True  # 未标注视为可信
    if not trusted:
        return True
    return str(q_pre).strip() in trusted


def aggregate_minute_precipitation(
    records: Sequence[dict],
    end_time: datetime,
    windows_hours: Sequence[int] = (1, 12, 24),
    trusted_q_pre: frozenset = TRUSTED_Q_PRE,
) -> list[dict]:
    """按站聚合分钟降水累计。

    Args:
        records: 分钟降水记录列表（含 Station_Id_C/Datetime/PRE/Q_PRE 等字段）
        end_time: 聚合窗口结束时刻（BJT）
        windows_hours: 累计窗口小时数，默认 (1, 12, 24)
        trusted_q_pre: 可信 Q_PRE 标志集合，默认 {"0","3","4"}；空集表示不过滤

    Returns:
        每站一条聚合结果，包含：
        - Station_Id_C 及元信息（Lat/Lon/City/Station_Name/Station_levl 等）
        - PRE_{h}h: 各窗口累计降水
        - pre_count_{h}h: 各窗口参与累加的分钟数
    """
    if not records:
        return []

    windows = sorted(set(int(h) for h in windows_hours))
    max_window = max(windows) if windows else 24
    max_cutoff = end_time - timedelta(hours=max_window)
    window_cutoffs = {h: end_time - timedelta(hours=h) for h in windows}

    by_station: dict[str, dict] = {}
    latest_dt: dict[str, datetime] = {}
    station_meta: dict[str, dict] = {}

    for r in records:
        sid = r.get("Station_Id_C")
        if not sid:
            continue
        sid = str(sid).strip()
        if not sid:
            continue

        dt = _parse_datetime(r)
        if dt is None or dt > end_time or dt <= max_cutoff:
            continue

        if not _q_pre_valid(r.get("Q_PRE"), trusted_q_pre):
            continue

        pre = _safe_float(r.get("PRE"))
        if pre < 0:
            continue

        if sid not in by_station:
            by_station[sid] = {f"PRE_{h}h": 0.0 for h in windows}
            for h in windows:
                by_station[sid][f"pre_count_{h}h"] = 0
            latest_dt[sid] = dt
            station_meta[sid] = dict(r)
        elif dt > latest_dt[sid]:
            latest_dt[sid] = dt
            station_meta[sid] = dict(r)

        for h, cutoff in window_cutoffs.items():
            if dt > cutoff:
                by_station[sid][f"PRE_{h}h"] += pre
                by_station[sid][f"pre_count_{h}h"] += 1

    out = []
    for sid, sums in by_station.items():
        row = dict(station_meta[sid])
        row.update(sums)
        row["Station_Id_C"] = sid
        out.append(row)
    return out
```

- [ ] **Step 4: 运行测试确认 PASS**

```bash
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest ScheduledTask/tests/test_precipitation_aggregator.py -v
```

预期：7 passed

- [ ] **Step 5: Commit**

```bash
git add hhlyqyxt-master/ScheduledTask/precipitation_aggregator.py hhlyqyxt-master/ScheduledTask/tests/test_precipitation_aggregator.py
git commit -m "feat(traction): add precipitation_aggregator (minute-level rain accumulation with Q_PRE filter)"
```

---

### Task 2: 改造 `_fetch_hhly_rainfall_for_emergency` 换数据源

**Files:**
- Modify: `hhlyqyxt-master/ScheduledTask/emergency_response_monitor.py`

**Interfaces:**
- Consumes: `MusicClient.get_surf_pre_in_basin_timerange`（现有）
- Produces: 修改常量 `HHLY_MIN_DATA_CODE`、`HHLY_MIN_ELEMENTS`，函数返回类型不变（含 Q_PRE 字段）

- [ ] **Step 1: 修改常量**

在 `emergency_response_monitor.py` 顶部：

```python
# 数据源改为 SURF_CHN_PRE_MIN（分钟降水专用，含 Q_PRE 质量标志），
# 与问答智能体 emergency_api 保持一致，避免 SURF_CHN_MUL_MIN 的
# Station_levl 跨时刻不一致 / PRE 字符串等脏数据问题。
HHLY_MIN_DATA_CODE = "SURF_CHN_PRE_MIN"
HHLY_MIN_ELEMENTS = (
    "Station_Id_C,Station_levl,Lat,Lon,Alti,Admin_Code_CHN,V_ACODE_4SEARCH,Town_code,"
    "City,Station_Name,Cnty,NetCode,Province,REGIONCODE,Town,Country,COUNTRYCODE,"
    "Year,Mon,Day,Hour,Min,Datetime,PRE,Q_PRE,PRE_Sensor_Heigh,Station_Type,"
    "REP_CORR_ID,UPDATE_TIME,D_RETAIN_ID,DATA_ID,D_SOURCE_ID,V08010,RYMDHM,IYMDHM"
)
HHLY_MIN_COLUMNS = HHLY_MIN_ELEMENTS.split(",")
```

- [ ] **Step 2: 确认 `_fetch_hhly_rainfall_for_emergency` 逻辑不变**

函数体不动（仍是 `own_client.get_surf_pre_in_basin_timerange(...)` + `pd.DataFrame(records)`）。因为数据源名和 elements 都是常量，改常量即可。

- [ ] **Step 3: 跑现有 emergency_response_monitor 测试**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/hhlyqyxt-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest utils/tests/test_emergency_response_monitor.py -v
```

预期：30 passed（因为 `_fetch_hhly_rainfall_for_emergency` 在测试里被 mock，函数体没变）

- [ ] **Step 4: Commit**

```bash
git add hhlyqyxt-master/ScheduledTask/emergency_response_monitor.py
git commit -m "feat(traction): switch HHLY data source to SURF_CHN_PRE_MIN (align with QA agent, include Q_PRE)"
```

---

### Task 3: 简化 `_append_hhly_5min_to_rolling_csv` 直接追加原始数据

**Files:**
- Modify: `hhlyqyxt-master/ScheduledTask/stationProcessMin.py`

**Interfaces:**
- Consumes: `_fetch_hhly_rainfall_for_emergency` 返回原始 DataFrame
- Produces: `hhly_tempfile` CSV 存**原始 1min 数据**（不再 resample），含 Q_PRE 列

- [ ] **Step 1: 修改 `_append_hhly_5min_to_rolling_csv` 函数**

删除所有 resample / groupby.agg 逻辑，只做：拉数据 → PRE 转 float → 追加 CSV → 滚动 24h 窗口。

```python
def _append_hhly_5min_to_rolling_csv(end_time: pd.Timestamp) -> None:
    """拉取 HHLY 分钟降水原始数据（1min 粒度），追加到 hhly_tempfile 并滚动 24h 窗口。

    存原始数据不做 5min 聚合，聚合搬到应急判定时（emergency_response_monitor）做。
    避免 resample 引入的 PRE 字符串拼接、Station_levl 不一致等 bug。
    """
    try:
        hhly_raw = _fetch_hhly_rainfall_for_emergency(_music_timerange_5min(end_time))
        if hhly_raw is None or hhly_raw.empty:
            return

        # 补齐可能缺失的元信息列
        for col in ("Station_levl", "Lat", "Lon", "City", "Station_Name",
                    "Cnty", "Province", "Town", "Q_PRE"):
            if col not in hhly_raw.columns:
                hhly_raw[col] = ""

        # PRE 转 float（避免字符串拼接），缺测哨兵置 0
        hhly_raw["PRE"] = pd.to_numeric(hhly_raw["PRE"], errors="coerce").fillna(0.0)
        hhly_raw.loc[hhly_raw["PRE"] > 99988, "PRE"] = 0.0

        # 追加到 CSV + 滚动 24h 窗口
        if os.path.exists(hhly_tempfile):
            existing = pd.read_csv(hhly_tempfile, encoding="utf-8-sig", low_memory=False)
            existing["Datetime"] = pd.to_datetime(existing["Datetime"])
            existing = existing[existing["Datetime"] >= end_time - pd.Timedelta(hours=24)]
            hhly_new = pd.concat([existing, hhly_raw], ignore_index=True)
        else:
            hhly_new = hhly_raw

        # 去除完全重复行（同一站同一分钟多次追加）
        hhly_new = hhly_new.drop_duplicates(
            subset=["Station_Id_C", "Datetime"], keep="last"
        )
        hhly_new = hhly_new.sort_values(
            by=["Station_Id_C", "Datetime"], ascending=[True, False]
        )
        hhly_new.to_csv(hhly_tempfile, index=False, encoding="utf-8-sig")
    except (OSError, requests.exceptions.RequestException, ValueError, KeyError,
            MusicApiError) as e:
        logger.warning("HHLY 分钟数据累积失败（应急响应将跳过本次）：%s", e, exc_info=True)
```

- [ ] **Step 2: 改写测试 `test_hhly_rolling_csv.py`**

将现有 3 条测试改写为原始数据追加逻辑（不 resample）。断言 CSV 里包含每分钟一条记录，PRE 为 float，Q_PRE 列保留：

```python
def _fake_hhly_raw(datetimes, station_id="A", station_levl="11", pre=0.2, q_pre="0"):
    return pd.DataFrame({
        "Station_Id_C": [station_id] * len(datetimes),
        "Datetime": pd.to_datetime(datetimes),
        "PRE": [pre] * len(datetimes),
        "Q_PRE": [q_pre] * len(datetimes),
        "Station_levl": [station_levl] * len(datetimes),
        "Lat": [39.0] * len(datetimes),
        "Lon": [117.0] * len(datetimes),
        "City": ["天津市"] * len(datetimes),
        "Station_Name": ["测试站"] * len(datetimes),
        "Cnty": ["南开"] * len(datetimes),
        "Province": ["天津"] * len(datetimes),
        "Town": ["某镇"] * len(datetimes),
    })


def test_hhly_append_preserves_raw_minute_records(tmp_path, monkeypatch):
    """CSV 应存原始分钟数据（不聚合），Q_PRE 保留。"""
    end_time = pd.Timestamp("2026-07-29 00:05:00")
    fake_raw = _fake_hhly_raw(
        [f"2026-07-29 00:0{i}:00" for i in range(1, 6)],
        station_levl="12",
    )
    hhly_csv = tmp_path / "hhly_test.csv"
    monkeypatch.setattr(spm, "hhly_tempfile", str(hhly_csv))

    with mock.patch(
        "ScheduledTask.stationProcessMin._fetch_hhly_rainfall_for_emergency",
        return_value=fake_raw,
    ):
        spm._append_hhly_5min_to_rolling_csv(end_time)

    df = pd.read_csv(hhly_csv, encoding="utf-8-sig")
    assert len(df) == 5, "应保留 5 条原始分钟记录（不聚合）"
    assert df["PRE"].dtype.kind == "f", "PRE 应为 float"
    assert set(df["Q_PRE"].astype(str)) == {"0"}
    assert set(df["Station_levl"].astype(str)) == {"12"}


def test_hhly_append_rolls_24h_window(tmp_path, monkeypatch):
    """24h 窗口滚动：写入新数据 + 丢弃超过 24h 的旧数据。"""
    hhly_csv = tmp_path / "hhly_test.csv"
    monkeypatch.setattr(spm, "hhly_tempfile", str(hhly_csv))

    old_time = pd.Timestamp("2026-07-27 23:00:00")
    fresh_time = pd.Timestamp("2026-07-28 19:00:00")
    existing = pd.DataFrame({
        "Station_Id_C": ["OLD", "FRESH"],
        "Datetime": [old_time, fresh_time],
        "PRE": [1.0, 2.0],
        "Q_PRE": ["0", "0"],
        "Station_levl": ["12", "12"],
        "Lat": [39.0, 39.0], "Lon": [117.0, 117.0],
        "City": ["天津", "天津"], "Station_Name": ["旧", "新"],
        "Cnty": ["南开", "南开"], "Province": ["天津", "天津"], "Town": ["", ""],
    })
    existing.to_csv(hhly_csv, index=False, encoding="utf-8-sig")

    end_time = pd.Timestamp("2026-07-29 00:05:00")
    fake_raw = _fake_hhly_raw(
        [f"2026-07-29 00:0{i}:00" for i in range(1, 6)],
        station_id="NEW",
    )
    with mock.patch(
        "ScheduledTask.stationProcessMin._fetch_hhly_rainfall_for_emergency",
        return_value=fake_raw,
    ):
        spm._append_hhly_5min_to_rolling_csv(end_time)

    df = pd.read_csv(hhly_csv, encoding="utf-8-sig")
    stations = set(df["Station_Id_C"].astype(str).tolist())
    assert "OLD" not in stations
    assert "FRESH" in stations
    assert "NEW" in stations


def test_hhly_append_empty_input_noop(tmp_path, monkeypatch):
    """空输入不写文件。"""
    hhly_csv = tmp_path / "hhly_test.csv"
    monkeypatch.setattr(spm, "hhly_tempfile", str(hhly_csv))

    with mock.patch(
        "ScheduledTask.stationProcessMin._fetch_hhly_rainfall_for_emergency",
        return_value=pd.DataFrame(),
    ):
        spm._append_hhly_5min_to_rolling_csv(pd.Timestamp("2026-07-29 00:05:00"))

    assert not hhly_csv.exists()


def test_hhly_append_dedupes_same_station_minute(tmp_path, monkeypatch):
    """同站同分钟重复追加应去重。"""
    hhly_csv = tmp_path / "hhly_test.csv"
    monkeypatch.setattr(spm, "hhly_tempfile", str(hhly_csv))

    end_time = pd.Timestamp("2026-07-29 00:05:00")
    fake_raw = _fake_hhly_raw([f"2026-07-29 00:0{i}:00" for i in range(1, 6)])

    with mock.patch(
        "ScheduledTask.stationProcessMin._fetch_hhly_rainfall_for_emergency",
        return_value=fake_raw,
    ):
        spm._append_hhly_5min_to_rolling_csv(end_time)
        # 第二次追加同样数据
        spm._append_hhly_5min_to_rolling_csv(end_time)

    df = pd.read_csv(hhly_csv, encoding="utf-8-sig")
    assert len(df) == 5, "重复追加后仍应保持 5 条（去重）"
```

- [ ] **Step 3: 运行测试**

```bash
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest ScheduledTask/tests/test_hhly_rolling_csv.py -v
```

预期：4 passed（新增 dedup 测试）

- [ ] **Step 4: Commit**

```bash
git add hhlyqyxt-master/ScheduledTask/stationProcessMin.py hhlyqyxt-master/ScheduledTask/tests/test_hhly_rolling_csv.py
git commit -m "refactor(traction): _append_hhly stores raw 1min records (no resample), aggregation moved to emergency_response_monitor"
```

---

### Task 4: 改造 `compute_emergency_response_stats` 使用内存聚合

**Files:**
- Modify: `hhlyqyxt-master/ScheduledTask/emergency_response_monitor.py`

**Interfaces:**
- Consumes: `aggregate_minute_precipitation`（Task 1）
- Produces: `compute_emergency_response_stats(source, datatime)` 逻辑不变，返回 dict 字段/口径不变

- [ ] **Step 1: 修改 `compute_emergency_response_stats`**

现有函数从 CSV 读入后用 pandas `.sum()` / `.count()` 手工聚合。改为：
1. 读 CSV → list[dict]
2. 调 `aggregate_minute_precipitation(records, end_time)` 得每站 PRE_1h/12h/24h
3. 按 12h、24h 阈值统计触发站数 + 占比

需要仔细看现有代码结构确保阈值和字段名对齐（`station_12h_baoyu` / `ratio_12h_baoyu` / `station_24h_baoyu` / `station_24h_dabaoyu` / `station_24h_tedabaoyu`）。

新实现骨架：

```python
def compute_emergency_response_stats(
    source: Union[str, pd.DataFrame],
    datatime: Union[str, datetime, None] = None,
) -> Optional[dict]:
    """基于分钟降水记录计算应急响应统计。

    读 CSV → aggregate_minute_precipitation（Q_PRE 过滤 + 内存聚合）
    → 按 12h/24h 阈值统计国家站触发数与占比 → 判定 response_level。
    """
    from ScheduledTask.precipitation_aggregator import aggregate_minute_precipitation

    if isinstance(source, str):
        if not Path(source).exists():
            return None
        try:
            df = pd.read_csv(source, encoding="utf-8-sig", low_memory=False)
        except pd.errors.EmptyDataError:
            return None
    else:
        df = source.copy()

    if df is None or df.empty:
        return None

    # 解析 datatime（BJT）
    if datatime is None:
        df["Datetime_parsed"] = pd.to_datetime(df["Datetime"], errors="coerce")
        df = df.dropna(subset=["Datetime_parsed"])
        if df.empty:
            return None
        end_time_dt = df["Datetime_parsed"].max().to_pydatetime()
    else:
        end_time_dt = _parse_datatime(datatime)

    # 转为 list[dict] 传给聚合器
    records = df.to_dict(orient="records")
    aggregated = aggregate_minute_precipitation(
        records, end_time=end_time_dt, windows_hours=(12, 24),
    )
    if not aggregated:
        return None

    # 国家站过滤（Station_levl in {"11","12","13","16"}）
    national = [
        r for r in aggregated
        if _normalize_station_level(r.get("Station_levl", "")) in NATIONAL_STATION_LEVELS
    ]
    if not national:
        return {
            "datatime": end_time_dt,
            "total_national_stations": 0,
            "station_12h_baoyu": 0, "ratio_12h_baoyu": 0.0,
            "station_24h_baoyu": 0, "ratio_24h_baoyu": 0.0,
            "station_24h_dabaoyu": 0, "ratio_24h_dabaoyu": 0.0,
            "station_24h_tedabaoyu": 0, "ratio_24h_tedabaoyu": 0.0,
            "response_level": 0,
        }

    total = len(national)

    # 12h 分母 = 有 12h 数据的国家站数
    stations_12h = [r for r in national if r.get("pre_count_12h", 0) > 0]
    total_12h = len(stations_12h)
    n_12h_baoyu = sum(1 for r in stations_12h if r.get("PRE_12h", 0.0) >= 50.0)
    ratio_12h_baoyu = (n_12h_baoyu / total_12h) if total_12h else 0.0

    # 24h 各级分子分母（分母 = total 国家站数）
    n_24h_baoyu = sum(1 for r in national if r.get("PRE_24h", 0.0) >= 50.0)
    n_24h_dabaoyu = sum(1 for r in national if r.get("PRE_24h", 0.0) >= 100.0)
    n_24h_tedabaoyu = sum(1 for r in national if r.get("PRE_24h", 0.0) >= 250.0)
    ratio_24h_baoyu = n_24h_baoyu / total
    ratio_24h_dabaoyu = n_24h_dabaoyu / total
    ratio_24h_tedabaoyu = n_24h_tedabaoyu / total

    response_level = _determine_response_level(
        ratio_12h_baoyu, ratio_24h_baoyu, ratio_24h_dabaoyu, ratio_24h_tedabaoyu,
    )

    return {
        "datatime": end_time_dt,
        "total_national_stations": total,
        "station_12h_baoyu": n_12h_baoyu, "ratio_12h_baoyu": round(ratio_12h_baoyu, 4),
        "station_24h_baoyu": n_24h_baoyu, "ratio_24h_baoyu": round(ratio_24h_baoyu, 4),
        "station_24h_dabaoyu": n_24h_dabaoyu, "ratio_24h_dabaoyu": round(ratio_24h_dabaoyu, 4),
        "station_24h_tedabaoyu": n_24h_tedabaoyu, "ratio_24h_tedabaoyu": round(ratio_24h_tedabaoyu, 4),
        "response_level": response_level,
    }
```

- [ ] **Step 2: 运行现有 30 条 emergency_response 测试**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/hhlyqyxt-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest utils/tests/test_emergency_response_monitor.py -v
```

**预期**：可能有测试失败——现有测试可能用了 `SURF_CHN_MUL_MIN` 的字段结构（无 `Q_PRE`）或直接 sum PRE 的口径。

- [ ] **Step 3: 更新失败的测试**

对失败的每条测试，检查断言：
- 若测试造的 DataFrame 无 Q_PRE，`aggregate_minute_precipitation` 默认 `_q_pre_valid` 对空/None 返回 True，应该继续通过
- 若测试 mock 了 `_fetch_hhly_rainfall_for_emergency` 返回聚合后的 5min 数据（PRE_1h/PRE_12h/PRE_24h），需要改为返回**原始 1min 数据**
- 若测试断言 `response_level` 依赖具体分子分母，检查数据是否符合新聚合口径

不逐条列举失败用例——由 subagent 执行时视实际报错处理。

- [ ] **Step 4: Commit**

```bash
git add hhlyqyxt-master/ScheduledTask/emergency_response_monitor.py hhlyqyxt-master/utils/tests/test_emergency_response_monitor.py
git commit -m "refactor(traction): compute_emergency_response_stats uses aggregate_minute_precipitation (Q_PRE filter + in-memory aggregation)"
```

---

### Task 5: 全量回归 + Verify 脚本更新

**Files:**
- Modify: `hhlyqyxt-master/scripts/verify_emergency_scenario.py`（如需要）

- [ ] **Step 1: 全量测试**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/hhlyqyxt-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest ScheduledTask/tests/ utils/tests/ -v 2>&1 | tail -15
```

预期：全部通过（原 104 + 新增 7 aggregator + 可能 1 dedup = 112 附近）

- [ ] **Step 2: 检查 verify_emergency_scenario.py 是否需要更新**

若脚本里 `_fetch_hhly_24h` 有做 5min resample（Task 3 之前保留的），要删掉那段 resample 逻辑，改为直接写原始数据到 CSV。

- [ ] **Step 3: Commit（如有改动）**

```bash
git add hhlyqyxt-master/scripts/verify_emergency_scenario.py
git commit -m "chore(traction): verify_emergency_scenario aligns with raw-minute CSV"
```

---

### Task 6: PR + main merge + memory + CLAUDE.md 更新

- [ ] **Step 1: push + merge main**

```bash
git checkout main
git merge --ff-only feat/hhly-pre-min-refactor
git push origin main
git branch -d feat/hhly-pre-min-refactor
```

- [ ] **Step 2: 新增 claude-mem 记忆 `[[traction-hhly-pre-min-refactor]]`**

- [ ] **Step 3: 更新 `hhlyqyxt-master/CLAUDE.md`** —— 添加 `SURF_CHN_PRE_MIN` + Q_PRE 过滤 + 内存聚合的约束

- [ ] **Step 4: MEMORY.md 索引更新**
