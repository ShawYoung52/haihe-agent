"""滚动预报过去日期解析与历史实况路由标记测试。

覆盖两类修复：
1. "号"字日期解析（8月10号 / 裸 10号 / 写全年份严格 / 同一年已过去日期按今年历史实况）；
2. 过去日历日在 query_rolling_forecast_core 返回结构化 past_date 标记，
   不再静默回退未来预报、不再报 240h 原始异常、不再把过去日期当回算预报返回。
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import rolling_forecast_service as rfs  # noqa: E402


NOW = datetime(2026, 8, 13, 10, 0, tzinfo=rfs.TIANJIN_TIMEZONE)


def _extract(query: str, current: datetime = NOW) -> list[date]:
    return rfs._extract_explicit_query_dates(query, current)


class TestExtractExplicitQueryDatesHao:
    def test_hao_recent_past_stays_current_year(self):
        """8月10号：近期过去（3天前）应保持当年，供历史实况判定。"""
        assert _extract("8月10号天津大学天气怎么样") == [date(2026, 8, 10)]

    def test_hao_future_stays_current_year(self):
        """8月14号：未来日期正常解析。"""
        assert _extract("8月14号天津天气") == [date(2026, 8, 14)]

    def test_ri_and_hao_equivalent(self):
        """8月10日 与 8月10号 解析一致。"""
        assert _extract("8月10日天津天气") == [date(2026, 8, 10)]

    def test_bare_day_recent_past(self):
        """裸 10号：当月近期过去（3天前）保持当月。"""
        assert _extract("10号天津天气怎么样") == [date(2026, 8, 10)]

    def test_bare_day_future_this_month(self):
        """裸 30号：当月未来日期（17天后）保持当月，不推到下月。"""
        assert _extract("30号天津天气") == [date(2026, 8, 30)]

    def test_bare_day_guarded_by_di(self):
        """第10号（台风/预警编号）不得被当作日期。"""
        assert _extract("关于第10号台风的路径") == []

    def test_bare_day_guarded_by_place_suffix(self):
        """3号教学楼/5号病房等地址建筑编号不得被当作日期。"""
        assert _extract("3号教学楼天气怎么样") == []
        assert _extract("5号病房温度合适吗") == []
        assert _extract("2号院明天天气") == []
        # 天气问法里的裸日期仍正常解析
        assert _extract("10号天津天气") == [date(2026, 8, 10)]

    def test_far_past_no_year_keeps_current_year_historical(self):
        """3月5日（5个月前）：无年份今年已过去日期按今年历史实况处理，不推明年（天气问答推明年超240h无答案）。"""
        assert _extract("3月5日天津天气") == [date(2026, 3, 5)]

    def test_far_past_ri_and_hao_same_year_historical(self):
        """7月11日/7月11号（同一年已过去>15天）：一律保持今年历史，号与日解析一致。"""
        assert _extract("同乐小学7月11日天气怎么样") == [date(2026, 7, 11)]
        assert _extract("同乐小学7月11号天气怎么样") == [date(2026, 7, 11)]

    def test_bare_far_past_keeps_current_month(self):
        """裸 2号（本月已过去>15天，如 8/20 问 2号）：保持当月，不推下月。"""
        current = datetime(2026, 8, 20, 10, 0, tzinfo=rfs.TIANJIN_TIMEZONE)
        assert rfs._extract_explicit_query_dates("2号天津天气", current) == [date(2026, 8, 2)]

    def test_full_year_exact_past(self):
        """写全年份的过去日期严格按该年解析，不做未来化。"""
        assert _extract("2025年8月10日天津天气") == [date(2025, 8, 10)]
        assert _extract("2026-8-10天津天气") == [date(2026, 8, 10)]

    def test_range_dates(self):
        """8月10号到8月12号：解析两个日期且按序。"""
        result = _extract("8月10号到8月12号天津天气")
        assert result == [date(2026, 8, 10), date(2026, 8, 12)]


class TestPastDateMarker:
    def _fake_get(self, url, params, timeout):
        raise AssertionError(f"历史日期不应发起滚动预报接口请求：{params}")

    def test_point_past_date_returns_marker(self, monkeypatch):
        """点位模式 + 近期过去日期 → 返回 past_date 标记且不请求预报接口。"""
        monkeypatch.setattr(rfs.requests, "get", self._fake_get)
        result = rfs.query_rolling_forecast_core(
            "8月10号天津大学天气怎么样",
            lon=117.2, lat=39.1, point_name="天津大学",
            now=NOW,
        )
        assert result.get("status") == "past_date"
        assert result.get("query_mode") == "historical_obs_request"
        assert "historical_window" in result
        assert result["historical_window"]["target_start"].startswith("2026-08-10")
        assert "query_poi_historical_weather" in result.get("message", "")
        assert result.get("periods") is None or result["periods"] == []

    def test_region_past_date_returns_marker(self, monkeypatch):
        """区域模式 + 近期过去日期 → 同样返回 past_date 标记。"""
        monkeypatch.setattr(rfs.requests, "get", self._fake_get)
        result = rfs.query_rolling_forecast_core("8月10号天津天气", now=NOW)
        assert result.get("status") == "past_date"
        assert "天津市区" in result.get("query_regions", [])

    def test_full_year_past_date_returns_marker(self, monkeypatch):
        """写全年份的过去日期（2025年）同样判定为历史。"""
        monkeypatch.setattr(rfs.requests, "get", self._fake_get)
        result = rfs.query_rolling_forecast_core("2025年8月10日天津天气", now=NOW)
        assert result.get("status") == "past_date"
        assert result["historical_window"]["target_start"].startswith("2025-08-10")

    def test_today_not_historical(self, monkeypatch):
        """今天/明天/未来日期仍走正常预报，不判定为历史。"""
        calls = {"n": 0}

        def fake_get(url, params, timeout):
            calls["n"] += 1
            return type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"resultData": {}}})()

        monkeypatch.setattr(rfs.requests, "get", fake_get)
        result = rfs.query_rolling_forecast_core("今天天津天气", now=NOW)
        assert result.get("status") != "past_date"
        assert calls["n"] == 1

        calls["n"] = 0
        result = rfs.query_rolling_forecast_core("8月14号天津天气", now=NOW)
        assert result.get("status") != "past_date"
        assert result.get("query_mode") == "calendar_daily"
        assert calls["n"] == 1

    def test_yesterday_and_day_before_yesterday_marker(self, monkeypatch):
        """昨天/前天 同样判定为历史日期并返回 past_date 标记。"""
        monkeypatch.setattr(rfs.requests, "get", self._fake_get)
        result = rfs.query_rolling_forecast_core("昨天天津天气", now=NOW)
        assert result.get("status") == "past_date"
        assert result["historical_window"]["target_start"].startswith("2026-08-12")
        result = rfs.query_rolling_forecast_core("前天天津大学天气怎么样", lon=117.2, lat=39.1, point_name="天津大学", now=NOW)
        assert result.get("status") == "past_date"
        assert result["historical_window"]["target_start"].startswith("2026-08-11")

    def test_same_year_far_past_returns_marker(self, monkeypatch):
        """同一年已过去>15天的日期（7月11日/3月5日）→ 返回 past_date 标记，不再报 240h 越界。"""
        monkeypatch.setattr(rfs.requests, "get", self._fake_get)
        result = rfs.query_rolling_forecast_core(
            "同乐小学7月11日天气怎么样", lon=117.2, lat=39.1, point_name="同乐小学", now=NOW
        )
        assert result.get("status") == "past_date"
        assert result["historical_window"]["target_start"].startswith("2026-07-11")

        result = rfs.query_rolling_forecast_core("3月5日天津天气", now=NOW)
        assert result.get("status") == "past_date"
        assert result["historical_window"]["target_start"].startswith("2026-03-05")
