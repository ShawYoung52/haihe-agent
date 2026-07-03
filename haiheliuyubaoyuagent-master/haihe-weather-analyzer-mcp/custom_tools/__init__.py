"""新增业务 MCP 工具统一入口。

后续新增的专用业务工具不再直接堆在 MCP 根目录，统一放到/迁移到 custom_tools 包内，
server.py 只从这里导入注册函数。
"""
from __future__ import annotations

from last_month_areal_rainfall_tool import register_last_month_areal_rainfall_tool
from last_year_max_daily_rainfall_tool import register_last_year_max_daily_rainfall_tool
from historical_same_period_rainfall_tool import register_historical_same_period_rainfall_tool

__all__ = [
    "register_last_month_areal_rainfall_tool",
    "register_last_year_max_daily_rainfall_tool",
    "register_historical_same_period_rainfall_tool",
]
