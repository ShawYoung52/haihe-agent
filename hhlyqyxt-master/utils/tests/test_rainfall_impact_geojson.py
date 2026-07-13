""" rainfall_impact_geojson 单元测试（无需真实数据库与 pkl 文件）。"""
from __future__ import annotations

import pickle
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

import pytest

# 当前测试环境可能未安装 pandas/psycopg2，用最小 stub 避免导入失败。
if "pandas" not in sys.modules:
    pandas_stub = types.ModuleType("pandas")
    pandas_stub.DataFrame = object
    pandas_stub.read_csv = lambda *args, **kwargs: pandas_stub.DataFrame()
    pandas_stub.to_datetime = lambda *args, **kwargs: None
    pandas_stub.isna = lambda value: False
    pandas_stub.Timestamp = object
    sys.modules["pandas"] = pandas_stub

if "psycopg2" not in sys.modules:
    psycopg2_stub = types.ModuleType("psycopg2")
    psycopg2_stub.extras = types.ModuleType("psycopg2.extras")
    sys.modules["psycopg2"] = psycopg2_stub
    sys.modules["psycopg2.extras"] = psycopg2_stub.extras

UTILS_DIR = Path(__file__).resolve().parent.parent
if str(UTILS_DIR) not in sys.path:
    sys.path.insert(0, str(UTILS_DIR))

import rainfall_impact_geojson as rig


class _MockMultiDiGraph:
    """最小 networkx MultiDiGraph 替代，满足 _find_direct_graph_starts 需求。"""

    def __init__(self, edges: list[tuple[Any, Any, Any, dict]]):
        self._edges = edges

    def is_multigraph(self) -> bool:
        return True

    def edges(self, keys: bool = True, data: bool = True):
        if not keys and not data:
            return [(u, v) for u, v, _k, _a in self._edges]
        if not keys:
            return [(u, v, a) for u, v, _k, a in self._edges]
        return [(u, v, k, a) for u, v, k, a in self._edges]

    def out_edges(self, node, keys: bool = True, data: bool = True):
        out = [(u, v, k, a) for u, v, k, a in self._edges if u == node]
        if not keys and not data:
            return [(u, v) for u, v, _k, _a in out]
        if not keys:
            return [(u, v, a) for u, v, _k, a in out]
        return out


def _make_graph_path(edges: list[tuple[Any, Any, Any, dict]]) -> str:
    graph = _MockMultiDiGraph(edges)
    with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
        pickle.dump(graph, f)
        return f.name


def test_find_direct_graph_starts_matches_direct_part():
    """阶段一：pkl 边与真实直接河段 objectid/name 且几何匹配时产生起点。"""
    edges = [
        ("0,0", "1,0", 0, {"objectid": "100", "src_name": "东河A段", "length_km": 10.0}),
        ("1,0", "2,0", 0, {"objectid": "101", "src_name": "东河B段", "length_km": 10.0}),
    ]
    graph_path = _make_graph_path(edges)
    direct_rows = [
        {
            "objectid": "100",
            "river_name": "东河A段",
            "geom_json": '{"type":"LineString","coordinates":[[0,0],[1,0]]}',
        }
    ]
    stations = [{"lon": 0.0, "lat": 0.0, "rain_24h": 60.0}]
    starts, keys, stats = rig._find_direct_graph_starts(
        stations, direct_rows, graph_path, station_buffer_km=30.0, direct_match_km=10.0
    )
    assert len(starts) == 1
    assert "1,0" in starts
    assert stats["direct_part_matched_edge_count"] == 1
    assert stats["station_buffer_fallback_used"] is False


