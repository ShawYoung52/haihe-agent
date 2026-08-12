"""POI 最近观测站实况 query_poi_nearest_observation 缓存测试。

背景（2026-08-12 全问题类型性能优化）：
该工具每次查询重拉 POI(ES) + MUSIC 逐小时站点实况（最多 6 时次 × 2 接口 × 2 元素集 = 24 次调用）。
口径（用户已确认）：实况类短 TTL 60s，键含当前时次桶；错误/无数据结果不写缓存。
"""

from __future__ import annotations

import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from custom_tools import poi_nearest_observation_tool as tool

POI = {"name": "天津站", "address": "天津市和平区", "longitude": 117.2, "latitude": 39.1}
RECORD = {
    "Station_Id_C": "54517",
    "Station_Name": "天津站",
    "Province": "天津市",
    "City": "天津市",
    "Cnty": "和平",
    "Lon": 117.2,
    "Lat": 39.1,
    "Datetime": "2026-08-12 06:00:00",
    "TEM": "24.5",
    "PRE_1h": "0.0",
}


def _make_query_records(calls: dict):
    def fake_query(client, basin_codes, hours_back, admin_code, poi, max_distance_km):
        calls["n"] += 1
        return "20260812060000", [dict(RECORD)], "hourly_region_full"
    return fake_query


class TestPoiNearestObsCache:
    def _setup(self, monkeypatch, calls: dict, ttl: int):
        monkeypatch.setattr(tool, "_pick_first_poi", lambda keyword: dict(POI))
        monkeypatch.setattr(tool, "_query_station_records", _make_query_records(calls))
        monkeypatch.setattr(tool, "POI_NEAREST_OBS_CACHE_TTL", ttl)
        tool._poi_nearest_obs_cache.clear()

    def test_second_call_within_same_hour_hits_cache(self, monkeypatch):
        """同入参 + 同时次桶第二次命中缓存，不再拉 POI/MUSIC。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls, 3600)

        r1 = tool._query_poi_nearest_observation_core("天津站")
        assert r1["status"] == "ok"
        after_first = calls["n"]
        r2 = tool._query_poi_nearest_observation_core("天津站")
        assert r1 == r2
        assert calls["n"] == after_first, f"第二次应命中缓存，实际多拉了 {calls['n'] - after_first} 次"

    def test_distinct_keyword_does_not_share(self, monkeypatch):
        """不同关键词不互相命中。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls, 3600)

        tool._query_poi_nearest_observation_core("天津站")
        after_first = calls["n"]
        tool._query_poi_nearest_observation_core("海河医院")
        assert calls["n"] > after_first, "不同关键词应重新拉取"

    def test_cache_expires_after_ttl(self, monkeypatch):
        """TTL=0 强制过期后重新拉取。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls, 0)

        tool._query_poi_nearest_observation_core("天津站")
        after_first = calls["n"]
        tool._query_poi_nearest_observation_core("天津站")
        assert calls["n"] > after_first, "TTL=0 应重新拉取"

    def test_error_result_not_cached(self, monkeypatch):
        """POI 查询失败（no_data）不写缓存，下一次仍重拉。"""
        calls = {"n": 0}

        def failing_pick(keyword):
            calls["n"] += 1
            raise RuntimeError("ES 不可达")

        monkeypatch.setattr(tool, "_pick_first_poi", failing_pick)
        monkeypatch.setattr(tool, "POI_NEAREST_OBS_CACHE_TTL", 3600)
        tool._poi_nearest_obs_cache.clear()

        r1 = tool._query_poi_nearest_observation_core("天津站")
        assert r1["status"] == "no_data"
        after_first = calls["n"]
        tool._query_poi_nearest_observation_core("天津站")
        assert calls["n"] > after_first, "错误结果不应写缓存"
