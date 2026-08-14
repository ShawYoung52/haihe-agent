"""B1: forecast_evaluate_tool 缓存顺序缺陷测试。

缺陷：evaluate_forecast 先调 _validate_params_and_fetch（内部已调检验 API）再查缓存，
1h 缓存每次命中仍付全量 API 调用，缓存形同虚设。修复后拆成
「廉价校验/解析 → 缓存命中判断 → 昂贵取数（仅 miss 时）」。
测试锁定：同参第二次调用不再调检验 API（缓存命中跳过取数）。
"""

from __future__ import annotations

import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import forecast_evaluate_tool as fet


def _fake_rain_eva(calls: dict):
    def fake(**kwargs):
        calls["n"] += 1
        return {"request_success": True, "data": [], "raw_response": {}}
    return fake


class TestForecastEvaluateCacheOrder:
    def _setup(self, monkeypatch, calls: dict, ttl: int):
        monkeypatch.setattr(fet, "run_rain_eva", _fake_rain_eva(calls))
        monkeypatch.setattr(fet, "run_temp_eva", _fake_rain_eva(calls))
        monkeypatch.setattr(fet, "_format_evaluate_result",
                            lambda api_result, element, test_type, rain_type: {"element": element, "ok": True})
        monkeypatch.setattr(fet, "_CACHE_TTL_SECONDS", ttl)
        fet._CACHE.clear()

    def test_cache_hit_skips_api(self, monkeypatch):
        """同参第二次调用命中缓存，不再调检验 API。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls, 3600)

        r1 = fet._evaluate_forecast_core(
            "rain24", "daily", "ng", "2026-08-01 00:00:00", "2026-08-13 00:00:00", 24, ""
        )
        assert r1["ok"] is True
        after_first = calls["n"]
        r2 = fet._evaluate_forecast_core(
            "rain24", "daily", "ng", "2026-08-01 00:00:00", "2026-08-13 00:00:00", 24, ""
        )
        assert r1 == r2
        assert calls["n"] == after_first, f"第二次应命中缓存，实际多调了 {calls['n'] - after_first} 次检验 API"

    def test_distinct_params_do_not_share(self, monkeypatch):
        """不同参数（要素/时间）不互相命中。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls, 3600)

        fet._evaluate_forecast_core(
            "rain24", "daily", "ng", "2026-08-01 00:00:00", "2026-08-13 00:00:00", 24, ""
        )
        after_first = calls["n"]
        fet._evaluate_forecast_core(
            "tmax24", "daily", "", "2026-08-01 00:00:00", "2026-08-13 00:00:00", 24, ""
        )
        assert calls["n"] > after_first, "不同参数应重新调检验 API"

    def test_cache_expires_after_ttl(self, monkeypatch):
        """TTL=0 强制过期后重新调检验 API。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls, 0)

        fet._evaluate_forecast_core(
            "rain24", "daily", "ng", "2026-08-01 00:00:00", "2026-08-13 00:00:00", 24, ""
        )
        after_first = calls["n"]
        fet._evaluate_forecast_core(
            "rain24", "daily", "ng", "2026-08-01 00:00:00", "2026-08-13 00:00:00", 24, ""
        )
        assert calls["n"] > after_first, "TTL=0 应重新调检验 API"

    def test_invalid_params_never_cached(self, monkeypatch):
        """参数校验失败（error）不写缓存，也不调检验 API。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls, 3600)

        r1 = fet._evaluate_forecast_core("bad_element", "daily", "", "", "", 24, "")
        assert "error" in r1
        assert calls["n"] == 0, "参数非法不应调检验 API"
        fet._evaluate_forecast_core("bad_element", "daily", "", "", "", 24, "")
        assert calls["n"] == 0, "参数非法结果不应写缓存"
