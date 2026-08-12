"""当前天气实况 query_current_weather_observation_core 缓存测试。

背景（2026-08-12 全问题类型性能优化）：
实况类工具每次重拉天擎（6 候选时次 × 2 接口 = 12 次调用）。口径（用户已确认）：
实况类短 TTL 60s，键含当前时次桶（跨时次必 miss，防旧时次服务）；错误/无数据结果不写缓存。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import current_weather_observation_service as svc

API_UTC = datetime(2026, 8, 12, 4, 0, tzinfo=timezone.utc)


def _rec(station: str, province: str, city: str, cnty: str, pre: float = 5.0) -> dict:
    return {
        "Station_Id_C": station,
        "Station_Name": station,
        "Province": province,
        "City": city,
        "Cnty": cnty,
        "PRE": pre,
        "PRE_1h": pre,
        "Datetime": "2026-08-12 04:00:00",
    }


REGION_RECORDS = [
    _rec("TJ001", "天津市", "天津市", "和平", 8.0),
    _rec("BJ001", "北京市", "北京市", "朝阳", 3.0),
    _rec("HB001", "河北省", "石家庄市", "长安", 1.5),
]
BASIN_RECORDS = [_rec("HL001", "海河流域", "海河流域", "流域", 6.0)]


def _make_fake_query(calls: dict):
    def fake_query(client, *, now, hours_back):
        calls["n"] += 1
        return API_UTC, list(REGION_RECORDS), list(BASIN_RECORDS), []
    return fake_query


class TestCurrentWeatherCache:
    def test_second_call_within_same_hour_hits_cache(self, monkeypatch):
        """同一时次桶 + 同 hours_back 第二次命中缓存，不重复拉后端。"""
        calls = {"n": 0}
        monkeypatch.setattr(svc, "_query_same_successful_time", _make_fake_query(calls))
        monkeypatch.setattr(svc, "CURRENT_WEATHER_CACHE_TTL", 3600)
        svc._current_weather_cache.clear()

        fixed_now = datetime(2026, 8, 12, 12, 30, tzinfo=svc.BEIJING_TIMEZONE)
        r1 = svc.query_current_weather_observation_core(
            lambda: None, now=fixed_now, hours_back=6
        )
        assert r1["status"] == "ok"
        after_first = calls["n"]
        r2 = svc.query_current_weather_observation_core(
            lambda: None, now=fixed_now, hours_back=6
        )
        assert r1 == r2
        assert calls["n"] == after_first, f"第二次应命中缓存，实际多拉了 {calls['n'] - after_first} 次"

    def test_cache_expires_after_ttl(self, monkeypatch):
        """TTL=0 强制过期后重新拉后端。"""
        calls = {"n": 0}
        monkeypatch.setattr(svc, "_query_same_successful_time", _make_fake_query(calls))
        monkeypatch.setattr(svc, "CURRENT_WEATHER_CACHE_TTL", 0)
        svc._current_weather_cache.clear()

        fixed_now = datetime(2026, 8, 12, 12, 30, tzinfo=svc.BEIJING_TIMEZONE)
        svc.query_current_weather_observation_core(lambda: None, now=fixed_now, hours_back=6)
        after_first = calls["n"]
        svc.query_current_weather_observation_core(lambda: None, now=fixed_now, hours_back=6)
        assert calls["n"] > after_first, "TTL=0 应重新拉后端"

    def test_cache_key_contains_hour_bucket(self, monkeypatch):
        """跨时次桶（now 换小时）必 miss，不服务旧时次数据。"""
        calls = {"n": 0}
        monkeypatch.setattr(svc, "_query_same_successful_time", _make_fake_query(calls))
        monkeypatch.setattr(svc, "CURRENT_WEATHER_CACHE_TTL", 3600)
        svc._current_weather_cache.clear()

        now_a = datetime(2026, 8, 12, 12, 30, tzinfo=svc.BEIJING_TIMEZONE)
        now_b = datetime(2026, 8, 12, 13, 5, tzinfo=svc.BEIJING_TIMEZONE)
        svc.query_current_weather_observation_core(lambda: None, now=now_a, hours_back=6)
        after_first = calls["n"]
        svc.query_current_weather_observation_core(lambda: None, now=now_b, hours_back=6)
        assert calls["n"] > after_first, "跨时次桶应重新拉后端"

    def test_no_data_result_not_cached(self, monkeypatch):
        """status=no_data 不写缓存，下一次仍重拉后端。"""
        calls = {"n": 0}

        def fake_query_no_data(client, *, now, hours_back):
            calls["n"] += 1
            return None, [], [], []

        monkeypatch.setattr(svc, "_query_same_successful_time", fake_query_no_data)
        monkeypatch.setattr(svc, "CURRENT_WEATHER_CACHE_TTL", 3600)
        svc._current_weather_cache.clear()

        fixed_now = datetime(2026, 8, 12, 12, 30, tzinfo=svc.BEIJING_TIMEZONE)
        r1 = svc.query_current_weather_observation_core(
            lambda: None, now=fixed_now, hours_back=6
        )
        assert r1["status"] == "no_data"
        after_first = calls["n"]
        svc.query_current_weather_observation_core(lambda: None, now=fixed_now, hours_back=6)
        assert calls["n"] > after_first, "no_data 不应写缓存"
