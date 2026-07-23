"""fixed_rainfall_impact_tool 传播时间透传测试（无需数据库/pkl/网络）。"""
from __future__ import annotations

import pytest

import fixed_rainfall_impact_tool as frit

_PROPAGATION = {
    "flow_velocity_mps": 2.0,
    "rivers": [
        {
            "river_name": "滦河",
            "propagation_distance_km": 48.2,
            "propagation_time_hours": 6.7,
            "arrival_estimate_readable": "约6.7小时",
        }
    ],
}


def _builder_result(**overrides):
    result = {
        "segments": [],
        "river_geojson": None,
        "downstream_start_stats": {},
        "affected_rivers": ["滦河"],
        "impact_stations": [],
        "river_propagation": _PROPAGATION,
    }
    result.update(overrides)
    return result


def test_resolve_flow_velocity_defaults_and_rejects_negative():
    assert frit._resolve_flow_velocity(0) == 2.0
    assert frit._resolve_flow_velocity(0.0) == 2.0
    assert frit._resolve_flow_velocity(None) == 2.0
    assert frit._resolve_flow_velocity(3.0) == 3.0
    with pytest.raises(ValueError):
        frit._resolve_flow_velocity(-1)
    with pytest.raises(ValueError):
        frit._resolve_flow_velocity(float("nan"))


def test_empty_response_carries_empty_propagation_block():
    resp = frit._empty_response({"time_range_readable": "t"}, 50.0, set(), set(), 10.0)
    assert resp["river_propagation"] == {"flow_velocity_mps": 2.0, "rivers": []}


def test_format_mcp_response_passthrough_propagation():
    resp = frit._format_mcp_response(_builder_result(), {"time_range_readable": "t"}, 50.0, set(), set())
    assert resp["river_propagation"]["rivers"][0]["propagation_time_hours"] == 6.7


def test_format_mcp_response_fills_default_block_when_core_lacks_field():
    result = _builder_result()
    del result["river_propagation"]
    resp = frit._format_mcp_response(result, {"time_range_readable": "t"}, 50.0, set(), set())
    assert resp["river_propagation"] == {"flow_velocity_mps": 2.0, "rivers": []}


def test_build_result_forwards_velocity_to_builder(monkeypatch):
    captured = {}

    def fake_builder(stations, **kwargs):
        captured.update(kwargs)
        return _builder_result(
            river_propagation={"flow_velocity_mps": kwargs["flow_velocity_mps"], "rivers": []}
        )

    monkeypatch.setattr(frit, "_load_impact_builder", lambda: fake_builder)
    rainfall_result = {
        "time_range_readable": "t",
        "level_analysis": [
            {"level": "暴雨", "stations": [{"name": "s1", "lon": 117.0, "lat": 39.0, "rainfall": 80.0}]}
        ],
    }
    frit.build_affected_river_network_result(
        time_str="20260723080000",
        start_time="",
        end_time="",
        rainfall_threshold_mm=50.0,
        max_edges=100,
        include_background=True,
        downstream_km=50.0,
        direct_graph_match_km=10.0,
        pg_conf={},
        analyze_rainfall_core=lambda *a, **k: rainfall_result,
        rain_levels=[("暴雨", 50.0, 99.9)],
        graph_path=None,
        flow_velocity_mps=3.0,
    )
    assert captured["flow_velocity_mps"] == 3.0


def test_build_result_zero_velocity_uses_default(monkeypatch):
    captured = {}

    def fake_builder(stations, **kwargs):
        captured.update(kwargs)
        return _builder_result()

    monkeypatch.setattr(frit, "_load_impact_builder", lambda: fake_builder)
    rainfall_result = {
        "time_range_readable": "t",
        "level_analysis": [
            {"level": "暴雨", "stations": [{"name": "s1", "lon": 117.0, "lat": 39.0, "rainfall": 80.0}]}
        ],
    }
    frit.build_affected_river_network_result(
        time_str="20260723080000",
        start_time="",
        end_time="",
        rainfall_threshold_mm=50.0,
        max_edges=100,
        include_background=True,
        downstream_km=50.0,
        direct_graph_match_km=10.0,
        pg_conf={},
        analyze_rainfall_core=lambda *a, **k: rainfall_result,
        rain_levels=[("暴雨", 50.0, 99.9)],
        graph_path=None,
    )
    assert captured["flow_velocity_mps"] == 2.0
