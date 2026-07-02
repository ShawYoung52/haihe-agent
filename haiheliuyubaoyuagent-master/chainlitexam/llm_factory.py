"""Model factory helpers for the Chainlit gateway."""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

from settings import get_settings


class ConfiguredChatOpenAI(ChatOpenAI):
    """ChatOpenAI wrapper that reads runtime values from settings.py."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        settings = get_settings()
        kwargs["model"] = settings.openai_model
        kwargs["openai_api_base"] = settings.openai_api_base
        kwargs["openai_api_key"] = settings.openai_api_key
        kwargs["temperature"] = settings.llm_temperature
        super().__init__(*args, **kwargs)
