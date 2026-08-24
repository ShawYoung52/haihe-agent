"""滚动预报"时段词"（上午/下午/晚上/夜里/凌晨/中午/傍晚）收窄与 24h 风险列门控测试。

覆盖三类修复（2026-08-24 甲方反馈）：
1. "今天下午和今天晚上蓟州的天气"应真正按所问时段（逐小时）回答，而不是铺开整日。
   时段化后 query_mode 走 hourly（_time_of_day），hourly_summary 只含所问小时。
2. 24 小时内（今天/当前）的区域天气查询必须带"本次风险等级"列；
   24 小时之外（明天/未来N天/后天）不接等级列（整列隐藏，灾害表其余列照常）。
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

    def _run(self, monkeypatch, weather_list, rain_list, tmax=30.0, tmin=22.0, query="今天下午蓟州天气怎么样"):
        n = len(weather_list)

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"resultData": {"117.45_40.05": {
                    "WEA": list(weather_list), "TP1H": list(rain_list),
                    "TMAX": [tmax] * n, "TMIN": [tmin] * n,
                    "EDA": ["南风2级"] * n, "VISMIN": [8.0] * n,
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

    def test_temperature_is_period_range(self, monkeypatch):
        result = self._run(monkeypatch, ["多云"] * 6, [0.0] * 6, tmax=31.0, tmin=23.0)
        row = result["time_of_day_summary"][0]
        assert row["tmax"] == "31"
        assert row["tmin"] == "23"

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


class TestRiskFcstWindowApplies:
    """"本次风险等级"列门控：仅 24h 内（窗口起始日=今天 或 普通当前查询）。"""

    def test_today_calendar_window_applies(self):
        win = {"forecast_start_date": "2026-08-24", "forecast_days": 1}
        assert rfs._risk_fcst_window_applies(win, None, NOW) is True

    def test_no_window_applies(self):
        assert rfs._risk_fcst_window_applies(None, None, NOW) is True

    def test_tomorrow_window_not_applies(self):
        win = {"forecast_start_date": "2026-08-25", "forecast_days": 1}
        assert rfs._risk_fcst_window_applies(win, None, NOW) is False

    def test_future_three_days_not_applies(self):
        win = {"forecast_start_date": "2026-08-25", "forecast_days": 3}
        assert rfs._risk_fcst_window_applies(win, None, NOW) is False

    def test_day_after_tomorrow_not_applies(self):
        win = {"forecast_start_date": "2026-08-26", "forecast_days": 1}
        assert rfs._risk_fcst_window_applies(win, None, NOW) is False

    def test_tod_today_applies(self):
        hourly = {"target_start": rfs.datetime(2026, 8, 24, 12, 0, tzinfo=rfs.TIANJIN_TIMEZONE)}
        assert rfs._risk_fcst_window_applies(None, hourly, NOW) is True

    def test_tod_tomorrow_not_applies(self):
        hourly = {"target_start": rfs.datetime(2026, 8, 25, 12, 0, tzinfo=rfs.TIANJIN_TIMEZONE)}
        assert rfs._risk_fcst_window_applies(None, hourly, NOW) is False


class TestCoreRiskAttachGating:
    """query_rolling_forecast_core 按 24h 门控决定是否给 _query_region_hazards 接风险等级。"""

    def _capture(self, monkeypatch, user_query):
        captured = {}

        def fake_hazards(lon, lat, attach_risk_levels=True):
            captured["attach_risk_levels"] = attach_risk_levels
            return {
                "total_found": 1, "radius_km": 25.0,
                "categories": [{"key": "dzzh", "label": "地质灾害", "kind": "地灾", "count": 1}],
            }

        monkeypatch.setattr(rfs.requests, "get", _fake_get_for("117.45_40.05"))
        rfs._rolling_forecast_cache.clear()
        monkeypatch.setattr(rfs, "_query_region_hazards", fake_hazards)
        rfs.query_rolling_forecast_core(user_query=user_query, now=NOW)
        return captured

    def test_today_query_attaches_risk(self, monkeypatch):
        captured = self._capture(monkeypatch, "今天蓟州的天气怎么样")
        assert captured["attach_risk_levels"] is True

    def test_afternoon_evening_query_attaches_risk(self, monkeypatch):
        captured = self._capture(monkeypatch, "今天下午和今天晚上蓟州的天气")
        assert captured["attach_risk_levels"] is True

    def test_future_three_days_not_attach(self, monkeypatch):
        captured = self._capture(monkeypatch, "蓟州未来三天天气怎么样")
        assert captured["attach_risk_levels"] is False

    def test_tomorrow_not_attach(self, monkeypatch):
        captured = self._capture(monkeypatch, "明天蓟州天气怎么样")
        assert captured["attach_risk_levels"] is False


class TestQueryRegionHazardsRiskLevelsGating:
    """_query_region_hazards：attach_risk_levels=False 时不调风险接口、不带 risk_levels 字段。"""

    def _hazards_ok(self):
        return {
            "status": "ok", "total_found": 3, "categories": [
                {"key": "dzzh", "label": "地质灾害", "kind": "地灾", "count": 2},
            ],
        }

    def test_attach_true_calls_risk_and_marks_available(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(rfs, "_region_hazard_queryer", lambda lon, lat, radius: self._hazards_ok())

        def fake_levels(lon, lat, radius, fcst_times=None):
            called["n"] += 1
            return {"dzzh": {"label": "地质灾害风险", "kind": "geologic", "levels": {"三级": 2}, "total": 2}}

        monkeypatch.setattr(rfs, "_region_risk_level_queryer", fake_levels)
        result = rfs._query_region_hazards(117.45, 40.05, attach_risk_levels=True)
        assert called["n"] == 1
        assert result["risk_levels_available"] is True
        assert "risk_levels" in result

    def test_attach_false_skips_risk_and_no_fields(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(rfs, "_region_hazard_queryer", lambda lon, lat, radius: self._hazards_ok())

        def fake_levels(lon, lat, radius, fcst_times=None):
            called["n"] += 1
            return {}

        monkeypatch.setattr(rfs, "_region_risk_level_queryer", fake_levels)
        result = rfs._query_region_hazards(117.45, 40.05, attach_risk_levels=False)
        assert called["n"] == 0, "24h 外不应调用风险接口"
        assert "risk_levels" not in result
        assert "risk_levels_available" not in result
        # 灾害表其余字段照常
        assert result["total_found"] == 3
        assert result["categories"]
