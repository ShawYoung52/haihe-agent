"""通用 TTL 缓存装饰器（静态历史降雨/面雨量系列工具共用）。

规则（与 A 系列口径一致）：
- 同 key 在 TTL 内直接返回缓存，跳过计算；
- 只缓存 dict 且 status=="ok" 的结果（错误/无数据不写缓存）；
- TTL 在 import 期由各工具 env 决定（与其他缓存一致）。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from functools import wraps
from typing import Any, Callable, Tuple


def make_ttl_cache(
    ttl: float,
    key_fn: Callable[..., str],
    *,
    should_cache: Callable[[Any], bool] | None = None,
    max_size: int | None = None,
):
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
    if should_cache is None:
        should_cache = lambda value: (
            isinstance(value, dict) and value.get("status") == "ok"
        )
    capacity = max_size if max_size is not None and max_size > 0 else None
    cache: OrderedDict[str, Tuple[float, Any]] = OrderedDict()
    lock = threading.Lock()

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapped(*args, **kwargs):
            key = key_fn(*args, **kwargs)
            now = time.time()
            with lock:
                hit = cache.get(key)
                if hit and ttl > 0 and (now - hit[0]) < ttl:
                    cache.move_to_end(key)
                    return hit[1]
            result = fn(*args, **kwargs)
            if ttl > 0 and should_cache(result):
                with lock:
                    stored_at = time.time()
                    expired = [
                        item_key
                        for item_key, (item_time, _) in cache.items()
                        if ttl <= 0 or (stored_at - item_time) >= ttl
                    ]
                    for item_key in expired:
                        cache.pop(item_key, None)
                    cache[key] = (stored_at, result)
                    cache.move_to_end(key)
                    if capacity is not None:
                        while len(cache) > capacity:
                            cache.popitem(last=False)
            return result
        return wrapped

    return decorator, cache, lock
