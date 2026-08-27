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
        self.executed["statement"] = statement
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
        self.close_calls = 0
        self.executed["connection"] = self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self, **kwargs):
        return FakeCursor(self.executed)

    def close(self):
        self.close_calls += 1


class EmptyCursor(FakeCursor):
    def fetchone(self):
        return None


class EmptyConnection(FakeConnection):
    def cursor(self, **kwargs):
        return EmptyCursor(self.executed)


class RaisingCursor(FakeCursor):
    def execute(self, statement, params):
        super().execute(statement, params)
        raise RuntimeError("query failed")


class RaisingConnection(FakeConnection):
    def cursor(self, **kwargs):
        return RaisingCursor(self.executed)


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
    assert "ST_SRID(geom) = 0" in executed["sql"]
    assert "::geography" in executed["sql"]
    assert "3857" not in executed["sql"]
    assert "), best AS (" in executed["sql"]
    assert "merged AS" in executed["sql"]
    assert "FROM best" in executed["sql"].split("merged AS", maxsplit=1)[1]
    assert executed["params"] == {
        "river_name": "泃河",
        "contains": "%泃河%",
        "source_srid": 4326,
        "buffer_m": 5000.0,
    }
    assert corridor.buffer_km == 5.0
    assert executed["connection"].close_calls == 1


def test_corridor_query_uses_composed_identifiers_and_keeps_river_name_out_of_sql(monkeypatch):
    """Catches unsafe identifier/value interpolation in the PostGIS query boundary."""
    executed = {}
    river_name = "泃河'%; DROP TABLE rivers; --"
    monkeypatch.setattr(rqf.psycopg2, "connect", lambda **kwargs: FakeConnection(executed))
    monkeypatch.setattr(rqf, "_geometry_from_wkb", lambda value: object())

    rqf.load_river_corridor(
        river_name,
        {"schema": "safe_schema", "river_table_full": "safe_river_table"},
    )

    statement = executed["statement"]
    assert isinstance(statement, rqf.sql.Composed)
    identifiers = [part._wrapped for part in statement._wrapped if isinstance(part, rqf.sql.Identifier)]
    assert identifiers == [("safe_schema",), ("safe_river_table",)]
    assert river_name not in executed["sql"]
    assert all(f"%({key})s" in executed["sql"] for key in ("river_name", "contains", "source_srid", "buffer_m"))
    assert executed["params"]["river_name"] == river_name
    assert executed["params"]["contains"] == f"%{river_name}%"


def test_corridor_query_defaults_to_full_v6_table(monkeypatch):
    """Catches an outdated river-table default when the config omits its table name."""
    executed = {}
    monkeypatch.setattr(rqf.psycopg2, "connect", lambda **kwargs: FakeConnection(executed))
    monkeypatch.setattr(rqf, "_geometry_from_wkb", lambda value: object())

    rqf.load_river_corridor("泃河", {"schema": "public"})

    assert "haihe_river_directed_full_v6" in executed["sql"]


def test_missing_river_raises_a_distinct_not_found_error(monkeypatch):
    """Catches treating missing river geometry as a successful no-rain result."""
    executed = {}
    connection = EmptyConnection(executed)
    monkeypatch.setattr(rqf.psycopg2, "connect", lambda **kwargs: connection)

    with pytest.raises(rqf.RiverNotFoundError):
        rqf.load_river_corridor("不存在河", {"river_table_full": "haihe_river_directed_full_v6"})

    assert connection.close_calls == 1


def test_empty_corridor_wkb_raises_not_found_and_closes_connection(monkeypatch):
    """Catches a null spatial result being treated as a valid corridor."""
    executed = {}
    connection = FakeConnection(executed)
    monkeypatch.setattr(rqf.psycopg2, "connect", lambda **kwargs: connection)
    monkeypatch.setattr(
        FakeCursor,
        "fetchone",
        lambda self: {"matched_name": "泃河", "srid": 4326, "geom_wkb": b""},
    )

    with pytest.raises(rqf.RiverNotFoundError):
        rqf.load_river_corridor("泃河", {"river_table_full": "haihe_river_directed_full_v6"})

    assert connection.close_calls == 1


