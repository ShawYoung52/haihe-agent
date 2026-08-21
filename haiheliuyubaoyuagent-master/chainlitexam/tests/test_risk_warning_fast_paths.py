"""Behavioral tests for risk warning fast path routing.

Locks in the routing for the four canonical user questions so that changes to
``_detect_risk_kind`` / ``_is_risk_question`` cannot silently break them.
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fast_paths.risk_warning_fast_paths import (  # noqa: E402
    _detect_risk_kind,
    _format,
    _is_risk_question,
)


def test_canonical_questions_route_correctly():
    cases = [
        ("有没有山洪风险？", "mountain"),
        ("有没有地质灾害风险？", "geologic"),
        ("哪些区域需注意中小河流洪水？", "river"),
        ("山区有没有滑坡风险？", "geologic"),
    ]
    for text, expected in cases:
        assert _detect_risk_kind(text) == expected, f"{text!r} -> {expected}"
        assert _is_risk_question(text) is True, f"{text!r} should be a risk question"


def test_non_risk_text_is_not_routed():
    for text in ("今天天气怎么样？", "流域面雨量多少？", "天津明天会下雨吗？"):
        assert _detect_risk_kind(text) is None
        assert _is_risk_question(text) is False


class TestFormat:
    def test_format_includes_level_breakdown_when_present(self):
        # 2026-08-21：MCP 端灾害点匹配后返回 county_totals / county_risk_summary /
        # level_advice，快路径必须渲染逐级统计与逐级防范建议。
        data = {
            "status": "ok",
            "risk_kind": "geologic",
            "risk_label": "地质灾害风险",
            "count": 3,
            "risk_count": 3,
            "areas": ["冀州区", "蓟州区"],
            "levels": ["三级", "四级"],
            "records": [{"area": "冀州区", "level": "四级"}, {"area": "蓟州区", "level": "三级"}],
            "message": "",
            "county_totals": {"冀州区": 257, "蓟州区": 89},
            "county_risk_summary": [
                {"county": "蓟州区", "level": "三级", "count": 1},
                {"county": "冀州区", "level": "四级", "count": 1},
            ],
            "level_advice": [
                {"level": "一级", "advice": "停止露天作业"},
                {"level": "二级", "advice": "暂停户外作业"},
                {"level": "三级", "advice": "加强巡查监测"},
                {"level": "四级", "advice": "关注雨情变化"},
            ],
        }
        out = _format(types.SimpleNamespace(), data, "有没有地质灾害风险？", "geologic")
        assert "**本次风险等级统计**" in out
        assert "冀州区" in out and "蓟州区" in out
        assert "257 个" in out  # 隐患点总数
        assert "**防范建议（按风险等级）**" in out
        assert "一级" in out and "四级" in out
        # 有逐级建议时不再输出旧的笼统"**建议**"
        assert "**建议**：" not in out

    def test_format_without_level_data_keeps_generic_advice(self):
        # 无新字段（旧后端/无匹配数据）时保持原有"**建议**"行为，向后兼容。
        data = {
            "status": "ok",
            "risk_kind": "geologic",
            "risk_label": "地质灾害风险",
            "count": 1,
            "risk_count": 1,
            "areas": ["冀州区"],
            "levels": ["三级"],
            "records": [{"area": "冀州区", "level": "三级"}],
            "message": "",
        }
        out = _format(types.SimpleNamespace(), data, "山区有没有滑坡风险？", "geologic")
        assert "**建议**：" in out
        assert "**本次风险等级统计**" not in out
        assert "**防范建议（按风险等级）**" not in out

    def test_format_level_advice_suppressed_when_no_risk_records(self):
        # 2026-08-21 gating：本次无风险记录（county_risk_summary 空）时，
        # 即便 level_advice 存在也不刷四级文案——"本次无风险"的回答不该带防范建议。
        data = {
            "status": "ok",
            "risk_kind": "geologic",
            "risk_label": "地质灾害风险",
            "count": 3,
            "risk_count": 0,
            "areas": [],
            "levels": [],
            "records": [],
            "message": "",
            "county_totals": {"冀州区": 257, "蓟州区": 89},
            "county_risk_summary": [],
            "level_advice": [
                {"level": "一级", "advice": "停止露天作业"},
                {"level": "二级", "advice": "暂停户外作业"},
                {"level": "三级", "advice": "加强巡查监测"},
                {"level": "四级", "advice": "关注雨情变化"},
            ],
        }
        out = _format(types.SimpleNamespace(), data, "有没有地质灾害风险？", "geologic")
        assert "当前未发现明显地质灾害风险" in out
        # 隐患点总数作为背景上下文仍可展示（"冀州 257 个，但本次无风险"）
        assert "257 个" in out
        # 无风险 → 不刷逐级统计/逐级防范建议，回退笼统建议
        assert "**本次风险等级统计**" not in out
        assert "**防范建议（按风险等级）**" not in out
        assert "**建议**：" in out

    def test_format_records_uses_normalized_level(self):
        # MCP 端归一好 level_norm（"5"→"一级"）后，记录表与本次风险等级统计表口径一致，
        # 不再同一条回答里出现"5"和"一级"两种写法。
        data = {
            "status": "ok",
            "risk_kind": "geologic",
            "risk_label": "地质灾害风险",
            "count": 1,
            "risk_count": 1,
            "areas": ["冀州区"],
            "levels": ["一级"],
            "records": [{"area": "冀州区", "level": "5", "level_norm": "一级"}],
            "message": "",
            "county_totals": {"冀州区": 257},
            "county_risk_summary": [{"county": "冀州区", "level": "一级", "count": 1}],
            "level_advice": [{"level": "一级", "advice": "停止露天作业"}],
        }
        out = _format(types.SimpleNamespace(), data, "有没有地质灾害风险？", "geologic")
        assert "| 冀州区 | 一级 |" in out  # 记录表显示归一等级
        assert "| 5 |" not in out          # 原始数字 5 不再出现在表里
