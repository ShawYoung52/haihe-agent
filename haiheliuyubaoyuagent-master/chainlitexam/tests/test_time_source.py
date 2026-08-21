# -*- coding: utf-8 -*-
"""chainlitexam 侧统一时间源与锚点测试（不依赖内网/不 import chain_gzt）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from utils import time_source
from utils.time_source import now, override_date_str, set_override_from_text, clear_override


@pytest.fixture()
def sim_file(tmp_path, monkeypatch):
    f = tmp_path / "sim.json"
    monkeypatch.setenv("SIM_TIME_FILE", str(f))
    time_source._invalidate()
    yield f
    time_source._invalidate()


def test_no_file_returns_real_time(sim_file):
    assert time_source.get_override() is None
    before = datetime.now()
    assert before <= now() <= datetime.now()


def test_set_and_restore(sim_file):
    set_override_from_text("2026-07-10 15:00:00")
    assert now() == datetime(2026, 7, 10, 15, 0, 0)
    assert override_date_str() == "2026-07-10"
    clear_override()
    assert time_source.get_override() is None
    assert override_date_str() == datetime.now().strftime("%Y-%m-%d")


def test_now_with_tz(sim_file):
    set_override_from_text("2026-07-10 15:00:00")  # +08
    assert now(timezone.utc) == datetime(2026, 7, 10, 7, 0, 0, tzinfo=timezone.utc)
    cn = timezone(timedelta(hours=8))
    assert now(cn) == datetime(2026, 7, 10, 15, 0, 0, tzinfo=cn)


def test_epoch_follows_override(sim_file):
    """HTTP 运行时 epoch 随覆盖日期变（system prompt 自动刷新）。"""
    import qa_http_api

    assert qa_http_api._runtime_epoch() == datetime.now().strftime("%Y-%m-%d")
    set_override_from_text("2026-07-10 15:00:00")
    assert qa_http_api._runtime_epoch() == "2026-07-10"
    clear_override()
    assert qa_http_api._runtime_epoch() == datetime.now().strftime("%Y-%m-%d")


def test_decision_now_bjt_flips(sim_file):
    """决策天气的北京时"现在"随覆盖翻转（_decision_target_dates 的锚定基准）。"""
    from tools.decision_weather_core import _decision_now_bjt

    real = _decision_now_bjt()
    assert real.tzinfo is not None
    set_override_from_text("2026-07-10 15:00:00")
    assert _decision_now_bjt() == datetime(2026, 7, 10, 15, 0, 0, tzinfo=real.tzinfo)
    clear_override()
    # 恢复后重新落在真实时间附近
    assert datetime.now() - _decision_now_bjt().replace(tzinfo=None) < timedelta(seconds=5)


def test_external_write_picked_up(sim_file):
    """模拟另一进程（MCP）写入同一文件后本进程读到（穿透进程边界）。"""
    import json

    sim_file.write_text(
        json.dumps({"override_datetime": "2026-07-10T15:00:00+08:00", "mode": "fixed"}),
        encoding="utf-8",
    )
    time_source._invalidate()
    assert now() == datetime(2026, 7, 10, 15, 0, 0)
