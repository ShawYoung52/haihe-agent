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
    """最小 networkx MultiDiGraph 替代，满足图遍历类函数需求。"""

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


def _run_classify(
    edges: list[tuple[Any, Any, Any, dict]],
    candidate_rows: list[dict],
    stations: list[dict],
    *,
    station_buffer_km: float = 30.0,
    direct_match_km: float = 10.0,
):
    graph_path = _make_graph_path(edges)
    graph = rig.get_graph(graph_path)
    return rig._classify_graph_edges(
        candidate_rows, graph, stations, station_buffer_km, direct_match_km
    )


def _candidate_row(objectid, p1, p2, *, min_dist=None, name="东河", **extra):
    row = {
        "objectid": objectid,
        "src_name": name,
        "river_name": name,
        "is_luan": False,
        "from_x": p1[0],
        "from_y": p1[1],
        "to_x": p2[0],
        "to_y": p2[1],
        "len_km": 10.0,
        "geom_json": f'{{"type":"LineString","coordinates":[[{p1[0]},{p1[1]}],[{p2[0]},{p2[1]}]]}}',
        "min_station_distance_km": min_dist,
        "trigger_stations": [],
        "trigger_station_count": 0,
    }
    row.update(extra)
    return row


def test_classify_uses_sql_distance_for_meandering_geometry():
    """真实几何在缓冲区内（SQL 距离小）但端点弦距超缓冲区时，仍应保留该边。"""
    edges = [
        ("0,0", "1,0", 0, {"objectid": "100", "src_name": "东河", "length_km": 10.0}),
    ]
    # 弦距约 100km（站点在 50,50），但 SQL 真实几何距离 5km
    rows = [_candidate_row("100", (0.0, 0.0), (1.0, 0.0), min_dist=5.0)]
    stations = [{"lon": 50.0, "lat": 50.0, "rain_24h": 60.0}]
    direct_edges, start_nodes, stats = _run_classify(edges, rows, stations)
    assert len(direct_edges) == 1
    assert list(direct_edges.values())[0]["is_direct_graph_edge"] is True
    assert len(start_nodes) == 1
    assert stats["direct_part_matched_edge_count"] == 1


def test_classify_buffer_only_edges_become_starts_not_direct():
    """缓冲区命中但超出 direct_match_km 的边也应作为 direct_buffer 输出（is_direct_graph_edge=False），避免下游无上游。"""
    edges = [
        ("0,0", "0.1,0", 0, {"objectid": "100", "src_name": "东河", "length_km": 10.0}),
        ("0.1,0", "0.2,0", 0, {"objectid": "101", "src_name": "东河", "length_km": 10.0}),
    ]
    rows = [
        _candidate_row("100", (0.0, 0.0), (0.1, 0.0), min_dist=5.0, trigger_station_count=1),
        _candidate_row("101", (0.1, 0.0), (0.2, 0.0), min_dist=20.0, trigger_station_count=1),
    ]
    stations = [{"lon": 0.0, "lat": 0.0, "rain_24h": 60.0}]
    direct_edges, start_nodes, stats = _run_classify(edges, rows, stations)
    # 两条候选边都进 direct_edges，用 is_direct_graph_edge 区分
    assert len(direct_edges) == 2
    by_oid = {e["objectid"]: e for e in direct_edges.values()}
    assert by_oid["100"]["is_direct_graph_edge"] is True
    assert by_oid["101"]["is_direct_graph_edge"] is False
    assert len(start_nodes) == 2
    assert stats["direct_part_matched_edge_count"] == 1
    assert stats["station_buffer_fallback_used"] is True
    assert stats["station_buffer_fallback_edge_count"] == 1


def test_classify_skips_edges_without_candidate_row():
    """无 full_v6 候选行匹配的 pkl 边应被跳过。"""
    edges = [
        ("10,10", "11,10", 0, {"objectid": "100", "src_name": "东河", "length_km": 10.0}),
    ]
    rows = [_candidate_row("999", (0.0, 0.0), (1.0, 0.0), min_dist=1.0, name="西河")]
    stations = [{"lon": 0.0, "lat": 0.0, "rain_24h": 60.0}]
    direct_edges, start_nodes, stats = _run_classify(edges, rows, stations)
    assert len(direct_edges) == 0
    assert len(start_nodes) == 0
    assert stats["station_buffer_fallback_used"] is False


