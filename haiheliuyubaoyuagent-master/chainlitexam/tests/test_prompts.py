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
    """3.4 工具列表中，新工具条目必须要求引用 data_source 而非硬编码数据来源。"""
    text = PROMPTS_PATH.read_text(encoding="utf-8")
    tool_list_section = text[text.find("#### 3.4 降水相关工具列表"):text.find("#### 3.5")]
    for line in tool_list_section.splitlines():
        if "get_river_system_rainfall_forecast" in line:
            assert "data_source" in line
            break
    else:
        raise AssertionError("3.4 工具列表缺少 get_river_system_rainfall_forecast 条目")


def test_prompts_basin_rule_covers_today_wording():
    """"今天海河流域天气怎么样"必须被流域预报规则覆盖，防止路由到天津滚动预报。"""
    text = PROMPTS_PATH.read_text(encoding="utf-8")
    assert "今天海河流域天气怎么样" in text


def test_prompts_forbids_tianjin_as_basin_representative():
    text = PROMPTS_PATH.read_text(encoding="utf-8")
    assert "以天津" in text and "代表" in text  # 存在禁止"以天津代表全流域"的表述
    assert "禁止" in text


def test_prompts_rolling_forecast_excludes_basin_for_all_times():
    """query_rolling_forecast 规则须明确：流域问题无论今天/明天/未来都禁止调用。"""
    text = PROMPTS_PATH.read_text(encoding="utf-8")
    assert "无论今天、明天还是未来" in text
