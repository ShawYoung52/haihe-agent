"""区域综合风险 MCP 工具注册边界测试。"""
from __future__ import annotations

import importlib.util
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


MCP_DIR = Path(__file__).resolve().parents[1]
MODULE_NAME = "_region_risk_registration_haihe_mcp_tools"


class FakeMCP:
    """只记录真实注册函数名，避免依赖 FastMCP 服务运行时。"""

    def __init__(self) -> None:
        self.tool_names: list[str] = []

    def tool(self):
        def register(function):
            self.tool_names.append(function.__name__)
            return function

        return register


@contextmanager
def _isolated_haihe_mcp_tools() -> Iterator[types.ModuleType]:
    """以测试专用模块名导入，并恢复本测试临时写入的模块状态。"""
    original_path = list(sys.path)
    original_module_names = set(sys.modules)
    original_fastmcp = sys.modules.get("fastmcp")
    fake_fastmcp = types.ModuleType("fastmcp")
    fake_fastmcp.FastMCP = FakeMCP
    sys.path.insert(0, str(MCP_DIR))
    sys.modules["fastmcp"] = fake_fastmcp
    spec = importlib.util.spec_from_file_location(MODULE_NAME, MCP_DIR / "haihe_mcp_tools.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(MODULE_NAME, None)
        if original_fastmcp is None:
            sys.modules.pop("fastmcp", None)
        else:
            sys.modules["fastmcp"] = original_fastmcp
        for module_name in set(sys.modules) - original_module_names:
            sys.modules.pop(module_name, None)
        sys.path[:] = original_path


def test_register_haihe_tools_registers_region_weather_risk_tool():
    """遗漏包装器时，MCP 工具目录不再暴露区域综合风险能力。"""
    modules_before = set(sys.modules)
    with _isolated_haihe_mcp_tools() as module:
        mcp = FakeMCP()
        module.register_haihe_tools(mcp)

    assert "query_region_weather_risks" in mcp.tool_names
    assert set(sys.modules) == modules_before
