"""Static checks for prompts.py rules."""
from __future__ import annotations

from pathlib import Path

PROMPTS_PATH = Path(__file__).resolve().parents[1] / "prompts.py"


def test_prompts_mentions_river_system_forecast_tool():
    text = PROMPTS_PATH.read_text(encoding="utf-8")
    assert "get_river_system_rainfall_forecast" in text


def test_prompts_prioritizes_river_system_for_basin_forecast():
    text = PROMPTS_PATH.read_text(encoding="utf-8")
    # 流域预报规则中应优先提到新工具
    basin_section = text[text.find("用户问\"流域\"时区分实况与预报"):text.find("数据来源必须如实")]
    assert "get_river_system_rainfall_forecast" in basin_section


def test_prompts_subbasin_section_uses_river_system_tool():
    text = PROMPTS_PATH.read_text(encoding="utf-8")
    subbasin_section = text[text.find("#### 3.5 子流域未来天气查询规范"):text.find("### 4. 决策天气")]
    assert "get_river_system_rainfall_forecast" in subbasin_section
    assert "优先调用" in subbasin_section


def test_prompts_does_not_hardcode_ec_for_river_system():
    text = PROMPTS_PATH.read_text(encoding="utf-8")
    # 检查新工具的数据来源规则是否要求读取 data_source 字段
    river_section = text[text.find("get_river_system_rainfall_forecast"):]
    first_paragraph = river_section[:river_section.find("\n-")]
    assert "data_source" in first_paragraph
