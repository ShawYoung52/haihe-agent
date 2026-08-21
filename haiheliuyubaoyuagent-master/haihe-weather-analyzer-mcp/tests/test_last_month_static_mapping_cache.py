from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


MCP_DIR = Path(__file__).resolve().parents[1]


def _load_module(monkeypatch):
    custom_tools = types.ModuleType("custom_tools")
    custom_tools.__path__ = [str(MCP_DIR / "custom_tools")]
    monkeypatch.setitem(sys.modules, "custom_tools", custom_tools)

    ttl_path = MCP_DIR / "custom_tools" / "_ttl_cache.py"
    ttl_spec = importlib.util.spec_from_file_location("custom_tools._ttl_cache", ttl_path)
    assert ttl_spec is not None and ttl_spec.loader is not None
    ttl_module = importlib.util.module_from_spec(ttl_spec)
    monkeypatch.setitem(sys.modules, "custom_tools._ttl_cache", ttl_module)
    ttl_spec.loader.exec_module(ttl_module)

    fastmcp = types.ModuleType("fastmcp")
    fastmcp.FastMCP = object
    monkeypatch.setitem(sys.modules, "fastmcp", fastmcp)

    psycopg2 = types.ModuleType("psycopg2")
    psycopg2.connect = lambda **_: None
    extras = types.ModuleType("psycopg2.extras")
    extras.RealDictCursor = object
    monkeypatch.setitem(sys.modules, "psycopg2", psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", extras)

    tools = types.ModuleType("tools")
    tools.config = {
        "postgres": {
            "host": "db",
            "port": "5432",
            "dbname": "weather",
            "schema": "public",
        }
    }
    monkeypatch.setitem(sys.modules, "tools", tools)

    module_path = MCP_DIR / "custom_tools" / "last_month_areal_rainfall_tool.py"
    spec = importlib.util.spec_from_file_location("last_month_mapping_under_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fine_to_zone9_mapping_reuses_canonical_cache_key(monkeypatch):
    module = _load_module(monkeypatch)
    calls = {"count": 0}

    def query(area_ids):
        calls["count"] += 1
        return {"1": {"zone9_code": "h9_001"}}

    monkeypatch.setattr(module, "_query_fine_area_ids_to_zone9", query)
    module._fine_to_zone9_cache.clear()

    first = module._map_fine_area_ids_to_zone9(["fine_001", "002"])
    second = module._map_fine_area_ids_to_zone9(["2", "fine_001"])

    assert first == second
    assert calls["count"] == 1

