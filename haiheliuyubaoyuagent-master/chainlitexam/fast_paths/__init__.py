"""Chainlit 前端自然语言快速路径统一入口。

后续新增 fast path 不再直接堆到 chainlitexam 根目录，统一放到/迁移到 fast_paths 包内，
并通过 install_all_fast_paths() 统一安装。
"""
from __future__ import annotations


def install_all_fast_paths() -> None:
    """统一安装前端快速路径。

    当前按依赖顺序安装：
    - 面雨量入口：上月面雨量，并由其链式安装子流域最多、今年累计等同入口路由；
    - 降雨分析入口：去年最大日降雨量、自动站最大雨量，并由自动站路由链式安装历史同期路由。
    """
    try:
        from last_month_areal_patch import install_last_month_areal_patch

        install_last_month_areal_patch()
    except Exception as exc:
        print(f"[fast_paths] last month areal route init failed: {exc}")

    try:
        from last_year_max_daily_rainfall_patch import install_last_year_max_daily_rainfall_patch

        install_last_year_max_daily_rainfall_patch()
    except Exception as exc:
        print(f"[fast_paths] last year max daily rainfall route init failed: {exc}")

    try:
        from max_auto_station_rainfall_patch import install_max_auto_station_rainfall_patch

        install_max_auto_station_rainfall_patch()
    except Exception as exc:
        print(f"[fast_paths] max auto station rainfall route init failed: {exc}")
