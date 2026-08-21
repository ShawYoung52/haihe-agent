"""LLM 构造工厂 _build_chat_llm 的生成端限速测试。

回归背景（2026-08-12 性能排查）：
planner/answer 之前 `max_tokens` 靠 env 才有值、默认不设上限，模型可一直生成到
上下文上限；且全代码无 `extra_body`/`enable_thinking`，Qwen3 默认生成一大段
`<think>` 思考块。输入侧优化（prompt 拆分/历史裁剪）已到头（planner 输入仅 2064
字符仍 60s 超时），瓶颈在生成端。

口径（已与用户逐条确认）：
- 始终给 max_tokens 上限：PLANNER 默认 2048、ANSWER 默认 4096，env 可覆盖。
  （PLANNER 取 2048 而非 1024：planner 有时被复用为面向用户的完整回答，1024 可能截断。）
- 默认 Qwen3 模型关闭隐藏思考块；`LLM_DISABLE_THINKING=false` 可回退。
"""

import os
import sys
import types
from pathlib import Path

# 跳过 SQLAlchemyDataLayer 初始化，避免 asyncpg 依赖（与 test_chain_timeout.py 一致）
os.environ["CHAINLIT_ENABLE_DB"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# 只 mock 真正缺失的依赖（不 clobber 真实 chainlit）
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


class _CaptureChatOpenAI:
    """捕获构造 kwargs 的假 ChatOpenAI。"""

    last_kwargs = None

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs


@pytest.fixture
def captured(monkeypatch):
    import chain_gzt

    _CaptureChatOpenAI.last_kwargs = None
    monkeypatch.setattr(chain_gzt, "ChatOpenAI", _CaptureChatOpenAI)
    return chain_gzt


def test_planner_default_max_tokens_and_disables_qwen_thinking(captured, monkeypatch):
    monkeypatch.delenv("PLANNER_MAX_TOKENS", raising=False)
    monkeypatch.delenv("LLM_DISABLE_THINKING", raising=False)

    captured._build_chat_llm("PLANNER")

    assert _CaptureChatOpenAI.last_kwargs["max_tokens"] == 2048
    assert _CaptureChatOpenAI.last_kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    assert _CaptureChatOpenAI.last_kwargs["streaming"] is True


def test_answer_default_max_tokens(captured, monkeypatch):
    monkeypatch.delenv("ANSWER_MAX_TOKENS", raising=False)
    monkeypatch.delenv("LLM_DISABLE_THINKING", raising=False)

    captured._build_chat_llm("ANSWER")

    assert _CaptureChatOpenAI.last_kwargs["max_tokens"] == 4096
    assert _CaptureChatOpenAI.last_kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_env_max_tokens_overrides_default(captured, monkeypatch):
    monkeypatch.setenv("PLANNER_MAX_TOKENS", "512")
    monkeypatch.delenv("LLM_DISABLE_THINKING", raising=False)

    captured._build_chat_llm("PLANNER")

    assert _CaptureChatOpenAI.last_kwargs["max_tokens"] == 512


def test_disable_thinking_adds_extra_body(captured, monkeypatch):
    monkeypatch.delenv("PLANNER_MAX_TOKENS", raising=False)
    monkeypatch.setenv("LLM_DISABLE_THINKING", "true")

    captured._build_chat_llm("PLANNER")

    assert _CaptureChatOpenAI.last_kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_explicit_false_restores_thinking(captured, monkeypatch):
    monkeypatch.setenv("LLM_DISABLE_THINKING", "false")

    captured._build_chat_llm("PLANNER")

    assert "extra_body" not in _CaptureChatOpenAI.last_kwargs


def test_custom_non_qwen_model_does_not_disable_thinking_by_default(captured, monkeypatch):
    monkeypatch.delenv("LLM_DISABLE_THINKING", raising=False)
    monkeypatch.setenv("PLANNER_MODEL", "custom-model")

    captured._build_chat_llm("PLANNER")

    assert "extra_body" not in _CaptureChatOpenAI.last_kwargs


def test_split_prompts_are_default_with_legacy_rollback(captured, monkeypatch):
    monkeypatch.delenv("ENABLE_NEW_PLANNER_PROMPT", raising=False)
    monkeypatch.delenv("ENABLE_NEW_ANSWER_PROMPT", raising=False)
    planner_prompt, answer_prompt = captured._select_system_prompts()
    assert planner_prompt == captured.PLANNER_SYSTEM_PROMPT
    assert answer_prompt == captured.METEO_ANSWER_SYSTEM_PROMPT

    monkeypatch.setenv("ENABLE_NEW_PLANNER_PROMPT", "false")
    monkeypatch.setenv("ENABLE_NEW_ANSWER_PROMPT", "false")
    planner_prompt, answer_prompt = captured._select_system_prompts()
    assert planner_prompt == captured.WEATHER_ASSISTANT_PROMPT
    assert answer_prompt == captured.WEATHER_ASSISTANT_PROMPT
