"""一次工具轮次的结构化证据容器。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


TOOL_QUERY_TYPES: dict[str, str] = {
    "get_effective_warning_info": "warning",
    "get_history_warning_info": "warning",
    "get_national_warning_info": "warning",
    "get_today_warning_summary": "warning",
    "query_rolling_forecast": "forecast",
    "query_current_weather_observation": "current",
    "query_water_level": "water_level",
    "query_decision_weather_for_poi": "decision_poi",
    "query_basin_areal_rainfall": "rain",
}


@dataclass(frozen=True)
class ToolEvidenceItem:
    tool_name: str
    status: str
    payload: Any

    @property
    def query_type(self) -> str:
        return TOOL_QUERY_TYPES.get(self.tool_name, "unknown")


class ToolRoundEvidence:
    """只保存可序列化/普通 Python 结果，不保存工具或会话对象。"""

    def __init__(self) -> None:
        self._items: list[ToolEvidenceItem] = []

    @property
    def items(self) -> list[ToolEvidenceItem]:
        return list(self._items)

    def record(self, tool_name: str, status: str, payload: Any) -> None:
        normalized = status if status in {"ok", "error", "missing"} else "error"
        self._items.append(ToolEvidenceItem(str(tool_name or ""), normalized, payload))

    def items_for(self, query_type: str) -> list[ToolEvidenceItem]:
        if query_type == "unknown":
            return []
        return [item for item in self._items if item.query_type == query_type]

    def has_errors_for(self, query_type: str) -> bool:
        return any(item.status != "ok" for item in self.items_for(query_type))
