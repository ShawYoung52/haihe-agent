"""天津当前天气实况附【天津市区】灾害风险表（前端渲染 + LLM 指令）测试。

背景（2026-09-01 用户口径）：「天津当前天气实况」走 planner + answer LLM 路径，
实况工具 payload 没有 region_hazards，回答缺灾害风险表（"第一个问题就是还是没风险那些的"）。
MCP 侧 `query_current_weather_observation_core` 现已在 status=="ok" 时附 region_hazards；
前端 `build_current_observation_risk_instruction` 复用滚动预报的 `_region_hazard_table`
确定性渲染风险表并附"原样输出"指令，由 `_run_tool_round` 拼进 observation_text。

零编造：表内容全部来自 region_hazards（MCP 代码统计），answer LLM 只原样透传。
"""

from __future__ import annotations

import sys
from pathlib import Path

CHAINLIT_DIR = Path(__file__).resolve().parents[1]
if str(CHAINLIT_DIR) not in sys.path:
    sys.path.insert(0, str(CHAINLIT_DIR))

from tools.current_weather_observation_response import (
    build_current_observation_risk_instruction,
    build_current_observation_risk_section,
)

PAYLOAD_WITH_HAZARDS = {
    "status": "ok",
    "region_hazards": [
        {
            "region": "tianjin",
            "region_display": "天津市区",
            "total_found": 17,
            "radius_km": 25,
            "categories": [
                {"key": "zxhl", "label": "中小河流", "kind": "river", "count": 17},
                {"key": "dzzh", "label": "地质灾害", "kind": "geologic", "count": 0},
            ],
            "hazards_available": True,
            "risk_levels": {"zxhl": {"levels": {}}, "dzzh": None},
            "risk_levels_available": True,
        }
    ],
}


class TestBuildCurrentObservationRiskSection:
    def test_renders_tianjin_risk_table(self):
        section = build_current_observation_risk_section(PAYLOAD_WITH_HAZARDS)
        assert "【天津市区灾害风险】" in section
        assert "灾害类型" in section and "隐患点数量" in section
        assert "中小河流" in section and "17 处" in section

    def test_no_region_hazards_returns_empty(self):
        assert build_current_observation_risk_section({"status": "ok"}) == ""

    def test_region_hazards_not_list_returns_empty(self):
        assert build_current_observation_risk_section({"region_hazards": "x"}) == ""

    def test_non_dict_payload_returns_empty(self):
        assert build_current_observation_risk_section(None) == ""
        assert build_current_observation_risk_section("ok") == ""


class TestBuildCurrentObservationRiskInstruction:
    def test_instruction_wraps_table_with_verbatim_directive(self):
        instruction = build_current_observation_risk_instruction(PAYLOAD_WITH_HAZARDS)
        assert "【天津市区灾害风险】" in instruction
        # 指示 LLM 原样附在实况表后、数据来源之前，禁止改动
        assert "原样" in instruction
        assert "数据来源之前" in instruction
        assert "禁止" in instruction

    def test_empty_when_no_hazards(self):
        assert build_current_observation_risk_instruction({"status": "ok"}) == ""
