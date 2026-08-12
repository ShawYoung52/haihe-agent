"""LLM 构造工厂 _build_chat_llm 的生成端限速测试。

回归背景（2026-08-12 性能排查）：
planner/answer 之前 `max_tokens` 靠 env 才有值、默认不设上限，模型可一直生成到
上下文上限；且全代码无 `extra_body`/`enable_thinking`，Qwen3 默认生成一大段
`<think>` 思考块。输入侧优化（prompt 拆分/历史裁剪）已到头（planner 输入仅 2064
字符仍 60s 超时），瓶颈在生成端。

口径（已与用户逐条确认）：
- 始终给 max_tokens 上限：PLANNER 默认 2048、ANSWER 默认 4096，env 可覆盖。
  （PLANNER 取 2048 而非 1024：planner 有时被复用为面向用户的完整回答，1024 可能截断。）
- 思考块开关 `LLM_DISABLE_THINKING` 默认关（生产行为不变），开启后经 extra_body
  传 `chat_template_kwargs.enable_thinking=False`。
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


def test_planner_default_max_tokens_and_no_extra_body(captured, monkeypatch):
    monkeypatch.delenv("PLANNER_MAX_TOKENS", raising=False)
    monkeypatch.delenv("LLM_DISABLE_THINKING", raising=False)

    captured._build_chat_llm("PLANNER")

    assert _CaptureChatOpenAI.last_kwargs["max_tokens"] == 2048
    assert "extra_body" not in _CaptureChatOpenAI.last_kwargs
    assert _CaptureChatOpenAI.last_kwargs["streaming"] is True


def test_answer_default_max_tokens(captured, monkeypatch):
    monkeypatch.delenv("ANSWER_MAX_TOKENS", raising=False)
    monkeypatch.delenv("LLM_DISABLE_THINKING", raising=False)

    captured._build_chat_llm("ANSWER")

    assert _CaptureChatOpenAI.last_kwargs["max_tokens"] == 4096
    assert "extra_body" not in _CaptureChatOpenAI.last_kwargs


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
