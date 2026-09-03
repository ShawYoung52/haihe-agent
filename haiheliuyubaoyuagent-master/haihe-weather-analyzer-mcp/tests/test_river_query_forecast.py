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


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("明天海河流域内泃河有雨吗？", "泃河"),
        ("明天大清河流域内拒马河有雨吗？", "拒马河"),
        ("明天海河流域中的大清河有雨吗？", "大清河"),
        ("明天海河流域的潮白河有雨吗？", "潮白河"),
        ("明天海河流域内泃河河道有雨吗？", "泃河"),
        ("明天海河流域中的大清河河道有雨吗？", "大清河"),
        ("明天海河流域中的黑龙港有雨吗？", "黑龙港"),
        ("明天全流域内黑龙港有雨吗？", "黑龙港"),
    ],
)
def test_extract_river_prefers_named_child_inside_parent_basin(query, expected):
    """同句出现上位流域和下一级河流时，应提取用户实际询问的下一级目标。"""
    assert rqf.extract_river_target(query) == expected


@pytest.mark.parametrize(
    "query",
    (
        "明天海河流域内的河流有雨吗？",
        "明天海河流域内的河道有雨吗？",
        "明天海河流域内哪些河有雨吗？",
        "明天海河流域内各河有雨吗？",
        "明天海河流域内主要河流有雨吗？",
        "明天海河流域内中小河流有雨吗？",
        "明天海河流域内每条河有雨吗？",
        "明天海河流域内哪几条河有雨吗？",
        "明天海河流域内这些河有雨吗？",
        "明天海河流域内各条河有雨吗？",
    ),
)
def test_extract_river_ignores_generic_river_word_inside_parent_basin(query):
    """“流域内的河流”没有点名下一级河流，应保持上位流域范围。"""
    assert rqf.extract_river_target(query) == "海河流域"


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


def test_future_days_labels_are_friendly_dates_not_future_day_n():
    """2026-09-01 用户口径：河流预报时段标签要像"天气怎么样"那样用 明天/后天/具体日期，
    不得用"未来第N天"；且明天/后天要带具体日期（明天（9月2日）），避免与绝对日期混排时歧义。"""
    now = datetime(2026, 9, 1, 8, 0, tzinfo=TZ)  # 2026-09-01
    periods = rqf.resolve_river_forecast_periods("未来三天泃河有雨吗？", now)
    assert [p.label for p in periods] == ["明天（9月2日）", "后天（9月3日）", "9月4日"]
    assert all("未来第" not in p.label for p in periods)

    # 未来 2 天 → 明天（9月2日）/后天（9月3日）
    two = rqf.resolve_river_forecast_periods("未来两天泃河有雨吗？", now)
    assert [p.label for p in two] == ["明天（9月2日）", "后天（9月3日）"]

    # 未来 5 天 → 明天（9月2日）/后天（9月3日）/9月4日/9月5日/9月6日
    five = rqf.resolve_river_forecast_periods("未来五天泃河有雨吗？", now)
    assert [p.label for p in five] == [
        "明天（9月2日）", "后天（9月3日）", "9月4日", "9月5日", "9月6日",
    ]


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


@pytest.mark.parametrize("query", [
    "海河流域未来一周天气", "海河流域一周天气", "海河流域周末天气",
    "海河未来一周降雨如何", "海河今天起3天有雨吗", "海河大后天有雨吗",
    "海河今天下午有雨吗", "海河明天晚上有雨吗", "海河8月30日有雨吗",
    "海河今天和明天有雨吗", "海河未来三天下午有雨吗", "卫河未来24小时降雨如何",
])
def test_unsupported_time_window_is_invalid_without_querying_any_forecast(monkeypatch, query):
    def unexpected_query(*args, **kwargs):
        pytest.fail("unsupported time must not query a substitute forecast window")

    monkeypatch.setattr(rqf, "load_river_corridor", unexpected_query)
    monkeypatch.setattr(rqf.rsf, "get_river_system_rainfall_forecast", unexpected_query)

    result = rqf.query_river_rainfall_forecast_core(query, {}, now=datetime(2026, 8, 27, 9, tzinfo=TZ))

    assert result["status"] == "invalid_request"
    assert result["periods"] == []