def test_classify_falls_back_to_chord_distance_without_sql_distance():
    """候选行缺 min_station_distance_km 时，退化为 pkl 端点弦距分类。"""
    edges = [
        ("0,0", "0.1,0", 0, {"objectid": "100", "src_name": "东河", "length_km": 10.0}),
    ]
    rows = [_candidate_row("100", (0.0, 0.0), (0.1, 0.0), min_dist=None)]
    stations = [{"lon": 0.05, "lat": 0.0, "rain_24h": 60.0}]
    direct_edges, start_nodes, stats = _run_classify(edges, rows, stations)
    assert len(direct_edges) == 1
    assert list(direct_edges.values())[0]["is_direct_graph_edge"] is True


def test_collect_downstream_skips_direct_edges():
    """已是直接边的 pkl 边不再重复记录为下游边，但遍历会继续穿过它。"""
    edges = [
        ("0,0", "0.1,0", 0, {"objectid": "100", "src_name": "东河", "length_km": 10.0}),
        ("0.1,0", "0.2,0", 0, {"objectid": "101", "src_name": "东河", "length_km": 10.0}),
        ("0.2,0", "0.3,0", 0, {"objectid": "102", "src_name": "东河", "length_km": 10.0}),
    ]
    graph_path = _make_graph_path(edges)
    graph = rig.get_graph(graph_path)
    direct_keys = {rig._edge_key("0.1,0", "0.2,0", 0, edges[1][3])}
    downstream = rig._collect_downstream_edges({"0.1,0": 0.0}, graph, direct_keys, 50.0)
    keys = {e["edge_key"] for e in downstream}
    assert rig._edge_key("0.1,0", "0.2,0", 0, edges[1][3]) not in keys
    # 穿过直接边后，更下游的边仍被记录且距离累计正确
    third = rig._edge_key("0.2,0", "0.3,0", 0, edges[2][3])
    assert third in keys
    third_edge = next(e for e in downstream if e["edge_key"] == third)
    assert third_edge["min_distance_km"] == 10.0


def test_clip_geometry_reversed_digitization_direction():
    """数字化方向与流向相反时，应从靠近 pkl from 节点的一端开始裁剪。"""
    geometry = {"type": "LineString", "coordinates": [[2.0, 0.0], [1.0, 0.0], [0.0, 0.0]]}
    clipped = rig._clip_geometry_to_keep_km(geometry, 30.0, (0.0, 0.0))
    coords = clipped["coordinates"]
    assert coords[0] == [0.0, 0.0]
    # 30km 约 0.35 个经度
    assert 0.2 < coords[-1][0] < 0.5
    assert len(coords) == 2


def test_clip_geometry_multilinestring_picks_longest_part():
    """MultiLineString 输入应取最长 part 裁剪，不再依赖 Shapely。"""
    geometry = {
        "type": "MultiLineString",
        "coordinates": [
            [[0.0, 0.0], [0.01, 0.0]],
            [[0.0, 1.0], [1.0, 1.0]],
        ],
    }
    clipped = rig._clip_geometry_to_keep_km(geometry, 30.0, (0.0, 1.0))
    assert clipped["type"] == "LineString"
    assert clipped["coordinates"][0] == [0.0, 1.0]


def test_clip_geometry_keep_km_covers_full_length():
    """keep_km 覆盖全长时返回完整几何（MultiLineString 解包为 LineString）。"""
    geometry = {"type": "MultiLineString", "coordinates": [[[0.0, 0.0], [0.1, 0.0]]]}
    clipped = rig._clip_geometry_to_keep_km(geometry, 999.0, (0.0, 0.0))
    assert clipped == {"type": "LineString", "coordinates": [[0.0, 0.0], [0.1, 0.0]]}


def test_edge_lookup_key_absorbs_precision_drift():
    """lookup 键应吸收 1e-7 量级的坐标精度差异。"""
    rows = [
        {"objectid": "1", "from_x": 116.08299999999998, "from_y": 39.5, "to_x": 116.2, "to_y": 39.6, "river_name": "A"},
    ]
    lookup = rig._build_edge_lookup(rows)
    assert lookup.get(rig._edge_lookup_key("1", 116.083, 39.5, 116.2, 39.6)) is not None


