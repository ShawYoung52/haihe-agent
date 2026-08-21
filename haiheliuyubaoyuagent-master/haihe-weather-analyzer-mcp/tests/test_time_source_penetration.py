# -*- coding: utf-8 -*-
"""切换系统时间穿透 MCP 侧验证。

证明：设置共享时间源文件（SIM_TIME_FILE=2026-07-10 15:00）后，不传 now 参数的
滚动预报解析（"今天/明天/未来三天/本周末/昨天"）全部按 7月10日 锚定——即
rolling_forecast_service 的默认值确实走 time_source.now()。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import time_source  # noqa: E402
import rolling_forecast_service as rfs  # noqa: E402


@pytest.fixture()
def sim(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_TIME_FILE", str(tmp_path / "sim.json"))
    time_source._invalidate()
    yield
    time_source._invalidate()


def _set():
    time_source.set_override_from_text("2026-07-10 15:00:00")


def _fake_get(url, params, timeout):
    raise AssertionError(f"历史日期不应发起滚动预报接口请求：{params}")


class TestCalendarWindowPenetration:
    def test_today_resolves_0710(self, sim):
        _set()
        win = rfs.resolve_requested_calendar_window("今天天津天气")
        assert win["forecast_start_date"] == "2026-07-10"
        assert win["forecast_days"] == 1

    def test_tomorrow_resolves_0711(self, sim):
        _set()
        win = rfs.resolve_requested_calendar_window("明天天津天气")
        assert win["forecast_start_date"] == "2026-07-11"

    def test_next_three_days_resolves_0711_3d(self, sim):
        _set()
        win = rfs.resolve_requested_calendar_window("未来三天天津天气")
        assert win["forecast_start_date"] == "2026-07-11"
        assert win["forecast_days"] == 3

    def test_weekend_resolves_0711_2d(self, sim):
        # 2026-07-10 是周五 → 本周末 = 7/11(六) + 7/12(日)
        _set()
        win = rfs.resolve_requested_calendar_window("本周末适合去泰达航母主题公园游玩吗")
        assert win["forecast_start_date"] == "2026-07-11"
        assert win["forecast_days"] == 2

    def test_no_override_uses_real_today(self, sim):
        win = rfs.resolve_requested_calendar_window("今天天津天气")
        assert win["forecast_start_date"] == datetime.now().strftime("%Y-%m-%d")


class TestCorePastDatePenetration:
    def test_yesterday_is_past_date_0709(self, sim, monkeypatch):
        """昨天（7/9 < 7/10）→ past_date 标记，且不请求预报接口。"""
        _set()
        monkeypatch.setattr(rfs.requests, "get", _fake_get)
        result = rfs.query_rolling_forecast_core("昨天天津天气")
        assert result.get("status") == "past_date"
        assert result.get("query_mode") == "historical_obs_request"
        assert result["historical_window"]["target_start"].startswith("2026-07-09")

    def test_today_not_past(self, sim, monkeypatch):
        """今天（7/10 = 覆盖今）→ 走预报/日历路径，不是历史。"""
        _set()
        monkeypatch.setattr(rfs.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("应走日历路径")))
        # 只验证解析层：今天的 target 是 7/10 而不是历史标记
        win = rfs.resolve_requested_calendar_window("今天天津天气")
        assert win["forecast_start_date"] == "2026-07-10"