@pytest.mark.parametrize(("query", "start", "end", "count"), [
    ("海河今天有雨吗", "2026-08-27T00:00:00+08:00", "2026-08-28T00:00:00+08:00", 1),
    ("海河明天有雨吗", "2026-08-28T00:00:00+08:00", "2026-08-29T00:00:00+08:00", 1),
    ("海河后天有雨吗", "2026-08-29T00:00:00+08:00", "2026-08-30T00:00:00+08:00", 1),
    ("海河今晚有雨吗", "2026-08-27T18:00:00+08:00", "2026-08-28T00:00:00+08:00", 1),
    ("海河未来3天有雨吗", "2026-08-28T00:00:00+08:00", "2026-08-31T00:00:00+08:00", 3),
])
def test_supported_windows_keep_their_exact_dates(query, start, end, count):
    periods = rqf.resolve_river_forecast_periods(query, datetime(2026, 8, 27, 9, tzinfo=TZ))
    assert len(periods) == count
    assert periods[0].target_start.isoformat() == start
    assert periods[-1].target_end.isoformat() == end


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
    monkeypatch.setattr(rqf.rsf, "_resolve_forecast_file", lambda *a, **k: ("rain.tif", "滚动预报网格"))
    monkeypatch.setattr(rqf.rsf, "_compute_rainfall_stats_for_geometry", fake_stats)

    result = rqf.query_river_rainfall_forecast_core(
        "明天泃河有雨吗？", TEST_CONFIG, now=FIXED_NOW
    )

    assert result["status"] == "ok"
    assert result["scope_type"] == "river_corridor"
    assert result["scope_description"] == "泃河河道两侧约5公里沿线范围"
    assert result["periods"][0]["has_rain"] is True


def test_child_river_inside_parent_basin_still_uses_corridor(monkeypatch):
    """“海河流域内泃河”必须使用泃河5公里走廊，不能被上位流域抢占。"""
    monkeypatch.setattr(rqf, "load_river_corridor", fake_juhe_corridor)
    monkeypatch.setattr(rqf.rsf, "_resolve_forecast_file", lambda *a, **k: ("rain.tif", "滚动预报网格"))
    monkeypatch.setattr(rqf.rsf, "_compute_rainfall_stats_for_geometry", fake_stats)
    monkeypatch.setattr(
        rqf.rsf,
        "get_river_system_rainfall_forecast",
        lambda **kwargs: pytest.fail("the parent basin must not pre-empt its named child river"),
    )

    result = rqf.query_river_rainfall_forecast_core(
        "明天海河流域内泃河有雨吗？", TEST_CONFIG, now=FIXED_NOW
    )

    assert result["scope_type"] == "river_corridor"
    assert result["river_name"] == "泃河"
    assert result["scope_description"] == "泃河河道两侧约5公里沿线范围"


# —— 2026-09-01 用户口径：河流预报答案要像"天气怎么样"那样带灾害风险（走廊代表点查隐患+风险等级）——


def _fake_rfs(hazards=None, raise_exc=False, captured=None):
    """伪造 rolling_forecast_service，供走廊风险附着测试（免触发真实隐患/风险接口）。"""

    class _Fake:
        @staticmethod
        def _risk_fcst_times_from_window(window, now=None):
            if captured is not None:
                captured["window"] = window
                captured["now"] = now
            return ["SENTINEL_FCST"]

        @staticmethod
        def _query_region_hazards(lon, lat, fcst_times):
            if captured is not None:
                captured["lon"], captured["lat"], captured["fcst"] = lon, lat, fcst_times
            if raise_exc:
                raise RuntimeError("hazard iface down")
            return hazards

    return _Fake


def _stub_corridor_path(monkeypatch):
    monkeypatch.setattr(rqf, "load_river_corridor", fake_juhe_corridor)
    monkeypatch.setattr(rqf.rsf, "_resolve_forecast_file", lambda *a, **k: ("rain.tif", "滚动预报网格"))
    monkeypatch.setattr(rqf.rsf, "_compute_rainfall_stats_for_geometry", fake_stats)


