"""Configured Chainlit entrypoint.

Use this module instead of running `chain_gzt.py` directly. It applies runtime
configuration first, then imports the legacy application and patches the model
class lookup used during session initialization.
"""

from __future__ import annotations

from settings import apply_env_defaults, get_settings
from llm_factory import ConfiguredChatOpenAI

settings = apply_env_defaults()

import chain_gzt as legacy_app  # noqa: E402

# `chain_gzt._init_runtime_session` resolves ChatOpenAI from its module globals
# at call time, so this replaces the hard-coded constructor behavior without
# rewriting the large legacy file in one risky change.
legacy_app.ChatOpenAI = ConfiguredChatOpenAI

print(
    "[ChainlitGateway] configured entrypoint loaded: "
    f"env={settings.app_env}, model={settings.openai_model}, "
    f"mcp={settings.mcp_weather_url}"
)

# Re-export callbacks, FastAPI app, and helper functions for Chainlit discovery.
for _name in dir(legacy_app):
    if not _name.startswith("__"):
        globals()[_name] = getattr(legacy_app, _name)

__all__ = [name for name in globals() if not name.startswith("_")]
