"""应急九分区静态数据库元数据缓存测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import emergency_response_interface as eri


def _reset(monkeypatch, *, ttl=3600, max_size=16):
    monkeypatch.setattr(eri, "_STATIC_METADATA_CACHE_TTL", ttl, raising=False)
    monkeypatch.setattr(eri, "_STATIC_METADATA_CACHE_MAX_SIZE", max_size, raising=False)
    eri._STATIC_METADATA_CACHE.clear()


def test_nine_zone_codes_are_cached(monkeypatch):
    _reset(monkeypatch)
    calls = []

    def fake_query(config_path):
        calls.append(config_path)
        return "h9_001,h9_002"

    monkeypatch.setattr(eri, "_query_nine_zone_codes_from_db", fake_query, raising=False)
    config_path = "C:/test/config.ini"
    assert eri._load_nine_zone_codes_from_db(config_path) == "h9_001,h9_002"
    assert eri._load_nine_zone_codes_from_db(config_path) == "h9_001,h9_002"
    assert len(calls) == 1


def test_nine_zone_union_wkt_uses_canonical_code_key(monkeypatch):
    _reset(monkeypatch)
    calls = []

    def fake_query(config_path, zone_codes):
        calls.append((config_path, tuple(zone_codes)))
        return "MULTIPOLYGON EMPTY"

    monkeypatch.setattr(eri, "_query_nine_zone_union_wkt", fake_query, raising=False)
    config_path = "C:/test/config.ini"
    first = eri._load_nine_zone_union_wkt(config_path, ["h9_002", "h9_001"])
    second = eri._load_nine_zone_union_wkt(config_path, ["h9_001", "h9_002", "h9_001"])
    assert first == second == "MULTIPOLYGON EMPTY"
    assert len(calls) == 1
    assert calls[0][1] == ("h9_001", "h9_002")


def test_empty_or_failed_metadata_is_not_cached(monkeypatch):
    _reset(monkeypatch)
    code_calls = {"n": 0}
    wkt_calls = {"n": 0}

    def empty_codes(config_path):
        code_calls["n"] += 1
        return ""

    def failed_wkt(config_path, zone_codes):
        wkt_calls["n"] += 1
        raise ValueError("db unavailable")

    monkeypatch.setattr(eri, "_query_nine_zone_codes_from_db", empty_codes, raising=False)
    monkeypatch.setattr(eri, "_query_nine_zone_union_wkt", failed_wkt, raising=False)
    config_path = "C:/test/config.ini"

    assert eri._load_nine_zone_codes_from_db(config_path) == ""
    assert eri._load_nine_zone_codes_from_db(config_path) == ""
    assert code_calls["n"] == 2

    for _ in range(2):
        with pytest.raises(ValueError, match="db unavailable"):
            eri._load_nine_zone_union_wkt(config_path, ["h9_001"])
    assert wkt_calls["n"] == 2


def test_static_metadata_cache_is_bounded(monkeypatch):
    _reset(monkeypatch, max_size=2)
    monkeypatch.setattr(
        eri, "_query_nine_zone_codes_from_db", lambda config_path: f"code-{Path(config_path).stem}", raising=False
    )

    for name in ("one.ini", "two.ini", "three.ini"):
        eri._load_nine_zone_codes_from_db(f"C:/test/{name}")

    assert len(eri._STATIC_METADATA_CACHE) <= 2
