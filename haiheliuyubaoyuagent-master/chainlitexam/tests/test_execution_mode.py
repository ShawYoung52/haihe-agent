"""HTTP 执行模式不逐 chunk 刷新测试。"""
import os
import sys
import types
from pathlib import Path

# Skip the SQLAlchemyDataLayer init at import time to avoid asyncpg dependency
os.environ["CHAINLIT_ENABLE_DB"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Mock only truly MISSING dependencies (don't use ensure_stubs which clobbers real chainlit)
_MISSING_MODULES = {
    "langchain_mcp_adapters": "MultiServerMCPClient",
    "langchain_mcp_adapters.client": "MultiServerMCPClient",
    "langchain_openai": "ChatOpenAI",
}
for mod_name, class_name in _MISSING_MODULES.items():
    if mod_name not in sys.modules:
        m = types.ModuleType(mod_name)
        setattr(m, class_name, type(class_name, (), {}))
        sys.modules[mod_name] = m
# Link submodules to parent
if "langchain_mcp_adapters" in sys.modules:
    _mcp = sys.modules["langchain_mcp_adapters"]
    _mcp.client = sys.modules.get("langchain_mcp_adapters.client", types.ModuleType("langchain_mcp_adapters.client"))

import pytest

pytest.importorskip("chainlit.data", reason="chain_gzt tests require the real Chainlit package")


@pytest.mark.asyncio
async def test_http_mode_accumulates_answer_and_updates_once():
    """execution_mode='http' 时，astream_answer_chain_to_message 只调用一次 stream_msg.update()。"""
    import chain_gzt

    class _Chunk:
        def __init__(self, text):
            self.content = text

    class _FakeChain:
        async def astream(self, input_dict, config=None):
            for t in ["海河", "流域", "多云。"]:
                yield _Chunk(t)

        async def ainvoke(self, input_dict, config=None):
            return _Chunk("海河流域多云。")

    class _StreamMsg:
        def __init__(self):
            self.content = ""
            self.update_count = 0

        async def update(self):
            self.update_count += 1

    smsg = _StreamMsg()
    result = await chain_gzt.astream_answer_chain_to_message(
        _FakeChain(), {"messages": []}, smsg, execution_mode="http"
    )
    assert result == "海河流域多云。"
    assert smsg.content == "海河流域多云。"
    assert smsg.update_count == 1, f"HTTP 模式应只更新 1 次，实际 {smsg.update_count}"


@pytest.mark.asyncio
async def test_chainlit_mode_streams_per_chunk():
    import chain_gzt

    class _Chunk:
        def __init__(self, text):
            self.content = text

    class _FakeChain:
        async def astream(self, input_dict, config=None):
            for t in ["a", "b"]:
                yield _Chunk(t)

    class _StreamMsg:
        def __init__(self):
            self.content = ""
            self.update_count = 0

        async def update(self):
            self.update_count += 1

    smsg = _StreamMsg()
    result = await chain_gzt.astream_answer_chain_to_message(
        _FakeChain(), {"messages": []}, smsg, execution_mode="chainlit"
    )
    assert result == "ab"
    assert smsg.update_count > 1, "chainlit 模式应逐 chunk 更新"


@pytest.mark.asyncio
async def test_answer_return_value_has_repaired_table():
    """astream_answer_chain_to_message 返回值必须与展示一致：压成一行的表格在返回值里也拆好换行。

    2026-08-31 内网"天津当前天气实况"：answer LLM 输出 |...||:---|... 单行表格（模型偶发压行），
    函数内 _repair_markdown_layout 把 stream_msg.content 修好了，但返回值仍是未修复原文；
    调用方（_finalize_complete_tool_evidence 等）拿返回值覆盖 stream_msg.content → 表格又变回
    单行（|| 原样渲染）。返回值与展示内容必须一致（都过 _repair_markdown_layout）。
    """
    import chain_gzt

    class _Chunk:
        def __init__(self, text):
            self.content = text

    class _FakeChain:
        async def astream(self, input_dict, config=None):
            yield _Chunk(
                "【核心结论】截至8月31日18时，天津市无降水。\n"
                "|区域|平均降雨量|最大降雨量|最大降雨站点||:---|:---|:---|:---||全市|0.0毫米|0.0毫米|滨海新区滨海经开区东区||中心城区|0.0毫米|0.0毫米|河西区河西珠江里|\n"
                "数据来源：天擎自动站。"
            )

        async def ainvoke(self, input_dict, config=None):
            return _Chunk("")

    class _StreamMsg:
        def __init__(self):
            self.content = ""
            self.update_count = 0

        async def update(self):
            self.update_count += 1

    smsg = _StreamMsg()
    result = await chain_gzt.astream_answer_chain_to_message(
        _FakeChain(), {"messages": []}, smsg, execution_mode="http"
    )
    # 展示与返回值都不该再有 || 粘连，且表格行已被拆成规范多行
    assert "||" not in result, f"返回值仍含 ||：{result}"
    assert "||" not in smsg.content, f"stream_msg.content 仍含 ||：{smsg.content}"
    assert "|最大降雨站点|\n|:---" in result, f"表格行未被拆开：{result}"
    # 返回值与展示一致（调用方拿返回值覆盖 stream_msg.content 也不丢修复）
    assert result == smsg.content


@pytest.mark.asyncio
async def test_answer_keeps_multiline_table_rows_adjacent():
    """answer LLM 输出规范多行表格时，返回值与展示都保持表内行相邻（GFM 可渲染）。

    2026-08-31 内网"天津当前天气实况"：answer LLM 输出紧凑多行表格（|区域|.../|:---|...），
    _sanitize_display_text 规则 3 曾在每行行首 | 前插空行，表头与分隔行不再相邻，
    remark-gfm 把整张表拆成普通段落 → UI 显示原始 `|` 字符。修复后表内行必须相邻。
    """
    import chain_gzt

    class _Chunk:
        def __init__(self, text):
            self.content = text

    class _FakeChain:
        async def astream(self, input_dict, config=None):
            yield _Chunk(
                "【核心结论】截至8月31日18时，天津市全市平均降雨量为0.0毫米，无降水。\n"
                "|区域|平均降雨量|最大降雨量|最大降雨站点|小时雨强|降水判断|\n"
                "|:---|:---|:---|:---|:---|:---|\n"
                "|天津市|0.0mm|0.0mm|滨海新区滨海经开区东区|0.0mm|无降水|\n"
                "|中心城区|0.0mm|0.0mm|河西区河西珠江里|0.0mm|无降水|\n"
                "数据来源：天擎自动站"
            )

        async def ainvoke(self, input_dict, config=None):
            return _Chunk("")

    class _StreamMsg:
        def __init__(self):
            self.content = ""
            self.update_count = 0

        async def update(self):
            self.update_count += 1

    smsg = _StreamMsg()
    result = await chain_gzt.astream_answer_chain_to_message(
        _FakeChain(), {"messages": []}, smsg, execution_mode="http"
    )
    # 表内行相邻（表头与分隔行、分隔行与数据行都不能被空行拆开）
    assert "|小时雨强|降水判断|\n|:---" in result, f"表头/分隔行被拆开：{result}"
    assert "|:---|:---|:---|:---|:---|:---|\n|天津市" in result, f"分隔/数据行被拆开：{result}"
    assert "|无降水|\n|中心城区" in result, f"数据行被拆开：{result}"
    # 正文后接表格仍有空行（可渲染所需的段落分隔）
    assert "无降水。\n\n|区域" in result, f"正文/表格边界缺空行：{result}"
    assert result == smsg.content
