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
        self.confirm_person = kw.get("confirm_person")
        self.confirm_time = kw.get("confirm_time")
        self.send_time = kw.get("send_time")
        self.create_time = kw.get("create_time")


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

    def first(self):
        if self._session.prev_level_obj is not None:
            return self._session.prev_level_obj
        return self._session.all_rows[0] if self._session.all_rows else None

    def all(self):
        return self._session.all_rows


class _FakeSession:
    def __init__(self):
        self.added = []
        self.prev_level_obj = None
        self.all_rows = []

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
    return _FakeTask(
        id=rid, emergency_monitor_id=rid, response_level=level,
        datatime=datetime.strptime(datatime, "%Y-%m-%d %H:%M:%S"),
        impact_city="天津市", status="pending", report_docx_path=docx,
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
    result = retry_call_respond(5)
    assert result["success"] is True