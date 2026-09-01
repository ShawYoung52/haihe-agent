"""天津当前天气实况附【天津市区】灾害风险表（region_hazards）测试。

背景（2026-09-01 用户口径）：「天津当前天气实况」的回答没有灾害风险表，
而「天津未来三天天气」（滚动预报）有——"第一个问题就是还是没风险那些的"。
修复：`query_current_weather_observation_core` 在 status=="ok" 时附
`region_hazards`（天津市区代表点，与滚动预报区域风险同口径
`rolling_forecast_service._query_region_hazards`，risk_fcst_times=None 走
最近起报时次 + 同日回退），前端复用 `_region_hazard_table` 渲染。

零阻断：隐患/风险查询任何失败都静默降级（payload 不带 region_hazards 键），
绝不影响实况回答本身。
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import current_weather_observation_service as svc

API_UTC = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


def _rec(station: str, province: str, city: str, cnty: str, pre: float = 5.0) -> dict:
    return {
        "Station_Id_C": station,
        "Station_Name": station,
        "Province": province,
        "City": city,
        "Cnty": cnty,
        "PRE": pre,
        "PRE_1h": pre,
        "Datetime": "2026-09-01 00:00:00",
    }


REGION_RECORDS = [
    _rec("TJ_HX1", "天津市", "天津市", "河西区", 8.0),
    _rec("TJ_JZ", "天津市", "天津市", "蓟州区", 25.0),
    _rec("BJ_CY", "北京市", "北京市", "朝阳区", 30.0),
]
BASIN_RECORDS = [_rec("HL001", "海河流域", "海河流域", "流域", 6.0)]

FAKE_HAZARDS = {
    "total_found": 17,
    "radius_km": 25,
    "categories": [
        {"key": "zxhl", "label": "中小河流", "kind": "river", "count": 17},
    ],
    "hazards_available": True,
    "risk_levels": {"zxhl": {"levels": {}}},
    "risk_levels_available": True,
}


def _run(monkeypatch, *, query_result):
    def fake_query(client, *, now, hours_back):
        if query_result is None:
            return None, [], [], []
        return API_UTC, list(REGION_RECORDS), list(BASIN_RECORDS), []

    monkeypatch.setattr(svc, "_query_same_successful_time", fake_query)
    monkeypatch.setattr(svc, "CURRENT_WEATHER_CACHE_TTL", 3600)
    svc._current_weather_cache.clear()
    fixed_now = datetime(2026, 9, 1, 8, 30, tzinfo=svc.BEIJING_TIMEZONE)
    return svc.query_current_weather_observation_core(
        lambda: None, now=fixed_now, hours_back=6
    )


def _fake_rfs(captured: dict, hazards=FAKE_HAZARDS):
    def _query_region_hazards(lon, lat, risk_fcst_times):
        captured["lon"] = lon
        captured["lat"] = lat
        captured["risk_fcst_times"] = risk_fcst_times
        return hazards

    return types.SimpleNamespace(_query_region_hazards=_query_region_hazards)


class TestCurrentWeatherRegionHazards:
    def test_ok_attaches_tianjin_region_hazards(self, monkeypatch):
        """status==ok 时附 region_hazards：region=tianjin、region_display=天津市区、含 categories。"""
        captured: dict = {}
        monkeypatch.setattr(svc, "_load_region_hazard_queryer", lambda: _fake_rfs(captured))
        result = _run(monkeypatch, query_result="ok")

        assert result["status"] == "ok"
        hazards = result.get("region_hazards")
        assert isinstance(hazards, list) and len(hazards) == 1
        entry = hazards[0]
        assert entry["region"] == "tianjin"
        assert entry["region_display"] == "天津市区"
        assert entry["categories"][0]["key"] == "zxhl"
        assert entry["categories"][0]["count"] == 17
        assert entry["risk_levels_available"] is True

    def test_uses_tianjin_urban_point_and_latest_cycle(self, monkeypatch):
        """代表点为天津市区坐标（117.14/39.24），risk_fcst_times=None（最近起报+同日回退）。"""
        captured: dict = {}
        monkeypatch.setattr(svc, "_load_region_hazard_queryer", lambda: _fake_rfs(captured))
        _run(monkeypatch, query_result="ok")

        assert captured["lon"] == 117.14
        assert captured["lat"] == 39.24
        assert captured["risk_fcst_times"] is None

    def test_no_data_status_does_not_attach(self, monkeypatch):
        """status!=ok（no_data）不附 region_hazards。"""
        captured: dict = {}
        monkeypatch.setattr(svc, "_load_region_hazard_queryer", lambda: _fake_rfs(captured))
        result = _run(monkeypatch, query_result=None)

        assert result["status"] == "no_data"
        assert "region_hazards" not in result

    def test_query_failure_silently_degrades(self, monkeypatch):
        """隐患查询抛异常 → 静默降级，不带 region_hazards，实况回答照常。"""
        def _boom():
            raise RuntimeError("hazard db down")

        monkeypatch.setattr(svc, "_load_region_hazard_queryer", _boom)
        result = _run(monkeypatch, query_result="ok")

        assert result["status"] == "ok"
        assert "region_hazards" not in result
        assert "regions" in result  # 实况主体不受影响

    def test_empty_hazards_result_not_attached(self, monkeypatch):
        """_query_region_hazards 返回空/非 dict → 不附 region_hazards。"""
        monkeypatch.setattr(
            svc, "_load_region_hazard_queryer", lambda: _fake_rfs({}, hazards={})
        )
        result = _run(monkeypatch, query_result="ok")

        assert result["status"] == "ok"
        assert "region_hazards" not in result