def test_database_failure_raises_a_distinct_database_error(monkeypatch):
    """Catches database failures being misreported as a river-name miss."""
    def raise_connection_error(**kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(rqf.psycopg2, "connect", raise_connection_error)

    with pytest.raises(rqf.RiverDatabaseError, match="加载河道缓冲区失败"):
        rqf.load_river_corridor("泃河", {"river_table_full": "haihe_river_directed_full_v6"})


def test_query_error_closes_connection_before_raising_database_error(monkeypatch):
    """Catches a SQL error leaking its connection."""
    executed = {}
    connection = RaisingConnection(executed)
    monkeypatch.setattr(rqf.psycopg2, "connect", lambda **kwargs: connection)

    with pytest.raises(rqf.RiverDatabaseError, match="加载河道缓冲区失败"):
        rqf.load_river_corridor("泃河", {"river_table_full": "haihe_river_directed_full_v6"})

    assert connection.close_calls == 1


def test_geometry_error_closes_connection_before_raising_database_error(monkeypatch):
    """Catches a WKB parse error leaking the already-used database connection."""
    executed = {}
    connection = FakeConnection(executed)
    monkeypatch.setattr(rqf.psycopg2, "connect", lambda **kwargs: connection)
    monkeypatch.setattr(rqf, "_geometry_from_wkb", lambda value: (_ for _ in ()).throw(ValueError("bad wkb")))

    with pytest.raises(rqf.RiverDatabaseError, match="加载河道缓冲区失败"):
        rqf.load_river_corridor("泃河", {"river_table_full": "haihe_river_directed_full_v6"})

    assert connection.close_calls == 1


FIXED_NOW = datetime(2026, 8, 27, 15, 20, tzinfo=TZ)
TEST_CONFIG = {
    "postgres": {
        "schema": "public",
        "river_table_full": "haihe_river_directed_full_v6",
        "srid": "4326",
    }
}


def fake_juhe_corridor(*args, **kwargs):
    return rqf.RiverCorridor("泃河", "泃河", 4326, object(), 5.0)


def fake_stats(*args, **kwargs):
    return {
        "average_rainfall_mm": 2.4,
        "max_rainfall_mm": 8.1,
        "min_rainfall_mm": 0.0,
        "valid_count": 12,
    }


def test_juhe_uses_corridor_and_reports_scope(monkeypatch):
    """Catches a concrete river query being routed through a nine-zone aggregate."""
    monkeypatch.setattr(rqf, "load_river_corridor", fake_juhe_corridor)
    monkeypatch.setattr(rqf.rsf, "_resolve_forecast_file", lambda *a: ("rain.tif", "滚动预报网格"))
    monkeypatch.setattr(rqf.rsf, "_compute_rainfall_stats_for_geometry", fake_stats)

    result = rqf.query_river_rainfall_forecast_core(
        "明天泃河有雨吗？", TEST_CONFIG, now=FIXED_NOW
    )

    assert result["status"] == "ok"
    assert result["scope_type"] == "river_corridor"
    assert result["scope_description"] == "泃河河道两侧约5公里沿线范围"
    assert result["periods"][0]["has_rain"] is True


def test_bare_luanhe_uses_corridor_when_found(monkeypatch):
    """Catches bare 滦河 being pre-emptively broadened to the river-system scope."""
    calls = []
    monkeypatch.setattr(
        rqf,
        "load_river_corridor",
        lambda *a, **k: rqf.RiverCorridor("滦河", "滦河", 4326, object(), 5.0),
    )
    monkeypatch.setattr(rqf.rsf, "_resolve_forecast_file", lambda *a: ("rain.tif", "TEST"))
    monkeypatch.setattr(rqf.rsf, "_compute_rainfall_stats_for_geometry", fake_stats)
    monkeypatch.setattr(
        rqf.rsf,
        "get_river_system_rainfall_forecast",
        lambda **kwargs: calls.append(kwargs) or {"zones": []},
    )

    result = rqf.query_river_rainfall_forecast_core(
        "今天晚上滦河有雨吗？", TEST_CONFIG, now=FIXED_NOW
    )

    assert result["scope_type"] == "river_corridor"
    assert calls == []


def test_bare_luanhe_falls_back_to_existing_nine_zone_tool_only_when_not_found(monkeypatch):
    """Catches fallback to a broader scope on database errors or successful corridor matches."""
    calls = []
    monkeypatch.setattr(
        rqf,
        "load_river_corridor",
        lambda *a, **k: (_ for _ in ()).throw(rqf.RiverNotFoundError("not found")),
    )
    monkeypatch.setattr(rqf.rsf, "get_river_system_rainfall_forecast", lambda **kwargs: calls.append(kwargs) or {
        "data_source": "滚动预报网格",
        "zones": [{"zone_name": "滦河", "average_rainfall_mm": 1.0, "max_rainfall_mm": 4.0, "min_rainfall_mm": 0.0}],
    })

    result = rqf.query_river_rainfall_forecast_core(
        "今天晚上滦河有雨吗？", TEST_CONFIG, now=FIXED_NOW
    )

    assert result["scope_type"] == "river_system"
    assert calls[0]["river_system"] == "滦河"
    assert calls[0]["forecast_hours"] == 6


def test_explicit_river_system_uses_nine_zone_tool(monkeypatch):
    """Catches an explicit 河系/流域 request being narrowed to a river corridor."""
    calls = []
    monkeypatch.setattr(
        rqf,
        "load_river_corridor",
        lambda *a, **k: pytest.fail("explicit river-system request must not load a corridor"),
    )
    monkeypatch.setattr(rqf.rsf, "get_river_system_rainfall_forecast", lambda **kwargs: calls.append(kwargs) or {
        "data_source": "滚动预报网格",
        "zones": [{"zone_name": "滦河", "average_rainfall_mm": 0.0, "max_rainfall_mm": 0.0, "min_rainfall_mm": 0.0}],
    })

    result = rqf.query_river_rainfall_forecast_core(
        "明天滦河流域降雨", TEST_CONFIG, now=FIXED_NOW
    )

    assert result["scope_type"] == "river_system"
    assert calls[0]["river_system"] == "滦河"


def test_named_basin_request_uses_nine_zone_tool(monkeypatch):
    """Catches 海河流域 being treated as a bare river name because it is itself a known target."""
    calls = []
    monkeypatch.setattr(
        rqf,
        "load_river_corridor",
        lambda *a, **k: pytest.fail("a named basin request must not load a corridor"),
    )
    monkeypatch.setattr(rqf.rsf, "get_river_system_rainfall_forecast", lambda **kwargs: calls.append(kwargs) or {
        "data_source": "滚动预报网格",
        "zones": [{"zone_name": "海河", "average_rainfall_mm": 0.0, "max_rainfall_mm": 0.0, "min_rainfall_mm": 0.0}],
    })

    result = rqf.query_river_rainfall_forecast_core(
        "明天海河流域降雨", TEST_CONFIG, now=FIXED_NOW
    )

    assert result["scope_type"] == "river_system"
    assert calls[0]["river_system"] == "海河流域"


def test_three_days_are_computed_independently(monkeypatch):
    """Catches reuse of one daily raster window for every requested day."""
    resolved_starts = []
    monkeypatch.setattr(rqf, "load_river_corridor", fake_juhe_corridor)
    monkeypatch.setattr(
        rqf.rsf,
        "_resolve_forecast_file",
        lambda hours, start, path: resolved_starts.append(start) or ("rain.tif", "TEST"),
    )
    monkeypatch.setattr(rqf.rsf, "_compute_rainfall_stats_for_geometry", fake_stats)

    result = rqf.query_river_rainfall_forecast_core(
        "泃河未来三天降雨", TEST_CONFIG, now=FIXED_NOW
    )

    assert len(result["periods"]) == 3
    assert len(set(resolved_starts)) == 3


def test_no_coverage_is_not_reported_as_no_rain(monkeypatch):
    """Catches missing raster coverage being mislabeled as a dry period."""
    monkeypatch.setattr(rqf, "load_river_corridor", fake_juhe_corridor)
    monkeypatch.setattr(rqf.rsf, "_resolve_forecast_file", lambda *a: ("rain.tif", "TEST"))
    monkeypatch.setattr(
        rqf.rsf,
        "_compute_rainfall_stats_for_geometry",
        lambda *a, **k: {"average_rainfall_mm": None, "max_rainfall_mm": None, "min_rainfall_mm": None, "valid_count": 0},
    )

    result = rqf.query_river_rainfall_forecast_core(
        "明天泃河有雨吗？", TEST_CONFIG, now=FIXED_NOW
    )

    assert result["periods"][0]["status"] == "no_coverage"
    assert result["periods"][0]["has_rain"] is None


def test_database_error_does_not_fall_back_to_a_broader_scope(monkeypatch):
    """Catches a database outage silently changing a concrete river query to a river system."""
    monkeypatch.setattr(
        rqf,
        "load_river_corridor",
        lambda *a, **k: (_ for _ in ()).throw(rqf.RiverDatabaseError("database unavailable")),
    )
    monkeypatch.setattr(
        rqf.rsf,
        "get_river_system_rainfall_forecast",
        lambda **kwargs: pytest.fail("database errors must not trigger river-system fallback"),
    )

    result = rqf.query_river_rainfall_forecast_core(
        "明天滦河有雨吗？", TEST_CONFIG, now=FIXED_NOW
    )

    assert result["status"] == "database_error"


def test_missing_non_system_river_is_reported_as_not_found(monkeypatch):
    """Catches a missing concrete river being reported as a dry forecast."""
    monkeypatch.setattr(
        rqf,
        "load_river_corridor",
        lambda *a, **k: (_ for _ in ()).throw(rqf.RiverNotFoundError("not found")),
    )

    result = rqf.query_river_rainfall_forecast_core(
        "明天泃河有雨吗？", TEST_CONFIG, now=FIXED_NOW
    )

    assert result["status"] == "river_not_found"


def test_missing_forecast_file_is_reported_as_unavailable(monkeypatch):
    """Catches a missing forecast raster being represented as zero rainfall."""
    monkeypatch.setattr(rqf, "load_river_corridor", fake_juhe_corridor)
    monkeypatch.setattr(rqf.rsf, "_resolve_forecast_file", lambda *a: (None, "无可用预报文件"))

    result = rqf.query_river_rainfall_forecast_core(
        "明天泃河有雨吗？", TEST_CONFIG, now=FIXED_NOW
    )

    assert result["status"] == "forecast_unavailable"


def test_raster_statistics_failure_is_reported_as_calculation_error(monkeypatch):
    """Catches raster processing failures being represented as a dry forecast."""
    monkeypatch.setattr(rqf, "load_river_corridor", fake_juhe_corridor)
    monkeypatch.setattr(rqf.rsf, "_resolve_forecast_file", lambda *a: ("rain.tif", "TEST"))
    monkeypatch.setattr(
        rqf.rsf,
        "_compute_rainfall_stats_for_geometry",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("raster corrupt")),
    )

    result = rqf.query_river_rainfall_forecast_core(
        "明天泃河有雨吗？", TEST_CONFIG, now=FIXED_NOW
    )

    assert result["status"] == "calculation_error"


