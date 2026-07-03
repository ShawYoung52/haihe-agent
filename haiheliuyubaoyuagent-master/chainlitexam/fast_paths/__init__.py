"""Chainlit 前端自然语言快速路径统一入口。"""
from __future__ import annotations

from .rainfall_fast_paths import install_all_fast_paths as _install_rainfall_fast_paths
from .areal_fallback_patch import install_areal_fallback_patch


def install_all_fast_paths() -> None:
    _install_rainfall_fast_paths()
    install_areal_fallback_patch()


__all__ = ["install_all_fast_paths"]
