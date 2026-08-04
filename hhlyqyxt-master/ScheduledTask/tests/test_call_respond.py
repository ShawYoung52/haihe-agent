"""应急响应叫应功能单元测试（无需真实数据库）。"""
import json
import sys
import threading
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Models.QyCallRespondTask import QyCallRespondTask
from Models.QyCallRespondSendLog import QyCallRespondSendLog
from Models.QyEmergencyResponseMonitor import QyEmergencyResponseMonitor
from ScheduledTask import call_respond
from utils.wechat_send_file import send_file


def test_task_model_columns():
    t = QyCallRespondTask(
        emergency_monitor_id=1, response_level=2, datatime=None,
        impact_city="天津市", status="pending",
    )
    assert t.emergency_monitor_id == 1
    assert t.response_level == 2
    assert t.status == "pending"
    assert t.impact_city == "天津市"
    assert t.report_docx_path is None


def test_send_log_model_columns():
    log = QyCallRespondSendLog(task_id=1, target_group="天津市防汛值班群", status="success")
    assert log.task_id == 1
    assert log.target_group == "天津市防汛值班群"
    assert log.status == "success"


def test_load_group_config_missing_file_returns_empty(tmp_path):
    cfg = call_respond.load_group_config(str(tmp_path / "nope.json"))
    assert cfg == {"template": "", "groups": {}}


def test_load_group_config_reads_file(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "template": "T{level}",
        "groups": {"天津市": ["A群", "B群"]},
    }, ensure_ascii=False), encoding="utf-8")
    cfg = call_respond.load_group_config(str(p))
    assert cfg["template"] == "T{level}"
    assert cfg["groups"] == {"天津市": ["A群", "B群"]}


def test_render_template_replaces_placeholders():
    out = call_respond.render_template("【叫应】{city} 已启动 {level} 级", "天津市", 2)
    assert out == "【叫应】天津市 已启动 2 级"


def test_group_targets_splits_by_dunhao():
    cfg = {"groups": {"天津市": ["A群"], "廊坊市": ["B群", "C群"]}}
    out = call_respond.group_targets("天津市、廊坊市", cfg)
    assert out == ["A群", "B群", "C群"]


def test_group_targets_empty_config_returns_empty():
    assert call_respond.group_targets("天津市", {"groups": {}}) == []


def test_send_file_stub_returns_false():
    assert send_file("某群", "/tmp/r.docx", "话术") is False


class _FakeTask:
    def __init__(self, **kw):
        self.id = kw.get("id")
        self.emergency_monitor_id = kw.get("emergency_monitor_id")
        self.response_level = kw.get("response_level")
        self.datatime = kw.get("datatime")
        self.impact_city = kw.get("impact_city")
        self.status = kw.get("status")
        self.report_docx_path = kw.get("report_docx_path")
        self.report_pdf_path = kw.get("report_pdf_path")
        self.report_docx_url = kw.get("report_docx_url")
        self.report_pdf_url = kw.get("report_pdf_url")
        self.confirm_person = kw.get("confirm_person")
        self.confirm_time = kw.get("confirm_time")
        self.send_time = kw.get("send_time")
        self.create_time = kw.get("create_time")


class _FakeMonitor:
    """qy_emergency_response_monitor 假记录（含 report_docx_url/report_pdf_url）。"""

    def __init__(self, **kw):
        self.id = kw.get("id")
        self.response_level = kw.get("response_level")
        self.datatime = kw.get("datatime")
        self.report_docx_url = kw.get("report_docx_url")
        self.report_pdf_url = kw.get("report_pdf_url")


class _FakeSendLog:
    """qy_call_respond_send_log 假记录（逐群幂等判定用）。"""

    def __init__(self, **kw):
        self.id = kw.get("id")
        self.task_id = kw.get("task_id")
        self.target_group = kw.get("target_group")
        self.status = kw.get("status")
        self.detail = kw.get("detail")
        self.send_time = kw.get("send_time")


