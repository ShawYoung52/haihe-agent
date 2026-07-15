"""emergency_response_monitor 单元测试（无需真实数据库）。"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ScheduledTask import emergency_response_monitor as erm
from Models.QyEmergencyResponseMonitor import QyEmergencyResponseMonitor


@pytest.fixture
def make_csv(tmp_path: Path):
    def _make(rows: list[dict], datatime: str = "2026-07-15 10:00:00") -> str:
        csv_path = tmp_path / "yangxiao.csv"
        header = [
            "Station_Id_C",
            "Datetime",
            "PRE",
            "Lat",
            "Lon",
            "City",
            "Station_Name",
            "Cnty",
            "Province",
            "Town",
            "Station_levl",
        ]
        lines = [",".join(header)]
        base = {
            "Lat": "39.0",
            "Lon": "117.0",
            "City": "天津市",
            "Station_Name": "测试站",
            "Cnty": "测试区",
            "Province": "天津市",
            "Town": "测试镇",
        }
        for row in rows:
            full = {**base, **row}
            lines.append(",".join(str(full.get(col, "")) for col in header))
        csv_path.write_text("\n".join(lines), encoding="utf-8")
        return str(csv_path)

    return _make


def test_compute_stats_aggregates_by_station_and_window(make_csv):
    """同一站点在窗口内多条记录应求和参与阈值统计。"""
    csv_path = make_csv(
        [
            # 12h 窗口内 (2026-07-14 22:00 -> 2026-07-15 10:00]
            {"Station_Id_C": "A", "Datetime": "2026-07-15 08:00:00", "PRE": 30.0, "Station_levl": "011"},
            {"Station_Id_C": "A", "Datetime": "2026-07-15 09:00:00", "PRE": 60.0, "Station_levl": "011"},
            # 24h 窗口内更早的记录
            {"Station_Id_C": "B", "Datetime": "2026-07-14 12:00:00", "PRE": 120.0, "Station_levl": "012"},
            {"Station_Id_C": "B", "Datetime": "2026-07-14 13:00:00", "PRE": 90.0, "Station_levl": "012"},
            # 国家级站点 C，无暴雨
            {"Station_Id_C": "C", "Datetime": "2026-07-15 09:00:00", "PRE": 10.0, "Station_levl": "013"},
            # 非国家级站点 D，降水量大但应被忽略
            {"Station_Id_C": "D", "Datetime": "2026-07-15 09:00:00", "PRE": 300.0, "Station_levl": "014"},
        ],
        datatime="2026-07-15 10:00:00",
    )
    result = erm.compute_emergency_response_stats(csv_path, "2026-07-15 10:00:00")
    assert result["total_national_stations"] == 3
    assert result["station_12h_baoyu"] == 1  # A
    assert result["station_24h_baoyu"] == 1  # A（12h 属于 24h 子集）
    assert result["station_24h_dabaoyu"] == 1  # B
    assert result["station_24h_tedabaoyu"] == 0
    assert result["ratio_12h_baoyu"] == pytest.approx(0.3333, abs=1e-4)
    assert result["ratio_24h_baoyu"] == pytest.approx(0.3333, abs=1e-4)
    assert result["ratio_24h_dabaoyu"] == pytest.approx(0.3333, abs=1e-4)


def test_compute_stats_uses_sum_not_max(make_csv):
    """两条 40 mm 记录求和为 80 mm（暴雨），但最大值仅 40 mm（非暴雨），验证使用求和。"""
    csv_path = make_csv(
        [
            {"Station_Id_C": "A", "Datetime": "2026-07-15 08:00:00", "PRE": 40.0, "Station_levl": "011"},
            {"Station_Id_C": "A", "Datetime": "2026-07-15 09:00:00", "PRE": 40.0, "Station_levl": "011"},
            {"Station_Id_C": "B", "Datetime": "2026-07-15 09:00:00", "PRE": 10.0, "Station_levl": "011"},
        ],
        datatime="2026-07-15 10:00:00",
    )
    result = erm.compute_emergency_response_stats(csv_path, "2026-07-15 10:00:00")
    assert result["total_national_stations"] == 2
    # 若用 max，A 为 40 mm（非暴雨）；用 sum，A 为 80 mm（暴雨）
    assert result["station_12h_baoyu"] == 1
    assert result["station_24h_baoyu"] == 1
    assert result["ratio_12h_baoyu"] == pytest.approx(0.5, abs=1e-4)
    assert result["ratio_24h_baoyu"] == pytest.approx(0.5, abs=1e-4)


def test_response_level_1_when_tedabaoyu_ratio_high(make_csv):
    """24h 特大暴雨占比 >= 0.15 时应触发 I 级响应。"""
    rows = [
        {"Station_Id_C": f"S{i:02d}", "Datetime": "2026-07-14 12:00:00", "PRE": 260.0, "Station_levl": "011"}
        for i in range(3)
    ] + [
        {"Station_Id_C": f"N{i:02d}", "Datetime": "2026-07-15 09:00:00", "PRE": 10.0, "Station_levl": "011"}
        for i in range(17)
    ]
    csv_path = make_csv(rows, datatime="2026-07-15 10:00:00")
    result = erm.compute_emergency_response_stats(csv_path, "2026-07-15 10:00:00")
    assert result["total_national_stations"] == 20
    assert result["station_24h_tedabaoyu"] == 3
    assert result["ratio_24h_tedabaoyu"] == pytest.approx(0.15, abs=1e-4)
    assert result["response_level"] == 1


def test_response_level_2_when_dabaoyu_ratio_high(make_csv):
    """24h 大暴雨占比 >= 0.15 且无特大暴雨时触发 II 级响应。"""
    rows = [
        {"Station_Id_C": f"S{i:02d}", "Datetime": "2026-07-14 12:00:00", "PRE": 150.0, "Station_levl": "011"}
        for i in range(3)
    ] + [
        {"Station_Id_C": f"N{i:02d}", "Datetime": "2026-07-15 09:00:00", "PRE": 10.0, "Station_levl": "011"}
        for i in range(17)
    ]
    csv_path = make_csv(rows, datatime="2026-07-15 10:00:00")
    result = erm.compute_emergency_response_stats(csv_path, "2026-07-15 10:00:00")
    assert result["response_level"] == 2


def test_response_level_3_when_12h_baoyu_ratio_high(make_csv):
    """12h 暴雨占比 >= 0.20 且无更高级别时触发 III 级响应。"""
    rows = [
        {"Station_Id_C": f"S{i:02d}", "Datetime": "2026-07-15 08:00:00", "PRE": 60.0, "Station_levl": "011"}
        for i in range(2)
    ] + [
        {"Station_Id_C": f"N{i:02d}", "Datetime": "2026-07-15 09:00:00", "PRE": 10.0, "Station_levl": "011"}
        for i in range(8)
    ]
    csv_path = make_csv(rows, datatime="2026-07-15 10:00:00")
    result = erm.compute_emergency_response_stats(csv_path, "2026-07-15 10:00:00")
    assert result["ratio_12h_baoyu"] == pytest.approx(0.2, abs=1e-4)
    assert result["response_level"] == 3


def test_response_level_4_when_24h_baoyu_ratio_high(make_csv):
    """24h 暴雨占比 >= 0.20 且无更高级别时触发 IV 级响应。"""
    rows = [
        {"Station_Id_C": f"S{i:02d}", "Datetime": "2026-07-14 12:00:00", "PRE": 60.0, "Station_levl": "011"}
        for i in range(2)
    ] + [
        {"Station_Id_C": f"N{i:02d}", "Datetime": "2026-07-15 09:00:00", "PRE": 10.0, "Station_levl": "011"}
        for i in range(8)
    ]
    csv_path = make_csv(rows, datatime="2026-07-15 10:00:00")
    result = erm.compute_emergency_response_stats(csv_path, "2026-07-15 10:00:00")
    assert result["ratio_24h_baoyu"] == pytest.approx(0.2, abs=1e-4)
    assert result["response_level"] == 4


def test_response_level_0_when_no_threshold(make_csv):
    """未达任何阈值时应返回 0 级响应。"""
    rows = [
        {"Station_Id_C": f"S{i:02d}", "Datetime": "2026-07-15 09:00:00", "PRE": 30.0, "Station_levl": "011"}
        for i in range(10)
    ]
    csv_path = make_csv(rows, datatime="2026-07-15 10:00:00")
    result = erm.compute_emergency_response_stats(csv_path, "2026-07-15 10:00:00")
    assert result["response_level"] == 0


def test_station_levl_normalization(make_csv):
    """Station_levl 应支持 '11'、'011'、11 等多种输入形式。"""
    rows = [
        {"Station_Id_C": "A", "Datetime": "2026-07-15 09:00:00", "PRE": 60.0, "Station_levl": "11"},
        {"Station_Id_C": "B", "Datetime": "2026-07-15 09:00:00", "PRE": 60.0, "Station_levl": "011"},
        {"Station_Id_C": "C", "Datetime": "2026-07-15 09:00:00", "PRE": 60.0, "Station_levl": 11},
        {"Station_Id_C": "D", "Datetime": "2026-07-15 09:00:00", "PRE": 60.0, "Station_levl": "013"},
        {"Station_Id_C": "E", "Datetime": "2026-07-15 09:00:00", "PRE": 60.0, "Station_levl": "016"},
        {"Station_Id_C": "F", "Datetime": "2026-07-15 09:00:00", "PRE": 60.0, "Station_levl": "014"},
    ]
    csv_path = make_csv(rows, datatime="2026-07-15 10:00:00")
    result = erm.compute_emergency_response_stats(csv_path, "2026-07-15 10:00:00")
    # '11'、'011'、11 均归一为 '011'，加上 '013'、'016' 共 5 个国家级站点
    assert result["total_national_stations"] == 5
    assert result["station_12h_baoyu"] == 5


def test_missing_csv_returns_none_and_warns(caplog):
    """CSV 文件不存在时应记录警告并返回 None。"""
    with caplog.at_level("WARNING", logger="ScheduledTask.emergency_response_monitor"):
        result = erm.run_emergency_response_monitor("/nonexistent/path.csv", "2026-07-15 10:00:00")
    assert result is None
    assert "不存在" in caplog.text or "missing" in caplog.text.lower()


def test_run_emergency_response_monitor_persists(make_csv, monkeypatch):
    """run_emergency_response_monitor 应将结果持久化到数据库。"""
    csv_path = make_csv(
        [{"Station_Id_C": "A", "Datetime": "2026-07-15 09:00:00", "PRE": 60.0, "Station_levl": "011"}],
        datatime="2026-07-15 10:00:00",
    )

    mock_session = MagicMock()
    mock_session_cls = MagicMock(return_value=mock_session)
    monkeypatch.setattr(erm, "Session", mock_session_cls)

    result = erm.run_emergency_response_monitor(csv_path, "2026-07-15 10:00:00", minute_monitor_id=42)
    assert result is not None
    assert isinstance(result, QyEmergencyResponseMonitor)
    assert result.minute_monitor_id == 42
    assert result.total_national_stations == 1
    mock_session.add.assert_called_once_with(result)
    mock_session.commit.assert_called_once()
    mock_session.close.assert_called_once()


def test_run_emergency_response_monitor_rolls_back_on_db_error(make_csv, monkeypatch):
    """数据库异常时应回滚并重新抛出异常。"""
    csv_path = make_csv(
        [{"Station_Id_C": "A", "Datetime": "2026-07-15 09:00:00", "PRE": 60.0, "Station_levl": "011"}],
        datatime="2026-07-15 10:00:00",
    )

    mock_session = MagicMock()
    mock_session.commit.side_effect = RuntimeError("db down")
    mock_session_cls = MagicMock(return_value=mock_session)
    monkeypatch.setattr(erm, "Session", mock_session_cls)

    with pytest.raises(RuntimeError, match="db down"):
        erm.run_emergency_response_monitor(csv_path, "2026-07-15 10:00:00")

    mock_session.rollback.assert_called_once()
    mock_session.close.assert_called_once()


def test_sentinel_pre_value_treated_as_zero(make_csv):
    """PRE 缺测标识值（大于 99988）应视为 0，不参与阈值统计。"""
    csv_path = make_csv(
        [
            {"Station_Id_C": "A", "Datetime": "2026-07-15 09:00:00", "PRE": 99999, "Station_levl": "011"},
            {"Station_Id_C": "B", "Datetime": "2026-07-15 09:00:00", "PRE": 60.0, "Station_levl": "011"},
        ],
        datatime="2026-07-15 10:00:00",
    )
    result = erm.compute_emergency_response_stats(csv_path, "2026-07-15 10:00:00")
    assert result["total_national_stations"] == 2
    assert result["station_12h_baoyu"] == 1  # B
    assert result["station_24h_baoyu"] == 1  # B


def test_all_nan_station_not_counted_in_total(make_csv):
    """全为 NaN 的站点不应计入 total，避免拉低占比分母。"""
    csv_path = make_csv(
        [
            {"Station_Id_C": "A", "Datetime": "2026-07-15 09:00:00", "PRE": "", "Station_levl": "011"},
            {"Station_Id_C": "A", "Datetime": "2026-07-15 09:10:00", "PRE": "bad", "Station_levl": "011"},
            {"Station_Id_C": "B", "Datetime": "2026-07-15 09:00:00", "PRE": 60.0, "Station_levl": "011"},
        ],
        datatime="2026-07-15 10:00:00",
    )
    result = erm.compute_emergency_response_stats(csv_path, "2026-07-15 10:00:00")
    assert result["total_national_stations"] == 1
    assert result["station_12h_baoyu"] == 1
    assert result["ratio_12h_baoyu"] == pytest.approx(1.0, abs=1e-4)


def test_run_emergency_response_monitor_default_datatime_uses_csv_max(make_csv, monkeypatch):
    """datatime 为 None 时应使用 CSV 中的最大 Datetime。"""
    csv_path = make_csv(
        [
            {"Station_Id_C": "A", "Datetime": "2026-07-15 08:00:00", "PRE": 30.0, "Station_levl": "011"},
            {"Station_Id_C": "A", "Datetime": "2026-07-15 09:00:00", "PRE": 40.0, "Station_levl": "011"},
        ],
        datatime="2026-07-15 10:00:00",
    )

    mock_session = MagicMock()
    mock_session_cls = MagicMock(return_value=mock_session)
    monkeypatch.setattr(erm, "Session", mock_session_cls)

    result = erm.run_emergency_response_monitor(csv_path, datatime=None, minute_monitor_id=7)
    assert result is not None
    assert result.datatime == pd.Timestamp("2026-07-15 09:00:00")
    assert result.minute_monitor_id == 7
