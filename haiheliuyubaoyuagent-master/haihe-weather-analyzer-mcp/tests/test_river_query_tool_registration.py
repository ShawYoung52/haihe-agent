"""Unified river-forecast MCP registration tests."""
from __future__ import annotations

import importlib.util
import sys
import types
import configparser
from pathlib import Path


MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))


def _module(name: str, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_tools_with_minimal_stubs():
    """Load the real registration source without optional data-service packages."""
    stubbed_names = (
        "networkx", "fastmcp", "requests", "pandas", "psycopg2", "psycopg2.pool",
        "psycopg2.extras", "analyzers", "analyzers.RainfallAnalyzer", "exception",
        "exception.CustomException", "models", "time_source", "haihe_mcp_tools", "constants",
        "emergency_scenario_client", "river_system_forecast", "river_query_forecast", "tools",
    )
    previous_modules = {name: sys.modules.get(name) for name in stubbed_names}
    _module("networkx", DiGraph=object, NetworkXError=Exception)
    _module("fastmcp", FastMCP=object)
    _module("requests", exceptions=types.SimpleNamespace(
        Timeout=TimeoutError, ConnectionError=ConnectionError
    ))
    _module("pandas", DataFrame=object)
    pool = _module("psycopg2.pool", ThreadedConnectionPool=object)
    extras = _module("psycopg2.extras", RealDictCursor=object)
    _module("psycopg2", connect=lambda **kwargs: None, pool=pool, extras=extras)
    _module("analyzers")
    _module("analyzers.RainfallAnalyzer", RainfallAnalyzer=lambda config: object())
    _module("exception")
    _module("exception.CustomException", BusinessException=Exception)
    _module("models", RainfallCityData=object)
    _module("time_source")
    _module("haihe_mcp_tools", register_haihe_tools=lambda mcp: None)
    _module(
        "constants",
        DEFAULT_BASIN_CODES="",
        DIRECTED_GRAPH_FILENAME="river.pkl",
        RIVER_TABLE_FULL="river_table",
    )
    _module(
        "emergency_scenario_client",
        emergency_http_base_url=lambda: "",
        fetch_scenario_get=lambda *args, **kwargs: {},
    )
    _module("river_system_forecast")
    _module("river_query_forecast", query_river_rainfall_forecast_core=lambda **kwargs: {})

    try:
        spec = importlib.util.spec_from_file_location("tools", MCP_DIR / "tools.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["tools"] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        for name, previous in previous_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


tools = _load_tools_with_minimal_stubs()


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(func):
            self.tools[func.__name__] = func
            return func

        return decorate


def test_register_tools_exposes_unified_river_forecast_and_forwards_raw_query(monkeypatch):
    registered = FakeMCP()
    observed = {}
    runtime_config = configparser.ConfigParser()
    runtime_config.read_dict({
        "paths": {"ecOutput": "D:/forecast"},
        "postgres": {"host": "db.internal", "dbname": "weather"},
    })

    def fake_core(**kwargs):
        observed.update(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(tools.rqf, "query_river_rainfall_forecast_core", fake_core)
    monkeypatch.setattr(tools, "config", runtime_config)

    tools.register_tools(registered)

    assert "query_river_rainfall_forecast" in registered.tools
    result = registered.tools["query_river_rainfall_forecast"]("明天泃河有雨吗？")
    assert result == {"status": "ok"}
    assert observed["user_query"] == "明天泃河有雨吗？"
    assert observed["config"] == {
        "paths": {"ecoutput": "D:/forecast"},
        "postgres": {"host": "db.internal", "dbname": "weather"},
    }
    assert observed["ec_output_path"] == "D:/forecast"
