"""Tests for _sanitize_display_text table-preserving behavior.

2026-08-31 内网"天津当前天气实况"：answer LLM 输出的规范多行 GFM 表格在 UI 里
渲染成原始 `|` 字符（用户："压根就没表格，全是字符"）。根因 = _sanitize_display_text
规则 3 的字符类含空白（`\\s`），每个以 `|` 开头的表格行（行首 | 前一字符是 `\\n`）都会命中，
在表头/分隔行/数据行之间全部插入空行——GFM 表格要求表头行与分隔行相邻，空行把
整张表拆成若干带 `|` 的普通段落，remark-gfm 不再识别为表格。
本文件锁定：表内行必须保持相邻（修复），正文后接表格仍插空行（保留原意图）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chainlitexam.tests.stubs import ensure_stubs

ensure_stubs()

from chainlitexam.message_orchestrator import _sanitize_display_text


def test_multiline_gfm_table_rows_stay_adjacent():
    """规范多行表格：表头行、分隔行、数据行之间不能插入空行（GFM 表格要求相邻）。"""
    content = (
        "|区域|平均降雨量|最大降雨量|\n"
        "|:---|:---|:---|\n"
        "|全市|0.0毫米|0.0毫米|\n"
        "|中心城区|0.0毫米|0.0毫米|\n"
        "|蓟州区|0.0毫米|0.0毫米|"
    )
    out = _sanitize_display_text(content)
    assert "|最大降雨量|\n|:---" in out, f"表头与分隔行之间被插入空行：{out!r}"
    assert "|:---|:---|:---|\n|全市" in out, f"分隔行与数据行之间被插入空行：{out!r}"
    assert "|0.0毫米|\n|中心城区" in out, f"相邻数据行之间被插入空行：{out!r}"
    # 表内任何两行之间都不该出现空行
    for block in ("|区域|平均降雨量|最大降雨量|\n\n|:---", "|:---|:---|:---|\n\n|全市"):
        assert block not in out, f"表格内部出现空行：{block!r} in {out!r}"


def test_current_weather_observation_full_answer_keeps_table():
    """2026-08-31 生产场景：前缀 + 核心结论 + 多行表格 + 数据来源。

    表格行保持相邻（可渲染），正文与表格之间、表格与数据来源之间保留空行。
    """
    content = (
        "已结合实况观测数据完成分析，为您整理结论如下：\n"
        "【核心结论】截至8月31日18时，天津市全市平均降雨量为0.0毫米，无降水。\n"
        "|区域|平均降雨量|最大降雨量|最大降雨站点|小时雨强|降水判断|\n"
        "|:---|:---|:---|:---|:---|:---|\n"
        "|天津市|0.0mm|0.0mm|滨海新区滨海经开区东区|0.0mm|无降水|\n"
        "|中心城区|0.0mm|0.0mm|河西区河西珠江里|0.0mm|无降水|\n"
        "|蓟州区|0.0mm|0.0mm|蓟州穿芳峪毛家峪|0.0mm|无降水|\n"
        "数据来源：天擎自动站"
    )
    out = _sanitize_display_text(content)
    # 表内行相邻（修复后此断言成立）
    assert "|小时雨强|降水判断|\n|:---" in out, f"表头/分隔行被拆开：{out!r}"
    assert "|:---|:---|:---|:---|:---|:---|\n|天津市" in out, f"分隔/数据行被拆开：{out!r}"
    assert "|无降水|\n|中心城区" in out, f"数据行被拆开：{out!r}"
    # 正文/表格边界仍有空行（原规则意图保留）
    assert "无降水。\n\n|区域" in out, f"正文后接表格应插空行：{out!r}"
    assert "|蓟州区|0.0mm|0.0mm|蓟州穿芳峪毛家峪|0.0mm|无降水|\n\n数据来源" in out, (
        f"表格后接数据来源应插空行：{out!r}"
    )


def test_prose_then_table_still_gets_blank_line():
    """原规则 3 意图保留：正文（。结尾）直接粘表格时插入空行。"""
    content = "今天全市无降水。\n|区域|平均降雨量|\n|:---|:---|\n|全市|0.0毫米|"
    out = _sanitize_display_text(content)
    assert "无降水。\n\n|区域|平均降雨量|" in out, f"正文后接表格缺空行：{out!r}"
    # 表格本身仍相邻
    assert "|平均降雨量|\n|:---" in out, f"表头/分隔行仍被拆开：{out!r}"


def test_title_then_table_stays_intact():
    """滚动预报 _weather_table 式结构：标题行 + 表头 + 分隔行 + 数据行。"""
    content = (
        "【今日预报】\n"
        "| 日期 | 天气现象 | 气温(℃) | 降水量(毫米) |\n"
        "| --- | --- | --- | --- |\n"
        "| 8月31日 | 多云 | 28 | 0 |"
    )
    out = _sanitize_display_text(content)
    assert "| 降水量(毫米) |\n| --- " in out, f"表头/分隔行被拆开：{out!r}"
    assert "| --- | --- | --- | --- |\n| 8月31日" in out, f"分隔/数据行被拆开：{out!r}"


def test_prose_with_pipe_not_table_unchanged():
    """非表格上下文（行内 `|` 后紧跟文字）不受守卫影响（行为不变）。"""
    content = "请注意：|重要提醒"
    out = _sanitize_display_text(content)
    assert "请注意：\n\n|重要提醒" in out, f"行内竖线规则应保持：{out!r}"


def test_compact_table_punct_cell_separator_not_split():
    """紧凑表内标点结尾单元格：`结论：|`/`（中雨）|` 是单元格分隔符，
    守卫只看行首 | 即可保持整行不动（code-review 2026-08-31 加固）。"""
    content = "|区域|结论：|备注|\n|:---|:---|\n|全市|0.0毫米|"
    out = _sanitize_display_text(content)
    assert "|区域|结论：|备注|" in out, f"行中标点|被拆行：{out!r}"
    assert "|区域|结论：|备注|\n|:---" in out, f"表头/分隔行被拆开：{out!r}"
    assert "|:---|:---|\n|全市" in out, f"分隔/数据行被拆开：{out!r}"


def test_compact_table_parenthesis_cell_separator_not_split():
    content = "|站点|降雨（中雨）|\n|:---|:---|\n|天津|20毫米|"
    out = _sanitize_display_text(content)
    assert "|站点|降雨（中雨）|" in out, f"括号结尾单元格被拆行：{out!r}"
    assert "|降雨（中雨）|\n|:---" in out, f"表头/分隔行被拆开：{out!r}"
    assert "|:---|:---|\n|天津" in out, f"分隔/数据行被拆开：{out!r}"


if __name__ == "__main__":
    test_multiline_gfm_table_rows_stay_adjacent()
    test_current_weather_observation_full_answer_keeps_table()
    test_prose_then_table_still_gets_blank_line()
    test_title_then_table_stays_intact()
    test_prose_with_pipe_not_table_unchanged()
    test_compact_table_punct_cell_separator_not_split()
    test_compact_table_parenthesis_cell_separator_not_split()
    print("All tests passed.")
