"""候选工具召回索引（影子模式）测试。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from chainlitexam.tests.stubs import ensure_stubs
ensure_stubs()

from tools.tool_candidate_index import ToolCandidateIndex


def _fake_tool(name, desc=""):
    class _T:
        def __init__(self):
            self.name = name
            self.description = desc
        @property
        def args_schema(self):
            class _S:
                properties = {}
            return _S()
    return _T()


def test_candidates_include_weather_tools_for_weather_query():
    tools = [
        _fake_tool("query_rolling_forecast", "查询天津滚动预报未来天气"),
        _fake_tool("query_decision_weather_for_poi", "查询具体点位附近天气"),
        _fake_tool("get_effective_warning_info", "查询当前生效预警"),
        _fake_tool("query_water_level", "查询水位"),
    ]
    idx = ToolCandidateIndex(tools)
    cands = idx.candidates_for("梅江会展中心明天天气怎么样", limit=12)
    assert "query_decision_weather_for_poi" in cands, f"天气+点位查询应召回决策天气工具，实际 {cands}"


def test_candidates_include_warning_tool_for_warning_query():
    tools = [_fake_tool("get_effective_warning_info", "查询当前生效预警"), _fake_tool("query_rolling_forecast", "预报")]
    idx = ToolCandidateIndex(tools)
    cands = idx.candidates_for("天津有暴雨预警吗", limit=12)
    assert "get_effective_warning_info" in cands, f"预警查询应召回预警工具，实际 {cands}"


def test_index_built_once_is_stable():
    tools = [_fake_tool("query_rolling_forecast", "预报天气")]
    idx = ToolCandidateIndex(tools)
    first = idx.candidates_for("明天天气", limit=12)
    second = idx.candidates_for("明天天气", limit=12)
    assert first == second  # 索引稳定，不随调用变化
