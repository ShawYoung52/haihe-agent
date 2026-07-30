"""_append_hhly_5min_to_rolling_csv 单元测试。

契约（v3 5min 聚合方案）：
- 拉原始 1min 数据 → Q_PRE 过滤（可信 {"0","3","4"}）→ 5min 聚合（PRE=sum, 元信息=first）
- CSV 存 5min 聚合结果，每站每 5min 1 行（与 HHLY_JUECE `24hourmindata.csv` 一致）
- 24h 滚动窗口

由于 stationProcessMin.py 依赖 geopandas / apscheduler / sqlalchemy 等重模块，
本测试用 pytest.importorskip 跳过 import 失败的场景。
"""
import pandas as pd
import pytest
from unittest import mock

spm = pytest.importorskip("ScheduledTask.stationProcessMin",
                          reason="stationProcessMin 依赖不全")


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


def test_hhly_append_aggregates_to_5min(tmp_path, monkeypatch):
    """5 条 1min 记录 → 1 条 5min 聚合记录，PRE 累加，元信息保留。"""
    end_time = pd.Timestamp("2026-07-29 00:05:00")
    # 5 条 1min 记录：00:01-00:05，每条 PRE=0.2
    fake_raw = _fake_hhly_raw(
        [f"2026-07-29 00:0{i}:00" for i in range(1, 6)],
        station_levl="12",
        pre=0.2,
    )
    hhly_csv = tmp_path / "hhly_test.csv"
    monkeypatch.setattr(spm, "hhly_tempfile", str(hhly_csv))

    with mock.patch(
        "ScheduledTask.stationProcessMin._fetch_hhly_rainfall_for_emergency",
        return_value=fake_raw,
    ):
        spm._append_hhly_5min_to_rolling_csv(end_time)

    df = pd.read_csv(hhly_csv, encoding="utf-8-sig")
    assert len(df) == 1, "5 条 1min 记录应聚合为 1 条 5min 记录"
    assert df["PRE"].iloc[0] == pytest.approx(1.0), "PRE 应累加：5 × 0.2 = 1.0"
    assert df["PRE"].dtype.kind == "f", "PRE 应为 float"
    assert str(df["Station_levl"].iloc[0]) == "12"


def test_hhly_append_filters_untrusted_q_pre(tmp_path, monkeypatch):
    """Q_PRE 不在 {'0','3','4'} 的记录应被过滤后再聚合。"""
    end_time = pd.Timestamp("2026-07-29 00:05:00")
    # 混合 Q_PRE：3 条可信 + 2 条脏数据
    rows = []
    for m, (pre, q_pre) in enumerate(
        [(0.1, "0"), (999.0, "1"), (0.2, "3"), (999.0, "9"), (0.3, "4")], start=1
    ):
        rows.append({
            "Station_Id_C": "A",
            "Datetime": pd.Timestamp(f"2026-07-29 00:0{m}:00"),
            "PRE": pre, "Q_PRE": q_pre, "Station_levl": "12",
            "Lat": 39.0, "Lon": 117.0, "City": "天津", "Station_Name": "站A",
            "Cnty": "南开", "Province": "天津", "Town": "",
        })
    fake_raw = pd.DataFrame(rows)

    hhly_csv = tmp_path / "hhly_test.csv"
    monkeypatch.setattr(spm, "hhly_tempfile", str(hhly_csv))

    with mock.patch(
        "ScheduledTask.stationProcessMin._fetch_hhly_rainfall_for_emergency",
        return_value=fake_raw,
    ):
        spm._append_hhly_5min_to_rolling_csv(end_time)

    df = pd.read_csv(hhly_csv, encoding="utf-8-sig")
    assert len(df) == 1
    # Q_PRE=1/9 的两条被过滤，只累加 0.1+0.2+0.3=0.6
    assert df["PRE"].iloc[0] == pytest.approx(0.6)


def test_hhly_append_rolls_24h_window(tmp_path, monkeypatch):
    """24h 窗口滚动：写入新数据 + 丢弃超过 24h 的旧数据。"""
    hhly_csv = tmp_path / "hhly_test.csv"
    monkeypatch.setattr(spm, "hhly_tempfile", str(hhly_csv))

    old_time = pd.Timestamp("2026-07-27 23:00:00")   # 25h 前
    fresh_time = pd.Timestamp("2026-07-28 19:00:00")  # 5h 前
    existing = pd.DataFrame({
        "Station_Id_C": ["OLD", "FRESH"],
        "Datetime": [old_time, fresh_time],
        "PRE": [1.0, 2.0],
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
    assert "OLD" not in stations, "25h 前的数据应被丢弃"
    assert "FRESH" in stations, "5h 前的数据应保留"
    assert "NEW" in stations, "新聚合的数据应写入"


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


def test_hhly_append_dedupes_same_5min_bucket(tmp_path, monkeypatch):
    """同站同 5min 桶重复追加应去重（同一时刻聚合结果保持一条）。"""
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
    assert len(df) == 1, "重复追加同一 5min 桶，去重后仍应保持 1 条"
