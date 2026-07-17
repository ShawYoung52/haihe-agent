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


def test_build_edge_lookup_maps_row_by_objectid_and_endpoints():
    rows = [
        {"objectid": "1", "from_x": 116.0, "from_y": 39.0, "to_x": 116.1, "to_y": 39.1, "river_name": "A"},
        {"objectid": "1", "from_x": 116.1, "from_y": 39.1, "to_x": 116.2, "to_y": 39.2, "river_name": "B"},
    ]
    lookup = rig._build_edge_lookup(rows)
    assert lookup[("1", 116.0, 39.0, 116.1, 39.1)]["river_name"] == "A"
    assert lookup[("1", 116.1, 39.1, 116.2, 39.2)]["river_name"] == "B"


class _MockMultiDiGraph:
    """最小 networkx MultiDiGraph 替代，满足 _find_direct_graph_starts 需求。"""

    def __init__(self, edges: list[tuple[Any, Any, Any, dict]]):
        self._edges = edges

    def is_multigraph(self) -> bool:
        return True

    def edges(self, keys: bool = True, data: bool = True):
        return self._format_edges(self._edges, keys, data)

    def out_edges(self, node, keys: bool = True, data: bool = True):
        out = [e for e in self._edges if e[0] == node]
        return self._format_edges(out, keys, data)

    def _format_edges(self, edges, keys: bool, data: bool):
        if not keys and not data:
            return [(u, v) for u, v, _k, _a in edges]
        if not keys:
            return [(u, v, a) for u, v, _k, a in edges]
        return edges


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


def test_build_objectid_name_map_prefers_non_unknown():
    """pkl 图名称映射应跳过“未知”并优先有效名称。"""
    edges = [
        ("0,0", "1,0", 0, {"objectid": "2", "src_name": "牤牛河", "length_km": 10.0}),
        ("1,0", "2,0", 0, {"objectid": "2", "src_name": "未知", "length_km": 10.0}),
    ]
    graph_path = _make_graph_path(edges)
    mapping = rig._build_objectid_name_map(graph_path)
    assert mapping.get("2") == "牤牛河"


def test_enrich_unknown_river_names_from_graph():
    """GeoJSON 要素中 river_name 为“未知”时应被 pkl 图名称回填。"""
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 0]]},
            "properties": {"objectid": "2", "river_name": "未知", "impact_type": "direct_buffer"},
        },
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[1, 0], [2, 0]]},
            "properties": {"objectid": "70", "river_name": "永定河", "impact_type": "downstream_50km"},
        },
    ]
    name_map = {"2": "牤牛河"}
    rig._enrich_unknown_river_names(features, name_map)
    assert features[0]["properties"]["river_name"] == "牤牛河"
    assert features[1]["properties"]["river_name"] == "永定河"


def test_drop_downstream_covered_by_direct():
    """几何上被同 objectid 直接河段覆盖的下游河段应被剔除。"""
    pytest.importorskip("shapely")
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 0]]},
            "properties": {"objectid": "70", "river_name": "永定河", "impact_type": "direct_buffer"},
        },
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[0.2, 0], [0.8, 0]]},
            "properties": {"objectid": "70", "river_name": "永定河", "impact_type": "downstream_50km"},
        },
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[2, 0], [3, 0]]},
            "properties": {"objectid": "70", "river_name": "永定河", "impact_type": "downstream_50km"},
        },
    ]
    result = rig._drop_downstream_covered_by_direct(features)
    assert len(result) == 2
    assert all(f["properties"]["impact_type"] != "downstream_50km" or f["geometry"]["coordinates"][0][0] == 2.0 for f in result)


def test_normalize_river_name_expands_single_char():
    """单字河系名应补全为“X河”；已有完整名称或“未知”保持不变。"""
    assert rig._normalize_river_name("青") == "青河"
    assert rig._normalize_river_name("东") == "东河"
    assert rig._normalize_river_name("永定河") == "永定河"
    assert rig._normalize_river_name("未知") == "未知"
    assert rig._normalize_river_name("") == ""
    assert rig._normalize_river_name("湖") == "湖"  # 已含水系后缀，不再追加


