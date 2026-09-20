# -*- coding: utf-8 -*-
"""请求级锚点解析（前端 metadata 契约）测试。纯函数，不依赖内网。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from utils import reference_time, time_source

_CN = timezone(timedelta(hours=8))


@pytest.fixture()
def sim_file(tmp_path, monkeypatch):
    f = tmp_path / "sim.json"
    monkeypatch.setenv("SIM_TIME_FILE", str(f))
    time_source._invalidate()
    yield f
    time_source._invalidate()


def test_fixed_from_metadata():
    kind, value = reference_time.resolve_reference_time(
        {"time_mode": "fixed", "reference_time": "2026-07-10T15:00:00+08:00"}
    )
    assert kind == "fixed"
    assert value == datetime(2026, 7, 10, 15, 0, 0, tzinfo=_CN)


def test_fixed_from_top_level():
    """顶层与 metadata 两种放法都认（前端选哪个都行）。"""
    kind, value = reference_time.resolve_reference_time(
        None, top_level={"reference_time": "2026-07-10 15:00"}
    )
    assert kind == "fixed"
    assert value == datetime(2026, 7, 10, 15, 0, 0, tzinfo=_CN)


def test_metadata_wins_over_top_level():
    kind, value = reference_time.resolve_reference_time(
        {"reference_time": "2026-07-10T15:00:00+08:00"},
        top_level={"reference_time": "2020-01-01T00:00:00+08:00"},
    )
    assert value == datetime(2026, 7, 10, 15, 0, 0, tzinfo=_CN)


@pytest.mark.parametrize("mode", ["real", "REAL", "Real", "dynamic", "live"])
def test_real_modes(mode):
    kind, value = reference_time.resolve_reference_time(
        {"time_mode": mode, "reference_time": "2026-07-10T15:00:00+08:00"}
    )
    assert (kind, value) == ("real", None)


def test_empty_is_inherit():
    assert reference_time.resolve_reference_time(None) == ("inherit", None)
    assert reference_time.resolve_reference_time({}) == ("inherit", None)


def test_fixed_mode_without_reference_time_raises():
    with pytest.raises(reference_time.InvalidReferenceTime):
        reference_time.resolve_reference_time({"time_mode": "fixed"})


def test_unparsable_reference_time_raises():
    with pytest.raises(reference_time.InvalidReferenceTime):
        reference_time.resolve_reference_time({"reference_time": "昨天下午"})


def test_csv_and_date_only_forms():
    assert reference_time.resolve_reference_time(
        {"reference_time": "2026-07-10"})[1] == datetime(2026, 7, 10, 0, 0, tzinfo=_CN)
    assert reference_time.resolve_reference_time(
        {"reference_time": "2026-07-10 15:00:30"})[1] == datetime(2026, 7, 10, 15, 0, 30, tzinfo=_CN)


def test_naive_input_treated_as_beijing():
    kind, value = reference_time.resolve_reference_time({"reference_time": "2026-07-10T15:00:00"})
    assert value.utcoffset() == timedelta(hours=8)


def test_resolve_day():
    kind, value = reference_time.resolve_reference_time(
        {"reference_time": "2026-07-10T15:00:00+08:00"})
    assert reference_time.resolve_day(kind, value, fallback_day="1999-01-01") == "2026-07-10"
    assert reference_time.resolve_day("real", None, fallback_day="1999-01-01") == \
        datetime.now(_CN).strftime("%Y-%m-%d")
    assert reference_time.resolve_day("inherit", None, fallback_day="1999-01-01") == "1999-01-01"


def test_anchor_cache_key(sim_file):
    kind, value = reference_time.resolve_reference_time(
        {"reference_time": "2026-07-10T15:00:00+08:00"})
    assert reference_time.anchor_cache_key(kind, value) == "2026-07-10T15:00:00+08:00"
    assert reference_time.anchor_cache_key("real", None) == "real"
    assert reference_time.anchor_cache_key("inherit", None) is None
    # inherit 必须反映全局文件的当前值：否则全局锚点变了缓存键却没变，会返回陈旧答案。
    time_source.set_override_from_text("2026-07-10 15:00:00")
    assert reference_time.anchor_cache_key("inherit", None) == "2026-07-10T15:00:00+08:00"
    time_source.clear_override()


def test_request_anchor_context_manager(sim_file):
    with reference_time.request_anchor("fixed", datetime(2026, 7, 10, 15, 0, tzinfo=_CN)):
        assert time_source.now() == datetime(2026, 7, 10, 15, 0, 0)
        assert time_source.request_override_header_value() == "2026-07-10T15:00:00+08:00"
    assert time_source.now() != datetime(2026, 7, 10, 15, 0, 0)

    with reference_time.request_anchor("real", None):
        assert time_source.request_override_header_value() == "real"

    # inherit 是空操作：不设置锚点，自然回落全局文件。
    with reference_time.request_anchor("inherit", None):
        assert time_source.request_override_header_value() is None
    assert time_source.request_override_header_value() is None


def test_request_anchor_is_exception_safe(sim_file):
    """业务异常穿过 with 时锚点必须复位，否则会泄漏到后续请求。"""
    with pytest.raises(RuntimeError):
        with reference_time.request_anchor("fixed", datetime(2026, 7, 10, 15, 0, tzinfo=_CN)):
            raise RuntimeError("boom")
    assert time_source.request_override_header_value() is None


def test_message_metadata_shapes():
    """WS 侧 message.metadata 与 HTTP 侧 metadata 用同一套解析（含 None/无关字段容错）。"""
    assert reference_time.resolve_reference_time(None) == ("inherit", None)
    assert reference_time.resolve_reference_time({"location": "http://x/y"}) == ("inherit", None)
    kind, value = reference_time.resolve_reference_time(
        {"location": "http://x/y", "time_mode": "fixed",
         "reference_time": "2026-07-10T15:00:00+08:00"}
    )
    assert kind == "fixed"
    assert value == datetime(2026, 7, 10, 15, 0, 0, tzinfo=_CN)
