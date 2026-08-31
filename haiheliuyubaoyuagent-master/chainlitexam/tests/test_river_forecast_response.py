"""统一河流降雨预报确定性答案组装（tools/river_forecast_response.py）测试。

2026-08-31 用户口径：河流预报回答"太简单"。确定性组装 核心结论+逐时段降雨表+数据来源，
零编造（只引用工具返回的降雨字段与 data_source）。
"""
from __future__ import annotations

import sys
from pathlib import Path

CHAINLIT_DIR = Path(__file__).resolve().parents[1]
if str(CHAINLIT_DIR) not in sys.path:
    sys.path.insert(0, str(CHAINLIT_DIR))

from tools.river_forecast_response import build_river_forecast_answer  # noqa: E402


def _period(label, has_rain, avg, mx, source="滚动预报网格"):
    return {
        "label": label,
        "has_rain": has_rain,
        "average_rainfall_mm": avg,
        "max_rainfall_mm": mx,
        "min_rainfall_mm": 0.0,
        "data_source": source,
        "status": "ok",
        "valid_count": 12,
    }


def _result(periods, river="泃河", scope="泃河河道两侧约5公里沿线范围", status="ok"):
    return {
        "status": status,
        "river_name": river,
        "scope_type": "river_corridor",
        "scope_description": scope,
        "periods": periods,
    }


def test_single_day_no_rain():
    out = build_river_forecast_answer("明天泃河有雨吗？", _result([_period("明天", False, 0.0, 0.0)]))
    assert "【核心结论】" in out
    assert "预计明天泃河河道两侧约5公里沿线范围无明显降雨。" in out
    assert "【逐时段降雨预报】" in out
    assert "| 明天 | 0.0 | 0.0 | 无明显降雨 |" in out
    assert "数据来源：滚动预报网格。" in out


def test_single_period_with_rain_reports_max():
    out = build_river_forecast_answer("明天泃河有雨吗？", _result([_period("明天", True, 2.4, 6.0)]))
    assert "有降雨，时段最大雨量约 6.0 毫米" in out
    assert "| 明天 | 2.4 | 6.0 | 有降雨 |" in out


def test_multi_period_mixed_rain_breaks_down():
    periods = [_period("9月1日", True, 2.4, 6.0), _period("9月2日", False, 0.0, 0.0)]
    out = build_river_forecast_answer("未来两天泃河有雨吗？", _result(periods))
    assert "9月1日有降雨" in out
    assert "其余时段无明显降雨" in out
    assert "| 9月2日 | 0.0 | 0.0 | 无明显降雨 |" in out


def test_scope_not_duplicated_when_already_has_river_name():
    out = build_river_forecast_answer("明天泃河有雨吗？", _result([_period("明天", False, 0.0, 0.0)]))
    # scope_description 已含"泃河"，结论不应出现"泃河泃河"
    assert "泃河泃河" not in out


def test_no_coverage_period_shows_insufficient_data():
    period = _period("明天", None, None, None)
    period["status"] = "no_coverage"
    out = build_river_forecast_answer("明天泃河有雨吗？", _result([period]))
    assert "暂缺有效降雨预报资料" in out
    assert "| 明天 | — | — | 资料不足 |" in out


def test_non_ok_status_returns_none():
    assert build_river_forecast_answer("明天泃河有雨吗？", _result([], status="river_not_found")) is None
    assert build_river_forecast_answer("明天泃河有雨吗？", {"status": "ok", "periods": []}) is None
    assert build_river_forecast_answer("明天泃河有雨吗？", "not-a-dict") is None


def test_nan_and_garbage_rainfall_render_dash():
    period = _period("明天", True, float("nan"), "abc")
    out = build_river_forecast_answer("明天泃河有雨吗？", _result([period]))
    assert "| 明天 | — | — | 有降雨 |" in out