def test_generic_river_system_error_is_preserved_without_guessing_its_cause(monkeypatch):
    """Catches a generic helper error being falsely labeled as a database or raster failure."""
    monkeypatch.setattr(rqf.rsf, "get_river_system_rainfall_forecast", lambda **kwargs: {
        "error": "暂时无法获取河系预报数据，请稍后重试。"
    })

    result = rqf.query_river_rainfall_forecast_core(
        "明天滦河流域降雨", TEST_CONFIG, now=FIXED_NOW
    )

    assert result["status"] == "system_unavailable"
    assert result["error"] == "暂时无法获取河系预报数据，请稍后重试。"


def test_empty_river_system_zones_are_not_reported_as_no_rain(monkeypatch):
    """Catches an empty helper result being turned into a dry nine-zone forecast."""
    monkeypatch.setattr(rqf.rsf, "get_river_system_rainfall_forecast", lambda **kwargs: {
        "data_source": "滚动预报网格", "zones": []
    })

    result = rqf.query_river_rainfall_forecast_core(
        "明天滦河流域降雨", TEST_CONFIG, now=FIXED_NOW
    )

    assert result["status"] == "system_unavailable"


def _river_system_result(zones):
    return {"data_source": "滚动预报网格", "zones": zones}


def test_all_no_coverage_river_system_zones_are_not_reported_as_no_rain(monkeypatch):
    """Catches RainfallAnalyzer's valid_count=0 zero-value shape being treated as dry weather."""
    monkeypatch.setattr(rqf.rsf, "get_river_system_rainfall_forecast", lambda **kwargs: _river_system_result([
        {"zone_name": "滦河", "average_rainfall_mm": 0.0, "max_rainfall_mm": 0.0, "min_rainfall_mm": 0.0, "valid_count": 0},
        {"zone_name": "海河", "average_rainfall_mm": 0.0, "max_rainfall_mm": 0.0, "min_rainfall_mm": 0.0, "valid_count": 0},
    ]))

    result = rqf.query_river_rainfall_forecast_core(
        "明天滦河流域降雨", TEST_CONFIG, now=FIXED_NOW
    )

    period = result["periods"][0]
    assert period["status"] == "no_coverage"
    assert period["has_rain"] is None
    assert period["average_rainfall_mm"] is None


