"""新增业务 MCP 工具统一入口。"""
from __future__ import annotations

from .last_month_areal_rainfall_tool import register_last_month_areal_rainfall_tool
from .last_year_max_daily_rainfall_tool import register_last_year_max_daily_rainfall_tool
from .historical_same_period_rainfall_tool import register_historical_same_period_rainfall_tool
from .year_to_date_areal_rainfall_tool import register_year_to_date_areal_rainfall_tool
from .poi_nearest_observation_tool import register_poi_nearest_observation_tool
from .risk_warning_tool import register_risk_warning_tool
from .safe_emergency_response_tool import register_safe_emergency_response_tool

__all__ = [
    "register_last_month_areal_rainfall_tool",
    "register_last_year_max_daily_rainfall_tool",
    "register_historical_same_period_rainfall_tool",
    "register_year_to_date_areal_rainfall_tool",
    "register_poi_nearest_observation_tool",
    "register_risk_warning_tool",
    "register_safe_emergency_response_tool",
]