def test_downstream_feature_length_km_reports_keep_km():
    """下游裁剪段的 length_km 应等于 keep_km 而非 full_v6 全长。"""
    edges = [
        ("0,0", "1,0", 0, {"objectid": "2", "src_name": "牤牛河", "length_km": 10.0}),
    ]
    graph_path = _make_graph_path(edges)
    edge_info = {
        "edge_key": "k",
        "objectid": "2",
        "river_name": "牤牛河",
        "from_x": 0.0,
        "from_y": 0.0,
        "to_x": 1.0,
        "to_y": 0.0,
        "is_direct_graph_edge": False,
        "is_luan": False,
        "min_distance_km": 40.0,
        "end_distance_km": 50.0,
        "keep_km": 10.0,
        "clip_fraction": 0.125,
    }
    candidate_rows = [_candidate_row("2", (0.0, 0.0), (1.0, 0.0), name="牤牛河", len_km=80.0)]
    geojson = rig._build_river_geojson({}, [edge_info], candidate_rows, graph_path=graph_path)
    props = geojson["features"][0]["properties"]
    assert props["length_km"] == 10.0
    assert props["geometry_source"] == f"full_{rig.RIVER_TABLE_VERSION}_downstream_clipped"
    # per-edge 传播时间：下游用 end_distance_km
    assert props["propagation_distance_km"] == 50.0
    assert props["propagation_time_hours"] == pytest.approx(6.9, abs=0.1)  # 50/7.2


def test_geojson_direct_feature_has_per_edge_propagation():
    """直接河段 GeoJSON feature 应有 per-edge 传播时间（基于 length_km）。"""
    edges = [
        ("0,0", "1,0", 0, {"objectid": "2", "src_name": "东河", "length_km": 7.2}),
    ]
    graph_path = _make_graph_path(edges)
    direct = {
        "k": {
            "edge_key": "k", "objectid": "2", "river_name": "东河",
            "from_x": 0.0, "from_y": 0.0, "to_x": 1.0, "to_y": 0.0,
            "is_direct_graph_edge": True, "is_luan": False,
            "min_station_distance_km": 0.5, "length_km": 7.2,
            "trigger_station_count": 1,
            "trigger_stations": [{"station_id": "X", "station_name": "X站", "rain_24h": 60.0}],
            "row": _candidate_row("2", (0.0, 0.0), (1.0, 0.0), name="东河", len_km=7.2),
        },
    }
    candidate_rows = [_candidate_row("2", (0.0, 0.0), (1.0, 0.0), name="东河", len_km=7.2)]
    geojson = rig._build_river_geojson(direct, [], candidate_rows, graph_path=graph_path,
                                        flow_velocity_mps=2.0)
    props = geojson["features"][0]["properties"]
    assert props["propagation_distance_km"] == 7.2
    assert props["propagation_time_hours"] == pytest.approx(1.0, abs=0.1)  # 7.2/7.2


def test_pick_river_name_luan_mapping_does_not_override_full_name():
    """is_luan=true 但 src_name 已是合法全名时，不应被静态映射覆盖。"""
    edge = {"objectid": "13", "river_name": "未知", "is_luan": True}
    row = {"src_name": "青龙河干流", "river_name": "未知"}
    assert rig._pick_river_name(row, edge, {"13": "青龙河"}) == "青龙河干流"


def test_edge_lookup_direction_agnostic():
    """full_v6 行数字化方向与 pkl 流向相反时，应通过反向键命中。"""
    rows = [
        # pkl 流向 from=(0,0)→to=(1,0)，DB 行存反了 from=(1,0)→to=(0,0)
        {"objectid": "1", "from_x": 1.0, "from_y": 0.0, "to_x": 0.0, "to_y": 0.0, "river_name": "A"},
    ]
    lookup = rig._build_edge_lookup(rows)
    # 正向查 (0,0)→(1,0) 应命中（通过反向索引）
    assert rig._edge_lookup_key("1", 0.0, 0.0, 1.0, 0.0) in lookup
    assert lookup[rig._edge_lookup_key("1", 0.0, 0.0, 1.0, 0.0)]["river_name"] == "A"


def test_get_edge_length_km_falls_back_to_haversine_on_nan():
    """len_km 为 NaN 时应回退到端点 haversine 距离，避免污染下游 Dijkstra。"""
    import math
    attr = {"objectid": "19", "len_km": float("nan"), "from_x": 116.0, "from_y": 39.0, "to_x": 116.1, "to_y": 39.0}
    length = rig.get_edge_length_km(attr, from_xy=(116.0, 39.0), to_xy=(116.1, 39.0))
    assert math.isfinite(length) and length > 0
    # 0.1 经度在 39°N 约 8.7km
    assert 8.0 < length < 10.0