def test_zero_rain_with_an_uncovered_zone_is_not_reported_as_no_rain(monkeypatch):
    """Catches a covered dry zone masking a second zone with no valid raster pixels."""
    monkeypatch.setattr(rqf.rsf, "get_river_system_rainfall_forecast", lambda **kwargs: _river_system_result([
        {"zone_name": "滦河", "average_rainfall_mm": 0.0, "max_rainfall_mm": 0.0, "min_rainfall_mm": 0.0, "valid_count": 12},
        {"zone_name": "海河", "average_rainfall_mm": 0.0, "max_rainfall_mm": 0.0, "min_rainfall_mm": 0.0, "valid_count": 0},
    ]))

    result = rqf.query_river_rainfall_forecast_core(
        "明天滦河流域降雨", TEST_CONFIG, now=FIXED_NOW
    )

    period = result["periods"][0]
    assert period["status"] == "partial"
    assert period["has_rain"] is None
    assert period["average_rainfall_mm"] is None


def test_valid_rain_with_an_uncovered_zone_reports_rain_and_partial_coverage(monkeypatch):
    """Catches a confirmed rainy zone losing its rain indication when another zone lacks coverage."""
    monkeypatch.setattr(rqf.rsf, "get_river_system_rainfall_forecast", lambda **kwargs: _river_system_result([
        {"zone_name": "滦河", "average_rainfall_mm": 1.2, "max_rainfall_mm": 6.0, "min_rainfall_mm": 0.0, "valid_count": 12},
        {"zone_name": "海河", "average_rainfall_mm": 0.0, "max_rainfall_mm": 0.0, "min_rainfall_mm": 0.0, "valid_count": 0},
    ]))

    result = rqf.query_river_rainfall_forecast_core(
        "明天滦河流域降雨", TEST_CONFIG, now=FIXED_NOW
    )

    period = result["periods"][0]
    assert period["status"] == "partial"
    assert period["has_rain"] is True
    assert period["average_rainfall_mm"] is None


