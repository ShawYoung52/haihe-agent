"""应急响应叫应功能。

等级变化时创建叫应任务，值班人员人工确认后后台异步发送
叫应话术 + 天河报告文件到受影响单位微信群，并记录逐群发送台账。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import desc

from Models.QyCallRespondTask import QyCallRespondTask
from Models.QyCallRespondSendLog import QyCallRespondSendLog
from Models.QyEmergencyResponseMonitor import QyEmergencyResponseMonitor
from utils.db import Session
from utils.wechat_send_file import send_file

logger = logging.getLogger(__name__)

# 状态常量
STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_PENDING_SEND = "pending_send"
STATUS_SUSPENDED = "suspended"
STATUS_FAILED = "failed"

# retry_pending_sends 扫描的"可恢复"任务状态集合（缺报告/缺群映射/卡发送/
# 发送失败/已确认未发送）。pending/sent 不在其中：pending 待人工确认，
# sent 已全部成功无需补发。
RECOVERABLE_STATUSES = {
    STATUS_SUSPENDED, STATUS_PENDING_SEND, STATUS_SENDING, STATUS_FAILED, STATUS_CONFIRMED,
}

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "call_respond_config.json"

# 模块级 in-flight 守卫：同一 task_id 不允许并发重复发送（retry_pending_sends
# 与 confirm 线程可能同时触发 send_task，需幂等去重）。
_in_flight: set = set()
_in_flight_lock = threading.Lock()


def load_group_config(config_path: Optional[str] = None) -> dict:
    """读取群映射配置，返回 {"template": str, "groups": {city: [group,...]}}。

    文件缺失或 JSON 解析失败时返回空配置，不抛异常（调用方降级处理）。
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("读取叫应配置失败（%s），按空配置处理", e)
        return {"template": "", "groups": {}}
    return {
        "template": cfg.get("template", ""),
        "groups": cfg.get("groups", {}),
    }


def render_template(template: str, city: str, level: int) -> str:
    """替换 {city}/{level} 占位符。"""
    return template.replace("{city}", city).replace("{level}", str(level))


def group_targets(impact_city: str, config: dict) -> list:
    """按受影响城市（顿号分隔）返回目标群列表，去重保序。"""
    groups = config.get("groups", {})
    result = []
    for city in (impact_city or "").split("、"):
        city = city.strip()
        if city:
            result.extend(groups.get(city, []))
    return list(dict.fromkeys(result))


def _previous_response_level(datatime) -> int:
    """返回 qy_emergency_response_monitor 中 datatime 之前最近一条的等级，无则 0。"""
    session = Session()
    try:
        prev = (
            session.query(QyEmergencyResponseMonitor)
            .filter(QyEmergencyResponseMonitor.datatime < datatime)
            .order_by(desc(QyEmergencyResponseMonitor.datatime))
            .first()
        )
        return prev.response_level if prev is not None else 0
    finally:
        session.close()


def on_tick(record, impact_city: str, config: Optional[dict] = None) -> Optional[QyCallRespondTask]:
    """等级变化时创建叫应任务。

    record 为 None 或 0 级时不创建。等级变化判定：当前等级与前一条
    应急响应记录等级不同且当前 ≥1 时创建 pending 任务。
    """
    if record is None:
        return None
    level = int(record.response_level)
    if level < 1:
        return None
    prev_level = _previous_response_level(record.datatime)
    if prev_level == level:
        return None  # 同等级持续不重复创建

    task = QyCallRespondTask(
        emergency_monitor_id=record.id,
        response_level=level,
        datatime=record.datatime,
        impact_city=impact_city,
        status=STATUS_PENDING,
        report_docx_path=getattr(record, "report_docx_url", None),
        report_pdf_path=getattr(record, "report_pdf_url", None),
    )
    session = Session()
    try:
        session.add(task)
        session.commit()
        session.refresh(task)
        logger.info("等级变化创建叫应任务 id=%s level=%d impact=%s", task.id, level, impact_city)
        return task
    except Exception:
        session.rollback()
        logger.warning("创建叫应任务失败（不阻塞主流程）", exc_info=True)
        return None
    finally:
        session.close()


