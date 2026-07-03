"""Chainlit 前端自然语言快速路径统一入口。"""
from __future__ import annotations

from .rainfall_fast_paths import install_all_fast_paths as _install_rainfall_fast_paths
from .water_level_fast_paths import install_water_level_fast_paths
from .poi_weather_fast_paths import install_poi_weather_fast_paths


def install_all_fast_paths() -> None:
    _install_rainfall_fast_paths()
    install_water_level_fast_paths()
    install_poi_weather_fast_paths()


__all__ = ["install_all_fast_paths"]
