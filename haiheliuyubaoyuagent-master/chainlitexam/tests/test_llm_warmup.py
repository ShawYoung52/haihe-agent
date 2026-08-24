"""LLM 预热测试。

验证 chain_gzt._llm_warmup：
- planner / answer 各 ainvoke 一次（用假 chain，不触发真实 LLM / MCP）。
- chain 抛错时预热不抛异常（失败兜底，不阻塞启动）。
"""

import os
import sys
import types
from pathlib import Path

# 跳过 SQLAlchemyDataLayer 初始化，避免 asyncpg 依赖（与 test_chain_timeout.py 一致）
os.environ["CHAINLIT_ENABLE_DB"] = "0"

# 指向仓库根目录（含 chainlitexam 包）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# 只 mock 真正缺失的依赖（不 clobber 真实 chainlit）
# 注意：_llm_warmup 只在调用时使用传入的 chain，import 期不触发 load_sse_tools()（MCP 连接）。
for _mod, _cls in (
    ("langchain_mcp_adapters", "MultiServerMCPClient"),
    ("langchain_mcp_adapters.client", "MultiServerMCPClient"),
    ("langchain_openai", "ChatOpenAI"),
):
    if _mod not in sys.modules:
        m = types.ModuleType(_mod)
        setattr(m, _cls, type(_cls, (), {}))
        sys.modules[_mod] = m

import pytest

pytest.importorskip("chainlit.data", reason="chain_gzt tests require the real Chainlit package")


class _FakeChain:
    def __init__(self):
        self.calls = 0

    async def ainvoke(self, input_dict, config=None):
        self.calls += 1
        return type("R", (), {"content": "好"})()


@pytest.mark.asyncio
async def test_llm_warmup_invokes_both_chains():
    """ENABLE_LLM_WARMUP=true 时 planner 和 answer 各 ainvoke 一次。"""
    import chain_gzt

    planner = _FakeChain()
    answer = _FakeChain()
    await chain_gzt._llm_warmup({"planner_chain": planner, "answer_chain": answer})
    assert planner.calls == 1
    assert answer.calls == 1


@pytest.mark.asyncio
async def test_llm_warmup_tolerates_failure():
    """预热失败（chain 抛错）不抛异常。"""
    import chain_gzt

    class _Broken:
        async def ainvoke(self, *a, **k):
            raise RuntimeError("boom")

    await chain_gzt._llm_warmup({"planner_chain": _Broken(), "answer_chain": _Broken()})
