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


def test_candidates_for_top_n_layers():
    """Top-5/8/12 分层召回，候选按关键词命中顺序。"""
    tools = [
        _fake_tool("query_rolling_forecast", "查询天津滚动预报未来天气"),
        _fake_tool("query_decision_weather_for_poi", "查询具体点位附近天气"),
        _fake_tool("get_effective_warning_info", "查询当前生效预警"),
        _fake_tool("query_water_level", "查询水位"),
        _fake_tool("rag_search", "知识库检索"),
        _fake_tool("query_basin_areal_rainfall", "流域面雨量"),
    ]
    idx = ToolCandidateIndex(tools)
    cands5 = idx.candidates_for_top_n("天津明天天气怎么样", 5)
    cands8 = idx.candidates_for_top_n("天津明天天气怎么样", 8)
    assert len(cands5) <= 5
    assert len(cands8) <= 8
    assert cands5 == cands8[:5]  # 分层一致
    assert "query_rolling_forecast" in cands5  # 天气问法召回滚动预报


def test_candidates_for_top_n_returns_list():
    idx = ToolCandidateIndex([_fake_tool("rag_search", "知识库检索")])
    assert isinstance(idx.candidates_for_top_n("知识库", 3), list)
