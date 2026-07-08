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
    if "langchain_core" not in sys.modules:
        sys.modules["langchain_core"] = types.ModuleType("langchain_core")
    if "langchain_core.messages" not in sys.modules:
        lcms = types.ModuleType("langchain_core.messages")
        for name in ("ToolMessage", "HumanMessage", "AIMessage"):
            setattr(lcms, name, type(name, (), {}))
        sys.modules["langchain_core.messages"] = lcms


def _install_httpx_stub():
    if "httpx" not in sys.modules:
        sys.modules["httpx"] = types.ModuleType("httpx")


def ensure_stubs():
    """Install all minimal stubs required to import chainlitexam modules."""
    _install_chainlit_stub()
    _install_langchain_stub()
    _install_httpx_stub()
