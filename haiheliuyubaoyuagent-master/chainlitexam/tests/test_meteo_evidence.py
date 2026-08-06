"""meteo_evidence.is_evidence_complete 测试。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from chainlitexam.tests.stubs import ensure_stubs
ensure_stubs()

from tools.meteo_evidence import is_evidence_complete


def test_forecast_complete_when_rolling_bundle_has_table():
    """预报类，滚动预报 bundle 有 code_section → 证据完整。"""
    results = [{"tool_name": "query_rolling_forecast", "bundle": {"code_section": "| 表格 |", "data_source": "天津市气象台滚动预报"}}]
    assert is_evidence_complete("forecast", results) is True


def test_forecast_incomplete_without_code_section():
    results = [{"tool_name": "query_rolling_forecast", "bundle": {"code_section": ""}}]
    assert is_evidence_complete("forecast", results) is False


def test_forecast_incomplete_empty_results():
    assert is_evidence_complete("forecast", []) is False


def test_warning_complete_with_records():
    results = [{"tool_name": "get_effective_warning_info", "bundle": {"records": [{"eventType": "暴雨", "severity": "黄色"}]}}]
    assert is_evidence_complete("warning", results) is True


def test_warning_incomplete_no_records():
    results = [{"tool_name": "get_effective_warning_info", "bundle": {"records": []}}]
    assert is_evidence_complete("warning", results) is False


def test_current_complete_with_observation_time():
    results = [{"tool_name": "query_current_weather_observation", "bundle": {"observation_time": "2026-08-06 14:00"}}]
    assert is_evidence_complete("current", results) is True


def test_unknown_query_type_is_conservative():
    results = [{"tool_name": "query_rolling_forecast", "bundle": {"code_section": "表格"}}]
    assert is_evidence_complete("unknown_kind", results) is False