def _write_send_log(task_id: int, group: str, status: str, detail: str = "") -> None:
    """写入一条逐群发送日志，失败不抛出。"""
    log = QyCallRespondSendLog(task_id=task_id, target_group=group, status=status, detail=detail)
    session = Session()
    try:
        session.add(log)
        session.commit()
    except Exception:
        session.rollback()
        logger.warning("写入叫应发送日志失败：task=%s group=%s", task_id, group, exc_info=True)
    finally:
        session.close()


def _download_report(url: str) -> str:
    """下载报告 URL 到本地临时文件，返回路径。失败抛异常由调用方处理。"""
    import requests
    import tempfile
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    suffix = Path(url).suffix or ".docx"
    fd, path = tempfile.mkstemp(suffix=suffix)
    with open(fd, "wb") as f:
        f.write(resp.content)
    return path


def _resolve_report_urls(task, session) -> bool:
    """确保 task 有可用报告路径。

    task 自身 report_docx_path/report_pdf_path 已存在时直接返回 True；
    否则按 emergency_monitor_id 反查 qy_emergency_response_monitor 表
    取当前 report_docx_url/report_pdf_url 回填（天然兼容"报告缺失→挂起→
    回填→补发"）。两者都无则返回 False（调用方置 suspended）。
    """
    if task.report_docx_path or task.report_pdf_path:
        return True
    mon = (
        session.query(QyEmergencyResponseMonitor)
        .filter(QyEmergencyResponseMonitor.id == task.emergency_monitor_id)
        .first()
    )
    if mon is None:
        return False
    if not (getattr(mon, "report_docx_url", None) or getattr(mon, "report_pdf_url", None)):
        return False
    task.report_docx_path = getattr(mon, "report_docx_url", None)
    task.report_pdf_path = getattr(mon, "report_pdf_url", None)
    return True


def _group_already_sent(task_id: int, group: str) -> bool:
    """该群是否已有成功发送日志（逐群幂等：重试不重复发已成功群）。

    网络发送阶段不持长 session，自建自关短 session。
    """
    session = Session()
    try:
        row = (
            session.query(QyCallRespondSendLog)
            .filter(
                QyCallRespondSendLog.task_id == task_id,
                QyCallRespondSendLog.target_group == group,
                QyCallRespondSendLog.status == "success",
            )
            .first()
        )
        return row is not None
    finally:
        session.close()


def _send_to_group(task, group: str, config: dict, file_path: str) -> bool:
    """向单个群发送已下载的报告文件 + 话术，写 send_log，返回是否成功。

    该群已有成功发送日志则跳过（逐群幂等，重试只重发失败群，不重复
    已成功群）。报告已在 send_task 中下载一次，此处复用 file_path，
    不再逐群重复下载；临时文件由 send_task 统一清理。
    """
    if _group_already_sent(task.id, group):
        return True
    caption = render_template(config.get("template", ""), task.impact_city, task.response_level)
    try:
        ok = send_file(group, file_path, caption)
        _write_send_log(task.id, group, "success" if ok else "failed",
                        detail="" if ok else "send_file 返回 False")
        return ok
    except Exception as e:
        _write_send_log(task.id, group, "failed", detail=str(e)[:500])
        return False


def _finalize_send(task_id: int, status: str) -> None:
    """网络发送结束后重开短 session 置最终状态（sent/failed），失败不抛出。"""
    session = Session()
    try:
        task = session.query(QyCallRespondTask).filter(QyCallRespondTask.id == task_id).first()
        if task is None:
            return
        task.status = status
        task.send_time = datetime.now()
        session.commit()
    except Exception:
        session.rollback()
        logger.warning("更新叫应任务最终状态失败：task=%s", task_id, exc_info=True)
    finally:
        session.close()