def test_corridor_attaches_region_hazards(monkeypatch):
    captured = {}
    hazards = {
        "total_found": 3,
        "radius_km": 25.0,
        "categories": [{"key": "zxhl", "label": "中小河流", "kind": "river", "count": 3}],
        "hazards_available": True,
        "risk_levels": {},
        "risk_levels_available": True,
    }
    _stub_corridor_path(monkeypatch)
    monkeypatch.setattr(rqf, "_corridor_representative_point", lambda c: (116.5, 40.2))
    monkeypatch.setattr(
        rqf, "_load_rolling_forecast_service", lambda: _fake_rfs(hazards, captured=captured)
    )

    result = rqf.query_river_rainfall_forecast_core("未来三天泃河有雨吗？", TEST_CONFIG, now=FIXED_NOW)

    assert result["status"] == "ok"
    entry = result["region_hazards"][0]
    assert entry["region"] == "泃河"
    assert entry["region_display"] == "泃河沿线"
    assert entry["categories"][0]["count"] == 3
    # 代表点与逐日起报换算结果被透传给隐患查询
    assert captured["lon"] == 116.5 and captured["lat"] == 40.2
    assert captured["fcst"] == ["SENTINEL_FCST"]
    # 日历窗口由时段换算：起日 = 第一时段日期、天数 = 时段数
    assert captured["window"]["forecast_start_date"] == "2026-08-28"
    assert captured["window"]["forecast_days"] == 3
    assert captured["now"] is FIXED_NOW


def test_corridor_hazard_failure_degrades_silently(monkeypatch):
    """隐患/风险接口失败绝不阻断降雨回答——静默降级，不附 region_hazards。"""
    _stub_corridor_path(monkeypatch)
    monkeypatch.setattr(rqf, "_corridor_representative_point", lambda c: (116.5, 40.2))
    monkeypatch.setattr(rqf, "_load_rolling_forecast_service", lambda: _fake_rfs(raise_exc=True))

    result = rqf.query_river_rainfall_forecast_core("明天泃河有雨吗？", TEST_CONFIG, now=FIXED_NOW)

    assert result["status"] == "ok"
    assert "region_hazards" not in result


def test_corridor_empty_hazards_not_attached(monkeypatch):
    _stub_corridor_path(monkeypatch)
    monkeypatch.setattr(rqf, "_corridor_representative_point", lambda c: (116.5, 40.2))
    monkeypatch.setattr(rqf, "_load_rolling_forecast_service", lambda: _fake_rfs({}))

    result = rqf.query_river_rainfall_forecast_core("明天泃河有雨吗？", TEST_CONFIG, now=FIXED_NOW)

    assert "region_hazards" not in result


def test_corridor_representative_point_handles_dummy_geometry():
    """测试环境无 osgeo，dummy 几何（object()）触发异常应优雅返回 None。"""
    corridor = rqf.RiverCorridor("泃河", "泃河", 4326, object(), 5.0)
    assert rqf._corridor_representative_point(corridor) is None


def _stub_system_path(monkeypatch, zone_name="滦河"):
    """打桩九分区降雨主链路（显式河系/支流回退），返回指定分区的模拟降雨。"""
    monkeypatch.setattr(rqf.rsf, "get_river_system_rainfall_forecast", lambda **kwargs: {
        "data_source": "滚动预报网格",
        "zones": [{"zone_name": zone_name, "average_rainfall_mm": 1.0, "max_rainfall_mm": 4.0, "min_rainfall_mm": 0.0}],
    })


class _FakePoint:
    def __init__(self, x, y):
        self._x, self._y = x, y

    def IsEmpty(self):
        return False

    def GetX(self):
        return self._x

    def GetY(self):
        return self._y


class _FakeGeometry:
    def __init__(self, x, y):
        self._point = _FakePoint(x, y)

    def Centroid(self):
        return self._point


def test_system_attaches_region_hazards(monkeypatch):
    """九分区路径（显式河系问法）附着【分区】灾害风险——2026-09-01 用户口径"九分区也得做"。"""
    captured = {}
    hazards = {
        "total_found": 2,
        "radius_km": 25.0,
        "categories": [{"key": "dzzh", "label": "地质灾害", "kind": "geologic", "count": 2}],
        "hazards_available": True,
        "risk_levels": {},
        "risk_levels_available": True,
    }
    _stub_system_path(monkeypatch)
    monkeypatch.setattr(rqf, "_zone_representative_point", lambda target, config: (118.5, 39.8))
    monkeypatch.setattr(
        rqf, "_load_rolling_forecast_service", lambda: _fake_rfs(hazards, captured=captured)
    )

    result = rqf.query_river_rainfall_forecast_core("未来三天滦河流域降雨", TEST_CONFIG, now=FIXED_NOW)

    assert result["status"] == "ok"
    assert result["scope_type"] == "river_system"
    entry = result["region_hazards"][0]
    assert entry["region"] == "滦河"
    assert entry["region_display"] == "滦河九分区河系"
    assert entry["categories"][0]["count"] == 2
    # 代表点与逐日起报换算结果被透传给隐患查询
    assert captured["lon"] == 118.5 and captured["lat"] == 39.8
    assert captured["fcst"] == ["SENTINEL_FCST"]
    assert captured["window"]["forecast_start_date"] == "2026-08-28"
    assert captured["window"]["forecast_days"] == 3
    assert captured["now"] is FIXED_NOW