class _FakeQuery:
    def __init__(self, session, model):
        self._session = session
        self._model = model
        self._filters = []
        self._order = []

    def filter(self, *args):
        self._filters.append(args)
        return self

    def order_by(self, *args):
        self._order = args
        return self

    def limit(self, *args):
        return self

    def _matches(self, obj):
        """检查对象是否匹配当前所有 filter 条件（逐字段等值比较）。"""
        for group in self._filters:
            for expr in group:
                left = getattr(expr, "left", None)
                right = getattr(expr, "right", None)
                if left is None or right is None:
                    continue
                col = getattr(left, "key", None) or getattr(left, "attr", None)
                if col is None:
                    continue
                val = getattr(right, "value", None)
                if val is not None and getattr(obj, col, None) != val:
                    return False
        return True

    def _status_filter(self):
        """从 filter 中提取 status.in_([...]) 允许状态集合，无则返回 None。"""
        allowed = None
        for group in self._filters:
            for expr in group:
                left = getattr(expr, "left", None)
                right = getattr(expr, "right", None)
                if left is None or right is None:
                    continue
                col = getattr(left, "key", None) or getattr(left, "attr", None)
                if col != "status":
                    continue
                vals = getattr(right, "value", None)
                if vals is None:
                    vals = right  # in_() 右值为 tuple/list
                if isinstance(vals, (list, tuple)):
                    allowed = set(vals)
        return allowed

    def first(self):
        # QyCallRespondSendLog 查询按 filter 匹配 session.logs（逐群幂等判定）
        if self._model is QyCallRespondSendLog:
            for log in self._session.logs:
                if self._matches(log):
                    return log
            return None
        # 按模型区分：QyEmergencyResponseMonitor 查询走 monitor_obj/prev_level_obj，
        # 其余（QyCallRespondTask 等）从 all_rows 取任务。
        if self._model is QyEmergencyResponseMonitor:
            if self._session.monitor_obj is not None:
                return self._session.monitor_obj
            if self._session.prev_level_obj is not None:
                return self._session.prev_level_obj
            return self._session.all_rows[0] if self._session.all_rows else None
        return self._session.all_rows[0] if self._session.all_rows else None

    def all(self):
        # QyCallRespondTask 带 status 过滤时（retry_pending_sends 扫描）按状态过滤
        if self._model is QyCallRespondTask:
            allowed = self._status_filter()
            if allowed is not None:
                return [r for r in self._session.all_rows
                        if getattr(r, "status", None) in allowed]
        return self._session.all_rows


class _FakeSession:
    def __init__(self):
        self.added = []
        self.prev_level_obj = None
        self.monitor_obj = None
        self.all_rows = []
        self.logs = []

    def query(self, model):
        return _FakeQuery(self, model)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def refresh(self, obj):
        if obj.id is None:
            obj.id = 1


@pytest.fixture
def fake_session(monkeypatch):
    sess = _FakeSession()
    monkeypatch.setattr(call_respond, "Session", lambda: sess)
    return sess


def _rec(level=2, datatime="2026-08-04 10:00:00", rid=1, docx="http://x/a.docx"):
    """假应急响应记录。同时带 report_docx_url（on_tick 读）与
    report_docx_path（send_task 读），保证两处映射都被真实断言。"""
    return _FakeTask(
        id=rid, emergency_monitor_id=rid, response_level=level,
        datatime=datetime.strptime(datatime, "%Y-%m-%d %H:%M:%S"),
        impact_city="天津市", status="pending",
        report_docx_url=docx, report_docx_path=docx,
    )


def test_on_tick_creates_task_on_level_change(fake_session):
    fake_session.prev_level_obj = _rec(level=0)  # 前一条 0 级
    task = call_respond.on_tick(_rec(level=2), "天津市")
    assert task is not None
    assert task.status == "pending"
    assert len(fake_session.added) == 1


def test_on_tick_skips_when_same_level(fake_session):
    fake_session.prev_level_obj = _rec(level=2)  # 前一条同等级
    task = call_respond.on_tick(_rec(level=2), "天津市")
    assert task is None
    assert fake_session.added == []


def test_on_tick_skips_when_record_none(fake_session):
    assert call_respond.on_tick(None, "天津市") is None
    assert fake_session.added == []


