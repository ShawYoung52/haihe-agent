"""天津大风预警评估 get_tianjin_wind_warning_assessment 缓存测试。

背景（2026-08-12 全问题类型性能优化）：
工具每次查询重拉天擎天津风力实况。口径（用户已确认）：预警类短 TTL 120s，
缓存键 = 请求时次 request_time（同一时次数据 120s 内稳定）；接口失败结果不写缓存。
"""

from __future__ import annotations

import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import haihe_mcp_tools as hmt


class _FakeMusicClient:
    fetch_calls = 0

    def __init__(self):
        pass

    def get_surf_ele_in_region_by_time(self, admin_codes, times):
        type(self).fetch_calls += 1
        return []


def _canned_evaluate(records, query_time):
    return {
        "status": "ok",
        "query_time": query_time,
        "threshold_comparison": [],
        "station_table": [],
        "area_distribution": [],
    }


class TestTianjinWindWarningCache:
    def test_same_request_time_hits_cache(self, monkeypatch):
        """同一请求时次第二次命中缓存，不再拉天擎。"""
        _FakeMusicClient.fetch_calls = 0
        monkeypatch.setattr(hmt, "MusicClient", _FakeMusicClient)
        monkeypatch.setattr(hmt, "_evaluate_tianjin_wind_warning", _canned_evaluate)
        monkeypatch.setattr(hmt, "TIANJIN_WIND_CACHE_TTL", 3600)
        hmt._tianjin_wind_cache.clear()

        r1 = hmt._query_tianjin_wind_warning_core("20260812120000")
        assert r1["status"] == "ok"
        after_first = _FakeMusicClient.fetch_calls
        r2 = hmt._query_tianjin_wind_warning_core("20260812120000")
        assert r1 == r2
        assert _FakeMusicClient.fetch_calls == after_first, "第二次应命中缓存，不应重拉天擎"

    def test_distinct_request_time_does_not_share(self, monkeypatch):
        """不同请求时次不互相命中。"""
        _FakeMusicClient.fetch_calls = 0
        monkeypatch.setattr(hmt, "MusicClient", _FakeMusicClient)
        monkeypatch.setattr(hmt, "_evaluate_tianjin_wind_warning", _canned_evaluate)
        monkeypatch.setattr(hmt, "TIANJIN_WIND_CACHE_TTL", 3600)
        hmt._tianjin_wind_cache.clear()

        hmt._query_tianjin_wind_warning_core("20260812120000")
        after_first = _FakeMusicClient.fetch_calls
        hmt._query_tianjin_wind_warning_core("20260812130000")
        assert _FakeMusicClient.fetch_calls > after_first, "不同时次应重新拉取"

    def test_cache_expires_after_ttl(self, monkeypatch):
        """TTL=0 强制过期后重新拉取。"""
        _FakeMusicClient.fetch_calls = 0
        monkeypatch.setattr(hmt, "MusicClient", _FakeMusicClient)
        monkeypatch.setattr(hmt, "_evaluate_tianjin_wind_warning", _canned_evaluate)
        monkeypatch.setattr(hmt, "TIANJIN_WIND_CACHE_TTL", 0)
        hmt._tianjin_wind_cache.clear()

        hmt._query_tianjin_wind_warning_core("20260812120000")
        after_first = _FakeMusicClient.fetch_calls
        hmt._query_tianjin_wind_warning_core("20260812120000")
        assert _FakeMusicClient.fetch_calls > after_first, "TTL=0 应重新拉取"

    def test_fetch_error_not_cached(self, monkeypatch):
        """接口失败（wind_observation_api_failed）不写缓存。"""
        class _FailingClient(_FakeMusicClient):
            def get_surf_ele_in_region_by_time(self, admin_codes, times):
                type(self).fetch_calls += 1
                raise RuntimeError("MUSIC 不可达")

        monkeypatch.setattr(hmt, "MusicClient", _FailingClient)
        monkeypatch.setattr(hmt, "TIANJIN_WIND_CACHE_TTL", 3600)
        hmt._tianjin_wind_cache.clear()

        r1 = hmt._query_tianjin_wind_warning_core("20260812120000")
        assert r1["status"] == "wind_observation_api_failed"
        after_first = _FailingClient.fetch_calls
        hmt._query_tianjin_wind_warning_core("20260812120000")
        assert _FailingClient.fetch_calls > after_first, "接口失败结果不应写缓存"
