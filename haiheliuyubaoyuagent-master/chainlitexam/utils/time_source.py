# -*- coding: utf-8 -*-
"""统一"当前时间"时间源（切换系统时间功能）。

背景：智能体需要把全局"现在"锚定到任意指定的 年-月-日 时:分（如 2026-07-10 15:00），
使"今天/明天/未来三天/本周末/今天下午/14时"等相对时间与工具取数都按该日期回答。
这不是模拟，而是把系统的"现在"切换为指定时刻。

实现：单一事实源 = 宿主机上一个 JSON 文件（两进程——Chainlit 前端与 MCP 服务——各自
放置一份内容一致的本模块，读取同一个文件，从而在不改任何 MCP 工具 schema、不让 LLM
传时间参的前提下穿透进程边界）。冻结（fixed）锚点：设置后"现在"固定为该时刻，不随真实
时钟走动，便于复现测试。

线程安全：模块级 threading.Lock；文件读取按 (mtime_ns, size) 缓存，传播延迟≈0。
仅依赖标准库。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

__all__ = [
    "now",
    "get_override",
    "set_override_from_text",
    "clear_override",
    "override_date_str",
    "is_active",
]

# 中国时区（无夏令时，固定 +08:00 即可，等价于 Asia/Shanghai）。
_CN_TZ = timezone(timedelta(hours=8))

_LOCK = threading.Lock()

# 缓存：文件签名 (mtime_ns, size) -> 解析出的 override（aware datetime 或 None）。
_CACHE_KEY = None  # type: tuple[int, int] | None
_CACHE_VAL = None  # type: datetime | None
_CACHE_VALID = False


def _override_file() -> Path:
    """覆盖文件路径。env SIM_TIME_FILE 可配；默认系统临时目录下固定文件名。

    两包用同一表达式，保证同主机两进程读到同一个文件。
    """
    p = os.environ.get("SIM_TIME_FILE", "").strip()
    if p:
        return Path(p)
    return Path(tempfile.gettempdir()) / "haihe_system_time_override.json"


def _read_file_dt() -> datetime | None:
    """读文件并解析 override；文件不存在/损坏均返回 None（按真实时间）。

    按 (mtime_ns, size) 缓存：stat 是本地文件系统微秒级操作，mtime 不变就直接用缓存。
    """
    global _CACHE_KEY, _CACHE_VAL, _CACHE_VALID
    path = _override_file()
    try:
        st = path.stat()
    except OSError:
        # 文件不存在（或不可读）→ 无覆盖。
        with _LOCK:
            _CACHE_KEY, _CACHE_VAL, _CACHE_VALID = None, None, True
        return None
    key = (st.st_mtime_ns, st.st_size)
    with _LOCK:
        if _CACHE_VALID and _CACHE_KEY == key:
            return _CACHE_VAL
    try:
        raw = path.read_text(encoding="utf-8")
        obj = json.loads(raw)
        val = _parse_iso(obj.get("override_datetime"))
    except Exception:
        val = None
    with _LOCK:
        _CACHE_KEY, _CACHE_VAL, _CACHE_VALID = key, val, True
    return val


def _parse_iso(text) -> datetime | None:
    """解析 ISO/常见格式为 aware(+08:00) datetime；失败返回 None。"""
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None
    dt = _try_fromisoformat(s)
    if dt is None:
        dt = _try_patterns(s)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_CN_TZ)
    else:
        dt = dt.astimezone(_CN_TZ)
    return dt


def _try_fromisoformat(s: str) -> datetime | None:
    try:
        # Python 3.10 fromisoformat 不支持 "Z"，先替换。
        return datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
    except Exception:
        return None


_PATTERNS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
)


def _try_patterns(s: str) -> datetime | None:
    for fmt in _PATTERNS:
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


_DATE_ONLY_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$")


def now(tz=None) -> datetime:
    """返回"现在"。有覆盖→锚定时刻（换算到 tz；tz=None→naive）；无覆盖→真实 datetime.now(tz)。"""
    override = _read_file_dt()
    if override is None:
        return datetime.now(tz) if tz is not None else datetime.now()
    if tz is None:
        return override.replace(tzinfo=None)
    return override.astimezone(tz)


def get_override() -> datetime | None:
    """当前生效的锚定时刻（aware +08:00）；无覆盖返回 None。"""
    return _read_file_dt()


def is_active() -> bool:
    return _read_file_dt() is not None


def override_date_str() -> str:
    """"现在"的 "%Y-%m-%d"（供 orchestrator runtime 缓存键 / HTTP epoch 用）。

    有覆盖→锚定日期；无覆盖→真实日期。覆盖日期变化时本字符串变化 → 缓存自动重建。
    """
    return now(_CN_TZ).strftime("%Y-%m-%d")


def set_override_from_text(text, note=None) -> dict:
    """解析文本为锚定时刻并原子写文件。返回 REST data dict。

    支持："YYYY-MM-DD HH:MM[:SS]"、"YYYY-MM-DDTHH:MM[:SS]"、ISO 带时区、
    以及仅日期 "YYYY-MM-DD"（时间部分取设置那一刻的真实时分，便于"今天下午/14时"
    落在已发生时次）。非法输入抛 ValueError。
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("datetime 不能为空")
    s = text.strip()

    date_only = bool(_DATE_ONLY_RE.match(s))
    dt = _parse_iso(s)
    if dt is None:
        raise ValueError(f"无法解析的时间格式：{s!r}（支持 YYYY-MM-DD[ HH:MM[:SS]] 或 ISO）")

    if date_only:
        # 仅日期：时分取真实当前时刻（不取 00:00，否则"现在"=当天凌晨，
        # "今天下午/14时"会被判到未来而无法取实况）。
        real = datetime.now(_CN_TZ)
        dt = dt.replace(hour=real.hour, minute=real.minute, second=real.second, microsecond=0)
    else:
        dt = dt.replace(microsecond=0)

    payload = {
        "override_datetime": dt.isoformat(),
        "mode": "fixed",
        "set_at_real": datetime.now(_CN_TZ).isoformat(),
        "note": (note or "").strip() or None,
    }
    _atomic_write(payload)
    return {
        "active": True,
        "override_datetime": dt.isoformat(),
        "display": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "note": payload["note"],
    }


def _atomic_write(payload: dict) -> None:
    """temp + os.replace 原子写（同文件系统原子），避免读者读到半写文件。"""
    path = _override_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # 写后立即失效缓存，下一次 now() 必读到新值。
    _invalidate()


def clear_override() -> dict:
    """删除覆盖文件，恢复真实时间。"""
    path = _override_file()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    _invalidate()
    return {"active": False}


def _invalidate() -> None:
    global _CACHE_KEY, _CACHE_VAL, _CACHE_VALID
    with _LOCK:
        _CACHE_KEY, _CACHE_VAL, _CACHE_VALID = None, None, False
