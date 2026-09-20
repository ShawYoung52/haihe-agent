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
    pytest.importorskip("chainlit.emitter", reason="qa_http_api requires the real Chainlit package")
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


# ---------------------------------------------------------------------------
# 请求级锚点（2026-09-20）：把"现在"从进程级标量改成请求级值。
# ---------------------------------------------------------------------------


def test_request_override_beats_global_file(sim_file):
    """请求级锚点优先级高于全局覆盖文件。"""
    set_override_from_text("2026-07-10 15:00:00")
    token = time_source.set_request_override("2026-03-05T08:30:00+08:00")
    try:
        assert now() == datetime(2026, 3, 5, 8, 30, 0)
        assert override_date_str() == "2026-03-05"
    finally:
        time_source.reset_request_override(token)
    assert now() == datetime(2026, 7, 10, 15, 0, 0)


def test_request_override_real_sentinel_shadows_file(sim_file):
    """显式"真实时间"必须压过遗留的全局覆盖文件（修复"改回去了还是旧日期"）。"""
    set_override_from_text("2026-07-10 15:00:00")
    token = time_source.set_request_override("real")
    try:
        assert datetime.now() - now() < timedelta(seconds=5)
        assert override_date_str() == datetime.now().strftime("%Y-%m-%d")
    finally:
        time_source.reset_request_override(token)
    assert now() == datetime(2026, 7, 10, 15, 0, 0)


def test_request_override_reset_after_double_reset_is_safe(sim_file):
    """重复 reset 不能抛错——它跑在 finally 清理路径上。"""
    token = time_source.set_request_override("2026-03-05T08:30:00+08:00")
    time_source.reset_request_override(token)
    time_source.reset_request_override(token)  # 不应抛


def test_header_value_only_reflects_request_scope(sim_file):
    """interceptor 取值只看请求级：没设请求锚点时返回 None（不能把全局文件当 header 发出去）。"""
    set_override_from_text("2026-07-10 15:00:00")
    assert time_source.request_override_header_value() is None
    token = time_source.set_request_override("2026-03-05T08:30:00+08:00")
    try:
        assert time_source.request_override_header_value() == "2026-03-05T08:30:00+08:00"
    finally:
        time_source.reset_request_override(token)
    token = time_source.set_request_override("real")
    try:
        assert time_source.request_override_header_value() == "real"
    finally:
        time_source.reset_request_override(token)


def test_request_override_is_task_local(sim_file):
    """并发 Task 之间不串味（ContextVar 天然隔离）。"""
    import asyncio

    async def worker(text):
        token = time_source.set_request_override(text)
        try:
            await asyncio.sleep(0)
            return now()
        finally:
            time_source.reset_request_override(token)

    async def main():
        return await asyncio.gather(
            worker("2026-03-05T08:30:00+08:00"),
            worker("2026-09-09T20:00:00+08:00"),
        )

    a, b = asyncio.run(main())
    assert a == datetime(2026, 3, 5, 8, 30, 0)
    assert b == datetime(2026, 9, 9, 20, 0, 0)


def test_invalid_request_override_raises(sim_file):
    """显式给了错值应该报错，而不是静默按真实时间回答。"""
    with pytest.raises(ValueError):
        time_source.set_request_override("昨天下午")


def test_is_active_follows_request_anchor(sim_file):
    """is_active() 必须与 now() 走同一条优先级链，否则请求级锚点会被判成"未生效"。"""
    assert time_source.is_active() is False
    token = time_source.set_request_override("2026-03-05T08:30:00+08:00")
    try:
        assert time_source.is_active() is True
    finally:
        time_source.reset_request_override(token)
    assert time_source.is_active() is False

    # 显式"真实时间"不算生效中的锚点。
    token = time_source.set_request_override("real")
    try:
        assert time_source.is_active() is False
    finally:
        time_source.reset_request_override(token)


def test_get_override_stays_file_only(sim_file):
    """get_override() 只看全局兜底文件——GET /admin/system-time 与响应缓存键都靠它。"""
    token = time_source.set_request_override("2026-03-05T08:30:00+08:00")
    try:
        assert time_source.get_override() is None, "请求级锚点不该出现在全局兜底查询里"
        assert time_source.effective_override() is not None
    finally:
        time_source.reset_request_override(token)

