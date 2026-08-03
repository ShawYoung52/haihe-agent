"""POI 检索缓存测试。"""

from __future__ import annotations

import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import haihe_mcp_tools as hmt


def _fake_es(hits, calls):
    """构造返回指定 hits 的 fake ES 客户端。"""
    def fake_search(self, index, body):
        calls["n"] += 1
        return {"hits": {"hits": hits}}
    return type("ES", (), {"search": fake_search})()


class TestPOISearchCache:
    def test_cache_returns_same_result_for_same_keyword(self, monkeypatch):
        """相同关键词 + size 第二次命中缓存，不重复打 ES。"""
        calls = {"n": 0}
        monkeypatch.setattr(hmt, "_get_poi_es_client", lambda: _fake_es([{"_source": {"name": "X"}}], calls))
        hmt._poi_search_cache.clear()
        monkeypatch.setattr(hmt, "POI_SEARCH_CACHE_TTL", 3600)

        r1 = hmt._search_poi_core("梅江会展中心", 5)
        after_first = calls["n"]
        r2 = hmt._search_poi_core("梅江会展中心", 5)
        assert r1 == r2
        assert calls["n"] == after_first, f"第二次应命中缓存，实际多打了 {calls['n'] - after_first} 次 ES"

    def test_cache_distinct_keywords_do_not_share(self, monkeypatch):
        """不同关键词分别查询，不互相命中。"""
        calls = {"n": 0}
        monkeypatch.setattr(hmt, "_get_poi_es_client", lambda: _fake_es([{"_source": {"name": "X"}}], calls))
        hmt._poi_search_cache.clear()
        monkeypatch.setattr(hmt, "POI_SEARCH_CACHE_TTL", 3600)

        hmt._search_poi_core("梅江会展中心", 5)
        after_first = calls["n"]
        hmt._search_poi_core("天津站", 5)
        assert calls["n"] > after_first, "不同关键词应重新查询"

    def test_cache_expires_after_ttl(self, monkeypatch):
        """TTL 过期后重新查询。"""
        calls = {"n": 0}
        monkeypatch.setattr(hmt, "_get_poi_es_client", lambda: _fake_es([{"_source": {"name": "X"}}], calls))
        hmt._poi_search_cache.clear()
        monkeypatch.setattr(hmt, "POI_SEARCH_CACHE_TTL", 0)

        hmt._search_poi_core("梅江会展中心", 5)
        after_first = calls["n"]
        hmt._search_poi_core("梅江会展中心", 5)
        assert calls["n"] > after_first, "TTL=0 应重新查询"