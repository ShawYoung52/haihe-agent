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


_RAINFALL_RESULT = {
    "time_range_readable": "t",
    "level_analysis": [
        {"level": "暴雨", "stations": [{"name": "s1", "lon": 117.0, "lat": 39.0, "rainfall": 80.0}]}
    ],
}


def _run_build(monkeypatch, captured: dict, **kwargs):
    """以假 builder 执行 build_affected_river_network_result，捕获透传参数。"""

    def fake_builder(stations, **builder_kwargs):
        captured.update(builder_kwargs)
        return _builder_result(
            river_propagation={"flow_velocity_mps": builder_kwargs["flow_velocity_mps"], "rivers": []}
        )

    monkeypatch.setattr(frit, "_load_impact_builder", lambda: fake_builder)
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
        analyze_rainfall_core=lambda *a, **k: _RAINFALL_RESULT,
        rain_levels=[("暴雨", 50.0, 99.9)],
        graph_path=None,
        **kwargs,
    )


def test_build_result_forwards_velocity_to_builder(monkeypatch):
    captured: dict = {}
    _run_build(monkeypatch, captured, flow_velocity_mps=3.0)
    assert captured["flow_velocity_mps"] == 3.0


def test_build_result_zero_velocity_uses_default(monkeypatch):
    captured: dict = {}
    _run_build(monkeypatch, captured)
    assert captured["flow_velocity_mps"] == 2.0


def test_default_station_buffer_km_matches_traction_agent():
    """IMPACT_RULES["direct"] 应描述 20km 而非 30km。"""
    import fixed_rainfall_impact_tool as frit
    rules = frit.IMPACT_RULES
    direct_text = rules.get("direct", "")
    assert "20km" in direct_text, f"IMPACT_RULES.direct 应含 20km，实际: {direct_text}"
    assert "30km" not in direct_text, "IMPACT_RULES.direct 不应含 30km"


def test_empty_response_station_buffer_km_is_20():
    """_empty_response 的 station_buffer_km 应为 20.0。"""
    import fixed_rainfall_impact_tool as frit
    resp = frit._empty_response(
        rainfall_result={},
        threshold_mm=50.0,
        zones=set(),
        admins=set(),
    )
    start_stats = resp.get("start_stats", {})
    downstream_start_stats = start_stats.get("downstream_start_stats", {})
    assert downstream_start_stats.get("station_buffer_km") == 20.0, \
        f"station_buffer_km 应为 20.0，实际: {downstream_start_stats.get('station_buffer_km')}"


def test_derive_rain_end_time_from_time_range_readable():
    """从 time_range_readable（"至 YYYY-MM-DD HH:MM"）派生 ISO 结束时刻。"""
    import fixed_rainfall_impact_tool as frit
    result = {"time_range_readable": "2026-07-27 15:30 至 2026-07-28 07:30"}
    end = frit._derive_rain_end_time(result)
    assert end is not None
    assert "07:30" in str(end) or "07:30" in end


def test_derive_rain_end_time_returns_none_when_missing():
    """完全无时间字段时返回 None，不抛异常。"""
    import fixed_rainfall_impact_tool as frit
    assert frit._derive_rain_end_time({}) is None
    assert frit._derive_rain_end_time({"foo": "bar"}) is None
    assert frit._derive_rain_end_time(None) is None


def test_normalize_station_adds_rain_end_time():
    """_normalize_station 输出含 rain_end_time 字段。"""
    import fixed_rainfall_impact_tool as frit
    station = {"station_id": "A", "name": "站A", "lon": 117.0, "lat": 39.0, "rainfall": 60.0}
    result = frit._normalize_station(station, "暴雨", rain_end_time="2026-07-28T07:30:00Z")
    assert result.get("rain_end_time") == "2026-07-28T07:30:00Z"


def test_normalize_station_defaults_rain_end_time_to_none():
    """老调用不传 rain_end_time 时，输出 rain_end_time 为 None。"""
    import fixed_rainfall_impact_tool as frit
    station = {"station_id": "A", "name": "站A", "lon": 117.0, "lat": 39.0, "rainfall": 60.0}
    result = frit._normalize_station(station, "暴雨")  # 不传 rain_end_time
    assert result.get("rain_end_time") is None