def test_on_tick_skips_when_level_zero(fake_session):
    fake_session.prev_level_obj = _rec(level=0)
    task = call_respond.on_tick(_rec(level=0), "天津市")
    assert task is None
    assert fake_session.added == []


def test_confirm_task_requires_pending(fake_session, monkeypatch):
    fake_session.all_rows = [_rec(level=2, rid=7)]
    monkeypatch.setattr(call_respond, "send_task", lambda tid: None)
    result = call_respond.confirm_task(7, "张三")
    assert result["success"] is True
    assert result["status"] == "confirmed"


def test_send_task_suspended_when_no_report(fake_session, monkeypatch):
    monkeypatch.setattr(call_respond, "load_group_config", lambda: {"groups": {"天津市": ["A群"]}})
    task = _rec(level=2, rid=8, docx=None)  # 无报告
    fake_session.all_rows = [task]
    call_respond.send_task(8)
    assert task.status == "suspended"


def test_send_task_pending_send_when_no_group(fake_session, monkeypatch):
    monkeypatch.setattr(call_respond, "load_group_config", lambda: {"groups": {}})
    task = _rec(level=2, rid=9, docx="http://x/a.docx")
    fake_session.all_rows = [task]
    call_respond.send_task(9)
    assert task.status == "pending_send"


def test_send_task_sent_when_ok(fake_session, monkeypatch):
    monkeypatch.setattr(call_respond, "load_group_config", lambda: {"groups": {"天津市": ["A群"]}})
    monkeypatch.setattr(call_respond, "send_file", lambda g, f, c: True)
    monkeypatch.setattr(call_respond, "_download_report", lambda url: "/tmp/a.docx")
    task = _rec(level=2, rid=10, docx="http://x/a.docx")
    fake_session.all_rows = [task]
    fake_session.logs = []
    call_respond.send_task(10)
    assert task.status == "sent"


def test_send_task_sent_when_pdf_only(fake_session, monkeypatch):
    monkeypatch.setattr(call_respond, "load_group_config", lambda: {"groups": {"天津市": ["A群"]}})
    monkeypatch.setattr(call_respond, "send_file", lambda g, f, c: True)
    monkeypatch.setattr(call_respond, "_download_report", lambda url: "/tmp/a.pdf")
    task = _rec(level=2, rid=11, docx=None)  # 无 docx，仅 pdf
    task.report_pdf_path = "http://x/a.pdf"
    fake_session.all_rows = [task]
    fake_session.logs = []
    call_respond.send_task(11)
    assert task.status == "sent"


def test_on_tick_maps_report_url_to_task_path(fake_session):
    """C1/deferred：on_tick 必须把 record.report_docx_url 映射到 task.report_docx_path。"""
    fake_session.prev_level_obj = _rec(level=0)  # 前一条 0 级，触发等级变化
    task = call_respond.on_tick(_rec(level=2, rid=2, docx="http://x/b.docx"), "天津市")
    assert task is not None
    assert task.report_docx_path == "http://x/b.docx"
    assert task.report_pdf_path is None


def test_send_task_resolves_report_url_from_monitor(fake_session, monkeypatch):
    """C1：task 自身路径为空时，send_task 按 emergency_monitor_id 反查应急响应表取 URL。"""
    monkeypatch.setattr(call_respond, "load_group_config", lambda: {"groups": {"天津市": ["A群"]}})
    monkeypatch.setattr(call_respond, "send_file", lambda g, f, c: True)
    monkeypatch.setattr(call_respond, "_download_report", lambda url: "/tmp/r.docx")
    task = _rec(level=2, rid=12, docx=None)  # 任务自身无报告路径
    fake_session.all_rows = [task]
    fake_session.monitor_obj = _FakeMonitor(id=12, report_docx_url="http://x/resolved.docx")
    call_respond.send_task(12)
    assert task.status == "sent"
    assert task.report_docx_path == "http://x/resolved.docx"


def test_send_task_suspended_when_monitor_has_no_url(fake_session, monkeypatch):
    """C1：任务路径空且应急响应表也无 URL 时，仍挂起等报告。"""
    monkeypatch.setattr(call_respond, "load_group_config", lambda: {"groups": {"天津市": ["A群"]}})
    task = _rec(level=2, rid=13, docx=None)
    fake_session.all_rows = [task]
    fake_session.monitor_obj = _FakeMonitor(id=13)  # 无 URL
    call_respond.send_task(13)
    assert task.status == "suspended"


