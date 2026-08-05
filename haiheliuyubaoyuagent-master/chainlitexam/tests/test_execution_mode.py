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