def test_sorted_feature_river_names_skips_unknown():
    """汇总河系名时应跳过“未知”。"""
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 0]]}, "properties": {"river_name": "永定河", "impact_type": "direct_buffer"}},
            {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[1, 0], [2, 0]]}, "properties": {"river_name": "未知", "impact_type": "downstream_50km"}},
        ],
    }
    assert rig._sorted_feature_river_names(geojson, "direct_buffer") == ["永定河"]
    assert rig._sorted_feature_river_names(geojson, "downstream_50km") == []


def test_build_river_geojson_enriches_and_dedupes():
    """_build_river_geojson 应完成名称回填与下游去重。"""
    pytest.importorskip("shapely")
    edges = [
        ("0,0", "1,0", 0, {"objectid": "2", "src_name": "牤牛河", "length_km": 10.0}),
    ]
    graph_path = _make_graph_path(edges)
    direct_rows = [
        {
            "objectid": "2",
            "id": "2",
            "river_name": "未知",
            "geom_json": '{"type":"LineString","coordinates":[[0,0],[1,0]]}',
            "length_km": 10.0,
            "min_station_distance_km": 5.0,
            "trigger_station_count": 1,
            "trigger_stations": [],
        }
    ]
    downstream_rows = [
        {
            "edge_key": "k",
            "objectid": "2",
            "id": "2",
            "river_name": "未知",
            "geom_json": '{"type":"LineString","coordinates":[[0.3,0],[0.7,0]]}',
            "length_km": 0.4,
            "min_downstream_distance_km": 0.0,
            "end_downstream_distance_km": 0.4,
            "keep_km": 0.4,
            "clip_fraction": 1.0,
            "is_direct_graph_edge": False,
            "match_distance_km": 0.0,
            "from_x": 0.3,
            "from_y": 0.0,
            "to_x": 0.7,
            "to_y": 0.0,
        }
    ]
    geojson = rig._build_river_geojson(direct_rows, downstream_rows, graph_path=graph_path)
    names = {f["properties"]["river_name"] for f in geojson["features"]}
    assert "未知" not in names
    assert "牤牛河" in names
    # 下游段被直接段覆盖，应被删除
    assert len(geojson["features"]) == 1
    assert geojson["features"][0]["properties"]["impact_type"] == "direct_buffer"


def test_luan_river_name_mapping_by_objectid():
    """is_luan=true 的要素应按 objectid 替换为滦河系全名。"""
    edges = [
        ("118,40", "119,40", 0, {"objectid": "13", "src_name": "", "river_name": "青", "length_km": 10.0, "is_luan": True}),
    ]
    graph_path = _make_graph_path(edges)
    direct_rows = [
        {
            "objectid": "13",
            "id": "13",
            "river_name": "青",
            "is_luan": True,
            "geom_json": '{"type":"LineString","coordinates":[[118,40],[119,40]]}',
            "length_km": 10.0,
            "min_station_distance_km": 5.0,
            "trigger_station_count": 1,
            "trigger_stations": [],
        }
    ]
    geojson = rig._build_river_geojson(direct_rows, [], graph_path=graph_path)
    assert len(geojson["features"]) == 1
    props = geojson["features"][0]["properties"]
    assert props["is_luan"] is True
    assert props["river_name"] == "青龙河"


def test_haihe_river_name_not_overwritten_by_luan_mapping():
    """is_luan=false 的海河系同名 objectid 不应被滦河映射覆盖。"""
    edges = [
        ("115,39", "116,39", 0, {"objectid": "13", "src_name": "南拒马河", "river_name": "南拒马河", "length_km": 10.0, "is_luan": False}),
    ]
    graph_path = _make_graph_path(edges)
    direct_rows = [
        {
            "objectid": "13",
            "id": "13",
            "river_name": "南拒马河",
            "is_luan": False,
            "geom_json": '{"type":"LineString","coordinates":[[115,39],[116,39]]}',
            "length_km": 10.0,
            "min_station_distance_km": 5.0,
            "trigger_station_count": 1,
            "trigger_stations": [],
        }
    ]
    geojson = rig._build_river_geojson(direct_rows, [], graph_path=graph_path)
    assert geojson["features"][0]["properties"]["river_name"] == "南拒马河"


