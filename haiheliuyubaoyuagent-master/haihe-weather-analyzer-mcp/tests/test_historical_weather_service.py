"""历史实况查询服务单元测试。

全部使用 mock 的 MusicClient + 伪造站点行，不依赖内网天擎。
参考 tests/test_poi_hazard_reminder_tool.py 的测试模式。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from custom_tools import historical_weather_service as hws  # noqa: E402


def _row(station_id: str, lon: float, lat: float, **fields) -> dict:
    base = {
        "Station_Id_C": station_id,
        "Station_Name": f"站{station_id}",
        "Lon": lon,
        "Lat": lat,
        "Province": "天津",
        "City": "天津",
        "Cnty": "蓟州",
        "Town": "",
        "Datetime": "20260810060000",
        "UPDATE_TIME": "20260810060000",
    }
    base.update(fields)
    return base


class FakeMusicClient:
    def __init__(self, rows_by_time: dict[str, list[dict]]):
        self.rows_by_time = rows_by_time
        self.calls: list[tuple] = []

    def call_api(self, interface, dataCode=None, elements=None, times=None, adminCodes=None):
        self.calls.append(("region", times, elements))
        return self.rows_by_time.get(times, [])

    def get_surf_ele_in_basin_by_time(self, basin_codes=None, times=None, elements=None, data_code=None):
        self.calls.append(("basin", times, elements))
        return []


def _day_rows() -> dict[str, list[dict]]:
    """2026-08-10 四个观测整点的伪造站点行（UTC 时次 = 北京时 -8h）。"""
    # 站点位于 POI (117.2, 39.1) 附近约 6.6 公里
    station = {"Lon": 117.25, "Lat": 39.05}
    return {
        "20260809180000": [_row("A1", 117.25, 39.05, **station, TEM=22.0, PRE_1h=0.0, WIN_S_Avg_2mi=2.0, WIN_D_Avg_2mi=90.0, VIS_HOR_1MI=15000)],
        "20260810000000": [_row("A1", 117.25, 39.05, **station, TEM=28.0, PRE_1h=0.0, WIN_S_Avg_2mi=2.5, WIN_D_Avg_2mi=90.0, VIS_HOR_1MI=16000)],
        "20260810060000": [_row("A1", 117.25, 39.05, **station, TEM=31.0, PRE_1h=2.5, WIN_S_Avg_2mi=3.0, WIN_D_Avg_2mi=135.0, VIS_HOR_1MI=12000)],
        "20260810120000": [_row("A1", 117.25, 39.05, **station, TEM=25.0, PRE_1h=0.0, WIN_S_Avg_2mi=2.0, WIN_D_Avg_2mi=90.0, VIS_HOR_1MI=14000)],
    }


def _install_rows(rows_by_time, monkeypatch):
    client = FakeMusicClient(rows_by_time)
    monkeypatch.setattr(hws, "MusicClient", lambda config=None: client)
    return client


class TestHistoricalObsCore:
    def test_day_aggregation(self, monkeypatch):
        """单日聚合：气温极值、累计降水、天气现象、风况与时次查询正确。"""
        client = _install_rows(_day_rows(), monkeypatch)
        result = hws._query_historical_obs_core(
            lon=117.2, lat=39.1, start_time="2026-08-10", end_time="2026-08-11", point_name="天津大学"
        )
        assert result["status"] == "ok"
        assert result["data_source"] == "自动站历史实况"
        assert result["query_mode"] == "historical_obs"
        assert len(result["periods"]) == 1
        row = result["periods"][0]
        assert row["period_label"] == "8月10日"
        assert row["tmax"] == 31.0
        assert row["tmin"] == 22.0
        assert row["rainfall_mm"] == 2.5
        assert row["weather"] == "小雨"
        assert "级" in row["EDA"] or "风" in row["EDA"]
        assert result["nearest_station"]["station_id"] == "A1"
        # 4 个观测整点都发起了 region 查询
        region_times = [t for mode, t, _ in client.calls if mode == "region"]
        assert set(region_times) == {
            "20260809180000", "20260810000000", "20260810060000", "20260810120000",
        }

    def test_multi_day(self, monkeypatch):
        """多日窗口：按天生成逐日行（结束日不含）。"""
        rows = dict(_day_rows())
        # 补 8/11 四个观测整点（UTC 时次 = 北京时 -8h）
        station = {"Lon": 117.25, "Lat": 39.05}
        for utc_key, (tem,) in {
            "20260810180000": (24.0,),
            "20260811000000": (29.0,),
            "20260811060000": (30.0,),
            "20260811120000": (26.0,),
        }.items():
            rows[utc_key] = [_row("A1", 117.25, 39.05, **station, TEM=tem, PRE_1h=0.0, WIN_S_Avg_2mi=2.0, WIN_D_Avg_2mi=90.0, VIS_HOR_1MI=15000)]
        client = _install_rows(rows, monkeypatch)
        result = hws._query_historical_obs_core(
            lon=117.2, lat=39.1, start_time="2026-08-10", end_time="2026-08-12", point_name="天津大学"
        )
        assert result["status"] == "ok"
        assert [p["period_label"] for p in result["periods"]] == ["8月10日", "8月11日"]

    def test_no_data(self, monkeypatch):
        """该时段无观测 → status no_data，不抛异常。"""
        _install_rows({}, monkeypatch)
        result = hws._query_historical_obs_core(
            lon=117.2, lat=39.1, start_time="2026-08-10", end_time="2026-08-11"
        )
        assert result["status"] == "no_data"

    def test_no_rain_element_no_fabrication(self, monkeypatch):
        """该站无降水观测（PRE_1h 全缺）→ 不得编造“无降雨”，雨量置 None。"""
        rows = {
            "20260809180000": [_row("C1", 117.25, 39.05, TEM=20.0, WIN_S_Avg_2mi=1.0, WIN_D_Avg_2mi=0.0, VIS_HOR_1MI=15000)],
            "20260810000000": [_row("C1", 117.25, 39.05, TEM=22.0, WIN_S_Avg_2mi=1.0, WIN_D_Avg_2mi=0.0, VIS_HOR_1MI=15000)],
            "20260810060000": [_row("C1", 117.25, 39.05, TEM=23.0, WIN_S_Avg_2mi=1.0, WIN_D_Avg_2mi=0.0, VIS_HOR_1MI=15000)],
            "20260810120000": [_row("C1", 117.25, 39.05, TEM=21.0, WIN_S_Avg_2mi=1.0, WIN_D_Avg_2mi=0.0, VIS_HOR_1MI=15000)],
        }
        _install_rows(rows, monkeypatch)
        result = hws._query_historical_obs_core(
            lon=117.2, lat=39.1, start_time="2026-08-10", end_time="2026-08-11"
        )
        assert result["status"] == "ok"
        row = result["periods"][0]
        assert row["rainfall_mm"] is None
        assert row["weather"] == "无降水数据"
        assert row["weather"] != "无降雨"

    def test_day_anchor_station_lock(self, monkeypatch):
        """当日锚定站有观测时后续时次优先用同一站，不混站。"""
        # A1 在 02/08/20 时最近；14 时 A2（更近）也返回，但锚定 A1 后 14 时仍取 A1 记录
        station_a1 = {"Lon": 117.25, "Lat": 39.05}
        station_a2 = {"Lon": 117.22, "Lat": 39.07}
        rows = {
            "20260809180000": [_row("A1", 117.25, 39.05, **station_a1, TEM=20.0, PRE_1h=0.0)],
            "20260810000000": [_row("A1", 117.25, 39.05, **station_a1, TEM=22.0, PRE_1h=0.0)],
            "20260810060000": [
                _row("A1", 117.25, 39.05, **station_a1, TEM=30.0, PRE_1h=0.0),
                _row("A2", 117.22, 39.07, **station_a2, TEM=35.0, PRE_1h=0.0),
            ],
            "20260810120000": [_row("A1", 117.25, 39.05, **station_a1, TEM=25.0, PRE_1h=0.0)],
        }
        _install_rows(rows, monkeypatch)
        result = hws._query_historical_obs_core(
            lon=117.2, lat=39.1, start_time="2026-08-10", end_time="2026-08-11"
        )
        assert result["status"] == "ok"
        row = result["periods"][0]
        # 14 时即使 A2 更近，仍用锚定站 A1 的 30.0 而非 A2 的 35.0（不混站）
        assert row["tmax"] == 30.0
        assert result["nearest_station"]["station_id"] == "A1"

    def test_bad_time(self, monkeypatch):
        """非法时间参数 → status error。"""
        _install_rows(_day_rows(), monkeypatch)
        result = hws._query_historical_obs_core(lon=117.2, lat=39.1, start_time="abc", end_time="2026-08-11")
        assert result["status"] == "error"

    def test_end_before_start(self, monkeypatch):
        """结束早于开始 → status error。"""
        _install_rows(_day_rows(), monkeypatch)
        result = hws._query_historical_obs_core(
            lon=117.2, lat=39.1, start_time="2026-08-11", end_time="2026-08-10"
        )
        assert result["status"] == "error"

    def test_exceeds_max_days(self, monkeypatch):
        """超过 10 天窗口 → status error。"""
        _install_rows(_day_rows(), monkeypatch)
        result = hws._query_historical_obs_core(
            lon=117.2, lat=39.1, start_time="2026-08-01", end_time="2026-08-16"
        )
        assert result["status"] == "error"
        assert "10" in result["message"]

    def test_keyword_resolution(self, monkeypatch):
        """不传经纬度时按 keyword 解析 POI。"""
        _install_rows(_day_rows(), monkeypatch)
        monkeypatch.setattr(
            hws, "_pick_first_poi",
            lambda keyword: {"name": "天津大学", "address": "天津市", "longitude": 117.2, "latitude": 39.1},
        )
        result = hws._query_historical_obs_core(
            keyword="天津大学", start_time="2026-08-10", end_time="2026-08-11"
        )
        assert result["status"] == "ok"
        assert result["point_name"] == "天津大学"

    def test_no_poi_no_coord(self, monkeypatch):
        """无 keyword 也无经纬度 → status error。"""
        _install_rows(_day_rows(), monkeypatch)
        result = hws._query_historical_obs_core(start_time="2026-08-10", end_time="2026-08-11")
        assert result["status"] == "error"

    def test_fog_weather(self, monkeypatch):
        """无雨 + 低能见度 → 天气现象推导为雾/低能见度。"""
        rows = {
            "20260809180000": [_row("B1", 117.25, 39.05, TEM=20.0, PRE_1h=0.0, WIN_S_Avg_2mi=1.0, WIN_D_Avg_2mi=0.0, VIS_HOR_1MI=600)],
            "20260810000000": [_row("B1", 117.25, 39.05, TEM=22.0, PRE_1h=0.0, WIN_S_Avg_2mi=1.0, WIN_D_Avg_2mi=0.0, VIS_HOR_1MI=700)],
            "20260810060000": [_row("B1", 117.25, 39.05, TEM=23.0, PRE_1h=0.0, WIN_S_Avg_2mi=1.0, WIN_D_Avg_2mi=0.0, VIS_HOR_1MI=800)],
            "20260810120000": [_row("B1", 117.25, 39.05, TEM=21.0, PRE_1h=0.0, WIN_S_Avg_2mi=1.0, WIN_D_Avg_2mi=0.0, VIS_HOR_1MI=500)],
        }
        _install_rows(rows, monkeypatch)
        result = hws._query_historical_obs_core(
            lon=117.2, lat=39.1, start_time="2026-08-10", end_time="2026-08-11"
        )
        assert result["status"] == "ok"
        assert result["periods"][0]["weather"] == "雾/低能见度"
        assert result["periods"][0]["visibility_min_km"] == 0.5


class TestHistoricalWeatherTool:
    def test_tool_defaults_end_time(self, monkeypatch):
        """不传 end_time 时默认查当天 00:00 至次日 00:00。"""
        client = _install_rows(_day_rows(), monkeypatch)
        result = hws._query_historical_obs_core(lon=117.2, lat=39.1, start_time="2026-08-10")
        assert result["status"] == "ok"
        assert len(result["periods"]) == 1
        assert result["periods"][0]["period_label"] == "8月10日"
