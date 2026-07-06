"""官方防汛响应状态辅助判断。

预案第一类条件要求：海河防总启动防汛应急响应后，流域气象中心启动同级别
气象服务联防应急响应。这里先内置已核实的 2025 年 7 月关键响应时段，后续可
通过 HAIHE_OFFICIAL_RESPONSE_EVENTS_JSON 接入正式接口或配置文件覆盖。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

DEFAULT_OFFICIAL_RESPONSE_EVENTS: list[dict[str, Any]] = [
    {
        "start_time": "20250724150000",
        "end_time": "20250724215959",
        "level": "IV",
        "source": "海河防总启动防汛Ⅳ级、海委启动洪水防御Ⅳ级应急响应",
        "basis": "第一类：海河防总启动防汛应急响应后，流域气象中心启动同级别气象服务联防应急响应。",
    },
    {
        "start_time": "20250724220000",
        "end_time": "20250731235959",
        "level": "III",
        "source": "海河防总、海委将响应级别由Ⅳ级提升至Ⅲ级；后续处于Ⅲ级响应影响时段",
        "basis": "第一类：海河防总调整防汛应急响应级别后，流域气象中心相应调整应急响应级别。",
    },
]


def _parse_time(value: str) -> datetime | None:
    text = str(value or "").strip()
    for fmt in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _load_events() -> list[dict[str, Any]]:
    raw = os.getenv("HAIHE_OFFICIAL_RESPONSE_EVENTS_JSON", "").strip()
    if not raw:
        return list(DEFAULT_OFFICIAL_RESPONSE_EVENTS)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return list(DEFAULT_OFFICIAL_RESPONSE_EVENTS)
    if not isinstance(data, list):
        return list(DEFAULT_OFFICIAL_RESPONSE_EVENTS)
    return [item for item in data if isinstance(item, dict)]


def find_official_emergency_response(times: str) -> dict[str, Any] | None:
    query_dt = _parse_time(times)
    if query_dt is None:
        return None

    matched: dict[str, Any] | None = None
    for item in _load_events():
        start_dt = _parse_time(str(item.get("start_time") or ""))
        end_dt = _parse_time(str(item.get("end_time") or ""))
        if start_dt is None:
            continue
        if query_dt < start_dt:
            continue
        if end_dt is not None and query_dt > end_dt:
            continue
        if matched is None or str(item.get("start_time")) > str(matched.get("start_time")):
            matched = item
    return dict(matched) if matched else None


def build_official_response_payload(times: str, basin_codes: str = "HHLY") -> dict[str, Any] | None:
    event = find_official_emergency_response(times)
    if not event:
        return None
    level = str(event.get("level") or "").strip()
    source = str(event.get("source") or "官方防汛应急响应状态").strip()
    basis = str(event.get("basis") or "第一类应急响应条件").strip()
    return {
        "status": "ok",
        "triggered": True,
        "level": level,
        "message": f"满足第一类应急响应条件：{source}，应启动/维持{level}级气象服务联防应急响应。",
        "evidence": {
            "response_category": "first_class",
            "official_response": {
                "level": level,
                "source": source,
                "start_time": event.get("start_time"),
                "end_time": event.get("end_time"),
                "basis": basis,
            },
        },
        "query": {
            "basin_codes": basin_codes,
            "times": times,
            "official_response_checked": True,
        },
    }
