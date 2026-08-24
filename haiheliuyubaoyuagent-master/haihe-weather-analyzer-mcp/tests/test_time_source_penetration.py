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


class TestWeekdayQueryWindow:
    """星期问法（下周一/本周五/周三）→ 单日日历窗口（2026-08-24 生产缺陷修复）。

    背景：2026-08-24（周一）问"下周一津泰达实验学校附近天气怎么样"，
    resolve_requested_calendar_window 原本不识别星期问法 → 返回 None → 走默认
    240h/12h 全量窗口，决策天气截断 12 条后目标日 8/31 不在其中 → 表格铺开
    8/24~8/29、LLM 还把"下周一"错锚到 8/25。星期语义与决策层
    _decision_target_dates 同口径：下周X=下一自然周星期X；周X（无"下"）=
    本周星期X、已过取下周。
    """

    NOW_MON = rfs.datetime(2026, 8, 24, 10, 0, tzinfo=rfs.TIANJIN_TIMEZONE)  # 周一
    NOW_THU = rfs.datetime(2026, 8, 27, 10, 0, tzinfo=rfs.TIANJIN_TIMEZONE)  # 周四

    def test_next_monday_from_monday(self):
        # 周一问"下周一" → 下一自然周周一 = 8/31（不是明天 8/25！）
        win = rfs.resolve_requested_calendar_window("下周一津泰达实验学校附近天气怎么样", now=self.NOW_MON)
        assert win is not None
        assert win["forecast_start_date"] == "2026-08-31"
        assert win["forecast_days"] == 1

    def test_next_tuesday_and_wednesday(self):
        win = rfs.resolve_requested_calendar_window("下周二天气怎么样", now=self.NOW_MON)
        assert win["forecast_start_date"] == "2026-09-01"
        win = rfs.resolve_requested_calendar_window("下星期三天气", now=self.NOW_MON)
        assert win["forecast_start_date"] == "2026-09-02"

    def test_this_week_friday(self):
        win = rfs.resolve_requested_calendar_window("本周五天气怎么样", now=self.NOW_MON)
        assert win["forecast_start_date"] == "2026-08-28"
        win = rfs.resolve_requested_calendar_window("周五天气怎么样", now=self.NOW_MON)
        assert win["forecast_start_date"] == "2026-08-28"

    def test_today_weekday_is_today(self):
        # 周一问"周一" → 今天
        win = rfs.resolve_requested_calendar_window("周一天气怎么样", now=self.NOW_MON)
        assert win["forecast_start_date"] == "2026-08-24"

    def test_past_weekday_rolls_to_next_week(self):
        # 周四问"周二" → 本周周二已过 → 下周二 9/1
        win = rfs.resolve_requested_calendar_window("周二天气怎么样", now=self.NOW_THU)
        assert win["forecast_start_date"] == "2026-09-01"

    def test_sunday_this_week(self):
        # 周一问"周日/周天" → 本周日 8/30（未过）
        win = rfs.resolve_requested_calendar_window("周日天气怎么样", now=self.NOW_MON)
        assert win["forecast_start_date"] == "2026-08-30"
        win = rfs.resolve_requested_calendar_window("星期天天气怎么样", now=self.NOW_MON)
        assert win["forecast_start_date"] == "2026-08-30"

    def test_next_sunday_out_of_range_raises(self):
        # 周一问"下周日" = 9/6（13 天后，超 240h 时效）→ ValueError 由 core 转 out_of_range
        with pytest.raises(ValueError, match="240"):
            rfs.resolve_requested_calendar_window("下周日天气怎么样", now=self.NOW_MON)

    def test_weekend_not_stolen_by_weekday_rule(self):
        # "下周末"仍走周末窗口（周末=周六+周日两天），不被星期规则截走
        win = rfs.resolve_requested_calendar_window("本周末适合去公园吗", now=self.NOW_MON)
        assert win["forecast_start_date"] == "2026-08-29"
        assert win["forecast_days"] == 2

    def test_explicit_date_beats_weekday(self):
        # 明确日期优先于星期（"8月25日周一"按 8/25 答，防日期与星期冲突时错锚）
        win = rfs.resolve_requested_calendar_window("8月25日周一天气怎么样", now=self.NOW_MON)
        assert win["forecast_start_date"] == "2026-08-25"

    def test_no_weekday_returns_none(self):
        assert rfs.resolve_requested_calendar_window("天气怎么样", now=self.NOW_MON) is None


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
