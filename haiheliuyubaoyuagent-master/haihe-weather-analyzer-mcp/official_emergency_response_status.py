"""已停用：第一类官方防汛响应状态辅助判断。

当前问答链路只做预案 2.1.2 第二类应急响应判定，不使用本模块。
保留空实现是为了避免旧进程或旧引用导入时报错。
"""
from __future__ import annotations

from typing import Any


def find_official_emergency_response(times: str) -> dict[str, Any] | None:
    return None


def build_official_response_payload(times: str, basin_codes: str = "HHLY") -> dict[str, Any] | None:
    return None
