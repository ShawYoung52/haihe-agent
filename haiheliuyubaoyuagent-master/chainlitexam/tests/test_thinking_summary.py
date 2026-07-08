"""Tests for the business summary prefix builder."""

import sys
from pathlib import Path

# Make ``import chainlitexam`` work when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Install minimal stubs for optional dependencies.
from chainlitexam.tests.stubs import ensure_stubs

ensure_stubs()

from chainlitexam.message_orchestrator import _build_thinking_summary


def test_rainfall_distribution_summary():
    result = _build_thinking_summary("海河流域降雨分布图")
    assert isinstance(result, str)
    assert result.startswith("已生成海河流域降水实况分布图")


def test_warning_summary():
    result = _build_thinking_summary("天津有哪些气象预警")
    assert isinstance(result, str)
    assert "预警" in result


def test_river_network_summary():
    result = _build_thinking_summary("海河流域河网水系情况")
    assert isinstance(result, str)
    assert "河网" in result


def test_water_level_summary():
    result = _build_thinking_summary("天津水位情况")
    assert isinstance(result, str)
    assert "水位" in result


def test_emergency_response_summary():
    result = _build_thinking_summary("防汛应急响应启动了吗")
    assert isinstance(result, str)
    assert "防汛" in result or "应急" in result


def test_basin_areal_rainfall_summary():
    result = _build_thinking_summary("各子流域面雨量对比")
    assert isinstance(result, str)
    assert "面雨量" in result


def test_city_avg_rainfall_summary():
    result = _build_thinking_summary("全市平均降雨量")
    assert isinstance(result, str)
    assert "平均" in result or "城市" in result


def test_rain_duration_summary():
    result = _build_thinking_summary("降雨时长统计")
    assert isinstance(result, str)
    assert "时长" in result


def test_history_or_extreme_summary():
    """Queries without a specific intent branch fall back to the generic prefix."""
    result = _build_thinking_summary("历史极端降雨事件")
    assert isinstance(result, str)
    assert result.startswith("已理解您的问题，为您解答如下：")


def test_forecast_summary():
    result = _build_thinking_summary("未来三天降雨如何")
    assert isinstance(result, str)
    assert "预报" in result


def test_today_summary():
    result = _build_thinking_summary("今天天津降水情况")
    assert isinstance(result, str)
    assert "实况" in result


def test_empty_query():
    assert _build_thinking_summary("") == ""


def test_has_chart():
    result = _build_thinking_summary("未来三天降雨如何", has_chart=True)
    assert "并生成相关图表" in result


if __name__ == "__main__":
    test_rainfall_distribution_summary()
    test_warning_summary()
    test_river_network_summary()
    test_water_level_summary()
    test_emergency_response_summary()
    test_basin_areal_rainfall_summary()
    test_city_avg_rainfall_summary()
    test_rain_duration_summary()
    test_history_or_extreme_summary()
    test_forecast_summary()
    test_today_summary()
    test_empty_query()
    test_has_chart()
    print("All tests passed.")
