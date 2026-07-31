# chainlitexam/tests/test_forecast_evaluate_full.py
"""Test forecast evaluate full integration: charts + report + poor samples."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from message_orchestrator import _need_forecast_evaluate


class TestForecastEvaluateChartKeywords:
    """图表相关关键词检测"""

    CHART_TRIGGER = [
        "画个暴雨TS评分对比图",
        "降水检验趋势图",
        "各家模式准确率对比图表",
        "看看预报检验热力图",
        "生成一个温度检验趋势图",
    ]

    CHART_NON_TRIGGER = [
        "暴雨会落在哪些区域",
        "天津今天天气",
    ]

    @pytest.mark.parametrize("query", CHART_TRIGGER)
    def test_chart_keywords_trigger(self, query):
        assert _need_forecast_evaluate(query), f"Should trigger chart: {query}"

    @pytest.mark.parametrize("query", CHART_NON_TRIGGER)
    def test_chart_non_trigger(self, query):
        assert not _need_forecast_evaluate(query), f"Should NOT trigger: {query}"


class TestForecastEvaluateReportIntegration:
    """报告集成：verify _build_forecast_evaluate_answer handles report_markdown"""

    SAMPLE_DATA_WITH_REPORT = {
        "element": "24小时最高温度",
        "test_type": "逐日",
        "time_range": {"begin": "2026-07-01 00:00:00", "end": "2026-07-30 23:59:59"},
        "data_source": "检验API",
        "metrics": {
            "2℃准确率": {
                "ranking": [["天津预报", 84.09], ["国家指导", 79.17], ["ECMWF", 76.14]],
                "best": "天津预报", "best_value": 84.09, "unit": "%",
            },
        },
        "summary": "天津预报(84.09) > 国家指导(79.17) > ECMWF(76.14)",
        "report_markdown": "# 24小时最高温度预报检验\n\n**检验类型**: 逐日\n\n## 综述\n\n测试综述内容\n\n## 详细结果\n\n### 温度\n\n#### 2℃准确率\n\n| 产品 | 平均 |\n| --- | --- |\n| **天津预报** | 84.09 |\n",
        "poor_samples": [],
    }

    def test_report_markdown_preferred(self):
        """当 report_markdown 存在时，_build_forecast_evaluate_answer 应返回完整报告"""
        from message_orchestrator import _build_forecast_evaluate_answer
        result = _build_forecast_evaluate_answer(self.SAMPLE_DATA_WITH_REPORT, "测试")
        assert "## 综述" in result
        assert "测试综述内容" in result
        assert "## 详细结果" in result

    def test_fallback_when_no_report(self):
        """当 report_markdown 缺失时，应 fallback 到手拼排名表格"""
        from message_orchestrator import _build_forecast_evaluate_answer
        data_no_report = dict(self.SAMPLE_DATA_WITH_REPORT)
        data_no_report["report_markdown"] = ""
        result = _build_forecast_evaluate_answer(data_no_report, "测试")
        assert "\U0001f947" in result  # 排名图标
        assert "天津预报" in result