def test_find_direct_graph_starts_falls_back_to_station_buffer():
    """阶段二：直接河段匹配失败时，回退到暴雨站 30km 内 pkl 边。"""
    edges = [
        ("0,0", "1,0", 0, {"objectid": "999", "src_name": "未知河", "length_km": 10.0}),
        ("1,0", "2,0", 0, {"objectid": "999", "src_name": "未知河", "length_km": 10.0}),
    ]
    graph_path = _make_graph_path(edges)
    # 真实直接河段与 pkl 边 objectid/name 都不匹配
    direct_rows = [
        {
            "objectid": "100",
            "river_name": "东河",
            "geom_json": '{"type":"LineString","coordinates":[[10,10],[11,10]]}',
        }
    ]
    # 暴雨站位于 pkl 边 30km 内
    stations = [{"lon": 0.0, "lat": 0.0, "rain_24h": 60.0}]
    starts, keys, stats = rig._find_direct_graph_starts(
        stations, direct_rows, graph_path, station_buffer_km=30.0, direct_match_km=10.0
    )
    assert len(starts) == 1
    assert stats["direct_part_matched_edge_count"] == 0
    assert stats["station_buffer_fallback_used"] is True
    assert stats["station_buffer_fallback_edge_count"] == 1


def test_find_direct_graph_starts_empty_when_no_match_and_no_buffer_hit():
    """既无直接河段匹配，也无站点缓冲区命中时，返回空起点。"""
    edges = [
        ("10,10", "11,10", 0, {"objectid": "100", "src_name": "东河", "length_km": 10.0}),
    ]
    graph_path = _make_graph_path(edges)
    direct_rows = [
        {
            "objectid": "999",
            "river_name": "西河",
            "geom_json": '{"type":"LineString","coordinates":[[0,0],[1,0]]}',
        }
    ]
    stations = [{"lon": 0.0, "lat": 0.0, "rain_24h": 60.0}]
    starts, keys, stats = rig._find_direct_graph_starts(
        stations, direct_rows, graph_path, station_buffer_km=1.0, direct_match_km=10.0
    )
    assert len(starts) == 0
    assert stats["station_buffer_fallback_used"] is False


def test_find_direct_graph_starts_prefers_direct_part_over_fallback():
    """同时满足直接河段匹配和站点缓冲区时，优先使用直接河段，不标记 fallback。"""
    edges = [
        ("0,0", "1,0", 0, {"objectid": "100", "src_name": "东河", "length_km": 10.0}),
        ("0,1", "1,1", 0, {"objectid": "200", "src_name": "南河", "length_km": 10.0}),
    ]
    graph_path = _make_graph_path(edges)
    direct_rows = [
        {
            "objectid": "100",
            "river_name": "东河",
            "geom_json": '{"type":"LineString","coordinates":[[0,0],[1,0]]}',
        }
    ]
    # 站点在两条边 30km 内，但只有东河匹配真实直接河段
    stations = [{"lon": 0.0, "lat": 0.0, "rain_24h": 60.0}]
    starts, keys, stats = rig._find_direct_graph_starts(
        stations, direct_rows, graph_path, station_buffer_km=30.0, direct_match_km=10.0
    )
    assert len(starts) == 1
    assert "1,0" in starts
    assert stats["direct_part_matched_edge_count"] == 1
    assert stats["station_buffer_fallback_used"] is False


def test_fallback_starts_not_in_direct_keys():
    """兜底起点不应进入 direct_keys，避免被下游标记为真实直接河段。"""
    edges = [
        ("0,0", "1,0", 0, {"objectid": "999", "src_name": "未知河", "length_km": 10.0}),
    ]
    graph_path = _make_graph_path(edges)
    direct_rows = [
        {
            "objectid": "100",
            "river_name": "东河",
            "geom_json": '{"type":"LineString","coordinates":[[10,10],[11,10]]}',
        }
    ]
    stations = [{"lon": 0.0, "lat": 0.0, "rain_24h": 60.0}]
    starts, keys, stats = rig._find_direct_graph_starts(
        stations, direct_rows, graph_path, station_buffer_km=30.0, direct_match_km=10.0
    )
    assert len(starts) == 1
    assert stats["station_buffer_fallback_used"] is True
    assert len(keys) == 0
