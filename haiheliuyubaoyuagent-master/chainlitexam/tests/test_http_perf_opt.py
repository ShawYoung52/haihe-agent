"""HTTP 模式性能优化 A1-A4 测试（均不改变问答结果）。

A1  HTTP 请求期间抑制 chainlit data-layer 落库（HTTP 客户端不读 DB，写库纯浪费）
A2  stream_text_to_message 按 execution_mode 累积更新（HTTP 下不逐块 update+sleep）
A3  _response_cache 无界增长修剪（内存泄漏修复）
A4  answer 流式连接错误重试一次（连接错误重试；非连接错误/超时保持原回退，不改变成功结果）
"""
import asyncio
import os
import sys
import time
import types
from pathlib import Path

# Skip SQLAlchemyDataLayer init at import time (avoid asyncpg dep), same as other tests
os.environ["CHAINLIT_ENABLE_DB"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Mock only truly missing deps (don't clobber real chainlit)
for _mod, _cls in (
    ("langchain_mcp_adapters", "MultiServerMCPClient"),
    ("langchain_mcp_adapters.client", "MultiServerMCPClient"),
    ("langchain_openai", "ChatOpenAI"),
):
    if _mod not in sys.modules:
        m = types.ModuleType(_mod)
        setattr(m, _cls, type(_cls, (), {}))
        sys.modules[_mod] = m

try:
    import psycopg2  # noqa: F401
except ImportError:
    _pg = types.ModuleType("psycopg2")
    _pg.connect = lambda *args, **kwargs: None
    _pg_extras = types.ModuleType("psycopg2.extras")
    _pg_extras.RealDictCursor = object
    _pg_pool = types.ModuleType("psycopg2.pool")
    _pg_pool.ThreadedConnectionPool = object
    _pg.extras = _pg_extras
    _pg.pool = _pg_pool
    sys.modules["psycopg2"] = _pg
    sys.modules["psycopg2.extras"] = _pg_extras
    sys.modules["psycopg2.pool"] = _pg_pool

try:
    import matplotlib.pyplot  # noqa: F401
except ImportError:
    _mpl = types.ModuleType("matplotlib")
    _plt = types.ModuleType("matplotlib.pyplot")
    _plt.rcParams = {}
    _fm = types.ModuleType("matplotlib.font_manager")
    _fm.fontManager = types.SimpleNamespace(addfont=lambda *args, **kwargs: None)
    _fm.FontProperties = lambda **kwargs: types.SimpleNamespace(get_name=lambda: "sans")
    _mpl.pyplot = _plt
    _mpl.font_manager = _fm
    sys.modules["matplotlib"] = _mpl
    sys.modules["matplotlib.pyplot"] = _plt
    sys.modules["matplotlib.font_manager"] = _fm

if "chainlit.data.sql_alchemy" not in sys.modules:
    _cl_sql = types.ModuleType("chainlit.data.sql_alchemy")
    _cl_sql.SQLAlchemyDataLayer = type("SQLAlchemyDataLayer", (), {})
    sys.modules["chainlit.data.sql_alchemy"] = _cl_sql

import pytest

import chain_gzt
import qa_http_api


class _Chunk:
    def __init__(self, text):
        self.content = text


class _StreamMsg:
    def __init__(self):
        self.content = ""
        self.update_count = 0

    async def update(self):
        self.update_count += 1


# ---------------------------------------------------------------- A1
def test_suppress_chainlit_data_layer_returns_none_and_restores():
    """抑制期间 get_data_layer() 返回 None（跳过写库），退出后恢复原 data layer。"""
    import chainlit.data as cl_data

    prev_layer = getattr(cl_data, "_data_layer", None)
    prev_init = getattr(cl_data, "_data_layer_initialized", False)
    try:
        cl_data._data_layer = object()
        cl_data._data_layer_initialized = False
        with qa_http_api._suppress_chainlit_data_layer():
            assert cl_data.get_data_layer() is None, "抑制期间 get_data_layer 应为 None"
            assert cl_data._data_layer is None
            assert cl_data._data_layer_initialized is True, "抑制期间禁止懒加载重建 data layer"
        assert cl_data._data_layer is not None, "退出后应恢复原 data layer"
        assert cl_data._data_layer_initialized is False, "退出后应恢复原 initialized 状态"
    finally:
        cl_data._data_layer = prev_layer
        cl_data._data_layer_initialized = prev_init


# ---------------------------------------------------------------- A2
@pytest.mark.asyncio
async def test_stream_text_http_mode_updates_once(monkeypatch):
    """execution_mode='http' 时只更新一次，不再逐 32 字块 update+sleep。"""
    monkeypatch.setattr(chain_gzt, "_attach_pending_images", lambda _: None)
    smsg = _StreamMsg()
    text = "海河流域今日多云，明日转晴。"
    await chain_gzt.stream_text_to_message(text, smsg, execution_mode="http")
    assert smsg.content == text
    assert smsg.update_count == 1, f"HTTP 模式应只更新 1 次，实际 {smsg.update_count}"


@pytest.mark.asyncio
async def test_stream_text_chainlit_default_chunks(monkeypatch):
    """默认 chainlit 模式保持逐块更新（渐进显示观感不变）。"""
    monkeypatch.setattr(chain_gzt, "_attach_pending_images", lambda _: None)
    smsg = _StreamMsg()
    text = "海河流域今日多云，明日转晴，请注意防范局地强对流天气带来的不利影响。"
    await chain_gzt.stream_text_to_message(text, smsg)
    assert smsg.content == text
    assert smsg.update_count > 1, "默认 chainlit 模式应逐块更新"


# ---------------------------------------------------------------- A3
def test_response_cache_prunes_expired(monkeypatch):
    """_response_cache 超限时修剪过期条目（防无界增长）。"""
    monkeypatch.setattr(qa_http_api, "RESPONSE_CACHE_MAX_SIZE", 1)
    monkeypatch.setattr(qa_http_api, "RESPONSE_CACHE_TTL_SECONDS", 300)
    q = qa_http_api.QARuntime()
    q._response_cache.clear()
    now = time.time()
    q._response_cache["stale"] = (now - 400, {"answer": "old"})
    q._response_cache["fresh"] = (now, {"answer": "new"})
    q._maybe_prune_response_cache()
    assert "stale" not in q._response_cache, "过期条目应被修剪"
    assert "fresh" in q._response_cache, "未过期条目应保留"


def test_response_cache_prune_keeps_below_max_size(monkeypatch):
    """未超限时不修剪（避免每请求白扫）。"""
    monkeypatch.setattr(qa_http_api, "RESPONSE_CACHE_MAX_SIZE", 100)
    q = qa_http_api.QARuntime()
    q._response_cache.clear()
    now = time.time()
    q._response_cache["stale"] = (now - 400, {"answer": "old"})
    q._response_cache["fresh"] = (now, {"answer": "new"})
    q._maybe_prune_response_cache()
    assert len(q._response_cache) == 2, "未超限不应修剪"


# ---------------------------------------------------------------- A4
@pytest.mark.asyncio
async def test_answer_retries_connection_error_once():
    """answer 流式连接错误（无部分内容）重试一次后成功。"""
    class _FakeChain:
        attempts = 0

        async def astream(self, input_dict, config=None):
            type(self).attempts += 1
            if type(self).attempts == 1:
                raise ConnectionError("连接被重置")
            for t in ["海河", "多云"]:
                yield _Chunk(t)

        async def ainvoke(self, input_dict, config=None):
            return _Chunk("海河多云")

    smsg = _StreamMsg()
    result = await chain_gzt.astream_answer_chain_to_message(
        _FakeChain(), {"messages": []}, smsg
    )
    assert result == "海河多云"
    assert _FakeChain.attempts == 2, "连接错误应重试一次"


@pytest.mark.asyncio
async def test_answer_connection_error_exhausts_then_ainvoke():
    """重试耗尽后回退 ainvoke（原兜底语义保留）。"""
    class _FakeChain:
        attempts = 0

        async def astream(self, input_dict, config=None):
            type(self).attempts += 1
            raise ConnectionError("持续连接失败")
            yield _Chunk("x")  # pragma: no cover — 保证 astream 是 async generator

        async def ainvoke(self, input_dict, config=None):
            return _Chunk("兜底答案")

    smsg = _StreamMsg()
    result = await chain_gzt.astream_answer_chain_to_message(
        _FakeChain(), {"messages": []}, smsg
    )
    assert result == "兜底答案"
    assert _FakeChain.attempts == 2, "重试耗尽后应 ainvoke 兜底"


@pytest.mark.asyncio
async def test_answer_non_connection_error_falls_back_immediately():
    """非连接错误不重试，直接回退 ainvoke（行为不变）。"""
    class _FakeChain:
        attempts = 0

        async def astream(self, input_dict, config=None):
            type(self).attempts += 1
            raise ValueError("模型内部错误")
            yield _Chunk("x")  # pragma: no cover — 保证 astream 是 async generator

        async def ainvoke(self, input_dict, config=None):
            return _Chunk("兜底答案")

    smsg = _StreamMsg()
    result = await chain_gzt.astream_answer_chain_to_message(
        _FakeChain(), {"messages": []}, smsg
    )
    assert result == "兜底答案"
    assert _FakeChain.attempts == 1, "非连接错误不重试，直接回退"


# ---------------------------------------------------------------- A5
@pytest.mark.asyncio
async def test_orchestrator_runtime_cache_reuses_same_day_and_rebuilds_next_day(monkeypatch):
    """网页会话共享运行时；日期变化时重建，避免 prompt 的“当前日期”过期。"""
    builds = []
    cache_key = ["2026-08-21|config-a"]

    async def fake_build():
        runtime = {"build": len(builds) + 1}
        builds.append(runtime)
        return runtime

    monkeypatch.setattr(chain_gzt, "_build_orchestrator_runtime", fake_build)
    monkeypatch.setattr(chain_gzt, "_orchestrator_runtime_cache_key", lambda: cache_key[0])
    chain_gzt._clear_orchestrator_runtime_cache()

    first = await chain_gzt._get_orchestrator_runtime()
    second = await chain_gzt._get_orchestrator_runtime()
    assert first is second
    assert len(builds) == 1

    cache_key[0] = "2026-08-22|config-a"
    third = await chain_gzt._get_orchestrator_runtime()
    assert third is not first
    assert len(builds) == 2


def test_orchestrator_runtime_cache_isolated_between_event_loops(monkeypatch):
    """不同事件循环不得共享可能绑定 loop 的 LLM/MCP runtime 或 asyncio.Lock。"""
    builds = []

    async def fake_build():
        runtime = {"build": len(builds) + 1}
        builds.append(runtime)
        return runtime

    monkeypatch.setattr(chain_gzt, "_build_orchestrator_runtime", fake_build)
    monkeypatch.setattr(
        chain_gzt,
        "_orchestrator_runtime_cache_key",
        lambda: "2026-08-21|config-a",
    )
    chain_gzt._clear_orchestrator_runtime_cache()

    first = asyncio.run(chain_gzt._get_orchestrator_runtime())
    second = asyncio.run(chain_gzt._get_orchestrator_runtime())

    assert first is not second
    assert len(builds) == 2


@pytest.mark.asyncio
async def test_http_runtime_refreshes_on_new_day(monkeypatch):
    """HTTP 自身的 runtime 缓存也应跨日失效。"""
    day = ["2026-08-21"]
    builds = []

    async def factory():
        runtime = {"build": len(builds) + 1}
        builds.append(runtime)
        return runtime

    monkeypatch.setattr(qa_http_api, "_runtime_epoch", lambda: day[0], raising=False)
    runtime = qa_http_api.QARuntime()
    runtime.configure(factory)

    first = await runtime._get_runtime()
    assert await runtime._get_runtime() is first
    day[0] = "2026-08-22"
    second = await runtime._get_runtime()
    assert second is not first
    assert len(builds) == 2
