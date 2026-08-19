"""Shared helpers for unwrapping tool results and normalizing display cells."""
from __future__ import annotations

import json
from typing import Any


def _unwrap_tool_result(raw_result: Any) -> Any:
    """把 MCP / LangChain 工具返回结果统一拆成 Python 对象。

    支持以下常见返回形态：
    - None → None
    - [{"text": "..."}] → 解析 text 中的 JSON，失败则返回原字符串
    - JSON 字符串 → 解析为 Python 对象
    - 带 .content 属性的对象 → 优先解包 content
    - 其他 → 原样返回
    """
    if raw_result is None:
        return None

    data = raw_result
    if hasattr(data, "content"):
        data = data.content

    if isinstance(data, list) and data and isinstance(data[0], dict) and "text" in data[0]:
        data = data[0]["text"]

    if isinstance(data, str):
        stripped = data.lstrip()
        # 只有"看起来像 JSON"的字符串才尝试解析（以 { 或 [ 开头）。普通文本（如决策天气的
        # Markdown 答案）直接按原字符串返回，不再误报"JSON 解析失败"刷日志。
        if stripped[:1] in ("{", "["):
            try:
                return json.loads(data)
            except Exception as _parse_err:
                print(f"[tool_result] JSON 解析失败，按原字符串返回：{_parse_err}")
        return data

    return data


def _extract_self_report(data: Any) -> dict:
    """从已解包的工具结果中提取 self_report 字典；不存在时返回 data（字典）或空字典。"""
    if not isinstance(data, dict):
        return {}
    self_report = data.get("self_report")
    return self_report if isinstance(self_report, dict) else data


def _safe_cell(cleaner: Any, value: Any) -> str:
    """使用 message_orchestrator 的 _clean_table_cell（如果可用）清理表格单元格。"""
    if cleaner is not None and callable(cleaner):
        return cleaner(value)
    return "" if value is None else str(value).replace("|", "｜").strip()