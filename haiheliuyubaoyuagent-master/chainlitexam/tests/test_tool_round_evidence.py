from __future__ import annotations

from chainlitexam.tests.stubs import ensure_stubs

ensure_stubs()

from tools.meteo_evidence import is_evidence_complete
from tools.tool_round_evidence import ToolRoundEvidence


def test_water_level_payload_is_complete_and_keeps_structured_value():
    evidence = ToolRoundEvidence()
    payload = {"records": [{"water_level_m": 3.2}], "count": 1}
    evidence.record("query_water_level", "ok", payload)
    assert evidence.items[0].payload is payload
    assert is_evidence_complete("water_level", evidence.items) is True


def test_rain_error_is_reported_and_not_complete():
    evidence = ToolRoundEvidence()
    evidence.record("query_basin_areal_rainfall", "error", {"error": "timeout"})
    assert evidence.has_errors_for("rain") is True
    assert is_evidence_complete("rain", evidence.items_for("rain")) is False


def test_current_requires_time_and_actual_records_or_statistics():
    evidence = ToolRoundEvidence()
    evidence.record(
        "query_current_weather_observation",
        "ok",
        {"observation_time_label": "2026-08-21 14:00", "record_counts": {"region": 8}},
    )
    assert is_evidence_complete("current", evidence.items_for("current")) is True

    missing = ToolRoundEvidence()
    missing.record(
        "query_current_weather_observation",
        "ok",
        {"observation_time_label": "2026-08-21 14:00", "record_counts": {}},
    )
    assert is_evidence_complete("current", missing.items) is False


def test_nonempty_areal_rainfall_rows_are_complete_but_error_row_is_not():
    ok = ToolRoundEvidence()
    ok.record(
        "query_basin_areal_rainfall",
        "ok",
        [{"zone_name": "大清河", "avg_rainfall_mm": 12.3}],
    )
    assert is_evidence_complete("rain", ok.items) is True

    bad = ToolRoundEvidence()
    bad.record("query_basin_areal_rainfall", "ok", [{"error": "面雨量无数据"}])
    assert is_evidence_complete("rain", bad.items) is False


def test_unknown_tool_is_not_assigned_to_a_safe_domain():
    evidence = ToolRoundEvidence()
    evidence.record("some_new_tool", "ok", {"records": [1]})
    assert evidence.items_for("unknown") == []
    assert evidence.has_errors_for("unknown") is False