def test_send_task_partial_failure_sent(fake_session, monkeypatch):
    """I1：单个群失败不影响其他群，任一成功即 sent。"""
    monkeypatch.setattr(call_respond, "load_group_config",
                        lambda: {"groups": {"天津市": ["A群", "B群"]}})
    results = {"A群": True, "B群": False}
    monkeypatch.setattr(call_respond, "send_file", lambda g, f, c: results[g])
    monkeypatch.setattr(call_respond, "_download_report", lambda url: "/tmp/a.docx")
    task = _rec(level=2, rid=14, docx="http://x/a.docx")
    fake_session.all_rows = [task]
    call_respond.send_task(14)
    assert task.status == "sent"


def test_send_task_all_fail_failed(fake_session, monkeypatch):
    """I1：全部群失败 → failed。"""
    monkeypatch.setattr(call_respond, "load_group_config",
                        lambda: {"groups": {"天津市": ["A群", "B群"]}})
    monkeypatch.setattr(call_respond, "send_file", lambda g, f, c: False)
    monkeypatch.setattr(call_respond, "_download_report", lambda url: "/tmp/a.docx")
    task = _rec(level=2, rid=15, docx="http://x/a.docx")
    fake_session.all_rows = [task]
    call_respond.send_task(15)
    assert task.status == "failed"


def test_send_task_inflight_guard_skips_duplicate(fake_session, monkeypatch):
    """I2：同一 task_id 已在发送中时，send_task 直接跳过，不改状态。"""
    monkeypatch.setattr(call_respond, "load_group_config", lambda: {"groups": {"天津市": ["A群"]}})
    task = _rec(level=2, rid=16, docx="http://x/a.docx")
    task.status = "confirmed"
    fake_session.all_rows = [task]
    call_respond._in_flight.add(16)
    try:
        call_respond.send_task(16)
        assert task.status == "confirmed"  # 未执行发送，状态未被改写
    finally:
        call_respond._in_flight.discard(16)


def test_send_task_clears_inflight_after_send(fake_session, monkeypatch):
    """I2：正常发送完成后 in-flight 守卫被清理，后续可重试。"""
    monkeypatch.setattr(call_respond, "load_group_config", lambda: {"groups": {"天津市": ["A群"]}})
    monkeypatch.setattr(call_respond, "send_file", lambda g, f, c: True)
    monkeypatch.setattr(call_respond, "_download_report", lambda url: "/tmp/a.docx")
    task = _rec(level=2, rid=17, docx="http://x/a.docx")
    fake_session.all_rows = [task]
    call_respond.send_task(17)
    assert task.status == "sent"
    assert 17 not in call_respond._in_flight


from fastapi import HTTPException
from Controller.tool_router import (
    list_call_respond_tasks, confirm_call_respond, get_call_respond_logs,
    retry_call_respond, CallRespondConfirmRequest,
)


def test_confirm_endpoint_calls_confirm_task(fake_session, monkeypatch):
    monkeypatch.setattr(call_respond, "confirm_task", lambda tid, person: {"success": True, "task_id": tid, "status": "confirmed"})
    result = confirm_call_respond(7, CallRespondConfirmRequest(confirm_person="张三"))
    assert result["success"] is True


def test_confirm_endpoint_raises_when_not_found(fake_session, monkeypatch):
    monkeypatch.setattr(call_respond, "confirm_task", lambda tid, person: {"success": False, "detail": "任务不存在"})
    import pytest as _pytest
    with _pytest.raises(HTTPException) as ei:
        confirm_call_respond(999, CallRespondConfirmRequest(confirm_person="张三"))
    assert ei.value.status_code == 404


def test_confirm_endpoint_raises_409_when_not_confirmable(fake_session, monkeypatch):
    """I3：任务存在但状态不可确认 → 409，而非 404。"""
    monkeypatch.setattr(call_respond, "confirm_task",
                        lambda tid, person: {"success": False, "detail": "任务状态为 sent，不可确认",
                                             "status_code": 409})
    with pytest.raises(HTTPException) as ei:
        confirm_call_respond(7, CallRespondConfirmRequest(confirm_person="张三"))
    assert ei.value.status_code == 409


