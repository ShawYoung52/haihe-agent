"""新增业务 MCP 工具统一入口。"""
from __future__ import annotations

from .last_month_areal_rainfall_tool import register_last_month_areal_rainfall_tool
from .last_year_max_daily_rainfall_tool import register_last_year_max_daily_rainfall_tool
from .historical_same_period_rainfall_tool import register_historical_same_period_rainfall_tool
from .year_to_date_areal_rainfall_tool import register_year_to_date_areal_rainfall_tool
from .period_areal_rainfall_tool import register_period_areal_rainfall_tool

__all__ = [
    "register_last_month_areal_rainfall_tool",
    "register_last_year_max_daily_rainfall_tool",
    "register_historical_same_period_rainfall_tool",
    "register_year_to_date_areal_rainfall_tool",
    "register_period_areal_rainfall_tool",
]
