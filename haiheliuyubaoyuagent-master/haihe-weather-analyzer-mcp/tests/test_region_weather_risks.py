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
    monkeypatch.setattr(rfs, "_query_region_hazards", lambda lon, lat, times: {
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


def test_empty_reachable_levels_are_no_risk(monkeypatch):
    monkeypatch.setattr(rfs, "_query_region_hazards", lambda *a: hazard_payload(risk_levels={}))

    result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)

    assert all(item["risk_status"] == "no_risk" for item in result["regions"][0]["risks"])


def test_no_data_is_unavailable_with_distinct_reason(monkeypatch):
    monkeypatch.setattr(
        rfs,
        "_query_region_hazards",
        lambda *a: hazard_payload(risk_levels={"dzzh": "no_data"}),
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
        lambda *a: hazard_payload(risk_levels={"dzzh": {"levels": ["三级"]}}),
    )

    result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)

    assert _risk(result, "dzzh")["risk_status"] == "unavailable"
    assert _risk(result, "dzzh")["unavailable_reason"] == "malformed_risk_payload"


def test_partial_failure_is_reported_per_hazard(monkeypatch):
    monkeypatch.setattr(
        rfs,
        "_query_region_hazards",
        lambda *a: hazard_payload(risk_levels={"dzzh": None, "sh": {"levels": {"四级": 1}, "total": 1}}),
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
        lambda *a: hazard_payload(risk_levels=None, risk_levels_available=False),
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
            lambda *a: hazard_payload(risk_levels={"dzzh": "no_data"}),
        )
        result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)
        dzzh = _risk(result, "dzzh")
        assert dzzh["risk_status"] == "unavailable"
        assert dzzh["unavailable_reason"] == "risk_forecast_no_data"
        assert dzzh["status_text"] == "暂无风险预报资料"

    def test_kind_failure_status_text(self, monkeypatch):
        monkeypatch.setattr(
            rfs, "_query_region_hazards",
            lambda *a: hazard_payload(risk_levels={"dzzh": None, "sh": {"levels": {"四级": 1}, "total": 1}}),
        )
        result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)
        assert _risk(result, "dzzh")["status_text"] == "风险数据查询暂时不可用"
        assert _risk(result, "sh")["status_text"] == "有风险"
        assert _risk(result, "zxhl")["status_text"] == "无风险"

    def test_service_unavailable_status_text(self, monkeypatch):
        monkeypatch.setattr(
            rfs, "_query_region_hazards",
            lambda *a: hazard_payload(risk_levels=None, risk_levels_available=False),
        )
        result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)
        assert all(
            item["status_text"] == "风险数据查询暂时不可用"
            for item in result["regions"][0]["risks"]
        )

    def test_status_text_never_technical_jargon(self, monkeypatch):
        monkeypatch.setattr(
            rfs, "_query_region_hazards",
            lambda *a: hazard_payload(risk_levels={"dzzh": "no_data", "sh": None}),
        )
        result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)
        for item in result["regions"][0]["risks"]:
            assert "接口" not in item["status_text"]
            assert "预报数据" not in item["status_text"]


def test_static_hazard_failure_keeps_hidden_counts_unknown(monkeypatch):
    monkeypatch.setattr(
        rfs,
        "_query_region_hazards",
        lambda *a: {
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
    monkeypatch.setattr(rfs, "_query_region_hazards", lambda *a: hazard_payload(risk_levels={}))

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
        lambda *a: hazard_payload(risk_levels={"dzzh": {"levels": {"三级": invalid_count}}}),
    )

    result = rfs.query_region_weather_risks_core("今天蓟州可能有哪些风险？", now=FIXED_NOW)

    assert _risk(result, "dzzh")["risk_status"] == "unavailable"
    assert _risk(result, "dzzh")["unavailable_reason"] == "malformed_risk_payload"


@pytest.mark.parametrize("invalid_count", [-1, True, "1.5"])
def test_invalid_static_counts_are_unknown(monkeypatch, invalid_count):
    payload = hazard_payload(risk_levels={})
    payload["categories"] = [{"key": "dzzh", "label": "地质灾害", "count": invalid_count}]
    monkeypatch.setattr(rfs, "_query_region_hazards", lambda *a: payload)

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


def _run_real_aggregator(monkeypatch, outcomes):
    """Keep the real three-hazard aggregator; replace only static/HTTP IO boundaries."""
    with rwt._region_levels_lock:
        rwt._region_levels_cache.clear()
    monkeypatch.setattr(rfs, "_region_hazard_queryer", lambda *a: {"status": "no_data", "categories": []})

    def fetch(kind, extra, **kwargs):
        outcome = outcomes[kind][extra["fcstTime"]]
        if outcome == "missing":
            raise rwt.RiskInterfaceNoDataError("missing cycle")
        if outcome == "failure":
            raise RuntimeError("HTTP failure")
        if outcome == "empty":
            return {"data": []}
        return {"data": [{"level": "三级", "longitude": 117.45, "latitude": 40.05}]}

    monkeypatch.setattr(rwt, "_fetch_risk_warning", fetch)
    monkeypatch.setattr(
        rfs, "_region_risk_level_queryer",
        lambda lon, lat, radius, times, **kwargs: rwt.query_region_risk_levels(
            lon, lat, radius, times, include_coverage=kwargs.get("include_coverage", False)
        ),
    )
    return rfs.query_region_weather_risks_core("蓟州未来三天可能有哪些风险？", now=FIXED_NOW)


def _all_kind_outcomes(sequence):
    times = ["20260828080000", "20260829080000", "20260830080000"]
    return {kind: dict(zip(times, sequence)) for kind in ("geologic", "mountain", "river")}


def test_real_aggregation_full_empty_is_full_window_no_risk(monkeypatch):
    result = _run_real_aggregator(monkeypatch, _all_kind_outcomes(["empty", "empty", "empty"]))
    assert result["status"] == "ok"
    assert result["risk_window"]["coverage_status"] == "complete"
    assert all(risk["risk_status"] == "no_risk" for risk in result["regions"][0]["risks"])


@pytest.mark.parametrize("sequence", [
    ["empty", "missing", "missing"],
    ["risk", "missing", "missing"],
    ["risk", "failure", "empty"],
])
def test_real_aggregation_partial_cycles_never_claim_full_window_no_risk(monkeypatch, sequence):
    result = _run_real_aggregator(monkeypatch, _all_kind_outcomes(sequence))
    assert result["status"] == "partial"
    assert result["risk_window"]["coverage_status"] == "partial"
    assert result["risk_window"]["forecast_start_time"] is None
    assert not all(risk["risk_status"] == "no_risk" for risk in result["regions"][0]["risks"])
    if sequence[0] == "risk":
        assert all(risk["risk_status"] == "risk" for risk in result["regions"][0]["risks"])


def test_real_aggregation_fully_missing_is_unavailable(monkeypatch):
    result = _run_real_aggregator(monkeypatch, _all_kind_outcomes(["missing", "missing", "missing"]))
    assert result["status"] == "risk_service_unavailable"
    assert result["risk_window"]["coverage_status"] == "unavailable"
    assert all(risk["risk_status"] == "unavailable" for risk in result["regions"][0]["risks"])
