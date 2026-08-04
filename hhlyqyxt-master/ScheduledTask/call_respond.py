"""应急响应叫应功能。

等级变化时创建叫应任务，值班人员人工确认后后台异步发送
叫应话术 + 天河报告文件到受影响单位微信群，并记录逐群发送台账。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import desc

from Models.QyCallRespondTask import QyCallRespondTask
from Models.QyCallRespondSendLog import QyCallRespondSendLog
from Models.QyEmergencyResponseMonitor import QyEmergencyResponseMonitor
from utils.db import Session

logger = logging.getLogger(__name__)

# 状态常量
STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_PENDING_SEND = "pending_send"
STATUS_SUSPENDED = "suspended"
STATUS_FAILED = "failed"

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "call_respond_config.json"


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