def test_get_edge_length_km_nan_propagates_without_from_to():
    """没有 from/to 兜底时，NaN 返回 0（不传播 nan）。"""
    import math
    attr = {"len_km": float("nan")}
    length = rig.get_edge_length_km(attr)
    assert length == 0.0 and not math.isnan(length)


def test_unwrap_geometry_single_part_multilinestring():
    """单 part MultiLineString 应解包为 LineString。"""
    geom = {"type": "MultiLineString", "coordinates": [[[0, 0], [1, 1], [2, 2]]]}
    assert rig._unwrap_geometry(geom) == {"type": "LineString", "coordinates": [[0, 0], [1, 1], [2, 2]]}


def test_unwrap_geometry_multi_part_stays_multilinestring():
    """多 part MultiLineString 保持不变。"""
    geom = {"type": "MultiLineString", "coordinates": [[[0, 0], [1, 1]], [[2, 2], [3, 3]]]}
    assert rig._unwrap_geometry(geom) == geom


def test_resolve_edge_features_unwraps_multilinestring_direct_feature():
    """direct_buffer 特征的 MultiLineString 几何应解包为 LineString。"""
    edges = [
        ("0,0", "1,0", 0, {"objectid": "2", "src_name": "牤牛河", "river_name": "牤牛河", "length_km": 10.0}),
    ]
    graph_path = _make_graph_path(edges)
    direct_edges = {
        "k": {
            "edge_key": "k", "objectid": "2", "river_name": "牤牛河",
            "from_x": 0.0, "from_y": 0.0, "to_x": 1.0, "to_y": 0.0,
            "length_km": 10.0, "is_direct_graph_edge": True, "is_luan": False,
            "min_station_distance_km": 5.0, "trigger_station_count": 1, "trigger_stations": [],
        }
    }
    candidate_rows = [
        {
            "objectid": "2", "src_name": "牤牛河", "river_name": "牤牛河", "is_luan": False,
            "from_x": 0.0, "from_y": 0.0, "to_x": 1.0, "to_y": 0.0, "len_km": 10.0,
            "geom_json": '{"type":"MultiLineString","coordinates":[[[0,0],[0.5,0.1],[1,0]]]}',
        }
    ]
    geojson = rig._build_river_geojson(direct_edges, [], candidate_rows, graph_path=graph_path)
    assert geojson["features"][0]["geometry"]["type"] == "LineString"
    assert len(geojson["features"][0]["geometry"]["coordinates"]) == 3


def test_classify_emits_buffer_only_edge_as_direct_with_flag():
    """10-30km 缓冲区边应作为 direct_buffer 输出（is_direct_graph_edge=False），不再消失或被误标为 downstream。"""
    edges = [
        ("0,0", "0.1,0", 0, {"objectid": "100", "src_name": "东河", "length_km": 10.0}),
        ("0.1,0", "0.2,0", 0, {"objectid": "101", "src_name": "东河", "length_km": 10.0}),
        ("0.2,0", "0.3,0", 0, {"objectid": "102", "src_name": "东河", "length_km": 10.0}),
    ]
    rows = [
        _candidate_row("100", (0.0, 0.0), (0.1, 0.0), min_dist=5.0),
        _candidate_row("101", (0.1, 0.0), (0.2, 0.0), min_dist=15.0),
        # 102 不在候选行中（超出 30km 缓冲区）
    ]
    stations = [{"lon": 0.0, "lat": 0.0, "rain_24h": 60.0}]
    direct_edges, start_nodes, stats = _run_classify(edges, rows, stations)
    # 100 (≤10km) 和 101 (10-30km) 都应作为 direct_buffer
    assert len(direct_edges) == 2
    by_oid = {e["objectid"]: e for e in direct_edges.values()}
    assert by_oid["100"]["is_direct_graph_edge"] is True
    assert by_oid["101"]["is_direct_graph_edge"] is False


def test_match_edge_spatially_finds_row_by_geometry_proximity():
    """精确端点键失配时，按 objectid + 几何经过两端点空间兜底匹配。"""
    # pkl 边 from=(0,0) to=(1,0)，但 DB 行的 from_x/from_y 完全不同（精确键失配）
    # DB 行的几何经过 (0,0) 和 (1,0)
    row = {
        "objectid": "1",
        "from_x": 999.0, "from_y": 999.0,  # 故意不匹配
        "to_x": 888.0, "to_y": 888.0,
        "geom_json": '{"type":"LineString","coordinates":[[0,0],[0.5,0.01],[1,0]]}',
    }
    spatial_lookup = rig._build_spatial_lookup([row])
    matched = rig._match_edge_spatially("1", (0.0, 0.0), (1.0, 0.0), spatial_lookup)
    assert matched is row


