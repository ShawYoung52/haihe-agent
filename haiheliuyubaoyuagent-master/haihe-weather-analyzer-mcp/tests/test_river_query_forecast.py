from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import river_query_forecast as rqf

TZ = ZoneInfo("Asia/Shanghai")


def test_extracts_river_after_leading_time_words():
    assert rqf.extract_river_target("明天泃河有雨吗？") == "泃河"
    assert rqf.extract_river_target("今天晚上滦河有雨吗？") == "滦河"


@pytest.mark.parametrize(
    "query",
    ("请问明天泃河有雨吗？", "明天下午泃河有雨吗？", "泃河河道明天有雨吗？"),
)
def test_extract_river_ignores_question_time_and_corridor_words(query):
    assert rqf.extract_river_target(query) == "泃河"


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


@pytest.mark.parametrize(
    ("day_text", "expected_days"),
    (("一", 1), ("两", 2), ("十", 10), ("十一", 11), ("十二", 12), ("二十", 20), ("二十一", 21), ("九十九", 99)),
)
def test_future_chinese_day_count_supports_one_to_ninety_nine(day_text, expected_days):
    periods = rqf.resolve_river_forecast_periods(
        f"未来{day_text}天泃河有雨吗？", datetime(2026, 8, 27, 8, 0, tzinfo=TZ)
    )
    assert len(periods) == expected_days
    assert periods[0].target_start.date().isoformat() == "2026-08-28"
    assert all(a.target_end == b.target_start for a, b in zip(periods, periods[1:]))


def test_invalid_future_day_count_raises_instead_of_falling_back_to_today():
    with pytest.raises(ValueError, match="无法解析未来天数"):
        rqf.resolve_river_forecast_periods(
            "未来十几天泃河有雨吗？", datetime(2026, 8, 27, 8, 0, tzinfo=TZ)
        )
