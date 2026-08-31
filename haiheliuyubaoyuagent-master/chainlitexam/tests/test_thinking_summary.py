"""Tests for the business summary prefix builder."""

import sys
from pathlib import Path

# Make ``import chainlitexam`` work when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Install minimal stubs for optional dependencies.
from chainlitexam.tests.stubs import ensure_stubs

ensure_stubs()

from chainlitexam.message_orchestrator import _build_thinking_summary, _prepend_thinking_summary


def test_rainfall_distribution_summary():
    result = _build_thinking_summary("海河流域降雨分布图")
    assert isinstance(result, str)
    assert result.startswith("已生成海河流域降水实况分布图")


def test_warning_summary():
    result = _build_thinking_summary("天津有哪些气象预警")
    assert isinstance(result, str)
    assert "预警" in result


def test_river_network_summary():
    result = _build_thinking_summary("海河流域河网水系情况")
    assert isinstance(result, str)
    assert "河网" in result


def test_water_level_summary():
    result = _build_thinking_summary("天津水位情况")
    assert isinstance(result, str)
    assert "水位" in result


def test_emergency_response_summary():
    result = _build_thinking_summary("防汛应急响应启动了吗")
    assert isinstance(result, str)
    assert "防汛" in result or "应急" in result


def test_basin_areal_rainfall_summary():
    result = _build_thinking_summary("各子流域面雨量对比")
    assert isinstance(result, str)
    assert "面雨量" in result


def test_city_avg_rainfall_summary():
    result = _build_thinking_summary("全市平均降雨量")
    assert isinstance(result, str)
    assert "平均" in result or "城市" in result


def test_rain_duration_summary():
    result = _build_thinking_summary("降雨时长统计")
    assert isinstance(result, str)
    assert "时长" in result


def test_history_or_extreme_summary():
    """Queries without a specific intent branch fall back to the generic prefix."""
    result = _build_thinking_summary("历史极端降雨事件")
    assert isinstance(result, str)
    assert result.startswith("已理解您的问题，为您解答如下：")


def test_forecast_summary():
    result = _build_thinking_summary("未来三天降雨如何")
    assert isinstance(result, str)
    assert "预报" in result


def test_today_summary():
    result = _build_thinking_summary("今天天津降水情况")
    assert isinstance(result, str)
    assert "实况" in result


def test_current_observation_query_gets_observation_prefix():
    """2026-08-31 内网复测"天津当前天气实况"：含 实况/现在/当前/实时 的查询是实况查询，
    前缀必须是"已结合实况观测数据"，不能因同时含"天气"被"预报"分支
    （["未来","预报","明天","后天","周末","天气"] 在观察词之前）截走。"""
    for q in ("天津当前天气实况", "现在天津天气", "当前天气实况", "天津现在的天气实况", "天津实时天气"):
        result = _build_thinking_summary(q)
        assert result == "已结合实况观测数据完成分析，为您整理结论如下：", f"{q} -> {result}"


def test_today_with_weather_stays_forecast_prefix():
    """"今天/今日"必须保持在预报分支之后：今天+天气 是预报问法（走滚动预报），
    不得因含"今天"被标成实况。防止后人把两组观察词合并/调序回归。"""
    for q in ("今天天津天气", "今天海河流域天气怎么样", "今日天气如何"):
        result = _build_thinking_summary(q)
        assert result == "已结合预报数据完成分析，为您整理结论如下：", f"{q} -> {result}"


def test_basin_weather_not_river_network_prefix():
    """流域天气问题不得误命中河网可视化前缀（"河流" 是 "海河流域" 的子串）。"""
    for q in ("今天海河流域天气怎么样", "明天海河流域天气怎么样", "大清河流域未来三天降雨"):
        result = _build_thinking_summary(q)
        assert not result.startswith("已绘制河网可视化"), f"{q} -> {result}"


def test_tomorrow_basin_weather_is_forecast_prefix():
    result = _build_thinking_summary("明天海河流域天气怎么样")
    assert "预报" in result


def test_standalone_river_word_still_matches_river_network():
    result = _build_thinking_summary("海河下游河流有哪些")
    assert "河网" in result


def test_empty_query():
    assert _build_thinking_summary("") == ""


def test_has_chart():
    result = _build_thinking_summary("未来三天降雨如何", has_chart=True)
    assert "并生成相关图表" in result


def test_prepend_strips_llm_imitated_summary_prefix():
    """2026-08-31 内网"天津当前天气实况"重复摘要：answer LLM 仿写历史里带前缀的回答，
    开头又产出一句"已结合实况观测数据…如下："，代码再前置正确摘要→两句叠加。
    前置正确摘要前必须剥离开头已有的摘要行（只剩一句）。"""
    body = "已结合实况观测数据完成分析，为您整理结论如下：\n\n正文内容"
    out = _prepend_thinking_summary(body, "天津当前天气实况")
    assert out.startswith("已结合实况观测数据完成分析，为您整理结论如下：")
    assert out.count("完成分析") == 1
    assert out.count("如下：") == 1
    assert "正文内容" in out


def test_prepend_strips_multiple_stacked_summaries():
    body = (
        "已结合预报数据完成分析，为您整理结论如下：\n\n"
        "已结合实况观测数据完成分析，为您整理结论如下：\n\n"
        "正文内容"
    )
    out = _prepend_thinking_summary(body, "天津当前天气实况")
    assert out == "已结合实况观测数据完成分析，为您整理结论如下：\n\n正文内容"


def test_prepend_normal_case_unchanged():
    out = _prepend_thinking_summary("正文内容", "今天天津降水情况")
    assert out == "已结合实况观测数据完成分析，为您整理结论如下：\n\n正文内容"


def test_prepend_strips_chart_variant_summary():
    body = "已结合预报数据完成分析，并生成相关图表，为您整理结论如下：\n\n正文内容"
    out = _prepend_thinking_summary(body, "未来三天降雨如何", has_chart=True)
    assert out.count("如下：") == 1
    assert "正文内容" in out


def test_prepend_strips_halfwidth_colon_summary():
    """answer LLM 若用半角冒号仿写摘要前缀，也要剥掉，否则重复摘要仍在（只剩一句）。"""
    body = "已结合实况观测数据完成分析,为您整理结论如下:\n正文内容"
    out = _prepend_thinking_summary(body, "天津当前天气实况")
    assert out.count("完成分析") == 1
    assert "正文内容" in out


def test_prepend_does_not_strip_non_summary_content():
    """不以"已…如下："开头的正常回答不被误剥。"""
    body = "目前天津以晴为主，气温 25℃。"
    out = _prepend_thinking_summary(body, "天津当前天气实况")
    assert out == "已结合实况观测数据完成分析，为您整理结论如下：\n\n" + body


def test_prepend_warning_no_effective_guard_unchanged():
    out = _prepend_thinking_summary("当前无生效暴雨预警信号", "天津有哪些预警")
    assert out == "当前无生效暴雨预警信号"


if __name__ == "__main__":
    test_rainfall_distribution_summary()
    test_warning_summary()
    test_river_network_summary()
    test_water_level_summary()
    test_emergency_response_summary()
    test_basin_areal_rainfall_summary()
    test_city_avg_rainfall_summary()
    test_rain_duration_summary()
    test_history_or_extreme_summary()
    test_forecast_summary()
    test_today_summary()
    test_empty_query()
    test_has_chart()
    print("All tests passed.")