def test_downstream_edge_carries_is_luan():
    """_save_downstream_edge 应将 pkl 边的 is_luan 属性透传到边字典。"""
    attr = {"objectid": "19", "src_name": "", "river_name": "滦", "length_km": 10.0, "is_luan": True}
    edges = {}
    rig._save_downstream_edge(edges, "118,40", "119,40", 0, attr, 0.0, 50.0, set())
    assert len(edges) == 1
    edge = next(iter(edges.values()))
    assert edge["is_luan"] is True


def test_downstream_row_is_luan_maps_to_luan_full_name():
    """下游行 is_luan=true 时，即使 river_name 来自 DB 也应被映射为滦河全名。"""
    edges = [
        ("118,40", "119,40", 0, {"objectid": "13", "src_name": "", "river_name": "青", "length_km": 10.0, "is_luan": True}),
    ]
    graph_path = _make_graph_path(edges)
    downstream_rows = [
        {
            "edge_key": "k",
            "objectid": "13",
            "id": "13",
            "river_name": "南拒马河",
            "is_luan": True,
            "geom_json": '{"type":"LineString","coordinates":[[118,40],[119,40]]}',
            "length_km": 10.0,
            "min_downstream_distance_km": 0.0,
            "end_downstream_distance_km": 10.0,
            "keep_km": 10.0,
            "clip_fraction": 1.0,
            "is_direct_graph_edge": False,
            "match_distance_km": 0.0,
            "from_x": 118,
            "from_y": 40,
            "to_x": 119,
            "to_y": 40,
        }
    ]
    geojson = rig._build_river_geojson([], downstream_rows, graph_path=graph_path)
    assert len(geojson["features"]) == 1
    props = geojson["features"][0]["properties"]
    assert props["is_luan"] is True
    assert props["river_name"] == "青龙河"


def test_downstream_row_haihe_keeps_db_name():
    """下游行 is_luan=false 时应保留 DB 河名，不被滦河映射覆盖。"""
    edges = [
        ("115,39", "116,39", 0, {"objectid": "13", "src_name": "南拒马河", "river_name": "南拒马河", "length_km": 10.0, "is_luan": False}),
    ]
    graph_path = _make_graph_path(edges)
    downstream_rows = [
        {
            "edge_key": "k",
            "objectid": "13",
            "id": "13",
            "river_name": "南拒马河",
            "is_luan": False,
            "geom_json": '{"type":"LineString","coordinates":[[115,39],[116,39]]}',
            "length_km": 10.0,
            "min_downstream_distance_km": 0.0,
            "end_downstream_distance_km": 10.0,
            "keep_km": 10.0,
            "clip_fraction": 1.0,
            "is_direct_graph_edge": False,
            "match_distance_km": 0.0,
            "from_x": 115,
            "from_y": 39,
            "to_x": 116,
            "to_y": 39,
        }
    ]
    geojson = rig._build_river_geojson([], downstream_rows, graph_path=graph_path)
    assert geojson["features"][0]["properties"]["river_name"] == "南拒马河"


def test_unmatched_downstream_fallback_uses_luan_mapping():
    """SQL 未匹配到的下游边回退到 pkl 直线几何时，is_luan=true 仍应映射为滦河全名。"""
    edges = [
        ("118,40", "119,40", 0, {"objectid": "13", "src_name": "", "river_name": "青", "length_km": 10.0, "is_luan": True}),
    ]
    graph_path = _make_graph_path(edges)
    edges_dict = {}
    rig._save_downstream_edge(edges_dict, "118,40", "119,40", 0, edges[0][3], 0.0, 50.0, set())
    edge = next(iter(edges_dict.values()))
    rows = rig._fill_unmatched_downstream_edges([], [edge])
    assert len(rows) == 1
    assert rows[0]["is_luan"] is True
    geojson = rig._build_river_geojson([], rows, graph_path=graph_path)
    assert geojson["features"][0]["properties"]["river_name"] == "青龙河"