def test_all_missing_zone_valid_counts_are_unknown_coverage(monkeypatch):
    """Catches legacy zone payloads without valid_count being mislabeled as confirmed no coverage."""
    monkeypatch.setattr(rqf.rsf, "get_river_system_rainfall_forecast", lambda **kwargs: _river_system_result([
        {"zone_name": "滦河", "average_rainfall_mm": 0.0, "max_rainfall_mm": 0.0, "min_rainfall_mm": 0.0},
        {"zone_name": "海河", "average_rainfall_mm": 0.0, "max_rainfall_mm": 0.0, "min_rainfall_mm": 0.0},
    ]))

    result = rqf.query_river_rainfall_forecast_core(
        "明天滦河流域降雨", TEST_CONFIG, now=FIXED_NOW
    )

    period = result["periods"][0]
    assert period["status"] == "unknown_coverage"
    assert period["has_rain"] is None
    assert period["average_rainfall_mm"] is None


def test_illegal_zone_valid_count_is_unknown_coverage(monkeypatch):
    """Catches an invalid pixel count being mislabeled as confirmed no coverage."""
    monkeypatch.setattr(rqf.rsf, "get_river_system_rainfall_forecast", lambda **kwargs: _river_system_result([
        {"zone_name": "滦河", "average_rainfall_mm": 0.0, "max_rainfall_mm": 0.0, "min_rainfall_mm": 0.0, "valid_count": -1},
    ]))

    result = rqf.query_river_rainfall_forecast_core(
        "明天滦河流域降雨", TEST_CONFIG, now=FIXED_NOW
    )

    period = result["periods"][0]
    assert period["status"] == "unknown_coverage"
    assert period["has_rain"] is None


