"""Tests for chainlitexam.tools.rainfall_river_impact."""
from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import pytest

# rainfall_river_impact.py 懒加载 MCP 模块；测试里直接 mock 掉加载函数即可，
# 避免引入 pandas/psycopg2/fastmcp 等 MCP 侧重依赖。
from chainlitexam.tools.rainfall_river_impact import build_rainfall_river_impact_tools


def _mock_mcp_modules() -> tuple[types.ModuleType, types.ModuleType]:
    impact_mod = types.ModuleType("mcp_fixed_rainfall_impact")
    impact_mod.build_affected_river_network_result = MagicMock(
        return_value={
            "time_range_readable": "2026-07-12 08:00:00 ~ 2026-07-13 08:00:00",
            "affected_rivers": ["东河"],
            "segments": [],
            "stations": [],
        }
    )
    tools_mod = types.ModuleType("mcp_tools")
    tools_mod._analyze_rainfall_core = MagicMock()
    tools_mod.RAIN_LEVELS = [("暴雨", 50.0, 99.9), ("大暴雨", 100.0, 249.9), ("特大暴雨", 250.0, 9999.0)]
    return impact_mod, tools_mod


def _mock_config():
    real_configparser = __import__("configparser", fromlist=["ConfigParser"])
    parser = real_configparser.ConfigParser()
    parser.add_section("postgres")
    parser.set("postgres", "host", "10.0.0.1")
    parser.set("postgres", "port", "5432")
    parser.set("postgres", "dbname", "hhly")
    parser.set("postgres", "user", "user")
    parser.set("postgres", "password", "pass")
    parser.add_section("paths")
    parser.set("paths", "graph", r"E:\tj\line\result\river_directed.pkl")
    return parser


def _call_tool(tool, **kwargs):
    """兼容 decision_weather 测试安装的 _ToolWrapper stub 与真实 StructuredTool。"""
    if hasattr(tool, "func"):
        return tool.func(**kwargs)
    return tool._fn(**kwargs)


def _run_patched_tool(**kwargs):
    """Mock MCP 依赖后执行本地工具，返回 (tool_result, impact_mock)。"""
    impact_mod, tools_mod = _mock_mcp_modules()
    config = _mock_config()
    with patch(
        "chainlitexam.tools.rainfall_river_impact._load_mcp_modules",
        return_value=(impact_mod, tools_mod),
    ), patch(
        "chainlitexam.tools.rainfall_river_impact._load_mcp_config",
        return_value=config,
    ):
        tools = build_rainfall_river_impact_tools()
        tool = tools[0]
        return _call_tool(tool, **kwargs), impact_mod


def test_local_tool_passes_direct_match_km_default():
    """本地工具应将默认 direct_graph_match_km 透传给 builder，当前为 10km。"""
    result, impact_mod = _run_patched_tool(
        time_str="20250713080000",
        start_time="20250712080000",
        end_time="20250713080000",
    )
    assert result["affected_rivers"] == ["东河"]
    call_kwargs = impact_mod.build_affected_river_network_result.call_args.kwargs
    assert call_kwargs["direct_graph_match_km"] == 10.0
    assert call_kwargs["downstream_km"] == 50.0
    assert call_kwargs["rainfall_threshold_mm"] == 50.0


def test_local_tool_allows_custom_direct_match_km():
    """用户显式传入 direct_graph_match_km 时应被透传。"""
    _, impact_mod = _run_patched_tool(
        time_str="20250713080000",
        direct_graph_match_km=15.0,
    )
    call_kwargs = impact_mod.build_affected_river_network_result.call_args.kwargs
    assert call_kwargs["direct_graph_match_km"] == 15.0