def test_match_edge_spatially_rejects_far_endpoints():
    """pkl 端点远离几何时不应匹配。"""
    row = {
        "objectid": "1",
        "from_x": 999.0, "from_y": 999.0,
        "to_x": 888.0, "to_y": 888.0,
        "geom_json": '{"type":"LineString","coordinates":[[50,50],[51,50]]}',
    }
    spatial_lookup = rig._build_spatial_lookup([row])
    matched = rig._match_edge_spatially("1", (0.0, 0.0), (1.0, 0.0), spatial_lookup)
    assert matched is None


def test_classify_uses_spatial_fallback_when_endpoint_key_mismatches():
    """候选行 from_x/from_y 与 pkl 端点不一致但几何经过端点时，应通过空间兜底匹配。"""
    edges = [
        ("0,0", "1,0", 0, {"objectid": "1", "src_name": "东河", "length_km": 10.0}),
    ]
    # DB 行的 from_x/from_y 故意写错，但几何经过 (0,0)-(1,0)
    rows = [{
        "objectid": "1",
        "src_name": "东河",
        "river_name": "东河",
        "is_luan": False,
        "from_x": 999.0, "from_y": 999.0,  # 精确键失配
        "to_x": 888.0, "to_y": 888.0,
        "len_km": 10.0,
        "geom_json": '{"type":"LineString","coordinates":[[0,0],[0.5,0.01],[1,0]]}',
        "min_station_distance_km": 5.0,
        "trigger_stations": [],
        "trigger_station_count": 1,
    }]
    stations = [{"lon": 0.5, "lat": 0.0, "rain_24h": 60.0}]
    direct_edges, start_nodes, stats = _run_classify(edges, rows, stations)
    assert len(direct_edges) == 1
    assert list(direct_edges.values())[0]["is_direct_graph_edge"] is True


def test_build_river_geojson_uses_spatial_fallback_for_geometry():
    """下游边精确键失配时，_build_river_geojson 应通过空间兜底找到几何。"""
    edge_info = {
        "edge_key": "k",
        "objectid": "1",
        "river_name": "东河",
        "from_x": 0.0, "from_y": 0.0,
        "to_x": 1.0, "to_y": 0.0,
        "is_direct_graph_edge": False,
        "is_luan": False,
        "min_distance_km": 0.0,
        "end_distance_km": 10.0,
        "keep_km": 10.0,
        "clip_fraction": 1.0,
    }
    # DB 行 from_x 写错，但几何经过 (0,0)-(1,0)
    candidate_rows = [{
        "objectid": "1",
        "src_name": "东河",
        "river_name": "东河",
        "is_luan": False,
        "from_x": 999.0, "from_y": 999.0,
        "to_x": 888.0, "to_y": 888.0,
        "len_km": 10.0,
        "geom_json": '{"type":"LineString","coordinates":[[0,0],[0.5,0.01],[1,0]]}',
    }]
    geojson = rig._build_river_geojson({}, [edge_info], candidate_rows)
    props = geojson["features"][0]["properties"]
    # 应通过空间兜底拿到真实几何，而非直线兜底
    assert props["geometry_source"].startswith("full_")
    coords = geojson["features"][0]["geometry"]["coordinates"]
    assert len(coords) >= 2  # 真实几何而非 2 点直线（实际 3 点）


def test_pick_river_name_prefers_src_name_then_pkl():
    """名称优先级：full_v6.src_name > full_v6.river_name > pkl.river_name。"""
    luan_mapping = {}
    edge = {"objectid": "2", "river_name": "pkl_name", "is_luan": False}
    row_src = {"src_name": "src_name", "river_name": "row_name"}
    row_no_src = {"src_name": "未知", "river_name": "row_name"}
    row_unknown = {"src_name": "未知", "river_name": "未知"}
    assert rig._pick_river_name(row_src, edge, luan_mapping) == "src_name"
    assert rig._pick_river_name(row_no_src, edge, luan_mapping) == "row_name"
    assert rig._pick_river_name(row_unknown, edge, luan_mapping) == "pkl_name"


