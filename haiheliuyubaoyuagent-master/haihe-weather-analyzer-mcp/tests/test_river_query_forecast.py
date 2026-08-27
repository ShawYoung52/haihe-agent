from datetime import datetime
from zoneinfo import ZoneInfo

import river_query_forecast as rqf

TZ = ZoneInfo("Asia/Shanghai")


def test_extracts_river_after_leading_time_words():
    assert rqf.extract_river_target("明天泃河有雨吗？") == "泃河"
    assert rqf.extract_river_target("今天晚上滦河有雨吗？") == "滦河"


def test_tomorrow_is_a_natural_day():
    periods = rqf.resolve_river_forecast_periods(
        "明天泃河有雨吗？", datetime(2026, 8, 27, 10, 15, tzinfo=TZ)
    )
    assert [(p.target_start.hour, p.target_end.hour) for p in periods] == [(0, 0)]
    assert periods[0].target_start.date().isoformat() == "2026-08-28"
    assert periods[0].target_end.date().isoformat() == "2026-08-29"


def test_tonight_starts_at_18_before_evening_and_current_hour_after_18():
    before = rqf.resolve_river_forecast_periods(
        "今天晚上滦河有雨吗？", datetime(2026, 8, 27, 15, 20, tzinfo=TZ)
    )[0]
    after = rqf.resolve_river_forecast_periods(
        "今天晚上滦河有雨吗？", datetime(2026, 8, 27, 20, 35, tzinfo=TZ)
    )[0]
    assert before.target_start.hour == 18
    assert after.target_start.hour == 20
    assert before.target_end.date().isoformat() == "2026-08-28"


def test_future_three_days_returns_three_non_overlapping_periods():
    periods = rqf.resolve_river_forecast_periods(
        "泃河未来三天降雨", datetime(2026, 8, 27, 8, 0, tzinfo=TZ)
    )
    assert len(periods) == 3
    assert all(a.target_end == b.target_start for a, b in zip(periods, periods[1:]))
