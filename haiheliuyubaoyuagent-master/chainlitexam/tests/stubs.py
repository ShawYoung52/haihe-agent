"""Minimal dependency stubs for running unit tests in bare environments.

This module is imported by tests that need to load ``chainlitexam`` modules
without requiring Chainlit, LangChain, or HTTPX to be installed.
"""

import sys
import types

__all__ = ["ensure_stubs"]


def _install_chainlit_stub():
    if "chainlit" in sys.modules:
        return sys.modules["chainlit"]

    # 真实 chainlit 已安装时直接用真包，绝不装假 stub——假 stub 不是包，
    # 会让后续 `import chainlit.data` / `chainlit.emitter`（chain_gzt、qa_http_api）
    # 报 "'chainlit' is not a package"。stub 只在裸环境（无 chainlit）才作兜底。
    try:
        import chainlit as real_chainlit

        return real_chainlit
    except ImportError:
        pass

    chainlit = types.ModuleType("chainlit")

    class Step:
        """Lightweight stand-in for ``chainlit.Step``."""

        _instances: list["Step"] = []

        def __init__(self, **kwargs):
            self.name = kwargs.get("name", "")
            self.parent_id = kwargs.get("parent_id")
            self.type = kwargs.get("type", "")
            self.input = ""
            self.output = ""
            self.show_input = ""
            self.id = kwargs.get("id") or f"mock-step-{len(Step._instances)}"
            Step._instances.append(self)

        async def send(self):
            pass

        async def update(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        @classmethod
        def reset(cls):
            cls._instances.clear()

    chainlit.Step = Step
    chainlit.Message = type(
        "Message",
        (),
        {
            "send": lambda self: None,
            "remove": lambda self: None,
            "__init__": lambda self, **kwargs: None,
        },
    )
    chainlit.user_session = types.SimpleNamespace(
        get=lambda *a, **k: None, set=lambda *a, **k: None
    )
    sys.modules["chainlit"] = chainlit
    return chainlit


def _install_langchain_stub():
    # 真实 langchain_core 已安装时跳过——假 stub 的 ToolMessage 会丢弃 tool_call_id。
    try:
        import langchain_core.messages  # noqa: F401

        return
    except ImportError:
        pass
    if "langchain_core" not in sys.modules:
        sys.modules["langchain_core"] = types.ModuleType("langchain_core")
    if "langchain_core.messages" not in sys.modules:
        lcms = types.ModuleType("langchain_core.messages")

        class _BaseMessage:
            def __init__(self, content: str = "", **kwargs):
                self.content = content

        for name in ("ToolMessage", "HumanMessage", "AIMessage"):
            setattr(lcms, name, type(name, (_BaseMessage,), {}))
        sys.modules["langchain_core.messages"] = lcms


def _install_httpx_stub():
    if "httpx" not in sys.modules:
        sys.modules["httpx"] = types.ModuleType("httpx")


def ensure_stubs():
    """Install all minimal stubs required to import chainlitexam modules."""
    _install_chainlit_stub()
    _install_langchain_stub()
    _install_httpx_stub()