def test_pick_river_name_uses_luan_mapping():
    """is_luan=true 且其他名称都未知时，使用滦河 objectid 映射。"""
    edge = {"objectid": "13", "river_name": "未知", "is_luan": True}
    row = {"src_name": "未知", "river_name": "未知"}
    assert rig._pick_river_name(row, edge, {"13": "青龙河"}) == "青龙河"


def test_pick_river_name_haihe_not_overwritten_by_luan_mapping():
    """is_luan=false 时即使 objectid 与滦河系冲突，也保留 DB 名称。"""
    edge = {"objectid": "13", "river_name": "未知", "is_luan": False}
    row = {"src_name": "南拒马河", "river_name": "南拒马河"}
    assert rig._pick_river_name(row, edge, {"13": "青龙河"}) == "南拒马河"


def test_build_river_geojson_resolves_names():
    """_build_river_geojson 应通过 lookup 回填河流名称。"""
    edges = [
        ("0,0", "1,0", 0, {"objectid": "2", "src_name": "牤牛河", "river_name": "牤牛河", "length_km": 10.0}),
    ]
    graph_path = _make_graph_path(edges)
    direct_edges = {
        "k": {
            "edge_key": "k",
            "objectid": "2",
            "river_name": "牤牛河",
            "from_x": 0.0,
            "from_y": 0.0,
            "to_x": 1.0,
            "to_y": 0.0,
            "is_direct_graph_edge": True,
            "is_luan": False,
            "min_station_distance_km": 5.0,
            "trigger_station_count": 1,
            "trigger_stations": [],
        }
    }
    candidate_rows = [
        {
            "objectid": "2",
            "src_name": "未知",
            "river_name": "未知",
            "is_luan": False,
            "from_x": 0.0,
            "from_y": 0.0,
            "to_x": 1.0,
            "to_y": 0.0,
            "len_km": 10.0,
            "geom_json": '{"type":"LineString","coordinates":[[0,0],[1,0]]}',
        }
    ]
    geojson = rig._build_river_geojson(direct_edges, [], candidate_rows, graph_path=graph_path)
    assert len(geojson["features"]) == 1
    assert geojson["features"][0]["properties"]["river_name"] == "牤牛河"


def test_resolve_edge_features_uses_fallback_line_when_row_missing():
    """pkl 边在 full_v6 lookup 中缺失时，应使用直线几何兜底。"""
    edges = [
        ("118,40", "119,40", 0, {"objectid": "13", "src_name": "", "river_name": "青", "length_km": 10.0, "is_luan": True}),
    ]
    graph_path = _make_graph_path(edges)
    edge_info = {
        "edge_key": "k",
        "objectid": "13",
        "river_name": "青",
        "from_x": 118.0,
        "from_y": 40.0,
        "to_x": 119.0,
        "to_y": 40.0,
        "is_direct_graph_edge": False,
        "is_luan": True,
        "min_distance_km": 0.0,
        "end_distance_km": 10.0,
        "keep_km": 10.0,
        "clip_fraction": 1.0,
    }
    geojson = rig._build_river_geojson({}, [edge_info], [], graph_path=graph_path)
    assert len(geojson["features"]) == 1
    props = geojson["features"][0]["properties"]
    assert props["river_name"] == "青龙河"
    assert props["geometry_source"] == "pkl_edge_straight_fallback"


