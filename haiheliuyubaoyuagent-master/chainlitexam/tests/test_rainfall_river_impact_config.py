"""基础设施：rainfall_river_impact._load_mcp_config 惰性缓存测试。

背景（遍历发现）：_call_affected_river_network 每次工具调用都重读 config.ini
（磁盘读+解析）。config 静态，改为模块级惰性缓存。
"""

from __future__ import annotations

import sys
from pathlib import Path

CHAINLITEXAM_DIR = Path(__file__).resolve().parents[2]
if str(CHAINLITEXAM_DIR) not in sys.path:
    sys.path.insert(0, str(CHAINLITEXAM_DIR))

from tools import rainfall_river_impact as rri


def test_load_mcp_config_cached_same_instance(monkeypatch):
    """_load_mcp_config 多次调用返回同一实例（不再每次重读磁盘）。"""
    rri._MCP_CONFIG_CACHE = None
    try:
        a = rri._load_mcp_config()
        b = rri._load_mcp_config()
        assert a is b, "config 应惰性缓存为同一实例"
    finally:
        rri._MCP_CONFIG_CACHE = None


def test_load_mcp_config_readable():
    """缓存的 config 可正常读取 postgres 段（不破坏原行为）。"""
    rri._MCP_CONFIG_CACHE = None
    try:
        cfg = rri._load_mcp_config()
        assert cfg.has_section("postgres"), "config.ini 应含 postgres 段"
    finally:
        rri._MCP_CONFIG_CACHE = None
