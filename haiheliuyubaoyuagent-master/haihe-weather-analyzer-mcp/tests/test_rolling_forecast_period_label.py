"""滚动预报 period_label 日级聚合标签测试（2026-09-04）。

甲方口径：「今天蓟州可能有哪些风险」等综合风险回答里，天气表"时段"格
显示成 09月04日-09月05日 跨天区间看不懂——日级（interval>=24h）聚合行
只标起始日（09月04日），与决策天气 _decision_period_label 同口径；
逐小时（interval=1）保留起止范围不变。
"""
from __future__ import annotations

import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import rolling_forecast_service as rfs  # noqa: E402

_REGION, _COORD = next(iter(rfs.ROLLING_FORECAST_COORDS.items()))


def _result_data(n: int) -> dict:
    return {
        _COORD: {
            "WEA": ["晴"] * n,
            "TP1H": [0.0] * n,
            "TMAX": [30.0] * n,
            "TMIN": [20.0] * n,
            "EDA": ["西风 1-2级"] * n,
            "VISMIN": [8.0] * n,
        }
    }


def _build(interval: int, fcst_time: str, count: int):
    return rfs.build_rolling_forecast_periods(
        result_data=_result_data(count),
        regions=[_REGION],
        fcst_time=fcst_time,
        start_period=0,
        interval=interval,
    )


def test_daily_interval_labels_start_date_only():
    """interval=24 且 00 时起报（整日窗口）：标签只写起始日，不写跨天区间。"""
    periods = _build(interval=24, fcst_time="20260904000000", count=2)
    assert [p["period_label"] for p in periods] == ["09月04日", "09月05日"]
    assert "-" not in periods[0]["period_label"]


def test_daily_interval_non_midnight_start_also_date_only():
    """interval=24 且 08 时起报：同样只标起始日（"09月04日"），不带 08时。"""
    periods = _build(interval=24, fcst_time="20260904080000", count=1)
    assert periods[0]["period_label"] == "09月04日"


def test_hourly_interval_keeps_range_label():
    """逐小时（interval=1）时段标签保留起止范围，不受影响。"""
    periods = _build(interval=1, fcst_time="20260904080000", count=2)
    assert periods[0]["period_label"] == "09月04日08时-09月04日09时"
    assert periods[1]["period_label"] == "09月04日09时-09月04日10时"


def test_hourly_midnight_boundary_label_unchanged():
    """逐小时跨午夜边界：00 时端点仍按既有口径省略"00时"（只显日期）。"""
    periods = _build(interval=1, fcst_time="20260904230000", count=2)
    assert periods[0]["period_label"] == "09月04日23时-09月05日"