def test_luan_river_name_mapping_by_objectid():
    """is_luan=true 的要素应按 objectid 替换为滦河系全名。"""
    edges = [
        ("118,40", "119,40", 0, {"objectid": "13", "src_name": "", "river_name": "青", "length_km": 10.0, "is_luan": True}),
    ]
    graph_path = _make_graph_path(edges)
    direct_edges = {
        "k": {
            "edge_key": "k",
            "objectid": "13",
            "river_name": "青",
            "from_x": 118.0,
            "from_y": 40.0,
            "to_x": 119.0,
            "to_y": 40.0,
            "is_direct_graph_edge": True,
            "is_luan": True,
            "min_station_distance_km": 5.0,
            "trigger_station_count": 1,
            "trigger_stations": [],
        }
    }
    candidate_rows = [
        {
            "objectid": "13",
            "src_name": "未知",
            "river_name": "青",
            "is_luan": True,
            "from_x": 118.0,
            "from_y": 40.0,
            "to_x": 119.0,
            "to_y": 40.0,
            "len_km": 10.0,
            "geom_json": '{"type":"LineString","coordinates":[[118,40],[119,40]]}',
        }
    ]
    geojson = rig._build_river_geojson(direct_edges, [], candidate_rows, graph_path=graph_path)
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
    direct_edges = {
        "k": {
            "edge_key": "k",
            "objectid": "13",
            "river_name": "南拒马河",
            "from_x": 115.0,
            "from_y": 39.0,
            "to_x": 116.0,
            "to_y": 39.0,
            "is_direct_graph_edge": True,
            "is_luan": False,
            "min_station_distance_km": 5.0,
            "trigger_station_count": 1,
            "trigger_stations": [],
        }
    }
    candidate_rows = [
        {
            "objectid": "13",
            "src_name": "南拒马河",
            "river_name": "南拒马河",
            "is_luan": False,
            "from_x": 115.0,
            "from_y": 39.0,
            "to_x": 116.0,
            "to_y": 39.0,
            "len_km": 10.0,
            "geom_json": '{"type":"LineString","coordinates":[[115,39],[116,39]]}',
        }
    ]
    geojson = rig._build_river_geojson(direct_edges, [], candidate_rows, graph_path=graph_path)
    assert geojson["features"][0]["properties"]["river_name"] == "南拒马河"


def test_downstream_edge_carries_is_luan():
    """_save_downstream_edge 应将 pkl 边的 is_luan 属性透传到边字典。"""
    attr = {"objectid": "19", "src_name": "", "river_name": "滦", "length_km": 10.0, "is_luan": True}
    edges = {}
    rig._save_downstream_edge(edges, "118,40", "119,40", 0, attr, 0.0, 50.0, set())
    assert len(edges) == 1
    edge = next(iter(edges.values()))
    assert edge["is_luan"] is True


def test_classify_graph_edges_marks_direct_and_buffer_only():
    """_classify_graph_edges 应区分真实直接边和仅缓冲区边。"""
    edges = [
        ("116.0,39.0", "116.1,39.0", 0, {"objectid": "1", "src_name": "A", "river_name": "A", "length_km": 10.0}),
    ]
    graph_path = _make_graph_path(edges)
    graph = rig.get_graph(graph_path)
    candidate_rows = [
        {
            "objectid": "1",
            "src_name": "A",
            "river_name": "A",
            "is_luan": False,
            "from_x": 116.0,
            "from_y": 39.0,
            "to_x": 116.1,
            "to_y": 39.0,
            "len_km": 10.0,
            "geom_json": '{"type":"LineString","coordinates":[[116.0,39.0],[116.1,39.0]]}',
            "trigger_stations": [],
            "trigger_station_count": 1,
        }
    ]
    # station 5km from the edge midpoint → within direct_match_km
    stations = [{"lon": 116.05, "lat": 39.0, "rain_24h": 100.0}]
    direct_edges, start_nodes, stats = rig._classify_graph_edges(
        candidate_rows, graph, stations, station_buffer_km=30.0, direct_match_km=10.0
    )
    assert len(direct_edges) == 1
    assert list(direct_edges.values())[0]["is_direct_graph_edge"] is True
    assert len(start_nodes) == 1
    assert stats["direct_part_matched_edge_count"] == 1
    assert stats["station_buffer_fallback_edge_count"] == 0


# ---------------------------------------------------------------------------
# 传播时间估算（_build_river_propagation）
# ---------------------------------------------------------------------------


def _direct_edge(name: str, length_km: float, **extra) -> dict:
    edge = {"edge_key": f"k-{name}-{length_km}", "river_name": name, "length_km": length_km}
    edge.update(extra)
    return edge


def _downstream_edge(name: str, end_distance_km: float, **extra) -> dict:
    edge = {"edge_key": f"d-{name}-{end_distance_km}", "river_name": name, "end_distance_km": end_distance_km}
    edge.update(extra)
    return edge


def test_build_river_propagation_uses_max_downstream_end_distance():
    direct = {"a": _direct_edge("滦河", 3.0)}
    downstream = [_downstream_edge("滦河", 36.0), _downstream_edge("滦河", 12.0)]
    result = rig._build_river_propagation(direct, downstream, 2.0)
    assert result["flow_velocity_mps"] == 2.0
    assert len(result["rivers"]) == 1
    river = result["rivers"][0]
    assert river["river_name"] == "滦河"
    assert river["propagation_distance_km"] == 36.0
    assert river["propagation_time_hours"] == 5.0  # 36 / 7.2
    assert river["arrival_estimate_readable"] == "约5.0小时"
    assert river["has_downstream"] is True


