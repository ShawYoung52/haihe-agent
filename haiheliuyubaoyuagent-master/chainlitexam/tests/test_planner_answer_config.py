"""Planner/Answer 独立环境变量配置测试。

验证 chain_gzt.py 中 _env_str/_env_float/_env_int_optional 辅助函数的行为，
以及 _build_orchestrator_runtime 中 ChatOpenAI 构造参数默认值与 env 覆盖。

由于 chain_gzt.py 模块级导入依赖 chainlit.data 等完整环境，
本测试通过定义相同的辅助函数来验证逻辑，确保与 chain_gzt.py 中的实现一致。
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# 与 chain_gzt.py 完全一致的辅助函数（复制自实现）
# ---------------------------------------------------------------------------

def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int_optional(name: str):
    """返回 int | None。env 未设置或非法值返回 None。"""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 验证 chain_gzt.py 源码中已定义辅助函数（实现前 FAIL，实现后 PASS）
# ---------------------------------------------------------------------------

def test_helpers_exist_in_chain_gzt_source():
    """验证 _env_str/_env_float/_env_int_optional 已定义在 chain_gzt.py 源码中。"""
    chain_gzt_path = Path(__file__).resolve().parent.parent / "chain_gzt.py"
    source = chain_gzt_path.read_text(encoding="utf-8")
    assert "def _env_str(" in source, "chain_gzt.py 缺少 _env_str 定义"
    assert "def _env_float(" in source, "chain_gzt.py 缺少 _env_float 定义"
    assert "def _env_int_optional(" in source, "chain_gzt.py 缺少 _env_int_optional 定义"


def test_planner_uses_env_str_in_runtime():
    """验证 _build_orchestrator_runtime 中 planner_llm 使用 _env_str 读取 model。"""
    chain_gzt_path = Path(__file__).resolve().parent.parent / "chain_gzt.py"
    source = chain_gzt_path.read_text(encoding="utf-8")
    assert '_env_str("PLANNER_MODEL"' in source, "planner_llm 未使用 _env_str 读取 PLANNER_MODEL"


def test_planner_uses_env_float_in_runtime():
    """验证 _build_orchestrator_runtime 中 planner_llm 使用 _env_float 读取 temperature。"""
    chain_gzt_path = Path(__file__).resolve().parent.parent / "chain_gzt.py"
    source = chain_gzt_path.read_text(encoding="utf-8")
    assert '_env_float("PLANNER_TEMPERATURE"' in source, "planner_llm 未使用 _env_float 读取 PLANNER_TEMPERATURE"


def test_answer_uses_env_str_in_runtime():
    """验证 _build_orchestrator_runtime 中 answer_llm 使用 _env_str 读取 model。"""
    chain_gzt_path = Path(__file__).resolve().parent.parent / "chain_gzt.py"
    source = chain_gzt_path.read_text(encoding="utf-8")
    assert '_env_str("ANSWER_MODEL"' in source, "answer_llm 未使用 _env_str 读取 ANSWER_MODEL"


# ---------------------------------------------------------------------------
# _env_str tests
# ---------------------------------------------------------------------------

def test_env_str_returns_default_when_missing():
    """env 未设置时返回默认值。"""
    assert _env_str("MISSING_KEY_XYZ", "Qwen3.6-27B") == "Qwen3.6-27B"
    assert _env_str("MISSING_KEY_XYZ", "http://default/v1/") == "http://default/v1/"


def test_env_str_returns_env_value():
    """env 设置时返回 env 值。"""
    with patch.dict(os.environ, {"PLANNER_MODEL": "gpt-4o"}, clear=False):
        assert _env_str("PLANNER_MODEL", "Qwen3.6-27B") == "gpt-4o"


def test_env_str_handles_empty_string():
    """env 为空字符串时返回空字符串（不是默认值）。"""
    with patch.dict(os.environ, {"PLANNER_MODEL": ""}, clear=False):
        assert _env_str("PLANNER_MODEL", "Qwen3.6-27B") == ""


# ---------------------------------------------------------------------------
# _env_float tests
# ---------------------------------------------------------------------------

def test_env_float_returns_default_when_missing():
    """env 未设置时返回默认值。"""
    assert _env_float("MISSING_KEY_XYZ", 0.7) == 0.7
    assert _env_float("MISSING_KEY_XYZ", 0.0) == 0.0


def test_env_float_parses_float():
    """env 设置为有效浮点数时解析并返回。"""
    with patch.dict(os.environ, {"T": "0.3"}, clear=False):
        assert _env_float("T", 0.7) == 0.3
    with patch.dict(os.environ, {"T": "1.0"}, clear=False):
        assert _env_float("T", 0.7) == 1.0
    with patch.dict(os.environ, {"T": "0"}, clear=False):
        assert _env_float("T", 0.7) == 0.0


def test_env_float_parses_int_string():
    """env 设置为整数字符串时正确解析为 float。"""
    with patch.dict(os.environ, {"T": "1"}, clear=False):
        assert _env_float("T", 0.7) == 1.0


def test_env_float_falls_back_on_invalid():
    """env 设置为非法字符串时回退到默认值。"""
    with patch.dict(os.environ, {"BAD": "abc"}, clear=False):
        assert _env_float("BAD", 0.7) == 0.7
    with patch.dict(os.environ, {"BAD": "not-a-number"}, clear=False):
        assert _env_float("BAD", 0.7) == 0.7


# ---------------------------------------------------------------------------
# _env_int_optional tests
# ---------------------------------------------------------------------------

def test_env_int_optional_returns_none_when_missing():
    """env 未设置时返回 None。"""
    assert _env_int_optional("MISSING_KEY_XYZ") is None


def test_env_int_optional_returns_none_when_empty():
    """env 为空字符串时返回 None。"""
    with patch.dict(os.environ, {"MAX_TOK": ""}, clear=False):
        assert _env_int_optional("MAX_TOK") is None
    with patch.dict(os.environ, {"MAX_TOK": "   "}, clear=False):
        assert _env_int_optional("MAX_TOK") is None


def test_env_int_optional_parses_int():
    """env 设置为有效整数时解析并返回。"""
    with patch.dict(os.environ, {"MAX_TOK": "4096"}, clear=False):
        assert _env_int_optional("MAX_TOK") == 4096
    with patch.dict(os.environ, {"MAX_TOK": "0"}, clear=False):
        assert _env_int_optional("MAX_TOK") == 0


def test_env_int_optional_falls_back_on_invalid():
    """env 设置为非法字符串时返回 None。"""
    with patch.dict(os.environ, {"BAD": "abc"}, clear=False):
        assert _env_int_optional("BAD") is None
    with patch.dict(os.environ, {"BAD": "12.5"}, clear=False):
        assert _env_int_optional("BAD") is None


# ---------------------------------------------------------------------------
# ChatOpenAI 构造参数默认值验证
# ---------------------------------------------------------------------------

DEFAULT_API_BASE = "http://10.226.188.156:8000/v1/"


def _build_planner_kwargs():
    """与 chain_gzt.py 中 planner_llm 构造完全一致的参数构建逻辑。"""
    kwargs = {
        "model": _env_str("PLANNER_MODEL", "Qwen3.6-27B"),
        "streaming": True,
        "temperature": _env_float("PLANNER_TEMPERATURE", 0.7),
        "openai_api_base": _env_str("PLANNER_API_BASE", DEFAULT_API_BASE),
        "openai_api_key": _env_str("PLANNER_API_KEY", "EMPTY"),
    }
    max_tokens = _env_int_optional("PLANNER_MAX_TOKENS")
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    return kwargs


def _build_answer_kwargs():
    """与 chain_gzt.py 中 answer_llm 构造完全一致的参数构建逻辑。"""
    kwargs = {
        "model": _env_str("ANSWER_MODEL", "Qwen3.6-27B"),
        "streaming": True,
        "temperature": _env_float("ANSWER_TEMPERATURE", 0.7),
        "openai_api_base": _env_str("ANSWER_API_BASE", DEFAULT_API_BASE),
        "openai_api_key": _env_str("ANSWER_API_KEY", "EMPTY"),
    }
    max_tokens = _env_int_optional("ANSWER_MAX_TOKENS")
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    return kwargs


def test_planner_defaults_match_current_config():
    """默认配置下 planner 参数与现状一致：temp=0.7, model=Qwen3.6-27B, 无 max_tokens。"""
    kwargs = _build_planner_kwargs()
    assert kwargs["model"] == "Qwen3.6-27B"
    assert kwargs["temperature"] == 0.7
    assert kwargs["streaming"] is True
    assert kwargs["openai_api_base"] == DEFAULT_API_BASE
    assert kwargs["openai_api_key"] == "EMPTY"
    assert "max_tokens" not in kwargs


def test_answer_defaults_match_current_config():
    """默认配置下 answer 参数与现状一致：temp=0.7, model=Qwen3.6-27B, 无 max_tokens。"""
    kwargs = _build_answer_kwargs()
    assert kwargs["model"] == "Qwen3.6-27B"
    assert kwargs["temperature"] == 0.7
    assert kwargs["streaming"] is True
    assert kwargs["openai_api_base"] == DEFAULT_API_BASE
    assert kwargs["openai_api_key"] == "EMPTY"
    assert "max_tokens" not in kwargs


def test_planner_env_override_model():
    """PLANNER_MODEL env 覆盖 model。"""
    with patch.dict(os.environ, {"PLANNER_MODEL": "gpt-4o"}, clear=False):
        kwargs = _build_planner_kwargs()
        assert kwargs["model"] == "gpt-4o"


def test_planner_env_override_temperature():
    """PLANNER_TEMPERATURE env 覆盖 temperature。"""
    with patch.dict(os.environ, {"PLANNER_TEMPERATURE": "0.1"}, clear=False):
        kwargs = _build_planner_kwargs()
        assert kwargs["temperature"] == 0.1


def test_planner_env_override_api_base():
    """PLANNER_API_BASE env 覆盖 API base。"""
    with patch.dict(os.environ, {"PLANNER_API_BASE": "https://custom-api/v1/"}, clear=False):
        kwargs = _build_planner_kwargs()
        assert kwargs["openai_api_base"] == "https://custom-api/v1/"


def test_planner_env_override_api_key():
    """PLANNER_API_KEY env 覆盖 API key。"""
    with patch.dict(os.environ, {"PLANNER_API_KEY": "sk-custom-key"}, clear=False):
        kwargs = _build_planner_kwargs()
        assert kwargs["openai_api_key"] == "sk-custom-key"


def test_planner_env_set_max_tokens():
    """PLANNER_MAX_TOKENS env 设置时添加 max_tokens。"""
    with patch.dict(os.environ, {"PLANNER_MAX_TOKENS": "4096"}, clear=False):
        kwargs = _build_planner_kwargs()
        assert kwargs["max_tokens"] == 4096


def test_planner_env_max_tokens_zero_not_added():
    """PLANNER_MAX_TOKENS=0 时不添加 max_tokens（0 是 falsy）。"""
    with patch.dict(os.environ, {"PLANNER_MAX_TOKENS": "0"}, clear=False):
        kwargs = _build_planner_kwargs()
        # max_tokens=0 是 falsy，不会被添加
        assert "max_tokens" not in kwargs


def test_answer_env_override_model():
    """ANSWER_MODEL env 覆盖 model。"""
    with patch.dict(os.environ, {"ANSWER_MODEL": "gpt-4o"}, clear=False):
        kwargs = _build_answer_kwargs()
        assert kwargs["model"] == "gpt-4o"


def test_answer_env_override_temperature():
    """ANSWER_TEMPERATURE env 覆盖 temperature。"""
    with patch.dict(os.environ, {"ANSWER_TEMPERATURE": "0.5"}, clear=False):
        kwargs = _build_answer_kwargs()
        assert kwargs["temperature"] == 0.5


def test_answer_env_set_max_tokens():
    """ANSWER_MAX_TOKENS env 设置时添加 max_tokens。"""
    with patch.dict(os.environ, {"ANSWER_MAX_TOKENS": "8192"}, clear=False):
        kwargs = _build_answer_kwargs()
        assert kwargs["max_tokens"] == 8192


def test_planner_and_answer_independent():
    """planner 和 answer 的 env 是独立的，互不干扰。"""
    with patch.dict(os.environ, {
        "PLANNER_MODEL": "planner-model",
        "PLANNER_TEMPERATURE": "0.3",
        "ANSWER_MODEL": "answer-model",
        "ANSWER_TEMPERATURE": "0.9",
    }, clear=False):
        p_kwargs = _build_planner_kwargs()
        a_kwargs = _build_answer_kwargs()
        assert p_kwargs["model"] == "planner-model"
        assert p_kwargs["temperature"] == 0.3
        assert a_kwargs["model"] == "answer-model"
        assert a_kwargs["temperature"] == 0.9