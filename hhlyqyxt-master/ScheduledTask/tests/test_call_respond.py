"""应急响应叫应功能单元测试（无需真实数据库）。"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Models.QyCallRespondTask import QyCallRespondTask
from Models.QyCallRespondSendLog import QyCallRespondSendLog


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