# -*- coding: utf-8 -*-
"""time_source 统一时间源单元测试（纯标准库，不依赖内网）。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import time_source


@pytest.fixture()
def sim_file(tmp_path, monkeypatch):
    f = tmp_path / "sim.json"
    monkeypatch.setenv("SIM_TIME_FILE", str(f))
    time_source._invalidate()
    yield f
    time_source._invalidate()


def test_no_file_returns_real_time(sim_file):
    assert time_source.get_override() is None
    assert time_source.is_active() is False
    before = datetime.now()
    got = time_source.now()
    after = datetime.now()
    assert before <= got <= after


def test_set_full_datetime(sim_file):
    data = time_source.set_override_from_text("2026-07-10 15:00:00")
    assert data["active"] is True
    assert data["display"] == "2026-07-10 15:00:00"
    ov = time_source.get_override()
    assert ov is not None and ov.year == 2026 and ov.month == 7 and ov.day == 10
    assert (ov.hour, ov.minute, ov.second) == (15, 0, 0)
    # naive now() == 锚定时刻
    assert time_source.now() == datetime(2026, 7, 10, 15, 0, 0)
    assert time_source.is_active() is True


def test_iso_and_T_separator(sim_file):
    time_source.set_override_from_text("2026-07-10T08:30")
    assert time_source.now() == datetime(2026, 7, 10, 8, 30, 0)


def test_date_only_uses_real_clock_time(sim_file):
    data = time_source.set_override_from_text("2026-07-10")
    ov = time_source.get_override()
    real = datetime.now()
    assert (ov.year, ov.month, ov.day) == (2026, 7, 10)
    # 时分取真实当前时刻（不取 00:00），保证"今天下午/14时"落在已发生时次
    assert (ov.hour, ov.minute) == (real.hour, real.minute)


def test_now_with_tz_converts(sim_file):
    time_source.set_override_from_text("2026-07-10 15:00:00")  # +08:00
    got_utc = time_source.now(timezone.utc)
    assert got_utc == datetime(2026, 7, 10, 7, 0, 0, tzinfo=timezone.utc)  # 15:00+08 = 07:00Z
    cn = timezone(timedelta(hours=8))
    assert time_source.now(cn).hour == 15


def test_override_date_str(sim_file):
    time_source.set_override_from_text("2026-07-10 15:00:00")
    assert time_source.override_date_str() == "2026-07-10"


def test_clear_restores_real(sim_file):
    time_source.set_override_from_text("2026-07-10 15:00:00")
    assert time_source.is_active() is True
    time_source.clear_override()
    assert time_source.get_override() is None
    assert time_source.is_active() is False
    before = datetime.now()
    assert before <= time_source.now() <= datetime.now()


def test_corrupt_file_treated_as_no_override(sim_file):
    sim_file.write_text("{ not json ", encoding="utf-8")
    time_source._invalidate()
    assert time_source.get_override() is None
    assert time_source.is_active() is False


def test_invalid_text_raises(sim_file):
    with pytest.raises(ValueError):
        time_source.set_override_from_text("不是时间")
    with pytest.raises(ValueError):
        time_source.set_override_from_text("")


def test_external_write_picked_up(sim_file):
    """模拟另一进程写入同一文件：invalidate 后 now() 读到新值（穿透进程边界）。"""
    payload = {"override_datetime": "2026-07-10T15:00:00+08:00", "mode": "fixed"}
    sim_file.write_text(json.dumps(payload), encoding="utf-8")
    time_source._invalidate()
    assert time_source.now() == datetime(2026, 7, 10, 15, 0, 0)


def test_z_suffix_iso(sim_file):
    time_source.set_override_from_text("2026-07-10T07:00:00Z")  # UTC
    # 07:00Z == 15:00+08
    assert time_source.now() == datetime(2026, 7, 10, 15, 0, 0)