def test_list_tasks_returns_list(fake_session):
    fake_session.all_rows = [_rec(level=2, rid=3)]
    rows = list_call_respond_tasks()
    assert isinstance(rows, list)


def test_logs_endpoint_returns_list(fake_session):
    fake_session.all_rows = []
    rows = get_call_respond_logs(1)
    assert isinstance(rows, list)


def test_retry_endpoint_returns_success(fake_session, monkeypatch):
    monkeypatch.setattr(call_respond, "send_task", lambda tid: None)
    fake_session.all_rows = [_rec(level=2, rid=5)]
    result = retry_call_respond(5)
    assert result["success"] is True


def test_retry_endpoint_404_when_task_not_exist(fake_session):
    """code-review：retry 任务不存在 → 404，而非静默成功。"""
    fake_session.all_rows = []  # 无任务
    with pytest.raises(HTTPException) as ei:
        retry_call_respond(999)
    assert ei.value.status_code == 404


def test_send_task_skips_already_sent_group(fake_session, monkeypatch):
    """code-review：逐群重试 —— 已成功群被跳过，只重发失败/未成功群。"""
    monkeypatch.setattr(call_respond, "load_group_config",
                        lambda: {"groups": {"天津市": ["A群", "B群"]}})
    sent = []
    monkeypatch.setattr(call_respond, "send_file", lambda g, f, c: sent.append(g) or True)
    monkeypatch.setattr(call_respond, "_download_report", lambda url: "/tmp/a.docx")
    task = _rec(level=2, rid=20, docx="http://x/a.docx")
    fake_session.all_rows = [task]
    # A群已成功发送过，B群失败未重发
    fake_session.logs = [_FakeSendLog(task_id=20, target_group="A群", status="success")]
    call_respond.send_task(20)
    assert task.status == "sent"
    assert sent == ["B群"]  # 只重发 B群，A群被跳过


def test_send_task_rerun_sent_task_does_not_resend(fake_session, monkeypatch):
    """code-review：重跑已 sent 且所有群都已成功的任务 → 全部跳过，不重复发送。"""
    monkeypatch.setattr(call_respond, "load_group_config",
                        lambda: {"groups": {"天津市": ["A群", "B群"]}})
    sent = []
    monkeypatch.setattr(call_respond, "send_file", lambda g, f, c: sent.append(g) or True)
    monkeypatch.setattr(call_respond, "_download_report", lambda url: "/tmp/a.docx")
    task = _rec(level=2, rid=21, docx="http://x/a.docx")
    task.status = "sent"
    fake_session.all_rows = [task]
    fake_session.logs = [
        _FakeSendLog(task_id=21, target_group="A群", status="success"),
        _FakeSendLog(task_id=21, target_group="B群", status="success"),
    ]
    call_respond.send_task(21)
    assert sent == []  # 无重复发送
    assert task.status == "sent"


def test_retry_pending_sends_includes_confirmed(fake_session, monkeypatch):
    """code-review：confirmed 状态加入恢复扫描（非 pending，不重复创建）。"""
    started = []

    class _FakeThread:
        def __init__(self, target, args=(), daemon=None):
            self.target = target
            self.args = args

        def start(self):
            started.append(self.args[0])

    monkeypatch.setattr(threading, "Thread", _FakeThread)
    t_suspended = _rec(level=2, rid=30)
    t_suspended.status = "suspended"
    t_confirmed = _rec(level=2, rid=31)
    t_confirmed.status = "confirmed"
    t_failed = _rec(level=2, rid=32)
    t_failed.status = "failed"
    t_pending = _rec(level=2, rid=33)
    t_pending.status = "pending"
    fake_session.all_rows = [t_suspended, t_confirmed, t_failed, t_pending]
    call_respond.retry_pending_sends()
    assert 31 in started  # confirmed 被扫描
    assert 30 in started
    assert 32 in started
    assert 33 not in started  # pending 不应被扫描