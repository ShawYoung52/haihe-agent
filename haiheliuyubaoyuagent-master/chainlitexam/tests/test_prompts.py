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


def test_anti_redundancy_rule_present():
    """回答规范必须包含"只输出与当前问题直接相关的内容"强制规则。"""
    text = PROMPTS_PATH.read_text(encoding="utf-8")
    assert "只输出与当前问题直接相关的内容" in text
    # 反例示例：问市台预警不该给全市各区县
    assert "市台" in text or "全市" in text


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


def test_thinking_prompts_require_scope_consistency():
    """思考助手 prompt 必须约束地域口径：未提流域不得出现"海河流域"字样。"""
    import re as _re

    text = PROMPTS_PATH.read_text(encoding="utf-8")
    for name in ("THINKING_PROMPT", "FAST_PATH_THINKING_PROMPT"):
        m = _re.search(r"(?<![A-Z_])" + name + r'\s*=\s*"""(.*?)"""', text, _re.S)
        assert m, f"{name} 未找到"
        body = m.group(1)
        assert "地域" in body and "一致" in body, f"{name} 缺少地域一致性约束"
        assert "不得出现" in body and "海河流域" in body, f"{name} 缺少海河流域字样的禁用条款"


def test_prompt_split_constants_exist():
    """新 Prompt 常量存在且与旧 Prompt 不同。"""
    import prompts
    assert hasattr(prompts, "PLANNER_SYSTEM_PROMPT")
    assert hasattr(prompts, "METEO_ANSWER_SYSTEM_PROMPT")
    assert isinstance(prompts.PLANNER_SYSTEM_PROMPT, str) and len(prompts.PLANNER_SYSTEM_PROMPT) > 100
    assert isinstance(prompts.METEO_ANSWER_SYSTEM_PROMPT, str) and len(prompts.METEO_ANSWER_SYSTEM_PROMPT) > 100
    # 旧 Prompt 不变
    assert len(prompts.WEATHER_ASSISTANT_PROMPT) > 500


def test_planner_prompt_has_no_formatting():
    """PLANNER_SYSTEM_PROMPT 不含 Markdown 格式/回答结构/语言风格。"""
    import prompts
    p = prompts.PLANNER_SYSTEM_PROMPT
    assert "表格格式规范" not in p  # 无表格格式章节
    assert "回答结构" not in p  # 无回答结构章节
    assert "### 语言风格" not in p  # 无语言风格章节


def test_planner_prompt_has_tool_rules():
    """PLANNER_SYSTEM_PROMPT 含工具选择/参数/停止条件。"""
    import prompts
    p = prompts.PLANNER_SYSTEM_PROMPT
    assert "有工具必须用工具" in p or "调用工具获取真实数据" in p
    assert "决策天气" in p or "query_decision_weather" in p
    assert "单维度" in p  # 停止条件


def test_answer_prompt_has_expression_rules():
    """METEO_ANSWER_SYSTEM_PROMPT 含气象表达/格式/结论结构。"""
    import prompts
    p = prompts.METEO_ANSWER_SYSTEM_PROMPT
    assert "核心结论" in p
    assert "表格" in p or "| :---" in p
    assert "数据来源" in p


def test_answer_prompt_has_no_tool_selection():
    """METEO_ANSWER_SYSTEM_PROMPT 不含工具选择/路由规则。"""
    import prompts
    p = prompts.METEO_ANSWER_SYSTEM_PROMPT
    assert "有工具必须用工具" not in p
    assert "query_decision_weather_for_poi" not in p
