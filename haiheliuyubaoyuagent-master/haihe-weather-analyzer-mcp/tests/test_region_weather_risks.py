"""区域综合风险核心的契约测试。"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import rolling_forecast_service as rfs  # noqa: E402
from custom_tools import risk_warning_tool as rwt  # noqa: E402


FIXED_NOW = datetime(2026, 8, 27, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture(autouse=True)
def _stub_region_risk_weather_io(monkeypatch):
    """风险核心的多数测试只测风险；默认在真实天气解析边界返回无数据，禁止访问网络。"""
    monkeypatch.setattr(
        rfs,
        "_cached_rolling_forecast_request",
        lambda _params: {"code": 0, "message": "ok", "resultData": {}},
    )


def hazard_payload(*, risk_levels, risk_levels_available=True, hazards_available=True):
    return {
        "total_found": 298,
        "radius_km": 25.0,
        "categories": [
            {"key": "dzzh", "label": "地质灾害", "count": 257},
            {"key": "sh", "label": "山洪", "count": 27},
            {"key": "zxhl", "label": "中小河流", "count": 14},
        ],
        "hazards_available": hazards_available,
        "risk_levels": risk_levels,
        "risk_levels_available": risk_levels_available,
    }


def _risk(result, key):
    return next(item for item in result["regions"][0]["risks"] if item["key"] == key)


def test_jizhou_risk_query_returns_all_three_categories(monkeypatch):
    monkeypatch.setattr(rfs, "_query_region_hazards", lambda lon, lat, times, *, region="", **kwargs: {
        "total_found": 298,
        "radius_km": 25.0,
        "categories": [
            {"key": "dzzh", "label": "地质灾害", "count": 257},
            {"key": "sh", "label": "山洪", "count": 27},
            {"key": "zxhl", "label": "中小河流", "count": 14},
        ],
        "hazards_available": True,
        "risk_levels": {
            "dzzh": {"levels": {"三级": 1}, "total": 1, "level_advice": ["关注地质灾害风险"]},
            "sh": {"levels": {"四级": 2}, "total": 2, "level_advice": ["远离沟谷河道"]},
        },
        "risk_levels_available": True,
    })

    result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)

    assert result["status"] == "ok"
    assert result["regions"][0]["region"] == "蓟州"
    assert result["regions"][0]["radius_km"] == 25.0
    assert {item["key"] for item in result["regions"][0]["risks"]} == {"dzzh", "sh", "zxhl"}
    assert _risk(result, "dzzh")["risk_status"] == "risk"
    assert _risk(result, "zxhl")["risk_status"] == "no_risk"


def test_jizhou_risk_query_also_returns_real_today_weather_without_duplicate_risk_query(monkeypatch):
    """综合风险核心应附当天滚动预报，且天气查询不得再次查询三灾种风险。"""
    hazard_calls = []

    def hazards(lon, lat, times, *, region="", **kwargs):
        hazard_calls.append((lon, lat, times))
        return hazard_payload(risk_levels={})

    monkeypatch.setattr(rfs, "_query_region_hazards", hazards)
    monkeypatch.setattr(
        rfs,
        "_cached_rolling_forecast_request",
        lambda _params: {
            "code": 0,
            "message": "ok",
            "resultData": {
                "117.45_40.05": {
                    "WEA": ["多云"],
                    "TMAX": [30],
                    "TMIN": [20],
                    "EDA": ["东北风1-2级"],
                    "TP1H": [0.0],
                    "VISMIN": [8.0],
                }
            },
        },
    )

    result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)

    weather = result["weather_forecast"]
    assert weather["status"] == "ok"
    assert weather["data_source"] == "天津市气象台滚动预报"
    assert weather["periods"][0]["WEA"] == "多云"
    assert weather["periods"][0]["TMIN"] == 20
    assert weather["periods"][0]["TMAX"] == 30
    assert len(hazard_calls) == 1


def test_weather_failure_does_not_erase_available_region_risks(monkeypatch):
    monkeypatch.setattr(rfs, "_query_region_hazards", lambda *a, **k: hazard_payload(risk_levels={}))
    monkeypatch.setattr(
        rfs,
        "_cached_rolling_forecast_request",
        lambda _params: (_ for _ in ()).throw(TimeoutError("rolling forecast unavailable")),
    )

    result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)

    assert result["weather_forecast"]["status"] == "unavailable"
    assert result["regions"][0]["risks"][0]["risk_status"] == "no_risk"


def test_placeholder_weather_period_is_unavailable_and_keeps_region_risks(monkeypatch):
    """只有“--”占位行不算有效天气，不能诱导回答模型补写天气。"""
    monkeypatch.setattr(rfs, "_query_region_hazards", lambda *a, **k: hazard_payload(risk_levels={}))
    monkeypatch.setattr(
        rfs,
        "_cached_rolling_forecast_request",
        lambda _params: {
            "code": 0,
            "message": "ok",
            "resultData": {"117.45_40.05": {"WEA": ["--"]}},
        },
    )

    result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)

    assert result["weather_forecast"]["status"] == "unavailable"
    assert result["weather_forecast"]["periods"] == []
    assert result["regions"][0]["risks"][0]["risk_status"] == "no_risk"


def test_empty_reachable_levels_are_no_risk(monkeypatch):
    monkeypatch.setattr(rfs, "_query_region_hazards", lambda *a, **k: hazard_payload(risk_levels={}))

    result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)

    assert all(item["risk_status"] == "no_risk" for item in result["regions"][0]["risks"])


def test_no_data_is_unavailable_with_distinct_reason(monkeypatch):
    monkeypatch.setattr(
        rfs,
        "_query_region_hazards",
        lambda *a, **k: hazard_payload(risk_levels={"dzzh": "no_data"}),
    )

    result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)

    dzzh = _risk(result, "dzzh")
    assert dzzh["risk_status"] == "unavailable"
    assert dzzh["unavailable_reason"] == "risk_forecast_no_data"
    assert result["status"] == "partial"


def test_malformed_levels_are_unavailable_not_no_risk(monkeypatch):
    monkeypatch.setattr(
        rfs,
        "_query_region_hazards",
        lambda *a, **k: hazard_payload(risk_levels={"dzzh": {"levels": ["三级"]}}),
    )

    result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)

    assert _risk(result, "dzzh")["risk_status"] == "unavailable"
    assert _risk(result, "dzzh")["unavailable_reason"] == "malformed_risk_payload"


def test_partial_failure_is_reported_per_hazard(monkeypatch):
    monkeypatch.setattr(
        rfs,
        "_query_region_hazards",
        lambda *a, **k: hazard_payload(risk_levels={"dzzh": None, "sh": {"levels": {"四级": 1}, "total": 1}}),
    )

    result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)

    assert result["status"] == "partial"
    assert _risk(result, "dzzh")["risk_status"] == "unavailable"
    assert _risk(result, "sh")["risk_status"] == "risk"
    assert _risk(result, "zxhl")["risk_status"] == "no_risk"


def test_all_risk_interfaces_failed_is_not_no_risk(monkeypatch):
    monkeypatch.setattr(
        rfs,
        "_query_region_hazards",
        lambda *a, **k: hazard_payload(risk_levels=None, risk_levels_available=False),
    )

    result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)

    assert result["status"] == "risk_service_unavailable"
    assert all(item["risk_status"] == "unavailable" for item in result["regions"][0]["risks"])


class TestRegionRiskStatusText:
    """2026-08-31 用户口径：状态文案面向业务用户，由代码确定性生成 status_text。

    上层逐字采用 status_text，不得再出现"接口暂不可用""无对应预报数据"等技术化措辞。
    """

    def test_no_data_status_text_is_business_friendly(self, monkeypatch):
        monkeypatch.setattr(
            rfs, "_query_region_hazards",
            lambda *a, **k: hazard_payload(risk_levels={"dzzh": "no_data"}),
        )
        result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)
        dzzh = _risk(result, "dzzh")
        assert dzzh["risk_status"] == "unavailable"
        assert dzzh["unavailable_reason"] == "risk_forecast_no_data"
        assert dzzh["status_text"] == "暂无风险预报资料"

    def test_kind_failure_status_text(self, monkeypatch):
        monkeypatch.setattr(
            rfs, "_query_region_hazards",
            lambda *a, **k: hazard_payload(risk_levels={"dzzh": None, "sh": {"levels": {"四级": 1}, "total": 1}}),
        )
        result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)
        assert _risk(result, "dzzh")["status_text"] == "风险数据查询暂时不可用"
        assert _risk(result, "sh")["status_text"] == "有风险"
        assert _risk(result, "zxhl")["status_text"] == "无风险"

    def test_service_unavailable_status_text(self, monkeypatch):
        monkeypatch.setattr(
            rfs, "_query_region_hazards",
            lambda *a, **k: hazard_payload(risk_levels=None, risk_levels_available=False),
        )
        result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)
        assert all(
            item["status_text"] == "风险数据查询暂时不可用"
            for item in result["regions"][0]["risks"]
        )

    def test_status_text_never_technical_jargon(self, monkeypatch):
        monkeypatch.setattr(
            rfs, "_query_region_hazards",
            lambda *a, **k: hazard_payload(risk_levels={"dzzh": "no_data", "sh": None}),
        )
        result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)
        for item in result["regions"][0]["risks"]:
            assert "接口" not in item["status_text"]
            assert "预报数据" not in item["status_text"]


def test_static_hazard_failure_keeps_hidden_counts_unknown(monkeypatch):
    monkeypatch.setattr(
        rfs,
        "_query_region_hazards",
        lambda *a, **k: {
            "radius_km": 18.0,
            "categories": [],
            "hazards_available": False,
            "risk_levels": {"sh": {"levels": {"四级": 1}, "total": 1}},
            "risk_levels_available": True,
        },
    )

    result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)

    assert result["regions"][0]["radius_km"] == 18.0
    assert all(item["hidden_point_count"] is None for item in result["regions"][0]["risks"])
    assert _risk(result, "sh")["risk_status"] == "risk"


def test_unsupported_explicit_region_does_not_query(monkeypatch):
    def unexpected_query(*args):
        pytest.fail("unsupported region must not call the hazard service")

    monkeypatch.setattr(rfs, "_query_region_hazards", unexpected_query)

    result = rfs.query_region_weather_risks_core("今天雄安新区可能有哪些风险？", regions="雄安新区", now=FIXED_NOW)

    assert result["status"] == "unsupported_region"
    assert result["regions"] == []


def test_unrecognized_bare_region_does_not_default_to_tianjin(monkeypatch):
    def unexpected_query(*args):
        pytest.fail("unknown bare region must not call the hazard service")

    monkeypatch.setattr(rfs, "_query_region_hazards", unexpected_query)

    result = rfs.query_region_weather_risks_core("雄安未来风险如何？", now=FIXED_NOW)

    assert result["status"] == "unsupported_region"
    assert result["regions"] == []


def test_same_day_fallback_does_not_claim_calendar_day_coverage(monkeypatch):
    monkeypatch.setattr(rfs, "_query_region_hazards", lambda *a, **k: hazard_payload(risk_levels={}))

    result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)

    assert result["risk_window"] == {
        "forecast_start_time": None,
        "forecast_end_time": None,
        "forecast_days": None,
        "fcst_times": None,
        "time_mode": "latest_available_cycle_with_same_day_fallback",
    }


@pytest.mark.parametrize("invalid_count", [-1, True, "1.5"])
def test_invalid_risk_level_counts_are_unavailable(monkeypatch, invalid_count):
    monkeypatch.setattr(
        rfs,
        "_query_region_hazards",
        lambda *a, **k: hazard_payload(risk_levels={"dzzh": {"levels": {"三级": invalid_count}}}),
    )

    result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)

    assert _risk(result, "dzzh")["risk_status"] == "unavailable"
    assert _risk(result, "dzzh")["unavailable_reason"] == "malformed_risk_payload"


@pytest.mark.parametrize("invalid_count", [-1, True, "1.5"])
def test_invalid_static_counts_are_unknown(monkeypatch, invalid_count):
    payload = hazard_payload(risk_levels={})
    payload["categories"] = [{"key": "dzzh", "label": "地质灾害", "count": invalid_count}]
    monkeypatch.setattr(rfs, "_query_region_hazards", lambda *a, **k: payload)

    result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)

    assert _risk(result, "dzzh")["hidden_point_count"] is None


def test_risk_window_reports_the_clamped_actual_days(monkeypatch):
    captured = {}

    def query(lon, lat, times, **kwargs):
        captured["times"] = times
        return hazard_payload(risk_levels={})

    monkeypatch.setattr(rfs, "_query_region_hazards", query)

    result = rfs.query_region_weather_risks_core("蓟州未来七天可能有哪些风险？", now=FIXED_NOW)

    assert captured["times"] == ["20260828080000", "20260829080000", "20260830080000"]
    assert result["risk_window"]["forecast_days"] == 3
    assert result["risk_window"]["forecast_end_time"] == "2026-08-31 08:00"


def test_supported_and_unsupported_geography_is_not_silently_narrowed(monkeypatch):
    monkeypatch.setattr(rfs, "_query_region_hazards", lambda *a, **k: pytest.fail("mixed unsupported scope must not query"))
    result = rfs.query_region_weather_risks_core("今天蓟州和雄安新区可能有哪些风险？", now=FIXED_NOW)
    assert result["status"] == "unsupported_region"
    assert "雄安新区" in result["unsupported_regions"]


def test_optional_regions_cannot_hide_contradictory_raw_scope(monkeypatch):
    monkeypatch.setattr(rfs, "_query_region_hazards", lambda *a, **k: pytest.fail("contradictory scope must not query"))
    result = rfs.query_region_weather_risks_core(
        "今天蓟州和雄安新区可能有哪些风险？", regions="蓟州", now=FIXED_NOW
    )
    assert result["status"] == "unsupported_region"


def test_multiple_supported_regions_are_all_retained(monkeypatch):
    calls = []
    monkeypatch.setattr(rfs, "_query_region_hazards", lambda *a, **k: calls.append(a[:2]) or hazard_payload(risk_levels={}))
    result = rfs.query_region_weather_risks_core("今天蓟州和宝坻可能有哪些风险？", now=FIXED_NOW)
    assert result["status"] == "ok"
    assert [entry["region"] for entry in result["regions"]] == ["蓟州", "宝坻"]
    assert len(calls) == 2


def _run_real_aggregator(monkeypatch, outcome):
    """Keep the real risk-lists aggregation path; replace only static/HTTP IO boundaries.

    新 risk-lists 接口为一次性聚合清单（township/industry/key_objects），无逐日周期
    语义；fcst_times 仅映射为首项 start_time。outcome: "empty"=三清单全空、
    "failure"=接口失败、"risk"=清单有记录（蓟州地灾）。
    """
    with rwt._region_levels_lock:
        rwt._region_levels_cache.clear()
    monkeypatch.setattr(rfs, "_region_hazard_queryer", lambda *a: {"status": "no_data", "categories": []})

    if outcome == "failure":
        def fetch(*a, **kw):
            raise RuntimeError("HTTP failure")
    elif outcome == "empty":
        def fetch(*a, **kw):
            return {"township_risks": [], "industry_risks": [], "key_objects": [], "call_advice": []}
    else:
        def fetch(*a, **kw):
            return {
                "township_risks": [{
                    "township": "蓟县官庄镇",
                    "risk_level": "中",
                    "main_risks": [{"hazard_type": "地灾", "count": 2}],
                }],
                "industry_risks": [],
                "key_objects": [],
                "call_advice": ["加强巡查"],
            }

    monkeypatch.setattr(rwt, "_fetch_risk_lists", fetch)
    monkeypatch.setattr(
        rfs, "_region_risk_level_queryer",
        lambda lon, lat, radius, times, **kwargs: rwt.query_region_risk_levels(
            lon, lat, radius, times, include_coverage=kwargs.get("include_coverage", False),
            region=kwargs.get("region", ""),
        ),
    )
    return rfs.query_region_weather_risks_core("蓟州未来三天可能有哪些风险？", now=FIXED_NOW)


def test_real_aggregation_empty_lists_is_no_risk(monkeypatch):
    """新接口可达且三清单全空 = 本次无风险（不是不可用）。"""
    result = _run_real_aggregator(monkeypatch, "empty")
    assert result["status"] == "ok"
    assert all(risk["risk_status"] == "no_risk" for risk in result["regions"][0]["risks"])


def test_real_aggregation_failure_is_unavailable_not_no_risk(monkeypatch):
    """接口失败绝不能报成"无风险"。"""
    result = _run_real_aggregator(monkeypatch, "failure")
    assert result["status"] == "risk_service_unavailable"
    assert all(risk["risk_status"] == "unavailable" for risk in result["regions"][0]["risks"])


def test_real_aggregation_records_reported_per_hazard(monkeypatch):
    """清单有记录按灾种分层：地灾有风险，其余灾种本次无风险。"""
    result = _run_real_aggregator(monkeypatch, "risk")
    assert result["status"] == "ok"
    assert _risk(result, "dzzh")["risk_status"] == "risk"
    assert _risk(result, "dzzh")["levels"] == {"三级": 2}
    assert _risk(result, "sh")["risk_status"] == "no_risk"
    assert _risk(result, "zxhl")["risk_status"] == "no_risk"


def test_non_tianjin_region_no_coverage_not_malformed(monkeypatch):
    """外埠城市（唐山）：risk_levels=="no_coverage" 哨兵 → 专门状态，绝不可报
    "本次无风险"（清单只覆盖天津，说无风险是误报），也不是数据异常。"""
    monkeypatch.setattr(
        rfs, "_query_region_hazards",
        lambda *a, **k: hazard_payload(risk_levels="no_coverage"),
    )

    result = rfs.query_region_weather_risks_core("今天唐山可能有哪些风险？", now=FIXED_NOW)

    dzzh = _risk(result, "dzzh")
    assert dzzh["risk_status"] == "unavailable"
    assert dzzh["unavailable_reason"] == "risk_no_coverage"
    assert dzzh["unavailable_reason"] != "malformed_risk_payload"
    # 业务文案不得出现"无风险"
    assert "无风险" not in dzzh["status_text"]
