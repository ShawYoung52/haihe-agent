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


def _run_find_direct_graph_starts(
    edges: list[tuple[Any, Any, Any, dict]],
    direct_rows: list[dict],
    stations: list[dict],
    *,
    station_buffer_km: float = 30.0,
    direct_match_km: float = 10.0,
) -> tuple[dict[Any, float], set[str], dict]:
    graph_path = _make_graph_path(edges)
    return rig._find_direct_graph_starts(
        stations, direct_rows, graph_path, station_buffer_km, direct_match_km
    )


def test_find_direct_graph_starts_uses_station_buffer_as_primary():
    """30km 站点缓冲区内的所有 pkl 边都应作为下游追踪起点。"""
    edges = [
        ("0,0", "0.1,0", 0, {"objectid": "100", "src_name": "东河A段", "length_km": 10.0}),
        ("0.1,0", "0.2,0", 0, {"objectid": "101", "src_name": "东河B段", "length_km": 10.0}),
    ]
    direct_rows = [
        {
            "objectid": "100",
            "river_name": "东河A段",
            "geom_json": '{"type":"LineString","coordinates":[[0,0],[0.1,0]]}',
        }
    ]
    stations = [{"lon": 0.0, "lat": 0.0, "rain_24h": 60.0}]
    starts, keys, stats = _run_find_direct_graph_starts(edges, direct_rows, stations)
    # 两条边都在 30km 缓冲区内，都应作为起点
    assert len(starts) == 2
    assert "0.1,0" in starts
    assert "0.2,0" in starts
    assert stats["direct_part_matched_edge_count"] == 1
    assert stats["station_buffer_fallback_used"] is True
    assert stats["station_buffer_fallback_edge_count"] == 1


def test_find_direct_graph_starts_falls_back_to_station_buffer():
    """30km 缓冲区内无直接河段匹配时，仍应使用缓冲区边作为起点。"""
    edges = [
        ("0,0", "1,0", 0, {"objectid": "999", "src_name": "未知河", "length_km": 10.0}),
        ("1,0", "2,0", 0, {"objectid": "999", "src_name": "未知河", "length_km": 10.0}),
    ]
    direct_rows = [
        {
            "objectid": "100",
            "river_name": "东河",
            "geom_json": '{"type":"LineString","coordinates":[[10,10],[11,10]]}',
        }
    ]
    stations = [{"lon": 0.0, "lat": 0.0, "rain_24h": 60.0}]
    starts, keys, stats = _run_find_direct_graph_starts(edges, direct_rows, stations)
    assert len(starts) == 1
    assert stats["direct_part_matched_edge_count"] == 0
    assert stats["station_buffer_fallback_used"] is True
    assert stats["station_buffer_fallback_edge_count"] == 1


def test_find_direct_graph_starts_empty_when_no_buffer_hit():
    """无站点缓冲区命中时，返回空起点。"""
    edges = [
        ("10,10", "11,10", 0, {"objectid": "100", "src_name": "东河", "length_km": 10.0}),
    ]
    direct_rows = [
        {
            "objectid": "999",
            "river_name": "西河",
            "geom_json": '{"type":"LineString","coordinates":[[0,0],[1,0]]}',
        }
    ]
    stations = [{"lon": 0.0, "lat": 0.0, "rain_24h": 60.0}]
    starts, keys, stats = _run_find_direct_graph_starts(
        edges, direct_rows, stations, station_buffer_km=1.0
    )
    assert len(starts) == 0
    assert stats["station_buffer_fallback_used"] is False


def test_find_direct_graph_starts_keeps_direct_match_outside_buffer():
    """真实直接河段匹配边即使略超 30km 缓冲区，也应补充为起点并计入 direct_keys。"""
    # 边位于 y=0.2（约 22km 外），明显超出 0.1km 缓冲区，但直接匹配几何
    edges = [
        ("0,0.2", "0.1,0.2", 0, {"objectid": "100", "src_name": "东河", "length_km": 10.0}),
    ]
    direct_rows = [
        {
            "objectid": "100",
            "river_name": "东河",
            "geom_json": '{"type":"LineString","coordinates":[[0,0.2],[0.1,0.2]]}',
        }
    ]
    stations = [{"lon": 0.0, "lat": 0.0, "rain_24h": 60.0}]
    starts, keys, stats = _run_find_direct_graph_starts(
        edges, direct_rows, stations, station_buffer_km=0.1
    )
    # 直接匹配边因对齐偏差略超 0.1km 缓冲区，仍应被补充为起点
    assert len(starts) == 1
    assert "0.1,0.2" in starts
    assert stats["direct_part_matched_edge_count"] == 1


def test_find_direct_graph_starts_includes_all_rivers_in_buffer():
    """站点 30km 内多条河流时，即使只有一条匹配真实直接河段，其余河流也应被追踪。

    这是修复下游河段断裂/零散回归的关键场景：旧逻辑只使用直接匹配边作为起点，
    导致同一暴雨区域内因对齐偏差未被精确匹配的河系被遗漏。
    """
    edges = [
        ("0,0", "0.1,0", 0, {"objectid": "100", "src_name": "东河", "length_km": 10.0}),
        ("0,0.1", "0.1,0.1", 0, {"objectid": "200", "src_name": "南河", "length_km": 10.0}),
    ]
    direct_rows = [
        {
            "objectid": "100",
            "river_name": "东河",
            "geom_json": '{"type":"LineString","coordinates":[[0,0],[0.1,0]]}',
        }
    ]
    stations = [{"lon": 0.0, "lat": 0.0, "rain_24h": 60.0}]
    starts, keys, stats = _run_find_direct_graph_starts(edges, direct_rows, stations)
    # 两条边都在 30km 缓冲区内
    assert len(starts) == 2
    assert "0.1,0" in starts
    assert "0.1,0.1" in starts
    # 只有东河被标记为真实直接河段
    assert stats["direct_part_matched_edge_count"] == 1
    assert stats["station_buffer_fallback_used"] is True
    assert stats["station_buffer_fallback_edge_count"] == 1


def test_fallback_starts_not_in_direct_keys():
    """缓冲区起点中非直接河段匹配边不应进入 direct_keys。"""
    edges = [
        ("0,0", "1,0", 0, {"objectid": "999", "src_name": "未知河", "length_km": 10.0}),
    ]
    direct_rows = [
        {
            "objectid": "100",
            "river_name": "东河",
            "geom_json": '{"type":"LineString","coordinates":[[10,10],[11,10]]}',
        }
    ]
    stations = [{"lon": 0.0, "lat": 0.0, "rain_24h": 60.0}]
    starts, keys, stats = _run_find_direct_graph_starts(edges, direct_rows, stations)
    assert len(starts) == 1
    assert stats["station_buffer_fallback_used"] is True
    assert len(keys) == 0