def test_missing_zone_statistic_is_unknown_coverage(monkeypatch):
    """Catches a positive pixel count with incomplete rainfall statistics being treated as coverage."""
    monkeypatch.setattr(rqf.rsf, "get_river_system_rainfall_forecast", lambda **kwargs: _river_system_result([
        {"zone_name": "滦河", "max_rainfall_mm": 0.0, "min_rainfall_mm": 0.0, "valid_count": 12},
    ]))

    result = rqf.query_river_rainfall_forecast_core(
        "明天滦河流域降雨", TEST_CONFIG, now=FIXED_NOW
    )

    period = result["periods"][0]
    assert period["status"] == "unknown_coverage"
    assert period["has_rain"] is None


def test_complete_river_system_coverage_uses_valid_count_weighted_average(monkeypatch):
    """Catches a plain mean of zone averages when zones contain different numbers of valid pixels."""
    monkeypatch.setattr(rqf.rsf, "get_river_system_rainfall_forecast", lambda **kwargs: _river_system_result([
        {"zone_name": "滦河", "average_rainfall_mm": 1.0, "max_rainfall_mm": 2.0, "min_rainfall_mm": 0.0, "valid_count": 10},
        {"zone_name": "海河", "average_rainfall_mm": 3.0, "max_rainfall_mm": 4.0, "min_rainfall_mm": 1.0, "valid_count": 30},
    ]))

    result = rqf.query_river_rainfall_forecast_core(
        "明天滦河流域降雨", TEST_CONFIG, now=FIXED_NOW
    )

    period = result["periods"][0]
    assert period["status"] == "ok"
    assert period["average_rainfall_mm"] == pytest.approx(2.5)
    assert period["valid_count"] == 40
