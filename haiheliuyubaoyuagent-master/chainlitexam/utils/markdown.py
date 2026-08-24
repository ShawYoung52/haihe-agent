"""Markdown 输出安全处理。"""

from __future__ import annotations

import re
from typing import Any


_NUMERIC_TILDE_RANGE_RE = re.compile(r"(?<=\d)(?:\s*\\?~\s*)+(?=[+-]?\d)")


def normalize_markdown_ranges(value: Any) -> str:
    r"""把数字间的波浪号范围统一为全角 ``～``，避免 ``~~`` 被解析为删除线。

    兼容接口或历史文本里的 ``1~2``、``1\~2`` 和 ``1~~2``；只处理数字之间的
    范围符号，不改动普通 Markdown 文本。
    """
    return _NUMERIC_TILDE_RANGE_RE.sub("～", str(value))
