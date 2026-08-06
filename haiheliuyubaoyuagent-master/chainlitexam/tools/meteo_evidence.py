"""气象证据完整性判断（阶段五，默认只记录不改变流程）。

is_evidence_complete(query_type, tool_results) 判断当前工具结果是否足以直接回答，
无需再查询。设计为纯函数，便于测试。shadow 记录时由 process_message 调用。
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
_KNOWN_UNSAFE: set[str] = {"river", "impact", "unknown"}


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


def is_evidence_complete(query_type: str, tool_results: list[dict]) -> bool:
    """判断证据是否完整。

    tool_results: list of {"tool_name": str, "bundle": dict}。
    仅当 query_type 有映射、存在至少一个结果、且该结果满足必需字段时返回 True。
    """
    if not tool_results:
        return False
    if query_type in _KNOWN_UNSAFE:
        return False
    required = _REQUIRED_BUNDLE_KEYS.get(query_type)
    if required is None:
        return False  # 无映射 → 保守 False
    return any(_bundle_complete(required, r.get("bundle") or {}) for r in tool_results)
