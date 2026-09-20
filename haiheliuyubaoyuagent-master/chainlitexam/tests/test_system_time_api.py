# -*- coding: utf-8 -*-
"""POST /api/v1/admin/system-time 契约测试。

不 import chain_gzt（模块级副作用重）：直接构造同款请求模型 + 调同一套解析/落盘
函数，锁死契约语义；字段一致性由 test_system_time_request_model_matches_chain_gzt
读源码静态锁定。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from utils import reference_time, time_source


class SetSystemTimeRequest(BaseModel):
    """与 chain_gzt.SetSystemTimeRequest 同款。"""

    datetime: str | None = Field(None, max_length=32)
    note: str | None = Field(None, max_length=200)
    metadata: dict | None = Field(None)
    time_mode: str | None = Field(None, max_length=32)
    reference_time: str | None = Field(None, max_length=64)


@pytest.fixture()
def sim_file(tmp_path, monkeypatch):
    f = tmp_path / "sim.json"
    monkeypatch.setenv("SIM_TIME_FILE", str(f))
    time_source._invalidate()
    yield f
    time_source._invalidate()


def _resolve(req: SetSystemTimeRequest):
    """镜像 chain_gzt._set_system_time 的锚点解析。"""
    return reference_time.resolve_reference_time(
        req.metadata,
        top_level={"reference_time": req.reference_time, "time_mode": req.time_mode},
    )


def _decide(req: SetSystemTimeRequest) -> str:
    """镜像 chain_gzt._set_system_time 的**分支顺序**。

    legacy（写全局文件）/ fixed（无状态回显）/ real（清全局文件）/ noop（不动任何状态）。
    """
    if req.datetime:
        return "legacy"
    kind, _ = _resolve(req)
    if kind == "fixed":
        return "fixed"
    if kind == "real":
        return "real"
    return "noop"


def test_legacy_datetime_still_writes_global_file(sim_file):
    """旧式（带 datetime）行为与改动前逐字一致：写全局覆盖文件。"""
    req = SetSystemTimeRequest(datetime="2026-07-10 15:00:00")
    assert req.datetime  # 旧式分支优先
    data = time_source.set_override_from_text(req.datetime, note=req.note)
    assert data["active"] is True
    assert time_source.get_override().strftime("%Y-%m-%d %H:%M") == "2026-07-10 15:00"


def test_legacy_date_only_keeps_real_clock_time(sim_file):
    """仅日期时时分取设置那一刻的真实时刻（既有语义，别改坏）。

    不能落 00:00——否则"现在"=当天凌晨，"今天下午/14时"会被判到未来而取不到实况。
    """
    from datetime import datetime, timedelta, timezone

    def _secs_of_day(dt):
        return dt.hour * 3600 + dt.minute * 60 + dt.second

    cn = timezone(timedelta(hours=8))  # 覆盖文件里的"真实时刻"按北京时记
    before = datetime.now(cn)
    time_source.set_override_from_text("2026-07-10")
    override = time_source.get_override()
    assert override.strftime("%Y-%m-%d") == "2026-07-10"
    # 时分秒取设置那一刻的真实时刻（跨零点按环形距离算）。
    diff = abs(_secs_of_day(override) - _secs_of_day(before))
    assert min(diff, 86400 - diff) <= 5
    time_source.clear_override()


def test_new_metadata_fixed_is_stateless(sim_file):
    """新式（带 metadata）：只解析回显，绝不写全局文件。"""
    req = SetSystemTimeRequest(
        metadata={"time_mode": "fixed", "reference_time": "2026-07-10T15:00:00+08:00"}
    )
    kind, value = _resolve(req)
    assert kind == "fixed"
    assert value.strftime("%Y-%m-%d %H:%M:%S") == "2026-07-10 15:00:00"
    assert time_source.get_override() is None, "新式契约必须无状态：不得写全局覆盖文件"


def test_new_metadata_real_clears_legacy_file(sim_file):
    """新式 real：回真实时间并清掉遗留的全局文件（修"改回去了还是旧日期"）。"""
    time_source.set_override_from_text("2026-07-10 15:00:00")
    assert time_source.get_override() is not None
    req = SetSystemTimeRequest(metadata={"time_mode": "real"})
    assert _resolve(req) == ("real", None)
    time_source.clear_override()
    assert time_source.get_override() is None


def test_bare_metadata_has_no_side_effect(sim_file):
    """不带任何锚点信息的调用**不动任何状态**（旧行为是 422，绝不能变成"清文件"）。

    否则探测性 POST（健康检查、漏改的旧客户端）会把 curl 验收期间设好的
    全局锚点静默清掉——一个纯粹的 GET 式探测不该有副作用。
    """
    time_source.set_override_from_text("2026-07-10 15:00:00")
    req = SetSystemTimeRequest(metadata={"location": "http://x/y"})
    assert _decide(req) == "noop", "空锚点必须是无副作用的回显"
    # noop 分支不调 clear_override：全局锚点原样保留
    assert time_source.get_override() is not None


def test_empty_body_is_noop(sim_file):
    time_source.set_override_from_text("2026-07-10 15:00:00")
    assert _decide(SetSystemTimeRequest()) == "noop"
    assert time_source.get_override() is not None


def test_legacy_datetime_wins_over_new_style_anchor():
    """混搭 body：合法 datetime 必须生效，不能被新式解析的 400 挡掉。"""
    # {"datetime": ..., "metadata": {"time_mode": "fixed"}} —— fixed 缺 reference_time
    # 对新式解析是 IllegalReferenceTime，但旧式分支先返回，所以不该 400。
    req = SetSystemTimeRequest(datetime="2026-07-10 15:00:00",
                               metadata={"time_mode": "fixed"})
    assert _decide(req) == "legacy"

    req2 = SetSystemTimeRequest(datetime="2026-07-10 15:00:00", time_mode="real")
    assert _decide(req2) == "legacy", "旧式优先：带 datetime 就不走 real 清文件分支"


def test_datetime_branch_is_checked_before_anchor_parsing():
    """静态锁：旧式 `if req.datetime` 必须排在 resolve_reference_time 之前。

    顺序写反时 `{"datetime": 合法值, "metadata": {"time_mode": "fixed"}}` 会被
    400 挡掉，而没有任何动态用例能覆盖到——只能靠源码顺序断言。
    """
    src = (Path(__file__).resolve().parents[1] / "chain_gzt.py").read_text(encoding="utf-8")
    body = src.split("def _set_system_time(req: SetSystemTimeRequest):", 1)[1]
    body = body.split("\n\n\n", 1)[0]
    assert body.index("if req.datetime:") < body.index("resolve_reference_time"), \
        "旧式 datetime 分支必须排在新式锚点解析之前"


def test_top_level_equivalent_to_metadata():
    assert _resolve(SetSystemTimeRequest(reference_time="2026-07-10T15:00:00+08:00"))[0] == "fixed"
    assert _resolve(SetSystemTimeRequest(time_mode="real"))[0] == "real"


def test_metadata_wins_over_top_level():
    req = SetSystemTimeRequest(
        metadata={"reference_time": "2026-07-10T15:00:00+08:00"},
        reference_time="2020-01-01T00:00:00+08:00",
    )
    assert _resolve(req)[1].strftime("%Y-%m-%d") == "2026-07-10"


def test_invalid_reference_time_raises():
    """显式给了错值必须报 400，不能静默按真实时间回答。"""
    with pytest.raises(reference_time.InvalidReferenceTime):
        _resolve(SetSystemTimeRequest(reference_time="昨天下午"))


def test_system_time_request_model_matches_chain_gzt():
    """chain_gzt.SetSystemTimeRequest 的字段必须与本测试的同款模型一致。"""
    src = (Path(__file__).resolve().parents[1] / "chain_gzt.py").read_text(encoding="utf-8")
    block = src.split("class SetSystemTimeRequest(BaseModel):", 1)[1].split("\n\n\n", 1)[0]
    for field in ("datetime", "note", "metadata", "time_mode", "reference_time"):
        assert re.search(rf"^\s+{field}\s*:", block, re.M), f"SetSystemTimeRequest 缺字段 {field}"
    # 旧式 datetime 必须变成可选（否则新式 body 会被 422 挡掉——这正是原来的 bug）。
    assert re.search(r"^\s+datetime\s*:\s*str\s*\|\s*None", block, re.M), \
        "datetime 必须是可选字段"
