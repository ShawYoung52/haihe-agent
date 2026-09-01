"""_repair_markdown_layout 表格修复测试（2026-09-01，"表格没渲染出来"）。

根因：answer LLM 偶发把表格输出成 malformed markdown——① 表头被拆成多行
（`|区域|平均降雨量(mm)` 换行 `|最大降雨量(mm)` …）、② 小标题【...】粘到表头
且无分隔；GFM 要求表头/分隔行单行相邻，否则渲染成原始 `|`。
修复：_repair_markdown_layout 拼接被拆行的表头 + 把行首【标题】与表头拆分到独立行。
"""
import os
import sys
import types
from pathlib import Path

os.environ["CHAINLIT_ENABLE_DB"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_MISSING = {
    "langchain_mcp_adapters": "MultiServerMCPClient",
    "langchain_mcp_adapters.client": "MultiServerMCPClient",
    "langchain_openai": "ChatOpenAI",
}
for mod_name, class_name in _MISSING.items():
    if mod_name not in sys.modules:
        m = types.ModuleType(mod_name)
        setattr(m, class_name, type(class_name, (), {}))
        sys.modules[mod_name] = m

import pytest

pytest.importorskip("chainlit.data", reason="chain_gzt tests require the real Chainlit package")


@pytest.fixture(scope="module")
def repair():
    import chain_gzt
    return chain_gzt._repair_markdown_layout


def test_split_table_header_is_joined(repair):
    """被拆成多行的表头（紧贴 |---| 分隔行之上）拼回单行。"""
    text = (
        "【核心结论】截至9月1日17时，天津市当前无降水。\n"
        "|区域|平均降雨量(mm)\n"
        "|最大降雨量(mm)\n"
        "|最大小时降雨量(mm)\n"
        "|降水等级|\n"
        "|:---|:---|:---|:---|:---|\n"
        "|全市|0.0|0.0|0.0|无降水|"
    )
    out = repair(text)
    assert "|区域|平均降雨量(mm)|最大降雨量(mm)|最大小时降雨量(mm)|降水等级|" in out
    # 表头与分隔行相邻（GFM 要求）
    assert "降水等级|\n|:---|" in out


def test_heading_glued_to_table_is_separated(repair):
    """行首【标题】粘到表头 → 标题独占一行。"""
    text = (
        "|静海区|0.0|0.0|0.0|无降水|\n"
        "【天津市区灾害风险】|灾害类型|隐患点数量|\n"
        "|---|---|---|\n"
        "|中小河流|17处|本次无风险|"
    )
    out = repair(text)
    assert "【天津市区灾害风险】\n|灾害类型|" in out


def test_heading_inside_table_cell_not_touched(repair):
    """防误伤：单元格内的【...】后接 | 不是标题，不能拆。"""
    text = (
        "|名称|级别|\n"
        "|---|---|\n"
        "|【蓝色预警】暴雨|四级|"
    )
    out = repair(text)
    assert "|【蓝色预警】暴雨|四级|" in out


def test_well_formed_compact_table_unchanged(repair):
    """良构紧凑表（单行表头）不受影响。"""
    text = (
        "|区域|平均降雨量|\n"
        "|:---|:---|\n"
        "|全市|0.0|\n"
        "|蓟州|0.0|"
    )
    out = repair(text)
    assert "|区域|平均降雨量|\n|:---|:---|\n|全市|0.0|" in out


def test_well_formed_spaced_table_unchanged(repair):
    """良构带空格表（_markdown_table 生成风格）不受影响。"""
    text = (
        "| 灾害类型 | 隐患点数量 |\n"
        "| --- | --- |\n"
        "| 中小河流 | 17 处 |"
    )
    out = repair(text)
    assert "| 中小河流 | 17 处 |" in out


def test_heading_glued_and_blank_before_separator(repair):
    """标题粘表头 + 表头/分隔行间被插空行：拆分标题并把表头/分隔行合回相邻。

    _sanitize_display_text 规则 3 在"上一行以【开头"（标题粘表头）时误判，于表头与
    |---| 分隔行间插入空行；GFM 要求二者相邻，需合回。
    """
    text = (
        "【天津市区灾害风险】|灾害类型|隐患点数量|\n"
        "\n"
        "|---|---|---|\n"
        "|中小河流|17处|本次无风险|"
    )
    out = repair(text)
    assert "【天津市区灾害风险】\n|灾害类型|隐患点数量|\n|---|---|" in out
