"""水位查询 query_water_level 缓存测试。

背景（2026-08-12 全问题类型性能优化）：
水位工具每次查询请求十四所接口。口径（用户已确认）：水位类短 TTL 120s。
默认查询（不传 begin/end）键 = 河名|类型|今日零点（跨天必 miss，TTL 管新鲜度）；
显式传时间段按完整时间段做键；接口失败结果不写缓存。
"""

from __future__ import annotations

import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import tools


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeRequests:
    def __init__(self, calls: dict, payload):
        self._calls = calls
        self._payload = payload

    def post(self, url, json=None, timeout=None):
        self._calls["n"] += 1
        return _FakeResp(self._payload)


def _patch_http(monkeypatch, calls: dict, payload=None):
    payload = (
        [{"stationName": "子牙河-工农兵闸", "waterLevel": 3.2, "waterWarn": 4.0}]
        if payload is None
        else payload
    )
    monkeypatch.setattr(tools, "requests", _FakeRequests(calls, payload))


class TestWaterLevelCache:
    def test_default_query_second_call_hits_cache(self, monkeypatch):
        """默认查询（不传时间段）同日第二次命中缓存，不再请求接口。"""
        calls = {"n": 0}
        _patch_http(monkeypatch, calls)
        monkeypatch.setattr(tools, "WATER_LEVEL_CACHE_TTL", 3600)
        tools._water_level_cache.clear()

        r1 = tools._query_water_level_core("子牙河", data_type="river")
        assert r1["count"] >= 1
        after_first = calls["n"]
        r2 = tools._query_water_level_core("子牙河", data_type="river")
        assert r1 == r2
        assert calls["n"] == after_first, f"第二次应命中缓存，实际多请求了 {calls['n'] - after_first} 次"

    def test_distinct_river_does_not_share(self, monkeypatch):
        """不同河名不互相命中。"""
        calls = {"n": 0}
        _patch_http(monkeypatch, calls)
        monkeypatch.setattr(tools, "WATER_LEVEL_CACHE_TTL", 3600)
        tools._water_level_cache.clear()

        tools._query_water_level_core("子牙河", data_type="river")
        after_first = calls["n"]
        tools._query_water_level_core("独流减河", data_type="river")
        assert calls["n"] > after_first, "不同河名应重新请求"

    def test_explicit_time_range_is_distinct_key(self, monkeypatch):
        """显式传时间段按完整时间段做键，与默认查询不共享。"""
        calls = {"n": 0}
        _patch_http(monkeypatch, calls)
        monkeypatch.setattr(tools, "WATER_LEVEL_CACHE_TTL", 3600)
        tools._water_level_cache.clear()

        tools._query_water_level_core("子牙河", data_type="river")
        after_first = calls["n"]
        tools._query_water_level_core(
            "子牙河",
            begin_time="2026-08-13 00:00:00",
            end_time="2026-08-13 23:00:00",
            data_type="river",
        )
        assert calls["n"] > after_first, "显式时间段应重新请求"

    def test_cache_expires_after_ttl(self, monkeypatch):
        """TTL=0 强制过期后重新请求。"""
        calls = {"n": 0}
        _patch_http(monkeypatch, calls)
        monkeypatch.setattr(tools, "WATER_LEVEL_CACHE_TTL", 0)
        tools._water_level_cache.clear()

        tools._query_water_level_core("子牙河", data_type="river")
        after_first = calls["n"]
        tools._query_water_level_core("子牙河", data_type="river")
        assert calls["n"] > after_first, "TTL=0 应重新请求"

    def test_connection_error_not_cached(self, monkeypatch):
        """接口失败（error 结果）不写缓存。"""
        import requests as real_requests

        calls = {"n": 0}

        class _FailingRequests:
            exceptions = real_requests.exceptions

            def post(self, url, json=None, timeout=None):
                calls["n"] += 1
                raise real_requests.exceptions.ConnectionError("水位服务不可达")

        monkeypatch.setattr(tools, "requests", _FailingRequests())
        monkeypatch.setattr(tools, "WATER_LEVEL_CACHE_TTL", 3600)
        tools._water_level_cache.clear()

        r1 = tools._query_water_level_core("子牙河", data_type="river")
        assert "error" in r1
        after_first = calls["n"]
        tools._query_water_level_core("子牙河", data_type="river")
        assert calls["n"] > after_first, "接口失败结果不应写缓存"
