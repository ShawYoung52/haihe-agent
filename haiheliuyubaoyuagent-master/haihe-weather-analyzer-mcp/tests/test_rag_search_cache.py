"""RAG 知识库检索 rag_search 缓存测试。

背景（2026-08-12 全问题类型性能优化）：
RAG 检索每次查询请求天河 RAG 接口。知识库为静态文档，非实况——TTL 取较长 600s。
键 = query|kb_key；unknown_kb_key / rag_api_failed 等错误结果不写缓存。
"""

from __future__ import annotations

import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import haihe_mcp_tools as hmt

KB = {"key": "yufang", "name": "防汛预案库", "description": "防汛应急预案知识库"}


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _patch_http(monkeypatch, calls: dict):
    payload = {
        "result": {
            "contexts": [
                {
                    "content": "海河流域防汛应急预案第一章……",
                    "source": "海河流域防汛预案.doc",
                    "score": 0.95,
                    "chunking_type": "paragraph",
                    "kb_name": "防汛预案库",
                }
            ]
        }
    }

    def fake_post(url, json=None, timeout=None):
        calls["n"] += 1
        return _FakeResp(payload)

    monkeypatch.setattr(hmt.requests, "post", fake_post)


class TestRagSearchCache:
    def _setup(self, monkeypatch, calls: dict, ttl: int):
        monkeypatch.setattr(
            hmt, "_rag_find_kb_by_key",
            lambda kb_key: KB if kb_key == "yufang" else None,
        )
        _patch_http(monkeypatch, calls)
        monkeypatch.setattr(hmt, "RAG_SEARCH_CACHE_TTL", ttl)
        hmt._rag_search_cache.clear()

    def test_second_call_hits_cache(self, monkeypatch):
        """同 query + kb_key 第二次命中缓存，不再请求 RAG 接口。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls, 3600)

        r1 = hmt._rag_search_core("什么是防汛预案", "yufang")
        assert r1["count"] == 1
        after_first = calls["n"]
        r2 = hmt._rag_search_core("什么是防汛预案", "yufang")
        assert r1 == r2
        assert calls["n"] == after_first, f"第二次应命中缓存，实际多请求了 {calls['n'] - after_first} 次"

    def test_distinct_query_does_not_share(self, monkeypatch):
        """不同 query 不互相命中。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls, 3600)

        hmt._rag_search_core("什么是防汛预案", "yufang")
        after_first = calls["n"]
        hmt._rag_search_core("暴雨预警信号分级标准", "yufang")
        assert calls["n"] > after_first, "不同 query 应重新请求"

    def test_cache_expires_after_ttl(self, monkeypatch):
        """TTL=0 强制过期后重新请求。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls, 0)

        hmt._rag_search_core("什么是防汛预案", "yufang")
        after_first = calls["n"]
        hmt._rag_search_core("什么是防汛预案", "yufang")
        assert calls["n"] > after_first, "TTL=0 应重新请求"

    def test_api_error_not_cached(self, monkeypatch):
        """RAG 接口失败（rag_api_failed）不写缓存。"""
        import requests as real_requests

        calls = {"n": 0}
        monkeypatch.setattr(
            hmt, "_rag_find_kb_by_key",
            lambda kb_key: KB if kb_key == "yufang" else None,
        )

        class _FailingRequests:
            exceptions = real_requests.exceptions

            def post(self, url, json=None, timeout=None):
                calls["n"] += 1
                raise real_requests.exceptions.ConnectionError("RAG 接口不可达")

        monkeypatch.setattr(hmt.requests, "post", _FailingRequests().post)
        monkeypatch.setattr(hmt, "RAG_SEARCH_CACHE_TTL", 3600)
        hmt._rag_search_cache.clear()

        r1 = hmt._rag_search_core("什么是防汛预案", "yufang")
        assert r1.get("error") == "rag_api_failed"
        after_first = calls["n"]
        hmt._rag_search_core("什么是防汛预案", "yufang")
        assert calls["n"] > after_first, "接口失败结果不应写缓存"
