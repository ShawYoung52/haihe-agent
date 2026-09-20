# -*- coding: utf-8 -*-
"""统一"当前时间"时间源（切换系统时间功能）。

背景：智能体需要把"现在"锚定到任意指定的 年-月-日 时:分（如 2026-07-10 15:00），
使"今天/明天/未来三天/本周末/今天下午/14时"等相对时间与工具取数都按该日期回答。

两级锚点，**请求级优先**：

1. **请求级锚点**（2026-09-20 新增，正常路径）：由调用方逐请求携带
   （HTTP 的 `metadata.reference_time`、WS 的 `cl.Message.metadata`），经
   `set_request_override()` 落到 ContextVar。请求结束即消失，**服务端不留任何
   跨用户状态**——一人切时间不会影响别人，这正是本模块要修的缺陷。
   Chainlit 进程与 MCP 进程是两个进程：跨进程靠 MCP header 传递（见
   `REFERENCE_TIME_HEADER`），由 chainlitexam 的 tool_interceptor 注入。
2. **全局锚点**（兜底，仅服务端 curl 验收用）：宿主机上一个 JSON 文件，两进程
   各放一份内容一致的本模块读同一个文件。这是进程级开关，**会**影响所有用户，
   因此不再作为前端路径。

优先级：请求 ContextVar → MCP header → 全局文件 → 真实时间。

线程安全：模块级 threading.Lock；文件读取按 (mtime_ns, size) 缓存，传播延迟≈0。
仅依赖标准库（fastmcp 惰性 import，缺失时自动降级为"无 header"）。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path

__all__ = [
    "now",
    "get_override",
    "set_override_from_text",
    "clear_override",
    "override_date_str",
    "is_active",
    "REFERENCE_TIME_HEADER",
    "set_request_override",
    "reset_request_override",
    "request_override_header_value",
    "effective_override",
    "try_parse_datetime",
    "is_date_only",
    "use_real_time_of_day",
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
        val = _parse_cn_datetime(obj.get("override_datetime"))
    except Exception:
        val = None
    with _LOCK:
        _CACHE_KEY, _CACHE_VAL, _CACHE_VALID = key, val, True
    return val


def try_parse_datetime(text) -> datetime | None:
    """按 ISO/常见格式解析成 datetime；失败返回 None。**不做时区处理**。

    无时区的原样 naive 返回，带偏移的保留原偏移——时区口径由调用方决定：本模块
    内部一律走 `_parse_cn_datetime` 归一到北京时；`utils/reference_time.py` 保留
    输入自带的偏移（那是它改动前的口径，不能顺手改）。

    这是"什么写法算合法"的唯一定义：覆盖文件、MCP header 与请求锚点三条入口共用，
    否则同一个字符串在不同入口会得到不同结果。
    """
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None
    dt = _try_fromisoformat(s)
    if dt is None:
        dt = _try_patterns(s)
    return dt


def _parse_cn_datetime(text) -> datetime | None:
    """`try_parse_datetime` + 归一到北京时；失败返回 None。

    覆盖文件与 MCP header 的口径：无时区按北京时解释，带偏移换算过来。
    """
    dt = try_parse_datetime(text)
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_CN_TZ)
    return dt.astimezone(_CN_TZ)


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


def is_date_only(text) -> bool:
    """是否为"仅日期"写法（"2026-07-10"/"2026/07/10"，不带时分）。"""
    return bool(isinstance(text, str) and _DATE_ONLY_RE.match(text.strip()))


def use_real_time_of_day(dt: datetime) -> datetime:
    """把仅日期锚点的时分秒换成**真实当前时刻**（不落 00:00）。

    落 00:00 会让"现在"=当天凌晨，"今天下午有雨吗 / 14时实况"被判到未来、
    实况与时段类工具直接取不到数据。`set_override_from_text` 与
    `utils/reference_time.py` 共用本函数，两条入口必须得到同一时刻。
    """
    real = datetime.now(_CN_TZ)
    return dt.replace(hour=real.hour, minute=real.minute, second=real.second, microsecond=0)


# ---------------------------------------------------------------------------
# 请求级锚点：把"现在"从进程级标量改成请求级值。
#
# 优先级：请求 ContextVar → MCP header → 全局文件 → 真实时间。
# ContextVar 在 asyncio Task 间天然隔离，并发请求不会串味；请求结束即消失，
# 服务端不留任何跨用户状态。
# ---------------------------------------------------------------------------

# Chainlit（8003）→ MCP（SSE 3333）传递请求级锚点的 header 名。
REFERENCE_TIME_HEADER = "x-haihe-reference-time"

# 字面量哨兵："real" 表示显式要求真实时间，用于压过可能遗留的全局覆盖文件。
_REAL_TEXT = "real"
_REAL = object()

# 请求级锚点：None=未设置（回落 header/文件） / _REAL=强制真实 / datetime=锚定
_req_override = ContextVar("haihe_ref_override", default=None)

_HEADER_LOOKUP_UNSET = object()
_header_lookup_fn = _HEADER_LOOKUP_UNSET


def _resolve_header_lookup():
    """惰性解析 fastmcp 的 get_http_headers，只探测一次。

    失败的 import **不会**被 Python 缓存进 sys.modules，所以不能每次 now() 都试：
    在没装 fastmcp 的环境（如 Chainlit 侧）那会变成热路径上的重复路径搜索。
    """
    global _header_lookup_fn
    if _header_lookup_fn is _HEADER_LOOKUP_UNSET:
        try:
            from fastmcp.server.dependencies import get_http_headers

            _header_lookup_fn = get_http_headers
        except Exception:
            _header_lookup_fn = None
    return _header_lookup_fn


def _coerce_request_value(value):
    """入口传入值 → aware datetime / _REAL；None 原样返回。非法抛 ValueError。"""
    if value is None or value is _REAL:
        return value
    if isinstance(value, str) and value.strip().lower() == _REAL_TEXT:
        return _REAL
    dt = value if isinstance(value, datetime) else _parse_cn_datetime(value)
    if dt is None:
        raise ValueError(f"无法解析的 reference_time：{value!r}")
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=_CN_TZ)


def set_request_override(value):
    """在请求入口设置锚点，返回 Token（调用方在 finally 里 reset）。

    value 支持：aware/naive datetime、ISO 字符串、字面量 "real"。
    """
    return _req_override.set(_coerce_request_value(value))


def reset_request_override(token) -> None:
    """复位请求级锚点。重复 reset / 跨 context 的 token 一律忽略，绝不抛错。

    这条路径跑在 finally 清理里，抛错会盖掉真正的业务异常。ContextVar.reset
    的三种失败都算：
    - RuntimeError：token 已被 reset 过一次
    - ValueError：token 由别的 Context 创建
    - LookupError：token 与本 ContextVar 不匹配
    """
    try:
        _req_override.reset(token)
    except (ValueError, LookupError, RuntimeError):
        pass


def request_override_header_value():
    """供 MCP interceptor：只取请求级锚点，返回 ISO 或 "real"；未设置返回 None。

    刻意**不**回落全局文件：全局锚点由 MCP 进程自己读文件，不需要也不应该
    被当成 header 再发一遍。
    """
    ov = _req_override.get()
    if ov is None:
        return None
    if ov is _REAL:
        return _REAL_TEXT
    return ov.isoformat()


def _header_override():
    """读 MCP 进程入站 header（fastmcp）。任何异常一律当作"没有锚点"。

    刻意不做缓存：MCP 进程里同一个 asyncio Task 是否跨请求复用无法保证，
    缓存会把上一个请求的 header 泄漏到下一个请求。get_http_headers() 本身
    只是 contextvar 读取 + 小 dict 构造，热路径上不值得为此冒险。
    """
    lookup = _resolve_header_lookup()
    if lookup is None:
        return None
    try:
        raw = (lookup() or {}).get(REFERENCE_TIME_HEADER)
    except Exception:
        return None
    if not raw:
        return None
    if isinstance(raw, str) and raw.strip().lower() == _REAL_TEXT:
        return _REAL
    return _parse_cn_datetime(raw)


def effective_override():
    """本上下文生效的锚点：请求级 → MCP header → 全局文件；None 表示真实时间。"""
    ov = _req_override.get()
    if ov is None:
        ov = _header_override()
    if ov is None:
        ov = _read_file_dt()
    return ov


def now(tz=None) -> datetime:
    """返回"现在"。请求级锚点 → MCP header → 覆盖文件 → 真实时间。

    tz=None → naive（保持既有调用点行为不变）；给了 tz → 换算到该时区。
    """
    override = effective_override()
    if override is None or override is _REAL:
        return datetime.now(tz) if tz is not None else datetime.now()
    if tz is None:
        return override.replace(tzinfo=None)
    return override.astimezone(tz)


def get_override() -> datetime | None:
    """**全局兜底文件**里当前生效的锚定时刻（aware +08:00）；无覆盖返回 None。

    刻意只看文件、不看请求级锚点与 MCP header：
    - `GET /admin/system-time` 用它报告全局兜底开关的状态；
    - `reference_time.anchor_cache_key` 的 inherit 分支用它做响应缓存键。
    要看"当前上下文真正生效的锚点"请用 `effective_override()`。
    """
    return _read_file_dt()


def is_active() -> bool:
    """当前上下文是否有生效的时间锚点（请求级 → MCP header → 全局文件）。

    走 `effective_override()`，与 `now()` 同一优先级链——否则在请求级锚点生效时
    会错误返回 False。要判断**全局兜底开关**的状态请用 `get_override()`。
    """
    override = effective_override()
    return override is not None and override is not _REAL


def override_date_str() -> str:
    """"现在"的 "%Y-%m-%d"（供 orchestrator runtime 缓存键 / HTTP epoch 用）。

    走 `now()`，因此请求级锚点、MCP header、全局覆盖都算在内；三者都没有则是真实日期。
    调用方带着不同锚点时本字符串不同 → runtime 分槽缓存各占一个槽位，互不覆盖。
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

    date_only = is_date_only(s)
    dt = _parse_cn_datetime(s)
    if dt is None:
        raise ValueError(f"无法解析的时间格式：{s!r}（支持 YYYY-MM-DD[ HH:MM[:SS]] 或 ISO）")

    if date_only:
        dt = use_real_time_of_day(dt)
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
