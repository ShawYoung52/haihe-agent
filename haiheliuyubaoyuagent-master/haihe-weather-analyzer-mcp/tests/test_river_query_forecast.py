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
    (
        "请问明天泃河有雨吗？",
        "请问一下明天泃河有雨吗？",
        "明天下午泃河有雨吗？",
        "明天早上泃河有雨吗？",
        "泃河河道明天有雨吗？",
    ),
)
def test_extract_river_ignores_leading_query_modifiers_and_corridor_words(query):
    assert rqf.extract_river_target(query) == "泃河"


@pytest.mark.parametrize(
    "query",
    ("泃河河道明天有雨吗？", "明天泃河的河道有雨吗？", "明天泃河有雨吗？"),
)
def test_extract_river_treats_corridor_phrases_as_structural_suffixes(query):
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


class FakeCursor:
    def __init__(self, executed):
        self.executed = executed

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params):
        self.executed["sql"] = repr(statement)
        self.executed["params"] = params

    def fetchone(self):
        return {
            "matched_name": "泃河",
            "srid": 4326,
            "geom_wkb": b"valid-wkb",
        }


class FakeConnection:
    def __init__(self, executed):
        self.executed = executed

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self, **kwargs):
        return FakeCursor(self.executed)


class EmptyCursor(FakeCursor):
    def fetchone(self):
        return None


class EmptyConnection(FakeConnection):
    def __init__(self):
        super().__init__({})

    def cursor(self, **kwargs):
        return EmptyCursor(self.executed)


def test_corridor_query_uses_full_table_exact_match_and_5000_metre_buffer(monkeypatch):
    """Catches a regression to a non-full table, degree buffering, or fuzzy-only merge."""
    executed = {}
    monkeypatch.setattr(rqf.psycopg2, "connect", lambda **kwargs: FakeConnection(executed))
    monkeypatch.setattr(rqf, "_geometry_from_wkb", lambda value: object())

    corridor = rqf.load_river_corridor(
        "泃河",
        {"schema": "public", "river_table_full": "haihe_river_directed_full_v6"},
    )

    assert "haihe_river_directed_full_v6" in executed["sql"]
    assert "match_rank" in executed["sql"]
    assert "MIN(match_rank)" in executed["sql"]
    assert "ST_Buffer" in executed["sql"]
    assert "3857" in executed["sql"]
    assert executed["params"] == {
        "river_name": "泃河",
        "contains": "%泃河%",
        "source_srid": 4326,
        "buffer_m": 5000.0,
    }
    assert corridor.buffer_km == 5.0


def test_corridor_query_defaults_to_full_v6_table(monkeypatch):
    """Catches an outdated river-table default when the config omits its table name."""
    executed = {}
    monkeypatch.setattr(rqf.psycopg2, "connect", lambda **kwargs: FakeConnection(executed))
    monkeypatch.setattr(rqf, "_geometry_from_wkb", lambda value: object())

    rqf.load_river_corridor("泃河", {"schema": "public"})

    assert "haihe_river_directed_full_v6" in executed["sql"]


def test_missing_river_raises_a_distinct_not_found_error(monkeypatch):
    """Catches treating missing river geometry as a successful no-rain result."""
    monkeypatch.setattr(rqf.psycopg2, "connect", lambda **kwargs: EmptyConnection())

    with pytest.raises(rqf.RiverNotFoundError):
        rqf.load_river_corridor("不存在河", {"river_table_full": "haihe_river_directed_full_v6"})


def test_database_failure_raises_a_distinct_database_error(monkeypatch):
    """Catches database failures being misreported as a river-name miss."""
    def raise_connection_error(**kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(rqf.psycopg2, "connect", raise_connection_error)

    with pytest.raises(rqf.RiverDatabaseError, match="加载河道缓冲区失败"):
        rqf.load_river_corridor("泃河", {"river_table_full": "haihe_river_directed_full_v6"})
