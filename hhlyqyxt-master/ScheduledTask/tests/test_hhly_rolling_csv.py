"""_append_hhly_5min_to_rolling_csv 单元测试。

C1 回归防护：resample 结果的元信息列（Station_levl 等）必须完整保留，
不能因 resample/Grouper closed 默认不一致而全部落 NaN。

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


def _fake_hhly_raw(datetimes, station_id="A", station_levl="11"):
    """构造模拟 HHLY 分钟原始数据（含元信息列）。"""
    return pd.DataFrame({
        "Station_Id_C": [station_id] * len(datetimes),
        "Datetime": pd.to_datetime(datetimes),
        "PRE": [0.2] * len(datetimes),
        "Station_levl": [station_levl] * len(datetimes),
        "Lat": [39.0] * len(datetimes),
        "Lon": [117.0] * len(datetimes),
        "City": ["天津市"] * len(datetimes),
        "Station_Name": ["测试站"] * len(datetimes),
        "Cnty": ["南开"] * len(datetimes),
        "Province": ["天津"] * len(datetimes),
        "Town": ["某镇"] * len(datetimes),
    })


def test_hhly_5min_aggregation_preserves_station_metadata(tmp_path, monkeypatch):
    """C1 回归防护：5min 聚合后 Station_levl 等元信息列不能落 NaN。"""
    end_time = pd.Timestamp("2026-07-29 00:05:00")
    fake_raw = _fake_hhly_raw(
        [f"2026-07-29 00:0{i}:00" for i in range(1, 6)],
        station_id="A_STATION",
        station_levl="12",
    )

    hhly_csv = tmp_path / "hhly_test.csv"
    monkeypatch.setattr(spm, "hhly_tempfile", str(hhly_csv))

    with mock.patch(
        "ScheduledTask.stationProcessMin._fetch_hhly_rainfall_for_emergency",
        return_value=fake_raw,
    ):
        spm._append_hhly_5min_to_rolling_csv(end_time)

    assert hhly_csv.exists(), "hhly_tempfile 应被创建"
    df = pd.read_csv(hhly_csv, encoding="utf-8-sig")

    # 关键断言：元信息列必须全部非 NaN（C1 修复）
    for col in ("Station_levl", "Lat", "Lon", "City", "Station_Name"):
        assert df[col].isna().sum() == 0, f"C1 回归：{col} 列不应有 NaN"

    # PRE 应累加：5 条 0.2 → 1.0
    assert df["PRE"].sum() == pytest.approx(1.0)

    # Station_levl 应保留原值（否则国家站过滤会失败，导致应急响应永远为 0）
    assert df["Station_levl"].astype(str).unique().tolist() == ["12"]


def test_hhly_5min_appends_to_existing_and_drops_old(tmp_path, monkeypatch):
    """24h 滚动窗口：写入新数据 + 丢弃超过 24h 的旧数据。"""
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
    assert "NEW" in stations, "新拉取的数据应写入"


def test_hhly_5min_empty_input_noop(tmp_path, monkeypatch):
    """空数据返回不写文件。"""
    hhly_csv = tmp_path / "hhly_test.csv"
    monkeypatch.setattr(spm, "hhly_tempfile", str(hhly_csv))

    with mock.patch(
        "ScheduledTask.stationProcessMin._fetch_hhly_rainfall_for_emergency",
        return_value=pd.DataFrame(),
    ):
        spm._append_hhly_5min_to_rolling_csv(pd.Timestamp("2026-07-29 00:05:00"))

    assert not hhly_csv.exists(), "空数据不应创建 CSV"
