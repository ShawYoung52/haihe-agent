"""滚动预报时段收窄与按目标日期逐日查询风险等级测试。

覆盖三类修复（2026-08-24 甲方反馈）：
1. "今天下午和今天晚上蓟州的天气"应真正按所问时段（逐小时）回答，而不是铺开整日。
   时段化后 query_mode 走 hourly（_time_of_day），hourly_summary 只含所问小时。
2. 单日和多日窗口都查询对应起报时次；多日逐日合并，不能用 24h 门控跳过接口。
3. 时段化与"今天"窗口衔接："今天下午"→ 今天 12:00-18:00；"今天下午和晚上"→ 12:00-24:00。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import rolling_forecast_service as rfs  # noqa: E402


# 固定"现在"为 2026-08-24（周一）上午 10:00
NOW = rfs.datetime(2026, 8, 24, 10, 0, tzinfo=rfs.TIANJIN_TIMEZONE)


def _hourly_result_data(coord, n_hours=12, weather="小雨"):
    """构造 hourly 模式时段化查询可用的 resultData（逐小时序列）。"""
    return {
        "resultData": {
            coord: {
                "WEA": [weather] * n_hours,
                "TP1H": [0.5] * n_hours,
                "TMAX": [29.0] * n_hours,
                "TMIN": [22.0] * n_hours,
                "EDA": ["东南风 2-3级"] * n_hours,
                "VISMIN": [8.0] * n_hours,
            }
        }
    }


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _fake_get_for(coord, weather="小雨"):
    """按请求的 startPeriod/endPeriod/interval 返回对应条数的逐时次序列（模拟真实接口切片）。"""

    def get(*a, **k):
        params = k.get("params", {})
        start_p = int(params.get("startPeriod", 0))
        end_p = int(params.get("endPeriod", 240))
        interval = int(params.get("interval", 24))
        n = max(1, (end_p - start_p) // interval)
        return _Resp(_hourly_result_data(coord, n, weather))

    return get


class TestDetectTimeOfDayRange:
    """时段词 → 合并的 [start_hour, end_hour)。"""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("今天下午的天气", (12, 18)),
            ("今天晚上蓟州天气", (18, 24)),
            ("今天上午天气", (8, 12)),
            ("今天中午热不热", (11, 14)),
            ("今天凌晨有没有雨", (0, 6)),
            ("今天夜里会不会下", (20, 30)),
            ("傍晚出门", (17, 20)),
            # 组合：并集（min start, max end）
            ("今天下午和今天晚上", (12, 24)),
            ("上午和晚上", (8, 24)),
            ("今天下午、傍晚和夜里", (12, 30)),
        ],
    )
    def test_ranges(self, text, expected):
        assert rfs._detect_time_of_day_range(text) == expected

    @pytest.mark.parametrize("text", ["今天天气怎么样", "明天蓟州天气", "未来三天天气"])
    def test_no_time_of_day_word_returns_none(self, text):
        # 纯"今天/明天/未来三天"无时段词 → None（只收窄到时段，不收窄整日）。
        assert rfs._detect_time_of_day_range(text) is None


class TestNarrowCalendarToTimeOfDay:
    """把单日日历窗口收窄为时段逐小时窗口；不适配回退 None。"""

    def _today_calendar(self):
        return rfs.resolve_calendar_query_window(rfs.datetime(2026, 8, 24).date(), 1, now=NOW)

    def test_afternoon_and_evening_combined(self):
        win = self._today_calendar()
        narrow = rfs._narrow_calendar_window_to_time_of_day(win, "今天下午和今天晚上蓟州的天气", NOW)
        assert narrow is not None
        assert narrow["interval"] == 1
        # 今天 12:00 → 24:00
        assert narrow["target_start"].hour == 12 and narrow["target_start"].date().day == 24
        assert narrow["target_end"].hour == 0 and narrow["target_end"].date().day == 25
        # fcst=今天08:00（now=10:00≥08:00 且 ≤target_start12:00）→ start_period=4, end_period=16
        assert narrow["fcst_time"] == "20260824080000"
        assert narrow["start_period"] == 4
        assert narrow["end_period"] == 16

    def test_evening_only(self):
        win = self._today_calendar()
        narrow = rfs._narrow_calendar_window_to_time_of_day(win, "今天晚上蓟州天气", NOW)
        assert narrow["target_start"].hour == 18 and narrow["target_start"].day == 24
        assert narrow["end_period"] - narrow["start_period"] == 6  # 18:00-24:00

    def test_night_crosses_midnight(self):
        win = self._today_calendar()
        narrow = rfs._narrow_calendar_window_to_time_of_day(win, "今天夜里会不会下雨", NOW)
        assert narrow["target_start"].hour == 20
        assert narrow["target_end"].hour == 6 and narrow["target_end"].day == 25  # 次日06

    def test_no_time_of_day_returns_none(self):
        win = self._today_calendar()
        assert rfs._narrow_calendar_window_to_time_of_day(win, "今天蓟州天气怎么样", NOW) is None

    def test_multi_day_window_not_narrowed(self):
        win = rfs.resolve_calendar_query_window(rfs.datetime(2026, 8, 25).date(), 3, now=NOW)
        assert rfs._narrow_calendar_window_to_time_of_day(win, "未来三天下午天气", NOW) is None

    def test_tomorrow_afternoon_narrows(self):
        win = rfs.resolve_calendar_query_window(rfs.datetime(2026, 8, 25).date(), 1, now=NOW)
        narrow = rfs._narrow_calendar_window_to_time_of_day(win, "明天下午蓟州天气", NOW)
        assert narrow is not None
        assert narrow["target_start"].date().day == 25 and narrow["target_start"].hour == 12


class TestCoreTimeOfDayHourlyMode:
    """"今天下午和今天晚上"等时段化查询在区域模式走 time_of_day，聚合为该时段单条汇总。

    甲方口径（2026-08-24）："今天下午有雨吗"这类问题不要逐小时，给出该时段的整体
    天气即可（时段写"今天下午"，含天气现象、气温、风力风向、降水量）。因此时段化
    查询产出 time_of_day_summary（单条/单区域），不再产 hourly_summary 逐小时行。
    """

    def _run(self, monkeypatch, weather="小雨"):
        monkeypatch.setattr(rfs.requests, "get", _fake_get_for("117.45_40.05", weather))
        monkeypatch.setattr(rfs, "_query_region_hazards", lambda lon, lat, attach_risk_levels=True: None)
        rfs._rolling_forecast_cache.clear()
        return rfs.query_rolling_forecast_core(
            user_query="今天下午和今天晚上蓟州的天气怎么样", now=NOW
        )

    def test_query_mode_is_time_of_day(self, monkeypatch):
        result = self._run(monkeypatch)
        assert result["query_mode"].endswith("_region")
        assert "time_of_day" in result["query_mode"]

    def test_produces_period_summary_not_hourly(self, monkeypatch):
        result = self._run(monkeypatch)
        assert "hourly_summary" not in result, "时段化查询不应再产逐小时行"
        summary = result.get("time_of_day_summary") or []
        assert len(summary) == 1  # 单区域聚合成一条
        assert summary[0]["region"] == "蓟州区"

    def test_time_of_day_label(self, monkeypatch):
        result = self._run(monkeypatch)
        assert result.get("time_of_day_label") == "今天下午到晚上"

    def test_no_daily_calendar_fields(self, monkeypatch):
        result = self._run(monkeypatch)
        # 时段化后不再走整日日历窗口
        assert "daily_summary" not in result


class TestTimeOfDaySummaryAggregation:
    """time_of_day_summary 聚合口径：天气合并、气温区间、降水量求和、风力风向去重。"""

    def _run(
        self, monkeypatch, weather_list, rain_list, tmax=30.0, tmin=22.0,
        query="今天下午蓟州天气怎么样", visibility_list=None,
    ):
        n = len(weather_list)
        visibility_list = list(visibility_list) if visibility_list is not None else [8.0] * n

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"resultData": {"117.45_40.05": {
                    "WEA": list(weather_list), "TP1H": list(rain_list),
                    "TMAX": [tmax] * n, "TMIN": [tmin] * n,
                    "EDA": ["南风2级"] * n, "VISMIN": visibility_list,
                }}}

        def fake_get(*a, **k):
            return Resp()

        monkeypatch.setattr(rfs.requests, "get", fake_get)
        monkeypatch.setattr(rfs, "_query_region_hazards", lambda lon, lat, attach_risk_levels=True: None)
        rfs._rolling_forecast_cache.clear()
        return rfs.query_rolling_forecast_core(user_query=query, now=NOW)

    def test_uniform_weather_kept_single(self, monkeypatch):
        result = self._run(monkeypatch, ["雷阵雨"] * 6, [2.0] * 6)
        row = result["time_of_day_summary"][0]
        assert row["weather"] == "雷阵雨"
        assert row["rainfall_mm"] == 12.0  # 6 小时 × 2.0 求和

    def test_varied_weather_joined_with_zhuan(self, monkeypatch):
        result = self._run(monkeypatch, ["阴", "阴", "阴", "雷阵雨", "雷阵雨", "多云"], [0.0, 0.0, 0.0, 5.0, 3.0, 0.0])
        row = result["time_of_day_summary"][0]
        assert row["weather"] == "阴转雷阵雨转多云"
        assert row["rainfall_mm"] == 8.0

    def test_thunder_rain_process_is_not_mechanically_repeated(self, monkeypatch):
        """同一雷雨过程的逐小时标签不应拼成“雨伴雷电转雨转雨伴雷电”。"""
        result = self._run(
            monkeypatch,
            ["雨伴随雷电", "雨", "雨伴随雷电", "阴有轻雾伴随雷电"],
            [1.0, 1.0, 1.0, 0.4],
            query="今天晚上蓟州天气怎么样",
        )
        row = result["time_of_day_summary"][0]
        assert row["weather"] == "雷阵雨转阴有轻雾"

    def test_meaningful_weather_changes_are_preserved(self, monkeypatch):
        """只压缩同一过程的冗余标签，不吞掉真实的天气演变。"""
        result = self._run(
            monkeypatch,
            ["小雨", "中雨伴随雷电", "小雨", "多云"],
            [0.5, 5.0, 0.5, 0.0],
        )
        row = result["time_of_day_summary"][0]
        assert row["weather"] == "小雨转中雨伴随雷电转小雨转多云"

    @pytest.mark.parametrize(
        ("weather_list", "expected"),
        [
            (["雨", "雨伴随雷电", "雨"], "雷阵雨"),
            (["雨伴随雷电", "雨", "雨伴随雷电", "雨", "雨伴随雷电"], "雷阵雨"),
            (["小雨", "小雨伴随雷电", "小雨"], "小雨伴随雷电"),
        ],
    )
    def test_same_rain_base_keeps_the_more_specific_thunder_label(
        self, monkeypatch, weather_list, expected,
    ):
        result = self._run(monkeypatch, weather_list, [1.0] * len(weather_list))
        assert result["time_of_day_summary"][0]["weather"] == expected

    def test_unrelated_phase_does_not_hide_later_thunder(self, monkeypatch):
        result = self._run(
            monkeypatch,
            ["雨伴随雷电", "多云", "阴有轻雾伴随雷电"],
            [1.0, 0.0, 0.0],
        )
        assert (
            result["time_of_day_summary"][0]["weather"]
            == "雷阵雨转多云转阴有轻雾伴随雷电"
        )

    def test_compound_source_weather_is_preserved_verbatim(self, monkeypatch):
        result = self._run(monkeypatch, ["雨伴随雷电转阴"], [1.0])
        assert result["time_of_day_summary"][0]["weather"] == "雨伴随雷电转阴"

    def test_temperature_is_period_range(self, monkeypatch):
        result = self._run(monkeypatch, ["多云"] * 6, [0.0] * 6, tmax=31.0, tmin=23.0)
        row = result["time_of_day_summary"][0]
        assert row["tmax"] == "31"
        assert row["tmin"] == "23"

    def test_visibility_uses_minimum_positive_value(self, monkeypatch):
        result = self._run(
            monkeypatch, ["多云"] * 6, [0.0] * 6,
            visibility_list=[8.0, 0.0, 3.0, 1.2, 5.0, 6.0],
        )
        assert result["time_of_day_summary"][0]["visibility_min_km"] == 1.2

    def test_visibility_zero_placeholder_becomes_missing(self, monkeypatch):
        result = self._run(
            monkeypatch, ["多云"] * 6, [0.0] * 6,
            visibility_list=[0.0] * 6,
        )
        assert result["time_of_day_summary"][0]["visibility_min_km"] is None

    def test_single_afternoon_label(self, monkeypatch):
        result = self._run(monkeypatch, ["多云"] * 6, [0.0] * 6, query="今天下午蓟州天气怎么样")
        assert result["time_of_day_label"] == "今天下午"

    def test_evening_label(self, monkeypatch):
        result = self._run(monkeypatch, ["多云"] * 6, [0.0] * 6, query="今天晚上蓟州天气怎么样")
        assert result["time_of_day_label"] == "今天晚上"

    def test_tomorrow_afternoon_label(self, monkeypatch):
        result = self._run(monkeypatch, ["多云"] * 6, [0.0] * 6, query="明天下午蓟州天气怎么样")
        assert result["time_of_day_label"] == "明天下午"

    def test_future_n_hours_still_hourly(self, monkeypatch):
        """"未来6小时"这类逐小时问法不受影响，仍产 hourly_summary。"""
        monkeypatch.setattr(rfs.requests, "get", _fake_get_for("117.45_40.05", "小雨"))
        monkeypatch.setattr(rfs, "_query_region_hazards", lambda lon, lat, attach_risk_levels=True: None)
        rfs._rolling_forecast_cache.clear()
        result = rfs.query_rolling_forecast_core(user_query="蓟州未来6小时天气怎么样", now=NOW)
        assert result.get("hourly_summary"), "逐小时问法仍应产 hourly_summary"
        assert "time_of_day_summary" not in result


def test_daily_summary_ignores_zero_visibility_placeholder():
    """逐日汇总同样只消费正数能见度，避免 0 占位触发低能见度。"""
    periods = [
        {
            "region": "蓟州", "start_time": "2026-08-25 08:00:00", "end_time": "2026-08-26 08:00:00",
            "WEA": "多云", "TMAX": 30, "TMIN": 22, "EDA": "南风2级", "TP1H": 0.0,
            "VISMIN": visibility,
        }
        for visibility in (0.0, 8.0)
    ]

    row = rfs.build_daily_summary(periods)[0]

    assert row["visibility_min_km"] == 8.0


class TestRiskFcstTimesFromWindowRegression:
    """风险接口支持按预报窗口逐日查询，不能因 24h 门控直接伪造 no_data。"""

    def test_future_three_days_builds_three_daily_cycles(self):
        calendar = {"forecast_start_date": "2026-08-25", "forecast_days": 3}

        assert rfs._risk_fcst_times_from_window(calendar) == [
            "20260825080000",
            "20260826080000",
            "20260827080000",
        ]

    def test_future_three_days_core_passes_cycles_to_risk_query(self, monkeypatch):
        captured = {}

        def fake_hazards(lon, lat, risk_fcst_times=None):
            captured["risk_fcst_times"] = risk_fcst_times
            return {
                "total_found": 1,
                "radius_km": 25.0,
                "categories": [{"key": "dzzh", "label": "地质灾害", "kind": "地灾", "count": 1}],
                "risk_levels": {},
                "risk_levels_available": True,
            }

        monkeypatch.setattr(rfs.requests, "get", _fake_get_for("117.45_40.05"))
        monkeypatch.setattr(rfs, "_query_region_hazards", fake_hazards)
        rfs._rolling_forecast_cache.clear()

        rfs.query_rolling_forecast_core(user_query="蓟州未来三天天气怎么样", now=NOW)

        assert captured["risk_fcst_times"] == [
            "20260825080000",
            "20260826080000",
            "20260827080000",
        ]


class TestCoreRiskForecastTimes:
    """query_rolling_forecast_core 按目标日向风险接口传逐日起报时次。"""

    def _capture(self, monkeypatch, user_query):
        captured = {}

        def fake_hazards(lon, lat, risk_fcst_times=None):
            captured["risk_fcst_times"] = risk_fcst_times
            return {
                "total_found": 1, "radius_km": 25.0,
                "categories": [{"key": "dzzh", "label": "地质灾害", "kind": "地灾", "count": 1}],
            }

        monkeypatch.setattr(rfs.requests, "get", _fake_get_for("117.45_40.05"))
        rfs._rolling_forecast_cache.clear()
        monkeypatch.setattr(rfs, "_query_region_hazards", fake_hazards)
        rfs.query_rolling_forecast_core(user_query=user_query, now=NOW)
        return captured

    def test_today_query_uses_default_cycle_fallback(self, monkeypatch):
        captured = self._capture(monkeypatch, "今天蓟州的天气怎么样")
        assert captured["risk_fcst_times"] is None

    def test_afternoon_evening_query_uses_default_cycle_fallback(self, monkeypatch):
        captured = self._capture(monkeypatch, "今天下午和今天晚上蓟州的天气")
        assert captured["risk_fcst_times"] is None

    def test_future_three_days_passes_all_daily_cycles(self, monkeypatch):
        captured = self._capture(monkeypatch, "蓟州未来三天天气怎么样")
        assert captured["risk_fcst_times"] == [
            "20260825080000", "20260826080000", "20260827080000",
        ]

    def test_tomorrow_single_day_uses_tomorrow_cycle(self, monkeypatch):
        captured = self._capture(monkeypatch, "明天蓟州天气怎么样")
        assert captured["risk_fcst_times"] == ["20260825080000"]


class TestQueryRegionHazardsRiskLevels:
    """区域隐患查询始终按目标风险时次调用等级接口。"""

    def _hazards_ok(self):
        return {
            "status": "ok", "total_found": 3, "categories": [
                {"key": "dzzh", "label": "地质灾害", "kind": "地灾", "count": 2},
            ],
        }

    def test_explicit_cycles_are_forwarded_and_marked_available(self, monkeypatch):
        called = {"n": 0}
        captured = {}
        monkeypatch.setattr(rfs, "_region_hazard_queryer", lambda lon, lat, radius: self._hazards_ok())

        def fake_levels(lon, lat, radius, fcst_times=None):
            called["n"] += 1
            captured["fcst_times"] = fcst_times
            return {"dzzh": {"label": "地质灾害风险", "kind": "geologic", "levels": {"三级": 2}, "total": 2}}

        monkeypatch.setattr(rfs, "_region_risk_level_queryer", fake_levels)
        cycles = ["20260825080000", "20260826080000"]
        result = rfs._query_region_hazards(117.45, 40.05, cycles)
        assert called["n"] == 1
        assert captured["fcst_times"] == cycles
        assert result["risk_levels_available"] is True
        assert "risk_levels" in result


class TestSummarizeTodWind:
    """时段汇总风力风向：按风向分组合并风力区间、风向按出现顺序"转"连接。

    修复甲方反馈的可读性问题——逐小时 EDA 原文去重拼接会出
    "西北风0-1级；东南风0-1级；东风0-1级；东风1-2级"这种既长又自相矛盾的列表。
    """

    def test_same_direction_merges_force_range(self):
        # 东风 0-1 与 1-2 合并为 0~2，风向变化用"转"连接
        out = rfs._summarize_tod_wind(["西北风0-1级", "东南风0-1级", "东风0-1级", "东风1-2级"])
        assert out == "西北风0~1级转东南风0~1级转东风0~2级"

    def test_single_uniform_wind(self):
        assert rfs._summarize_tod_wind(["南风2级", "南风2级"]) == "南风2级"

    def test_direction_changes_joined_with_zhuan(self):
        out = rfs._summarize_tod_wind(["北风3级", "南风2级"])
        assert out == "北风3级转南风2级"

    def test_compound_wind_range_taken(self):
        # 复合风况取全部数字的区间（阵风并入）
        out = rfs._summarize_tod_wind(["东南风3～4级阵风6级"])
        assert out == "东南风3~6级"

    def test_empty_and_placeholder_skipped(self):
        assert rfs._summarize_tod_wind([]) is None
        assert rfs._summarize_tod_wind(["", "--"]) is None

    def test_unparseable_kept_verbatim(self):
        # 无风向词的原文原样保留，不丢信息
        out = rfs._summarize_tod_wind(["静风"])
        assert out == "静风"

    def test_summary_rows_uses_wind_summarizer(self, monkeypatch):
        """集成：time_of_day_summary 的风力风向走 _summarize_tod_wind，不再逐小时"；"拼接。"""

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"resultData": {"117.45_40.05": {
                    "WEA": ["多云"] * 6, "TP1H": [0.0] * 6,
                    "TMAX": [33.0] * 6, "TMIN": [30.0] * 6,
                    "EDA": ["西北风0-1级", "东南风0-1级", "东风0-1级", "东风1-2级", "东风1-2级", "东风1-2级"],
                    "VISMIN": [10.0] * 6,
                }}}

        monkeypatch.setattr(rfs.requests, "get", lambda *a, **k: Resp())
        monkeypatch.setattr(rfs, "_query_region_hazards", lambda lon, lat, attach_risk_levels=True: None)
        rfs._rolling_forecast_cache.clear()
        result = rfs.query_rolling_forecast_core(user_query="今天下午蓟州天气怎么样", now=NOW)
        row = result["time_of_day_summary"][0]
        assert "；" not in row["EDA"]
        assert row["EDA"] == "西北风0~1级转东南风0~1级转东风0~2级"
