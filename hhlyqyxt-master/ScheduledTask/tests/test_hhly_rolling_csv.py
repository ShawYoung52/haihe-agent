"""_append_hhly_5min_to_rolling_csv 单元测试。

重构后契约：CSV 存原始 1min 数据（不做 5min 聚合），聚合搬到应急判定时做。
避免 resample 引入的 PRE 字符串拼接、Station_levl 不一致等 bug。

由于 stationProcessMin.py 依赖 geopandas / apscheduler / sqlalchemy 等重模块，
本测试用 pytest.importorskip 跳过 import 失败的场景（如缺少依赖），
在完整依赖环境下（生产 + 开发 venv）会正常执行。
"""
import pandas as pd
import pytest
from unittest import mock

# 提前跳过：如果 stationProcessMin 依赖不全，本测试模块 skip
spm = pytest.importorskip("ScheduledTask.stationProcessMin",
                          reason="stationProcessMin 依赖不全（缺 geopandas/apscheduler/sqlalchemy 等）")


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
