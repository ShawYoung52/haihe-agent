"""应急响应叫应功能单元测试（无需真实数据库）。"""
import json
import sys
from pathlib import Path

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