def test_build_river_propagation_direct_only_uses_longest_direct_length():
    direct = {"a": _direct_edge("东河", 1.8), "b": _direct_edge("东河", 3.6)}
    result = rig._build_river_propagation(direct, [], 2.0)
    river = result["rivers"][0]
    assert river["propagation_distance_km"] == 3.6
    assert river["propagation_time_hours"] == 0.5  # 3.6 / 7.2
    assert river["arrival_estimate_readable"] == "约30分钟"
    assert river["has_downstream"] is False


def test_build_river_propagation_skips_non_finite_and_sorts_desc():
    direct = {"a": _direct_edge("甲河", float("nan")), "b": _direct_edge("乙河", 7.2)}
    downstream = [_downstream_edge("丙河", 72.0)]
    result = rig._build_river_propagation(direct, downstream, 2.0)
    names = [r["river_name"] for r in result["rivers"]]
    assert names == ["丙河", "乙河"]  # 甲河 NaN 被跳过；10.0h 的丙河排在 1.0h 的乙河前


def test_build_river_propagation_empty():
    assert rig._build_river_propagation({}, [], 2.0) == {"flow_velocity_mps": 2.0, "rivers": []}


def test_validate_params_rejects_non_positive_flow_velocity():
    with pytest.raises(ValueError):
        rig._validate_params(50.0, 30.0, 50.0, 0.0)
    with pytest.raises(ValueError):
        rig._validate_params(50.0, 30.0, 50.0, -1.0)
    with pytest.raises(ValueError):
        rig._validate_params(50.0, 30.0, 50.0, float("nan"))


def test_build_river_propagation_downstream_takes_priority_over_direct():
    """同一条河同时有直接边与下游边时，距离口径取下游累计距离（即使直接边更长）。"""
    direct = {"a": _direct_edge("滦河", 10.0)}
    downstream = [_downstream_edge("滦河", 5.0)]
    river = rig._build_river_propagation(direct, downstream, 2.0)["rivers"][0]
    assert river["propagation_distance_km"] == 5.0
    assert river["has_downstream"] is True


def test_build_river_propagation_hour_boundary_readable():
    direct = {"a": _direct_edge("东河", 7.2)}
    river = rig._build_river_propagation(direct, [], 2.0)["rivers"][0]
    assert river["propagation_time_hours"] == 1.0
    assert river["arrival_estimate_readable"] == "约1.0小时"


def test_build_river_propagation_resolves_luan_single_char_name():
    """滦河系 pkl 单字缩写必须经滦河映射回填，与 GeoJSON 命名口径一致。"""
    direct = {"a": _direct_edge("滦", 3.6, is_luan=True, objectid="1")}
    result = rig._build_river_propagation(direct, [], 2.0, luan_mapping={"1": "滦河"})
    assert result["rivers"][0]["river_name"] == "滦河"


def test_build_river_propagation_downstream_name_uses_full_v6_row():
    """下游边无 "row"，应经 candidate_rows 查 full_v6 行后命名，与 GeoJSON 口径一致，而非用 pkl river_name。"""
    candidate_rows = [
        _candidate_row("1", (0.0, 0.0), (1.0, 0.0), name="新名"),
    ]
    # 下游边由 _save_downstream_edge 构造：带 objectid/from_x/to_x，但 river_name 是 pkl 旧名、无 "row"
    downstream = [{
        "edge_key": "d1", "objectid": "1", "river_name": "旧名",
        "end_distance_km": 36.0, "from_x": 0.0, "from_y": 0.0, "to_x": 1.0, "to_y": 0.0,
    }]
    result = rig._build_river_propagation({}, downstream, 2.0, candidate_rows=candidate_rows)
    assert result["rivers"][0]["river_name"] == "新名"  # full_v6 src_name，非 pkl "旧名"


def test_empty_result_includes_river_propagation_block():
    result = rig._empty_result(
        stations=[],
        threshold=50.0,
        buffer_km=30.0,
        downstream_km=50.0,
        direct_match_km=10.0,
        schema="public",
        table="t",
        graph_path=None,
        extra=None,
        flow_velocity_mps=3.0,
    )
    assert result["river_propagation"] == {"flow_velocity_mps": 3.0, "rivers": []}
