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
