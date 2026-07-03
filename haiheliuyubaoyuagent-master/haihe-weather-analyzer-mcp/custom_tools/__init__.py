"""新增业务 MCP 工具统一入口。"""
from __future__ import annotations

# 上月面雨量工具体量较大，暂时保留在 MCP 根目录；新增和已迁移工具从 custom_tools 内部导入。
from last_month_areal_rainfall_tool import register_last_month_areal_rainfall_tool
from .last_year_max_daily_rainfall_tool import register_last_year_max_daily_rainfall_tool
from .historical_same_period_rainfall_tool import register_historical_same_period_rainfall_tool

__all__ = [
    "register_last_month_areal_rainfall_tool",
    "register_last_year_max_daily_rainfall_tool",
    "register_historical_same_period_rainfall_tool",
]