def test_tributary_fallback_attaches_zone_hazards(monkeypatch):
    """支流回退九分区（泃河→北三河）时风险附着用所属分区，display 按分区口径。"""
    captured = {}
    hazards = {
        "total_found": 1,
        "radius_km": 25.0,
        "categories": [{"key": "sh", "label": "山洪", "kind": "mountain", "count": 1}],
        "hazards_available": True,
        "risk_levels": {},
        "risk_levels_available": True,
    }
    monkeypatch.setattr(
        rqf,
        "load_river_corridor",
        lambda *a, **k: (_ for _ in ()).throw(rqf.RiverNotFoundError("not found")),
    )
    _stub_system_path(monkeypatch, zone_name="北三河")
    monkeypatch.setattr(rqf, "_zone_representative_point", lambda target, config: (117.0, 40.0))
    monkeypatch.setattr(
        rqf, "_load_rolling_forecast_service", lambda: _fake_rfs(hazards, captured=captured)
    )

    result = rqf.query_river_rainfall_forecast_core("明天泃河有雨吗？", TEST_CONFIG, now=FIXED_NOW)

    assert result["status"] == "ok"
    assert result["scope_type"] == "river_system"
    assert result["river_name"] == "泃河"
    entry = result["region_hazards"][0]
    assert entry["region"] == "北三河"
    assert entry["region_display"] == "北三河九分区河系"
    assert captured["lon"] == 117.0 and captured["lat"] == 40.0


def test_system_hazard_failure_degrades_silently(monkeypatch):
    """隐患/风险接口失败绝不阻断九分区降雨回答——静默降级，不附 region_hazards。"""
    _stub_system_path(monkeypatch)
    monkeypatch.setattr(rqf, "_zone_representative_point", lambda target, config: (118.5, 39.8))
    monkeypatch.setattr(rqf, "_load_rolling_forecast_service", lambda: _fake_rfs(raise_exc=True))

    result = rqf.query_river_rainfall_forecast_core("明天滦河流域降雨", TEST_CONFIG, now=FIXED_NOW)

    assert result["status"] == "ok"
    assert "region_hazards" not in result


def test_system_no_representative_point_skips_attach(monkeypatch):
    """分区边界不可用（无代表点）时静默跳过，且不触发隐患接口调用。"""
    called = []
    _stub_system_path(monkeypatch)
    monkeypatch.setattr(rqf, "_zone_representative_point", lambda target, config: None)
    monkeypatch.setattr(
        rqf, "_load_rolling_forecast_service", lambda: called.append(1) or _fake_rfs({})
    )

    result = rqf.query_river_rainfall_forecast_core("明天滦河流域降雨", TEST_CONFIG, now=FIXED_NOW)

    assert result["status"] == "ok"
    assert "region_hazards" not in result
    assert not called


def test_zone_representative_point_from_boundary_centroid(monkeypatch):
    """代表点 = 九分区边界几何质心（经 rsf._load_zone_boundaries_from_db 加载）。"""
    seen = {}

    def fake_load(zone_type, zone_name, config):
        seen["args"] = (zone_type, zone_name, config)
        return [{"zone_name": zone_name, "geometry": _FakeGeometry(118.1, 39.6)}]

    monkeypatch.setattr(rqf.rsf, "_load_zone_boundaries_from_db", fake_load)

    assert rqf._zone_representative_point("滦河", TEST_CONFIG) == (118.1, 39.6)
    assert seen["args"] == ("9", "滦河", TEST_CONFIG)


