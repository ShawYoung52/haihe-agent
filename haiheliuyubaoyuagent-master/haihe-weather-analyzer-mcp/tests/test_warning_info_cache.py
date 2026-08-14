"""预警工具缓存测试（effective/history/today_summary/national）。

背景（2026-08-12 全问题类型性能优化）：
预警 4 工具每次查询都请求预警接口。口径（用户已确认）：预警类短 TTL 60-120s；
include_raw=True 仅用于接口排查，不缓存；接口失败结果不写缓存。
"""

from __future__ import annotations

import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import haihe_mcp_tools as hmt


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _patch_http(monkeypatch, calls: dict, payload=None):
    payload = {"code": 200, "data": []} if payload is None else payload

    def fake_get(url, timeout=None, verify=None):
        calls["n"] += 1
        return _FakeResp(payload)

    monkeypatch.setattr(hmt.requests, "get", fake_get)


class TestWarningInfoCache:
    def test_effective_scope_hits_cache(self, monkeypatch):
        """effective 范围第二次命中缓存，不再打 HTTP。"""
        calls = {"n": 0}
        _patch_http(monkeypatch, calls)
        monkeypatch.setattr(hmt, "WARNING_INFO_CACHE_TTL", 3600)
        hmt._warning_info_cache.clear()

        r1 = hmt._fetch_warning_info("effective-path", "effective", include_raw=False)
        after_first = calls["n"]
        r2 = hmt._fetch_warning_info("effective-path", "effective", include_raw=False)
        assert r1 == r2
        assert calls["n"] == after_first, f"第二次应命中缓存，实际多打了 {calls['n'] - after_first} 次 HTTP"

    def test_history_scope_does_not_share_with_effective(self, monkeypatch):
        """history 与 effective 范围各自独立缓存。"""
        calls = {"n": 0}
        _patch_http(monkeypatch, calls)
        monkeypatch.setattr(hmt, "WARNING_INFO_CACHE_TTL", 3600)
        hmt._warning_info_cache.clear()

        hmt._fetch_warning_info("history-path", "history", include_raw=False)
        after_first = calls["n"]
        hmt._fetch_warning_info("effective-path", "effective", include_raw=False)
        assert calls["n"] > after_first, "不同范围应重新请求"

    def test_include_raw_never_cached(self, monkeypatch):
        """include_raw=True（接口排查）不缓存。"""
        calls = {"n": 0}
        _patch_http(monkeypatch, calls)
        monkeypatch.setattr(hmt, "WARNING_INFO_CACHE_TTL", 3600)
        hmt._warning_info_cache.clear()

        hmt._fetch_warning_info("effective-path", "effective", include_raw=True)
        after_first = calls["n"]
        hmt._fetch_warning_info("effective-path", "effective", include_raw=True)
        assert calls["n"] > after_first, "include_raw=True 不应缓存"

    def test_cache_expires_after_ttl(self, monkeypatch):
        """TTL=0 强制过期后重新请求。"""
        calls = {"n": 0}
        _patch_http(monkeypatch, calls)
        monkeypatch.setattr(hmt, "WARNING_INFO_CACHE_TTL", 0)
        hmt._warning_info_cache.clear()

        hmt._fetch_warning_info("effective-path", "effective", include_raw=False)
        after_first = calls["n"]
        hmt._fetch_warning_info("effective-path", "effective", include_raw=False)
        assert calls["n"] > after_first, "TTL=0 应重新请求"

    def test_http_error_not_cached(self, monkeypatch):
        """接口失败（warning_api_failed）不写缓存。"""
        calls = {"n": 0}

        def failing_get(url, timeout=None, verify=None):
            calls["n"] += 1
            raise RuntimeError("预警接口不可达")

        monkeypatch.setattr(hmt.requests, "get", failing_get)
        monkeypatch.setattr(hmt, "WARNING_INFO_CACHE_TTL", 3600)
        hmt._warning_info_cache.clear()

        r1 = hmt._fetch_warning_info("effective-path", "effective", include_raw=False)
        assert r1.get("error") == "warning_api_failed"
        after_first = calls["n"]
        hmt._fetch_warning_info("effective-path", "effective", include_raw=False)
        assert calls["n"] > after_first, "接口失败结果不应写缓存"


class TestTodayWarningSummaryCache:
    def test_second_call_hits_cache(self, monkeypatch):
        """今日汇总第二次命中缓存，不再走两次 HTTP。"""
        calls = {"n": 0}
        _patch_http(monkeypatch, calls)
        monkeypatch.setattr(hmt, "TODAY_WARNING_SUMMARY_CACHE_TTL", 3600)
        hmt._today_warning_summary_cache.clear()

        r1 = hmt._fetch_today_warning_summary()
        assert r1["warning_status"] == "today_summary"
        after_first = calls["n"]
        r2 = hmt._fetch_today_warning_summary()
        assert r1 == r2
        assert calls["n"] == after_first, "第二次应命中缓存"

    def test_cache_expires_after_ttl(self, monkeypatch):
        """TTL=0 强制过期后重新请求。"""
        calls = {"n": 0}
        _patch_http(monkeypatch, calls)
        monkeypatch.setattr(hmt, "TODAY_WARNING_SUMMARY_CACHE_TTL", 0)
        hmt._today_warning_summary_cache.clear()

        hmt._fetch_today_warning_summary()
        after_first = calls["n"]
        hmt._fetch_today_warning_summary()
        assert calls["n"] > after_first, "TTL=0 应重新请求"


class TestNationalWarningCache:
    def test_same_keywords_hits_cache(self, monkeypatch):
        """同关键词 + max_items 第二次命中缓存。"""
        calls = {"n": 0}
        _patch_http(monkeypatch, calls, payload=[])
        monkeypatch.setattr(hmt, "NATIONAL_WARNING_CACHE_TTL", 3600)
        hmt._national_warning_cache.clear()

        r1 = hmt._fetch_national_warning_info("天津", 30)
        assert r1["count"] == 0
        after_first = calls["n"]
        r2 = hmt._fetch_national_warning_info("天津", 30)
        assert r1 == r2
        assert calls["n"] == after_first, "第二次应命中缓存"

    def test_distinct_keywords_do_not_share(self, monkeypatch):
        """不同关键词不互相命中。"""
        calls = {"n": 0}
        _patch_http(monkeypatch, calls, payload=[])
        monkeypatch.setattr(hmt, "NATIONAL_WARNING_CACHE_TTL", 3600)
        hmt._national_warning_cache.clear()

        hmt._fetch_national_warning_info("天津", 30)
        after_first = calls["n"]
        hmt._fetch_national_warning_info("北京", 30)
        assert calls["n"] > after_first, "不同关键词应重新请求"

    def test_cache_expires_after_ttl(self, monkeypatch):
        """TTL=0 强制过期后重新请求。"""
        calls = {"n": 0}
        _patch_http(monkeypatch, calls, payload=[])
        monkeypatch.setattr(hmt, "NATIONAL_WARNING_CACHE_TTL", 0)
        hmt._national_warning_cache.clear()

        hmt._fetch_national_warning_info("天津", 30)
        after_first = calls["n"]
        hmt._fetch_national_warning_info("天津", 30)
        assert calls["n"] > after_first, "TTL=0 应重新请求"
