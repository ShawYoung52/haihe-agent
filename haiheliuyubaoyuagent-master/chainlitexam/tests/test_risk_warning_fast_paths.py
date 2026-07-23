"""Behavioral tests for risk warning fast path routing.

Locks in the routing for the four canonical user questions so that changes to
``_detect_risk_kind`` / ``_is_risk_question`` cannot silently break them.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fast_paths.risk_warning_fast_paths import _detect_risk_kind, _is_risk_question


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
