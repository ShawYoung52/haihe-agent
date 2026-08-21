"""通用 TTL 缓存装饰器 make_ttl_cache 测试。

规则：同 key 在 TTL 内命中；只缓存 status=="ok" 的 dict；错误/无数据不写缓存。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = MCP_DIR / "custom_tools" / "_ttl_cache.py"
SPEC = importlib.util.spec_from_file_location("ttl_cache_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
make_ttl_cache = MODULE.make_ttl_cache


class TestMakeTtlCache:
    def test_second_call_same_key_hits_cache(self):
        """同 key 第二次命中缓存，计算函数只执行一次。"""
        calls = {"n": 0}

        def compute(zone_type="9"):
            calls["n"] += 1
            return {"status": "ok", "records": [1, 2]}

        decorator, cache, lock = make_ttl_cache(3600, lambda zone_type="9": zone_type)
        wrapped = decorator(compute)

        r1 = wrapped("9")
        after_first = calls["n"]
        r2 = wrapped("9")
        assert r1 == r2
        assert calls["n"] == after_first, "第二次应命中缓存"
        assert cache, "成功结果应写缓存"

    def test_distinct_key_does_not_share(self):
        """不同 key 不互相命中。"""
        calls = {"n": 0}

        def compute(zone_type="9"):
            calls["n"] += 1
            return {"status": "ok", "records": []}

        decorator, cache, lock = make_ttl_cache(3600, lambda zone_type="9": zone_type)
        wrapped = decorator(compute)

        wrapped("9")
        after_first = calls["n"]
        wrapped("77")
        assert calls["n"] > after_first, "不同 key 应重新计算"

    def test_ttl_zero_always_refetches(self):
        """TTL=0 强制每次重新计算。"""
        calls = {"n": 0}

        def compute(zone_type="9"):
            calls["n"] += 1
            return {"status": "ok", "records": []}

        decorator, cache, lock = make_ttl_cache(0, lambda zone_type="9": zone_type)
        wrapped = decorator(compute)

        wrapped("9")
        after_first = calls["n"]
        wrapped("9")
        assert calls["n"] > after_first, "TTL=0 应每次重新计算"
        assert cache == {}, "TTL=0 表示完全禁用缓存，不应保留永不命中的条目"

    def test_error_result_not_cached(self):
        """status != ok 的结果不写缓存。"""
        calls = {"n": 0}

        def compute(zone_type="9"):
            calls["n"] += 1
            return {"status": "no_data", "reason": "raw_empty"}

        decorator, cache, lock = make_ttl_cache(3600, lambda zone_type="9": zone_type)
        wrapped = decorator(compute)

        wrapped("9")
        assert cache == {}, "非 ok 结果不应写缓存"
        wrapped("9")
        assert calls["n"] == 2, "未缓存应重新计算"

    def test_custom_success_predicate_supports_static_mappings(self):
        calls = {"n": 0}

        def compute(key):
            calls["n"] += 1
            return {key: {"zone9_code": "h9_001"}}

        decorator, cache, _ = make_ttl_cache(
            3600,
            lambda key: key,
            should_cache=bool,
        )
        wrapped = decorator(compute)
        assert wrapped("fine-1") == wrapped("fine-1")
        assert calls["n"] == 1

    def test_cache_capacity_is_bounded(self):
        decorator, cache, _ = make_ttl_cache(
            3600,
            lambda key: key,
            max_size=2,
        )
        wrapped = decorator(lambda key: {"status": "ok", "key": key})
        for key in ("one", "two", "three"):
            wrapped(key)
        assert len(cache) == 2
        assert "one" not in cache
