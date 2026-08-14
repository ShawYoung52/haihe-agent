"""通用 TTL 缓存装饰器（静态历史降雨/面雨量系列工具共用）。

规则（与 A 系列口径一致）：
- 同 key 在 TTL 内直接返回缓存，跳过计算；
- 只缓存 dict 且 status=="ok" 的结果（错误/无数据不写缓存）；
- TTL 在 import 期由各工具 env 决定（与其他缓存一致）。
"""

from __future__ import annotations

import threading
import time
from functools import wraps
from typing import Any, Callable, Dict, Tuple


def make_ttl_cache(ttl: float, key_fn: Callable[..., str]):
    """构造 (decorator, cache, lock)。key_fn 从工具参数算出缓存键。

    用法：
        decorator, _cache, _lock = make_ttl_cache(
            int(os.getenv("X_CACHE_TTL", "3600")),
            lambda zone_type="9": f"9|{month}",
        )
        @mcp.tool()
        @decorator
        def query_x(...) -> dict: ...
    """
    cache: Dict[str, Tuple[float, Any]] = {}
    lock = threading.Lock()

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapped(*args, **kwargs):
            key = key_fn(*args, **kwargs)
            with lock:
                hit = cache.get(key)
                if hit and (time.time() - hit[0]) < ttl:
                    return hit[1]
            result = fn(*args, **kwargs)
            if isinstance(result, dict) and result.get("status") == "ok":
                with lock:
                    cache[key] = (time.time(), result)
            return result
        return wrapped

    return decorator, cache, lock
