"""Centralized settings for the Chainlit gateway.

This module is intentionally dependency-light so it can be imported before the
legacy `chain_gzt.py` module. It normalizes environment defaults and prevents
legacy hard-coded fallback values from taking effect during normal startup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


_TRUE_VALUES = {"1", "true", "yes", "on", "y"}


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是整数，当前值: {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是数字，当前值: {raw!r}") from exc


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name, "1" if default else "0").lower()
    return raw in _TRUE_VALUES


@dataclass(frozen=True)
class ChainlitGatewaySettings:
    app_env: str

    chainlit_host: str
    chainlit_port: int
    chainlit_enable_db: bool
    chainlit_auth_secret: str

    chainlit_db_host: str
    chainlit_db_port: int
    chainlit_db_name: str
    chainlit_db_user: str
    chainlit_db_password: str
    chainlit_db_schema: str
    chainlit_db_sslmode: str
    chainlit_db_connect_timeout: int

    admin_username: str
    admin_password: str

    river_plot_db_host: str
    river_plot_db_port: int
    river_plot_db_name: str
    river_plot_db_user: str
    river_plot_db_password: str
    river_plot_db_connect_timeout: int
    river_plot_pool_maxconn: int

    mcp_weather_url: str
    mcp_extreme_weather_url: str

    openai_model: str
    openai_api_base: str
    openai_api_key: str
    llm_temperature: float

    stream_delay_ms: float

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"prod", "production"}

    def validate(self) -> None:
        """Fail fast for production-only unsafe configuration."""
        if not self.is_production:
            return
        weak_values = {"", "postgres", "admin123", "change-me", "change-me-local-admin", "replace-with-a-strong-password"}
        checks = {
            "CHAINLIT_AUTH_SECRET": self.chainlit_auth_secret,
            "CHAINLIT_ADMIN_PASSWORD": self.admin_password,
            "CHAINLIT_DB_PASSWORD": self.chainlit_db_password,
            "OPENAI_API_KEY": self.openai_api_key,
        }
        bad = [name for name, value in checks.items() if value in weak_values]
        if bad:
            raise RuntimeError("生产环境缺少安全配置: " + ", ".join(bad))


@lru_cache(maxsize=1)
def get_settings() -> ChainlitGatewaySettings:
    settings = ChainlitGatewaySettings(
        app_env=_env("APP_ENV", "local"),
        chainlit_host=_env("CHAINLIT_HOST", "0.0.0.0"),
        chainlit_port=_env_int("CHAINLIT_PORT", 8003),
        chainlit_enable_db=_env_bool("CHAINLIT_ENABLE_DB", True),
        chainlit_auth_secret=_env("CHAINLIT_AUTH_SECRET", "chainlit-local-dev-secret-change-me"),
        chainlit_db_host=_env("CHAINLIT_DB_HOST", "127.0.0.1"),
        chainlit_db_port=_env_int("CHAINLIT_DB_PORT", 5432),
        chainlit_db_name=_env("CHAINLIT_DB_NAME", "tjznt"),
        chainlit_db_user=_env("CHAINLIT_DB_USER", "postgres"),
        chainlit_db_password=_env("CHAINLIT_DB_PASSWORD", "postgres"),
        chainlit_db_schema=_env("CHAINLIT_DB_SCHEMA", "public"),
        chainlit_db_sslmode=_env("CHAINLIT_DB_SSLMODE", "disable"),
        chainlit_db_connect_timeout=_env_int("CHAINLIT_DB_CONNECT_TIMEOUT", 5),
        admin_username=_env("CHAINLIT_ADMIN_USERNAME", "admin"),
        admin_password=_env("CHAINLIT_ADMIN_PASSWORD", "change-me-local-admin"),
        river_plot_db_host=_env("DB_HOST", "127.0.0.1"),
        river_plot_db_port=_env_int("DB_PORT", 5432),
        river_plot_db_name=_env("DB_NAME", "postgres"),
        river_plot_db_user=_env("DB_USER", "postgres"),
        river_plot_db_password=_env("DB_PASSWORD", "postgres"),
        river_plot_db_connect_timeout=_env_int("DB_CONNECT_TIMEOUT", 5),
        river_plot_pool_maxconn=_env_int("RIVER_PLOT_PG_POOL_MAXCONN", 5),
        mcp_weather_url=_env("MCP_WEATHER_URL", _env("MCP_SERVER_URL", "http://127.0.0.1:3333/sse")),
        mcp_extreme_weather_url=_env("MCP_EXTREME_WEATHER_URL", _env("EXTRM_SERVER_URL", "")),
        openai_model=_env("OPENAI_MODEL", "Qwen3.6-27B"),
        openai_api_base=_env("OPENAI_API_BASE", "http://127.0.0.1:8000/v1/"),
        openai_api_key=_env("OPENAI_API_KEY", "EMPTY"),
        llm_temperature=_env_float("LLM_TEMPERATURE", 0.7),
        stream_delay_ms=_env_float("CHAINLIT_STREAM_DELAY_MS", 0.5),
    )
    settings.validate()
    return settings


def apply_env_defaults(settings: ChainlitGatewaySettings | None = None) -> ChainlitGatewaySettings:
    """Populate legacy environment variables before importing `chain_gzt.py`."""
    settings = settings or get_settings()
    defaults = {
        "CHAINLIT_HOST": settings.chainlit_host,
        "CHAINLIT_PORT": str(settings.chainlit_port),
        "CHAINLIT_ENABLE_DB": "1" if settings.chainlit_enable_db else "0",
        "CHAINLIT_AUTH_SECRET": settings.chainlit_auth_secret,
        "CHAINLIT_DB_HOST": settings.chainlit_db_host,
        "CHAINLIT_DB_PORT": str(settings.chainlit_db_port),
        "CHAINLIT_DB_NAME": settings.chainlit_db_name,
        "CHAINLIT_DB_USER": settings.chainlit_db_user,
        "CHAINLIT_DB_PASSWORD": settings.chainlit_db_password,
        "CHAINLIT_DB_SCHEMA": settings.chainlit_db_schema,
        "CHAINLIT_DB_SSLMODE": settings.chainlit_db_sslmode,
        "CHAINLIT_DB_CONNECT_TIMEOUT": str(settings.chainlit_db_connect_timeout),
        "CHAINLIT_ADMIN_USERNAME": settings.admin_username,
        "CHAINLIT_ADMIN_PASSWORD": settings.admin_password,
        "DB_HOST": settings.river_plot_db_host,
        "DB_PORT": str(settings.river_plot_db_port),
        "DB_NAME": settings.river_plot_db_name,
        "DB_USER": settings.river_plot_db_user,
        "DB_PASSWORD": settings.river_plot_db_password,
        "DB_CONNECT_TIMEOUT": str(settings.river_plot_db_connect_timeout),
        "RIVER_PLOT_PG_POOL_MAXCONN": str(settings.river_plot_pool_maxconn),
        "MCP_SERVER_URL": settings.mcp_weather_url,
        "MCP_WEATHER_URL": settings.mcp_weather_url,
        "OPENAI_MODEL": settings.openai_model,
        "OPENAI_API_BASE": settings.openai_api_base,
        "OPENAI_API_KEY": settings.openai_api_key,
        "LLM_TEMPERATURE": str(settings.llm_temperature),
        "CHAINLIT_STREAM_DELAY_MS": str(settings.stream_delay_ms),
    }
    if settings.mcp_extreme_weather_url:
        defaults["EXTRM_SERVER_URL"] = settings.mcp_extreme_weather_url
        defaults["MCP_EXTREME_WEATHER_URL"] = settings.mcp_extreme_weather_url

    for key, value in defaults.items():
        os.environ.setdefault(key, value)
    return settings
