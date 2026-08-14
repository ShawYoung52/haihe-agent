"""B2: 应急实况判定 evaluate_emergency_response_core 工具层缓存测试。

背景（全问题类型性能优化）：应急实况判定是单次最贵工具（24h 分钟降水取数 ~43s），
同一时次重复问会重复取数。口径（用户已确认短 TTL 60-120s）：120s TTL 缓存，
键 = 判定入参（不含 include_records）；include_records=True（要原始记录）不缓存。
只动核心函数外壳，不触碰 _evaluate_one_synoptic_time/_fetch_minute_hourly_curve。
"""

from __future__ import annotations

import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import haihe_mcp_tools as hmt

TS = "20260812080000"


def _make_fetch(calls: dict):
    def fake_fetch(*, basin_codes, times, elements=None):
        calls["n"] += 1
        return []
    return fake_fetch


class TestEmergencyEvaluationCache:
    def _setup(self, monkeypatch, calls: dict, ttl: int):
        monkeypatch.setattr(hmt, "_observation_fetch_core", _make_fetch(calls))
        monkeypatch.setattr(hmt, "EMERGENCY_EVALUATION_CACHE_TTL", ttl)
        hmt._emergency_evaluation_cache.clear()

    def test_same_params_second_call_hits_cache(self, monkeypatch):
        """同 basin_codes+times 第二次命中缓存，不再拉 24h 分钟数据。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls, 3600)

        r1 = hmt.evaluate_emergency_response_core(times=TS)
        after_first = calls["n"]
        r2 = hmt.evaluate_emergency_response_core(times=TS)
        assert r1 == r2
        assert calls["n"] == after_first, f"第二次应命中缓存，实际多拉了 {calls['n'] - after_first} 次"

    def test_distinct_times_do_not_share(self, monkeypatch):
        """不同时次不互相命中。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls, 3600)

        hmt.evaluate_emergency_response_core(times=TS)
        after_first = calls["n"]
        hmt.evaluate_emergency_response_core(times="20260812090000")
        assert calls["n"] > after_first, "不同时次应重新拉取"

    def test_cache_expires_after_ttl(self, monkeypatch):
        """TTL=0 强制过期后重新拉取。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls, 0)

        hmt.evaluate_emergency_response_core(times=TS)
        after_first = calls["n"]
        hmt.evaluate_emergency_response_core(times=TS)
        assert calls["n"] > after_first, "TTL=0 应重新拉取"

    def test_include_records_never_cached(self, monkeypatch):
        """include_records=True（要原始记录）不缓存。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls, 3600)

        hmt.evaluate_emergency_response_core(times=TS, include_records=True)
        after_first = calls["n"]
        hmt.evaluate_emergency_response_core(times=TS, include_records=True)
        assert calls["n"] > after_first, "include_records=True 不应缓存"