def send_task(task_id: int) -> None:
    """后台发送。读任务+配置，按状态分支：
    报告缺失（含反查应急响应表仍无）→suspended；群未配置→pending_send；
    否则报告下载一次 → 逐群发送→sent（任一成功）/failed（全部失败）。
    同一 task_id 已在发送中则跳过（in-flight 守卫，避免并发重复发送）。
    网络阶段（下载 + 逐群发送）不持有 DB session。
    """
    with _in_flight_lock:
        if task_id in _in_flight:
            return
        _in_flight.add(task_id)
    try:
        # 阶段一：加载任务 + 解析报告 + 置 sending（短 session，立即关闭）
        session = Session()
        try:
            task = session.query(QyCallRespondTask).filter(QyCallRespondTask.id == task_id).first()
            if task is None:
                return  # 任务不存在/非法，直接返回
            config = load_group_config()
            if not _resolve_report_urls(task, session):
                task.status = STATUS_SUSPENDED
                session.commit()
                return
            targets = group_targets(task.impact_city, config)
            if not targets:
                task.status = STATUS_PENDING_SEND
                session.commit()
                return
            task.status = STATUS_SENDING
            session.commit()
        finally:
            session.close()

        # 阶段二：网络发送（不持 DB session）。报告只下载一次，群间共享。
        file_path = None
        try:
            file_path = _download_report(task.report_docx_path or task.report_pdf_path)
        except Exception:
            logger.warning("下载报告失败：task=%s，无文件可发，任务置 failed", task_id, exc_info=True)
            _finalize_send(task_id, STATUS_FAILED)
            return
        try:
            any_ok = False
            for group in targets:
                if _send_to_group(task, group, config, file_path):
                    any_ok = True
        finally:
            if file_path:
                try:
                    os.remove(file_path)
                except OSError:
                    pass
        # 单个群失败不影响其他群；任一成功则 sent，全部失败才 failed
        _finalize_send(task_id, STATUS_SENT if any_ok else STATUS_FAILED)
    except Exception:
        logger.warning("发送叫应任务失败：task=%s", task_id, exc_info=True)
    finally:
        with _in_flight_lock:
            _in_flight.discard(task_id)


def confirm_task(task_id: int, confirm_person: str) -> dict:
    """人工确认。校验任务存在且 pending，置 confirmed，起后台线程发送。

    返回值带 status_code 供接口层区分：任务不存在→404；状态不可确认→409。
    """
    session = Session()
    try:
        task = session.query(QyCallRespondTask).filter(QyCallRespondTask.id == task_id).first()
        if task is None:
            return {"success": False, "detail": "任务不存在", "status_code": 404}
        if task.status != STATUS_PENDING:
            return {"success": False, "detail": f"任务状态为 {task.status}，不可确认",
                    "status_code": 409}
        task.status = STATUS_CONFIRMED
        task.confirm_person = confirm_person
        task.confirm_time = datetime.now()
        session.commit()
        tid = task.id
    finally:
        session.close()
    threading.Thread(target=send_task, args=(tid,), daemon=True).start()
    return {"success": True, "task_id": tid, "status": STATUS_CONFIRMED}


def retry_pending_sends() -> None:
    """扫描待补发任务，起后台线程补发（条件满足则发送，否则重新挂起）。

    涵盖 suspended/pending_send（缺报告/缺群映射）、sending/failed
    （进程中断卡 sending 或发送失败可重试）与 confirmed（确认后进程中断
    未发送）。in-flight 守卫保证并发去重，安全。
    """
    session = Session()
    try:
        tasks = (
            session.query(QyCallRespondTask)
            .filter(QyCallRespondTask.status.in_(RECOVERABLE_STATUSES))
            .all()
        )
        ids = [t.id for t in tasks]
    finally:
        session.close()
    for tid in ids:
        threading.Thread(target=send_task, args=(tid,), daemon=True).start()