"""Static checks for prompts.py rules."""
from __future__ import annotations

from pathlib import Path

import pytest
import prompts

PROMPTS_PATH = Path(__file__).resolve().parents[1] / "prompts.py"


@pytest.fixture(params=["PLANNER_SYSTEM_PROMPT", "WEATHER_ASSISTANT_PROMPT"])
def routing_prompt(request):
    return getattr(prompts, request.param)


def test_prompts_use_unified_river_forecast_entry(routing_prompt):
    assert "query_river_rainfall_forecast" in routing_prompt


def test_prompts_limit_unified_river_tool_to_supported_time_windows(routing_prompt):
    start = routing_prompt.index("#### 3.5 子流域未来天气查询规范")
    end = routing_prompt.index("### 4. 决策天气", start)
    section = routing_prompt[start:end]
    assert "今天/今日、明天/明日、后天、今天晚上/今晚、未来N天" in section
    assert "一周/周末、明确日期、其他日内时段或混合时段" in section
    assert "get_river_system_rainfall_forecast" in section
    assert "不得强制统一入口或改成今天" in section


def test_prompts_keep_basin_forecast_scope_and_observation_separate(routing_prompt):
    start = routing_prompt.index('用户问"流域"时区分实况与预报')
    end = routing_prompt.index("数据来源必须如实", start)
    basin_section = routing_prompt[start:end]
    assert "query_basin_areal_rainfall" in basin_section
    assert "实况" in basin_section
    assert "调用 `query_river_rainfall_forecast`" in basin_section
    assert "明确河系/流域复用九分区预报" in basin_section
    assert "未找到才回退已知河系" in basin_section
    assert "禁止调 `query_rolling_forecast`" in basin_section
    assert '禁止"以天津市代表海河流域"' in basin_section


def test_prompts_subbasin_entry_preserves_original_query_and_scope(routing_prompt):
    start = routing_prompt.index("#### 3.5 子流域未来天气查询规范")
    end = routing_prompt.index("### 4. 决策天气", start)
    subbasin_section = routing_prompt[start:end]
    assert "统一调用 `query_river_rainfall_forecast`" in subbasin_section
    assert "`user_query` 必须原样传入" in subbasin_section
    assert "具体河流优先匹配真实河道走廊，未找到才回退已知河系" in subbasin_section
    assert "明确河系/流域直接复用九分区预报" in subbasin_section
    assert "不得把全流域或天津市的预报直接套用到某个子流域上" in subbasin_section
    assert "不编造气温、风力、湿度等数据" in subbasin_section


def test_anti_redundancy_rule_present():
    """回答规范必须包含"只输出与当前问题直接相关的内容"强制规则。"""
    text = PROMPTS_PATH.read_text(encoding="utf-8")
    assert "只输出与当前问题直接相关的内容" in text
    # 反例示例：问市台预警不该给全市各区县
    assert "市台" in text or "全市" in text


def test_prompts_does_not_hardcode_ec_for_river_system(routing_prompt):
    """3.4 工具列表中，新工具条目必须要求引用 data_source 而非硬编码数据来源。"""
    start = routing_prompt.index("#### 3.4 降水相关工具列表")
    end = routing_prompt.index("#### 3.5", start)
    tool_list_section = routing_prompt[start:end]
    for line in tool_list_section.splitlines():
        if "query_river_rainfall_forecast" in line:
            assert "data_source" in line
            assert "只引用工具返回的降雨字段" in line
            assert "ECMWF" not in line
            break
    else:
        raise AssertionError("3.4 工具列表缺少 query_river_rainfall_forecast 条目")


def test_prompts_basin_rule_covers_today_wording(routing_prompt):
    """"今天海河流域天气怎么样"必须被流域预报规则覆盖，防止路由到天津滚动预报。"""
    start = routing_prompt.index('用户问"流域"时区分实况与预报')
    end = routing_prompt.index("数据来源必须如实", start)
    basin_section = routing_prompt[start:end]
    assert "今天海河流域天气怎么样" in basin_section
    assert "调用 `query_river_rainfall_forecast`" in basin_section
    assert "禁止调 `query_rolling_forecast`" in basin_section


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


def test_dynamic_event_dates_are_model_resolved_without_hardcoding_or_clarification():
    """节假日/高考日期会逐年变化，模型应解析明确日期窗口而不是使用固定日历或追问。"""
    import prompts

    for prompt in (prompts.PLANNER_SYSTEM_PROMPT, prompts.WEATHER_ASSISTANT_PROMPT):
        assert "节假日、高考等活动日期" in prompt
        assert "结合当前年份" in prompt
        assert "forecast_start_date" in prompt and "forecast_days" in prompt
        assert "不得硬编码" in prompt
        assert "无需向用户澄清" in prompt


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


def test_planner_prompt_knowledge_warning_question_carveout():
    """2026-08-21 修复：PLANNER 的"无生效预警"最高优先级规则必须排除知识性问法，
    防止"暴雨预警四个等级是什么"被引导成"当前无生效暴雨预警信号"。"""
    import prompts
    p = prompts.PLANNER_SYSTEM_PROMPT
    assert "当前无生效XX预警信号" in p
    assert "状态类" in p
    assert "知识性" in p
    assert "输出或追加" in p  # "**不得**输出或追加..."
    assert "四个等级是什么" in p


def test_answer_prompt_knowledge_warning_question_carveout():
    """2026-08-21 修复：旧版 WEATHER_ASSISTANT_PROMPT 的"无生效预警"规则同样排除知识性问法。"""
    import prompts
    p = prompts.WEATHER_ASSISTANT_PROMPT
    assert "当前无生效XX预警信号" in p
    assert "状态类" in p
    assert "知识性" in p
    assert "输出或追加" in p
    assert "四个等级是什么" in p


def test_new_answer_prompt_has_no_no_effective_warning_rule():
    """新版 METEO_ANSWER_SYSTEM_PROMPT 不含"当前无生效XX预警信号"最高优先级规则，
    知识性预警问法在 answer 端不会误追加该句（科普解释由 5.2 规则组织表格）。"""
    import prompts
    p = prompts.METEO_ANSWER_SYSTEM_PROMPT
    assert "当前无生效XX预警信号" not in p
    assert "科普解释类问题" in p
    assert "分级标准" in p
    assert "预警等级" in p


def test_longimg_trigger_covers_today_rain():
    """2026-08-21 验收 #4：问"今日雨情"应触发降水专题组合长图。

    双轨 prompt 的"降水专题组合长图"触发规则必须含"今日雨情/今天雨情"等今日+雨情
    问法并指明默认走本工具；否则 planner 不会为该问法调 generate_haihe_composite_longimg。
    """
    import prompts
    for p in (prompts.PLANNER_SYSTEM_PROMPT, prompts.WEATHER_ASSISTANT_PROMPT):
        header = "### 降水专题组合长图"
        start = p.find(header)
        assert start != -1, "组合长图规则段应存在"
        nxt = p.find("\n### ", start + len(header))
        section = p[start:nxt if nxt != -1 else start + 3000]
        assert "generate_haihe_composite_longimg" in section
        assert "今日雨情" in section
        assert "默认走本工具" in section
        # 明确"海河流域的雨情"不在此列（保持天擎站点分析路由）
        assert "海河流域的雨情" in section