def test_zone_representative_point_handles_load_failure_and_dummy_geometry(monkeypatch):
    """边界加载失败 / dummy 几何（测试环境无 osgeo）都优雅返回 None。"""
    monkeypatch.setattr(
        rqf.rsf,
        "_load_zone_boundaries_from_db",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    assert rqf._zone_representative_point("滦河", TEST_CONFIG) is None

    monkeypatch.setattr(
        rqf.rsf,
        "_load_zone_boundaries_from_db",
        lambda *a, **k: [{"zone_name": "滦河", "geometry": object()}],
    )
    assert rqf._zone_representative_point("滦河", TEST_CONFIG) is None


def test_tributary_corridor_miss_falls_back_to_parent_zone(monkeypatch):
    """泃河河道走廊未命中时回退所属北三河九分区（领导问题清单标黄："明天泃河有雨吗"）。

    泃河不在 KNOWN_RIVER_SYSTEMS，但属于北三河水系支流；走廊未命中时按所属分区回答，
    结果保留用户所问河名（river_name=泃河）并注明统计口径。
    """
    calls = []
    monkeypatch.setattr(
        rqf,
        "load_river_corridor",
        lambda *a, **k: (_ for _ in ()).throw(rqf.RiverNotFoundError("not found")),
    )
    monkeypatch.setattr(rqf.rsf, "get_river_system_rainfall_forecast", lambda **kwargs: calls.append(kwargs) or {
        "data_source": "滚动预报网格",
        "zones": [{"zone_name": "北三河", "average_rainfall_mm": 1.0, "max_rainfall_mm": 4.0, "min_rainfall_mm": 0.0}],
    })

    result = rqf.query_river_rainfall_forecast_core(
        "明天泃河有雨吗？", TEST_CONFIG, now=FIXED_NOW
    )

    assert result["scope_type"] == "river_system"
    assert calls[0]["river_system"] == "北三河"
    assert result["river_name"] == "泃河"
    assert "北三河" in result["scope_description"]


def test_bare_luanhe_uses_nine_zone_tool_and_preserves_tonight_window(monkeypatch):
    """九分区裸名称直接按分区统计，同时保留“今晚”实际小时窗口。"""
    calls = []
    monkeypatch.setattr(
        rqf,
        "load_river_corridor",
        lambda *a, **k: pytest.fail("a nine-zone name must not load a river corridor"),
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


@pytest.mark.parametrize(
    "river_system",
    (
        "大清河",
        "子牙河",
        "永定河",
        "北三河",
        "漳卫南运河",
        "徒骇马颊河",
        "黑龙港",
        "滦河",
        "海河",
    ),
)
def test_bare_nine_zone_name_always_uses_nine_zone_scope(monkeypatch, river_system):
    """九分区裸名称不能因 full_v6 是否存在同名河道而随机切换到 5 公里走廊。"""
    calls = []
    monkeypatch.setattr(
        rqf,
        "load_river_corridor",
        lambda *a, **k: pytest.fail("a nine-zone name must not load a river corridor"),
    )
    monkeypatch.setattr(rqf, "_attach_system_region_hazards", lambda *a, **k: None)
    monkeypatch.setattr(
        rqf.rsf,
        "get_river_system_rainfall_forecast",
        lambda **kwargs: calls.append(kwargs) or {
            "data_source": "滚动预报网格",
            "zones": [{
                "zone_name": river_system,
                "valid_count": 3,
                "average_rainfall_mm": 0.0,
                "max_rainfall_mm": 0.0,
                "min_rainfall_mm": 0.0,
            }],
        },
    )

    result = rqf.query_river_rainfall_forecast_core(
        f"明天{river_system}有雨吗？", TEST_CONFIG, now=FIXED_NOW
    )

    assert result["scope_type"] == "river_system"
    assert result["scope_description"] == f"{river_system}九分区河系范围"
    assert calls[0]["river_system"] == river_system


@pytest.mark.parametrize(
    ("query", "expected_start"),
    [
        ("明天海河流域降雨", "2026-08-28 00:00:00"),
        ("今天海河流域天气怎么样", "2026-08-27 00:00:00"),
    ],
)
def test_named_basin_request_uses_nine_zone_tool(monkeypatch, query, expected_start):
    """Catches 海河流域 being treated as a bare river name because it is itself a known target."""
    calls = []
    monkeypatch.setattr(
        rqf,
        "load_river_corridor",
        lambda *a, **k: pytest.fail("a named basin request must not load a corridor"),
    )
    monkeypatch.setattr(rqf.rsf, "get_river_system_rainfall_forecast", lambda **kwargs: calls.append(kwargs) or {
        "data_source": "滚动预报网格",
        "zones": [{"zone_name": "海河", "valid_count": 3, "average_rainfall_mm": 0.0, "max_rainfall_mm": 0.0, "min_rainfall_mm": 0.0}],
    })

    result = rqf.query_river_rainfall_forecast_core(
        query, TEST_CONFIG, now=FIXED_NOW
    )

    assert result["scope_type"] == "river_system"
    assert calls == [{
        "river_system": "海河流域",
        "start_time": expected_start,
        "forecast_hours": 24,
        "zone_type": "9",
        "config": TEST_CONFIG,
        "ec_output_path": "",
        "require_full_window": True,
    }]
    assert result["periods"][0]["data_source"] == "滚动预报网格"
    assert result["periods"][0]["status"] == "ok"
    assert result["periods"][0]["has_rain"] is False


@pytest.mark.parametrize(
    ("query", "expected_target", "expected_scope"),
    [
        ("明天海河干流有雨吗？", "海河", "海河九分区河系范围"),
        ("明天全流域有雨吗？", "全流域", "全流域九分区河系范围"),
    ],
)
def test_haihe_mainstream_and_whole_basin_keep_distinct_nine_zone_scopes(
    monkeypatch, query, expected_target, expected_scope
):
    """海河干流只查海河分区；全流域走全部九分区，二者不可混淆。"""
    calls = []
    monkeypatch.setattr(
        rqf,
        "load_river_corridor",
        lambda *a, **k: pytest.fail("a named nine-zone scope must not load a corridor"),
    )
    monkeypatch.setattr(rqf, "_attach_system_region_hazards", lambda *a, **k: None)
    monkeypatch.setattr(
        rqf.rsf,
        "get_river_system_rainfall_forecast",
        lambda **kwargs: calls.append(kwargs) or {
            "data_source": "滚动预报网格",
            "zones": [{
                "zone_name": "海河" if expected_target == "海河" else "大清河",
                "valid_count": 3,
                "average_rainfall_mm": 0.0,
                "max_rainfall_mm": 0.0,
                "min_rainfall_mm": 0.0,
            }],
        },
    )

    result = rqf.query_river_rainfall_forecast_core(query, TEST_CONFIG, now=FIXED_NOW)

    assert result["scope_type"] == "river_system"
    assert result["scope_description"] == expected_scope
    assert calls[0]["river_system"] == expected_target


def test_three_days_are_computed_independently(monkeypatch):
    """Catches reuse of one daily raster window for every requested day."""
    resolved_starts = []
    monkeypatch.setattr(rqf, "load_river_corridor", fake_juhe_corridor)
    monkeypatch.setattr(
        rqf.rsf,
        "_resolve_forecast_file",
        lambda hours, start, path, **kwargs: resolved_starts.append(start) or ("rain.tif", "TEST"),
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
    monkeypatch.setattr(rqf.rsf, "_resolve_forecast_file", lambda *a, **k: ("rain.tif", "TEST"))
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
        "明天泃河有雨吗？", TEST_CONFIG, now=FIXED_NOW
    )

    assert result["status"] == "database_error"


def test_missing_non_system_river_is_reported_as_not_found(monkeypatch):
    """Catches a missing concrete river being reported as a dry forecast."""
    monkeypatch.setattr(
        rqf,
        "load_river_corridor",
        lambda *a, **k: (_ for _ in ()).throw(rqf.RiverNotFoundError("not found")),
    )

    # 既非九分区也非已知支流（泃河等支流会回退所属分区，见
    # test_tributary_corridor_miss_falls_back_to_parent_zone）才真正 river_not_found。
    result = rqf.query_river_rainfall_forecast_core(
        "明天某某河有雨吗？", TEST_CONFIG, now=FIXED_NOW
    )

    assert result["status"] == "river_not_found"


def test_missing_forecast_file_is_reported_as_unavailable(monkeypatch):
    """Catches a missing forecast raster being represented as zero rainfall."""
    monkeypatch.setattr(rqf, "load_river_corridor", fake_juhe_corridor)
    monkeypatch.setattr(rqf.rsf, "_resolve_forecast_file", lambda *a, **k: (None, "无可用预报文件"))

    result = rqf.query_river_rainfall_forecast_core(
        "明天泃河有雨吗？", TEST_CONFIG, now=FIXED_NOW
    )

    assert result["status"] == "forecast_unavailable"


def test_raster_statistics_failure_is_reported_as_calculation_error(monkeypatch):
    """Catches raster processing failures being represented as a dry forecast."""
    monkeypatch.setattr(rqf, "load_river_corridor", fake_juhe_corridor)
    monkeypatch.setattr(rqf.rsf, "_resolve_forecast_file", lambda *a, **k: ("rain.tif", "TEST"))
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
