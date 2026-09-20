# -*- coding: utf-8 -*-
"""锚点感知的缓存键：请求级锚点下窗口逐请求不同，键必须跟着变。

回归的缺陷：这些缓存键原先只含"粗时间维度"或干脆只含入参，隐含假设是
`time_source.now()` 为进程级常量。请求级锚点打破了这个前提——同一年内两个不同
锚点的用户会互相命中，拿到对方窗口的 payload（连 readable 都是对方的日期）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import time_source  # noqa: E402


@pytest.fixture()
def sim_file(tmp_path, monkeypatch):
    f = tmp_path / "sim.json"
    monkeypatch.setenv("SIM_TIME_FILE", str(f))
    time_source._invalidate()
    yield f
    time_source._invalidate()


@pytest.fixture()
def anchored():
    def _run(iso, fn):
        token = time_source.set_request_override(iso)
        try:
            return fn()
        finally:
            time_source.reset_request_override(token)

    return _run


def test_year_to_date_key_follows_anchor(anchored):
    """今年以来面雨量：键原先只含年初起点，同一年不同锚点会共用。"""
    from custom_tools.year_to_date_areal_rainfall_tool import _ytd_cache_key

    a = anchored("2026-07-10T15:00:00+08:00", lambda: _ytd_cache_key("9"))
    b = anchored("2026-09-20T15:00:00+08:00", lambda: _ytd_cache_key("9"))
    assert a != b, "同一年不同锚点不能共用缓存键"
    assert a.startswith("9|20260101000000|"), f"键应含年初起点，实际 {a}"
    assert a.endswith("20260710150000"), f"键应含锚定的窗口末端，实际 {a}"


def test_year_to_date_key_distinguishes_zone(anchored):
    from custom_tools.year_to_date_areal_rainfall_tool import _ytd_cache_key

    iso = "2026-07-10T15:00:00+08:00"
    assert anchored(iso, lambda: _ytd_cache_key("9")) != \
        anchored(iso, lambda: _ytd_cache_key("11"))


def test_historical_same_period_key_follows_anchor(anchored):
    """历史同期：planner 常不传窗口，键原先恒为 "None|None|10"。"""
    from custom_tools.historical_same_period_rainfall_tool import _hsp_cache_key

    a = anchored("2026-07-10T15:00:00+08:00", lambda: _hsp_cache_key(None, None, 10))
    b = anchored("2026-09-20T15:00:00+08:00", lambda: _hsp_cache_key(None, None, 10))
    assert a != b, "都没传窗口时也必须按锚点区分"
    assert "None" not in a, f"键里不该残留原始入参，实际 {a}"


def test_historical_same_period_key_stable_for_same_anchor(anchored):
    from custom_tools.historical_same_period_rainfall_tool import _hsp_cache_key

    iso = "2026-07-10T15:00:00+08:00"
    assert anchored(iso, lambda: _hsp_cache_key(None, None, 10)) == \
        anchored(iso, lambda: _hsp_cache_key(None, None, 10)), "同锚点必须命中同一键"


def test_historical_same_period_key_respects_explicit_window(anchored):
    """显式窗口与锚点无关，必须继续复用同一键（别把缓存改废）。"""
    from custom_tools.historical_same_period_rainfall_tool import _hsp_cache_key

    args = ("20260101000000", "20260102000000", 10)
    assert anchored("2026-07-10T15:00:00+08:00", lambda: _hsp_cache_key(*args)) == \
        anchored("2026-09-20T15:00:00+08:00", lambda: _hsp_cache_key(*args))


def test_historical_same_period_key_respects_years(anchored):
    from custom_tools.historical_same_period_rainfall_tool import _hsp_cache_key

    iso = "2026-07-10T15:00:00+08:00"
    assert anchored(iso, lambda: _hsp_cache_key(None, None, 5)) != \
        anchored(iso, lambda: _hsp_cache_key(None, None, 10))
