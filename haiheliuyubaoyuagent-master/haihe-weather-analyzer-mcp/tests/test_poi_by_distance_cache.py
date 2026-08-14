"""按距离 POI 检索 search_poi_by_distance 缓存测试。

背景（2026-08-12 全问题类型性能优化）：
_search_poi_by_distance_core 与已缓存的 _search_poi_core 同族但一直无缓存，每次重复打 ES。
POI 数据静态，TTL 取 3600s（与 POI_SEARCH_CACHE_TTL 同口径）。键 = keyword|lon|lat|size|distance_km。
"""

from __future__ import annotations

import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import haihe_mcp_tools as hmt


def _fake_es(hits, calls):
    def fake_search(self, index, body):
        calls["n"] += 1
        return {"hits": {"hits": hits}}
    return type("ES", (), {"search": fake_search})()


class TestPoiByDistanceCache:
    def test_second_call_same_params_hits_cache(self, monkeypatch):
        """同关键词 + 坐标 + size + 距离第二次命中缓存，不重复打 ES。"""
        calls = {"n": 0}
        monkeypatch.setattr(hmt, "_get_poi_es_client", lambda: _fake_es([], calls))
        monkeypatch.setattr(hmt, "POI_BY_DISTANCE_CACHE_TTL", 3600)
        hmt._poi_by_distance_cache.clear()

        r1 = hmt._search_poi_by_distance_core("梅江会展中心", 117.2, 39.1, 10, 10)
        after_first = calls["n"]
        r2 = hmt._search_poi_by_distance_core("梅江会展中心", 117.2, 39.1, 10, 10)
        assert r1 == r2
        assert calls["n"] == after_first, f"第二次应命中缓存，实际多打了 {calls['n'] - after_first} 次 ES"

    def test_distinct_keyword_does_not_share(self, monkeypatch):
        """不同关键词不互相命中。"""
        calls = {"n": 0}
        monkeypatch.setattr(hmt, "_get_poi_es_client", lambda: _fake_es([], calls))
        monkeypatch.setattr(hmt, "POI_BY_DISTANCE_CACHE_TTL", 3600)
        hmt._poi_by_distance_cache.clear()

        hmt._search_poi_by_distance_core("梅江会展中心", 117.2, 39.1, 10, 10)
        after_first = calls["n"]
        hmt._search_poi_by_distance_core("天津站", 117.2, 39.1, 10, 10)
        assert calls["n"] > after_first, "不同关键词应重新查询"

    def test_distinct_coords_do_not_share(self, monkeypatch):
        """不同坐标不互相命中（距离检索依赖中心点）。"""
        calls = {"n": 0}
        monkeypatch.setattr(hmt, "_get_poi_es_client", lambda: _fake_es([], calls))
        monkeypatch.setattr(hmt, "POI_BY_DISTANCE_CACHE_TTL", 3600)
        hmt._poi_by_distance_cache.clear()

        hmt._search_poi_by_distance_core("梅江会展中心", 117.2, 39.1, 10, 10)
        after_first = calls["n"]
        hmt._search_poi_by_distance_core("梅江会展中心", 117.3, 39.2, 10, 10)
        assert calls["n"] > after_first, "不同坐标应重新查询"

    def test_cache_expires_after_ttl(self, monkeypatch):
        """TTL=0 强制过期后重新查询。"""
        calls = {"n": 0}
        monkeypatch.setattr(hmt, "_get_poi_es_client", lambda: _fake_es([], calls))
        monkeypatch.setattr(hmt, "POI_BY_DISTANCE_CACHE_TTL", 0)
        hmt._poi_by_distance_cache.clear()

        hmt._search_poi_by_distance_core("梅江会展中心", 117.2, 39.1, 10, 10)
        after_first = calls["n"]
        hmt._search_poi_by_distance_core("梅江会展中心", 117.2, 39.1, 10, 10)
        assert calls["n"] > after_first, "TTL=0 应重新查询"

    def test_invalid_lon_not_cached(self, monkeypatch):
        """非法坐标抛 BusinessException，不写缓存。"""
        monkeypatch.setattr(hmt, "POI_BY_DISTANCE_CACHE_TTL", 3600)
        hmt._poi_by_distance_cache.clear()

        with __import__("pytest").raises(Exception):
            hmt._search_poi_by_distance_core("梅江会展中心", "abc", 39.1, 10, 10)
        assert hmt._poi_by_distance_cache == {}, "非法参数不应写缓存"
