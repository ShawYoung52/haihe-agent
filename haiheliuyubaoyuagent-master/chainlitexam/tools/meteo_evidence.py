"""气象证据完整性判断。

is_evidence_complete(query_type, tool_results) 判断当前工具结果是否足以直接回答，
无需再查询。设计为纯函数，供 shadow 观测和安全提前收口共同使用。
"""
from __future__ import annotations

from typing import Any


# query_type → 必需字段映射（按工具 bundle 结构判断）
_REQUIRED_BUNDLE_KEYS: dict[str, set[str]] = {
    "forecast": {"code_section"},   # 滚动预报 bundle 有代码生成的表格即完整
    "rain": {"code_section"},
    "temperature": {"code_section"},
    "activity": {"code_section"},
    "visibility": {"code_section"},
    "warning": {"records"},          # 预警 bundle 有 records 即完整（空 records 视为"已解除"需 LLM 判断）
    "current": {"observation_time"},
    "water_level": {"water_level_m"},
}

# 已知但不支持提前收口的 query_type（保守返回 False）
_KNOWN_UNSAFE: set[str] = {"river", "impact", "unknown", "decision_poi"}


def _bundle_complete(required_keys: set[str], bundle: dict) -> bool:
    if not isinstance(bundle, dict):
        return False
    for key in required_keys:
        value = bundle.get(key)
        # records 空列表视为不完整（预警无记录时需 LLM 判断"已解除"）
        if value is None:
            return False
        if isinstance(value, (list, tuple)) and not value:
            return False
        if isinstance(value, str) and not value.strip():
            return False
    return True


def is_evidence_complete(query_type: str, tool_results: list[Any]) -> bool:
    """判断证据是否完整。

    tool_results: list of {"tool_name": str, "bundle": dict}。
    仅当 query_type 有映射、存在至少一个结果、且该结果满足必需字段时返回 True。
    """
    if not tool_results:
        return False
    if query_type in _KNOWN_UNSAFE:
        return False
    for result in tool_results:
        if isinstance(result, dict):
            # 兼容原 shadow 的 {tool_name, bundle} 结构。
            bundle = result.get("bundle") or {}
            required = _REQUIRED_BUNDLE_KEYS.get(query_type)
            if required is not None and _bundle_complete(required, bundle):
                return True
            continue

        status = getattr(result, "status", "error")
        if status != "ok":
            continue
        payload = getattr(result, "payload", None)
        if _payload_complete(query_type, payload):
            return True
    return False


def _payload_complete(query_type: str, payload: Any) -> bool:
    """判断解包后的真实工具 payload 是否足以进入 Answer。"""
    if query_type == "current":
        if not isinstance(payload, dict) or payload.get("error"):
            return False
        observation_time = (
            payload.get("observation_time")
            or payload.get("observation_time_label")
            or payload.get("observation_time_beijing")
        )
        counts = payload.get("record_counts")
        has_counts = isinstance(counts, dict) and any(
            isinstance(value, (int, float)) and value > 0 for value in counts.values()
        )
        has_records = isinstance(payload.get("records"), list) and bool(payload["records"])
        return bool(observation_time and (has_counts or has_records))

    if query_type == "water_level":
        if not isinstance(payload, dict) or payload.get("error"):
            return False
        records = payload.get("records")
        if not isinstance(records, list) or not records:
            return False
        water_keys = {
            "water_level_m", "水位(m)", "库上水位(m)", "闸上水位(m)", "闸下水位(m)"
        }
        return any(
            isinstance(row, dict)
            and any(row.get(key) is not None for key in water_keys)
            for row in records
        )

    if query_type == "rain":
        return (
            isinstance(payload, list)
            and bool(payload)
            and all(not isinstance(row, dict) or not row.get("error") for row in payload)
        )

    # forecast 继续使用含 code_section 的 bundle；warning 继续走专用工作流。
    return False
