"""Tests for rolling_forecast_response region activity mountain reminder."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chainlitexam.tests.stubs import ensure_stubs

ensure_stubs()

from chainlitexam.tools import rolling_forecast_response as rfr


def _daily(weather, rain):
    return [{"date_label": "8月20日", "weather": weather, "rainfall_max_24h_mm": rain, "EDA": "南风1-2级"}]


class TestMountainActivityReminder:
    def test_rain_mountain_query_gets_risk_reminder(self):
        bundle = rfr.build_rolling_forecast_bundle(
            "明天适合去蓟州游玩吗", {"daily_summary": _daily("阴转小雨", 5.0)}
        )
        section = bundle["code_section"]
        assert "⚠ 注意事项" in section
        assert "不建议登山、溯溪、野外徒步" in section
        assert "山洪、落石隐患" in section
        assert "防滑鞋" in section

    def test_no_rain_mountain_query_gets_light_reminder(self):
        bundle = rfr.build_rolling_forecast_bundle(
            "明天适合去蓟州游玩吗", {"daily_summary": _daily("多云转阴有轻雾", 0.0)}
        )
        section = bundle["code_section"]
        assert "量力而行" in section
        assert "山洪" not in section  # 无雨不硬塞山洪警告（不死板）

    def test_non_mountain_activity_no_reminder(self):
        bundle = rfr.build_rolling_forecast_bundle(
            "明天适合去水上公园玩吗", {"daily_summary": _daily("阴转小雨", 5.0)}
        )
        assert "注意事项" not in bundle["code_section"]

    def test_panshan_query_also_mountain(self):
        bundle = rfr.build_rolling_forecast_bundle(
            "明天去盘山游玩合适吗", {"daily_summary": _daily("晴", 0.0)}
        )
        assert "⚠ 注意事项" in bundle["code_section"]
