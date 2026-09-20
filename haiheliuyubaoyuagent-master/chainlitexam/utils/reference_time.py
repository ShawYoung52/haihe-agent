# -*- coding: utf-8 -*-
"""请求级锚定时间的解析（切换系统时间的请求侧契约）。

把前端传来的 metadata / 顶层字段归一到三态，供两个问答入口（HTTP
`/api/v1/qa/ask` 与 Chainlit WS `on_message`）共用：

- `("fixed", datetime)`  本次请求按该时刻回答
- `("real", None)`       本次请求强制真实时间（压过遗留的全局覆盖文件）
- `("inherit", None)`    没带锚点 → 回落全局覆盖文件 → 真实时间

纯函数、只依赖标准库与 `utils.time_source`，可单测、无重依赖。
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone

from utils import time_source

__all__ = [
    "InvalidReferenceTime",
    "resolve_reference_time",
    "resolve_day",
    "anchor_cache_key",
    "request_anchor",
]

_CN_TZ = timezone(timedelta(hours=8))

# time_mode 的"回到真实时间"取值（前端可能用任一写法）。
_REAL_MODES = frozenset({"real", "dynamic", "live"})


class InvalidReferenceTime(ValueError):
    """reference_time / time_mode 显式给了但不可用——入口应转 400。

    刻意与"没带锚点"区分开：没带是合法的（回落全局/真实时间），给了错值则说明
    调用方有 bug，静默按真实时间回答会把问题掩盖成一个"日期看起来不对"的投诉。
    """


def _pick(metadata, top_level, key):
    """metadata.<key> 优先，其次顶层 <key>；空串视为未提供。"""
    for source in (metadata, top_level):
        if isinstance(source, dict):
            value = source.get(key)
            if value not in (None, ""):
                return value
    return None


def _parse(text) -> datetime:
    """解析锚点文本为 aware datetime；不可解析抛 `InvalidReferenceTime`。

    "哪些写法算合法"与"仅日期取真实时分"的口径都由 `time_source` 提供（两条入口
    HTTP/WS 才不会对同一个字符串给出不同时刻）。时区口径留在本函数：无时区的按
    北京时解释（前端"2026-07-10 15:00"这类写法），**带偏移的保留原偏移**——与
    `time_source` 那份（归一到 +08:00）刻意不同，改动前就是如此。
    """
    s = str(text).strip()
    dt = time_source.try_parse_datetime(s)
    if dt is None:
        raise InvalidReferenceTime(f"无法解析的 reference_time：{s!r}")
    if time_source.is_date_only(s):
        # 仅日期时时分取**真实当前时刻**（不能落 00:00：那样"现在"=当天凌晨，
        # "今天下午有雨吗 / 14时实况"会被判到未来、实况与时段类工具取不到数据）。
        # 显式带了 00:00 的（如 ISO 串）不是"仅日期"，不受影响。
        dt = time_source.use_real_time_of_day(dt)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=_CN_TZ)


def resolve_reference_time(metadata=None, *, top_level=None):
    """→ `(kind, value)`。kind ∈ {"fixed","real","inherit"}；fixed 时 value 为 aware datetime。"""
    raw_mode = _pick(metadata, top_level, "time_mode")
    mode = raw_mode.strip().lower() if isinstance(raw_mode, str) else None
    if mode in _REAL_MODES:
        return ("real", None)
    raw = _pick(metadata, top_level, "reference_time")
    if raw is None:
        if mode == "fixed":
            # 说了 fixed 却没给时刻：这是调用方 bug，不能静默按真实时间回答。
            raise InvalidReferenceTime("time_mode=fixed 需要同时给出 reference_time")
        return ("inherit", None)
    return ("fixed", _parse(raw))


def resolve_day(kind: str, value, *, fallback_day: str) -> str:
    """本请求生效日期的 "%Y-%m-%d"（供 runtime 分槽缓存键）。

    "real" 用真实今天而不是 fallback：调用方刚说了"要真实时间"，重建出来的
    prompt 前缀也必须是真的今天。
    """
    if kind == "fixed":
        return value.strftime("%Y-%m-%d")
    if kind == "real":
        return datetime.now(_CN_TZ).strftime("%Y-%m-%d")
    return fallback_day


def anchor_cache_key(kind: str, value):
    """响应缓存的时间维度键。

    "inherit" 取全局覆盖文件的当前值——否则全局锚点变了、缓存键却不变，
    会返回陈旧答案（这正是本次要修的缺陷之一）。
    """
    if kind == "fixed":
        return value.isoformat()
    if kind == "real":
        return "real"
    override = time_source.get_override()
    return override.isoformat() if override is not None else None


@contextlib.contextmanager
def request_anchor(kind: str, value):
    """把解析结果落到 `time_source` 请求级锚点，退出时复位（异常路径也复位）。

    "inherit" 是空操作：不设置即自然回落全局文件，无需 reset。
    """
    if kind == "inherit":
        yield
        return
    token = time_source.set_request_override("real" if kind == "real" else value)
    try:
        yield
    finally:
        time_source.reset_request_override(token)
