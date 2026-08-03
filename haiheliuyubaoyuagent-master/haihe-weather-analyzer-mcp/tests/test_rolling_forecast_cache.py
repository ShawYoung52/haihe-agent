"""滚动预报数据查询缓存测试。"""

from __future__ import annotations

import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import rolling_forecast_service as rfs


class TestRollingForecastCache:
    def test_cache_returns_same_payload_for_same_params(self, monkeypatch):
        """相同参数第二次查询命中缓存，不重复请求接口。"""
        calls = {"n": 0}

        def fake_get(url, params, timeout):
            calls["n"] += 1
            return type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"resultData": {}}})()

        monkeypatch.setattr(rfs.requests, "get", fake_get)
        # 清空缓存
        rfs._rolling_forecast_cache.clear()

        params = {"fcstTime": "20260801000000", "lon": "117.2", "lat": "39.1", "startPeriod": "0", "endPeriod": "24", "interval": "12"}
        p1 = rfs._cached_rolling_forecast_request(params)
        p2 = rfs._cached_rolling_forecast_request(params)
        assert p1 == p2
        assert calls["n"] == 1, f"期望 1 次请求，实际 {calls['n']}"

    def test_cache_distinct_params_do_not_share(self, monkeypatch):
        """不同参数分别请求，不互相命中。"""
        calls = {"n": 0}

        def fake_get(url, params, timeout):
            calls["n"] += 1
            return type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"d": params["fcstTime"]}})()

        monkeypatch.setattr(rfs.requests, "get", fake_get)
        rfs._rolling_forecast_cache.clear()

        p1 = {"fcstTime": "A", "lon": "1", "lat": "2", "startPeriod": "0", "endPeriod": "24", "interval": "12"}
        p2 = {"fcstTime": "B", "lon": "1", "lat": "2", "startPeriod": "0", "endPeriod": "24", "interval": "12"}
        rfs._cached_rolling_forecast_request(p1)
        rfs._cached_rolling_forecast_request(p2)
        assert calls["n"] == 2, f"期望 2 次请求，实际 {calls['n']}"

    def test_cache_expires_after_ttl(self, monkeypatch):
        """TTL 过期后重新请求。"""
        calls = {"n": 0}

        def fake_get(url, params, timeout):
            calls["n"] += 1
            return type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"ok": True}})()

        monkeypatch.setattr(rfs.requests, "get", fake_get)
        rfs._rolling_forecast_cache.clear()
        monkeypatch.setattr(rfs, "ROLLING_FORECAST_CACHE_TTL", 0)

        params = {"fcstTime": "A", "lon": "1", "lat": "2", "startPeriod": "0", "endPeriod": "24", "interval": "12"}
        rfs._cached_rolling_forecast_request(params)
        rfs._cached_rolling_forecast_request(params)
        assert calls["n"] == 2, f"TTL=0 应每次请求，实际 {calls['n']}"