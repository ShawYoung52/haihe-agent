# chainlitexam/tests/test_forecast_evaluate_fast_path.py
"""Test forecast evaluate fast path keyword detection and static contract compliance."""

import sys
from pathlib import Path

# 确保能从 chainlitexam 目录运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from message_orchestrator import _need_forecast_evaluate


class TestForecastEvaluateKeywordDetection:
    """测试 _need_forecast_evaluate 关键词检测"""

    TRIGGER_QUERIES = [
        "最近一次暴雨的TS评分是多少",
        "哪个模式的晴雨预报最准",
        "各家模式的暴雨TS评分对比",
        "大模型预报效果如何",
        "有没有模式误差分析",
        "哪个模式对暴雨落区预报最好",
        "最近的预报准确率对比",
        "BIAS偏差怎么样",
        "MAE误差分析",
        "天津预报和ECMWF的TS评分",
    ]

    NON_TRIGGER_QUERIES = [
        "明天会下雨吗",
        "天津今天天气怎么样",
        "大清河流域未来三天天气",
        "当前降雨情况如何",
        "暴雨影响哪些河流",
    ]

    @pytest.mark.parametrize("query", TRIGGER_QUERIES)
    def test_trigger_keywords(self, query):
        assert _need_forecast_evaluate(query), f"Should trigger: {query}"

    @pytest.mark.parametrize("query", NON_TRIGGER_QUERIES)
    def test_no_false_trigger(self, query):
        assert not _need_forecast_evaluate(query), f"Should NOT trigger: {query}"
