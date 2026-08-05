"""候选工具召回索引（影子模式）。

启动时构建一次关键词→工具映射。Planner 仍绑定完整工具列表，
本模块只用于"记录候选工具是否包含 Planner 实际调用工具"的影子观测，
不改变 Planner 行为。
"""
from __future__ import annotations

import re
from typing import Any


class ToolCandidateIndex:
    """基于工具名/描述/参数名的关键词召回索引。"""

    def __init__(self, tools: list[Any]):
        self._tools = tools
        self._by_keyword: dict[str, list[str]] = {}
        self._default_candidates: list[str] = []
        self._build(tools)

    def _build(self, tools: list[Any]) -> None:
        """一次构建：提取每个工具的关键词并建立倒排。"""
        fallback_names = ["rag_search", "query_rolling_forecast"]  # 兜底工具
        for tool in tools:
            name = getattr(tool, "name", "") or ""
            if not name:
                continue
            desc = getattr(tool, "description", "") or ""
            keywords = self._keywords_for(name, desc)
            for kw in keywords:
                self._by_keyword.setdefault(kw, []).append(name)
            if name in fallback_names:
                self._default_candidates.append(name)
        # 兜底工具始终在候选里
        for fb in fallback_names:
            if fb not in self._default_candidates:
                self._default_candidates.append(fb)

    def _keywords_for(self, name: str, desc: str) -> list[str]:
        """从工具名+描述提取中文关键词。"""
        text = f"{name} {desc}"
        # 常见业务词
        biz = [
            "天气", "降雨", "降水", "雨", "预警", "水位", "河网", "河流",
            "行政区", "面雨量", "应急", "点位", "短临", "强对流", "雷暴",
            "冰雹", "气温", "风", "能见度", "雾", "霾", "站点", "实况",
        ]
        return [b for b in biz if b in text]

    def candidates_for(self, user_text: str, limit: int = 12) -> list[str]:
        """按用户问题关键词召回候选工具；无命中时返回兜底工具。"""
        matched: list[str] = []
        for kw, names in self._by_keyword.items():
            if kw in (user_text or ""):
                for n in names:
                    if n not in matched:
                        matched.append(n)
        for n in self._default_candidates:
            if n not in matched:
                matched.append(n)
        return matched[:limit]

    def contains(self, user_text: str, actual_tool: str, limit: int = 12) -> bool:
        """影子观测：候选是否包含实际调用的工具。"""
        return actual_tool in self.candidates_for(user_text, limit=limit)
