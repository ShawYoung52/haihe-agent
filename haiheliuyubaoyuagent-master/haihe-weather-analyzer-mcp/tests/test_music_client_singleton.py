"""基础设施：MUSIC client 进程级单例（复用 Session/TCP 连接）。

背景（遍历发现）：haihe_mcp_tools 每次调用 new MusicClient() → new requests.Session()
→ 每条查询新建 TCP 连接。tools.py 已有 `_get_music_client()` 单例先例（并发复用安全）。
本测试锁定：_get_music_client() 返回同一实例。
"""

from __future__ import annotations

import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import haihe_mcp_tools as hmt


def test_get_music_client_returns_same_instance(monkeypatch):
    """_get_music_client() 多次调用返回同一实例（复用 Session）。"""
    monkeypatch.setenv("MUSIC_USER", "test_user")
    monkeypatch.setenv("MUSIC_PASSWORD", "test_pwd")
    hmt._MUSIC_CLIENT_SINGLETON = None

    a = hmt._get_music_client()
    b = hmt._get_music_client()
    assert a is b, "单例应复用同一 MusicClient 实例"

    hmt._MUSIC_CLIENT_SINGLETON = None
