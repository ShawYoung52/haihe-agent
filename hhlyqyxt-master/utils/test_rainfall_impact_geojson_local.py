r"""本地测试：5 分钟 CSV -> 24 小时累计降水 -> 暴雨影响河流 GeoJSON。

这个脚本不依赖 PostGIS，适合在本地用 CSV + river_directed_v5.pkl 快速验证算法链路。
它会使用 pkl 中的河网拓扑和边坐标生成 GeoJSON。

默认路径按当前测试环境写好：
- CSV: C:\Users\gaozr\Downloads\24hourmindata.csv
- 河网 pkl: E:\tj\line\result\river_directed_v5.pkl

运行示例：
    cd hhlyqyxt-master
    python utils/test_rainfall_impact_geojson_local.py

自定义示例：
    python utils/test_rainfall_impact_geojson_local.py \
        --csv "C:\Users\gaozr\Downloads\24hourmindata.csv" \
        --graph "E:\tj\line\result\river_directed_v5.pkl" \
        --output-dir "C:\Users\gaozr\Downloads\rain_impact_test"
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import defaultdict
from itertools import count
from pathlib import Path
from typing import Any, Iterable, Iterator

import pandas as pd


CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rainfall_impact_geojson import (  # noqa: E402
    aggregate_5min_station_pre_to_24h,
    get_edge_length_km,
    get_edge_river_name,
    get_graph,
    iter_graph_edges,
    iter_out_edges,
    make_edge_id,
)


DEFAULT_CSV_PATH = r"C:\Users\gaozr\Downloads\24hourmindata.csv"
DEFAULT_GRAPH_PATH = r"E:\tj\line\result\river_directed_v5.pkl"


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return None
        return out
    except (TypeError, ValueError):
        return None


def _as_xy_pair(value: Any) -> list[float] | None:
    """把 tuple/list/string 节点解析成 [lon, lat]。"""
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        x = _safe_float(value[0])
        y = _safe_float(value[1])
        if x is not None and y is not None:
            return [x, y]

    text = str(value)
    nums = re.findall(r"[-+]?\d+(?:\.\d+)?", text)
    if len(nums) >= 2:
        x = _safe_float(nums[0])
        y = _safe_float(nums[1])
        if x is not None and y is not None:
            return [x, y]
    return None


def _looks_like_xy(value: Any) -> bool:
    return _as_xy_pair(value) is not None


def _normalize_lines(data: Any) -> list[list[list[float]]]:
    """把 GeoJSON/paths/coordinates 统一成 [[[lon,lat], ...], ...]。"""
    if data is None:
        return []

    if hasattr(data, "__geo_interface__"):
        data = data.__geo_interface__

    if isinstance(data, str):
        text = data.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []

    if isinstance(data, dict):
        geom_type = data.get("type")
        coords = data.get("coordinates")
        if geom_type == "LineString":
            return _normalize_lines(coords)
        if geom_type == "MultiLineString":
            return _normalize_lines(coords)
        return []

    if not isinstance(data, (list, tuple)) or not data:
        return []

    # [[x,y], [x,y], ...]
    if all(_looks_like_xy(item) for item in data):
        line = []
        for item in data:
            xy = _as_xy_pair(item)
            if xy is not None:
                line.append(xy)
        return [line] if len(line) >= 2 else []

    # [[[x,y], ...], [[x,y], ...]]
    lines: list[list[list[float]]] = []
    for part in data:
        lines.extend(_normalize_lines(part))
    return [line for line in lines if len(line) >= 2]


def edge_to_lines(u: Any, v: Any, attr: dict) -> list[list[list[float]]]:
    """优先读取边上的真实折线；没有时退化为节点连线。"""
    for key in (
        "paths",
        "path",
        "geometry",
        "geom",
        "geojson",
        "coordinates",
        "coords",
        "line",
        "polyline",
    ):
        if isinstance(attr, dict) and key in attr:
            lines = _normalize_lines(attr.get(key))
            if lines:
                return lines

    start = _as_xy_pair(u)
    end = _as_xy_pair(v)
    if start and end:
        return [[start, end]]
    return []


def _project_lonlat(lon: float, lat: float, ref_lat: float) -> tuple[float, float]:
    """经纬度转近似平面公里坐标，用于本地距离筛选。"""
    x = lon * 111.320 * math.cos(math.radians(ref_lat))
    y = lat * 110.574
    return x, y


def _point_to_segment_distance_km(
    px: float,
    py: float,
    ax: float,
    ay: float,
    bx: float,
    by: float,
) -> float:
    ref_lat = py
    p = _project_lonlat(px, py, ref_lat)
    a = _project_lonlat(ax, ay, ref_lat)
    b = _project_lonlat(bx, by, ref_lat)

    vx = b[0] - a[0]
    vy = b[1] - a[1]
    wx = p[0] - a[0]
    wy = p[1] - a[1]

    denom = vx * vx + vy * vy
    if denom <= 1e-12:
        return math.hypot(p[0] - a[0], p[1] - a[1])

    t = max(0.0, min(1.0, (wx * vx + wy * vy) / denom))
    cx = a[0] + t * vx
    cy = a[1] + t * vy
    return math.hypot(p[0] - cx, p[1] - cy)


def point_to_lines_distance_km(lon: float, lat: float, lines: list[list[list[float]]]) -> float:
    best = math.inf
    for line in lines:
        for i in range(len(line) - 1):
            a = line[i]
            b = line[i + 1]
            if len(a) < 2 or len(b) < 2:
                continue
            dist = _point_to_segment_distance_km(lon, lat, a[0], a[1], b[0], b[1])
            if dist < best:
                best = dist
    return best


def _line_to_geometry(lines: list[list[list[float]]]) -> dict | None:
    cleaned = []
    for line in lines:
        clean_line = [[float(x), float(y)] for x, y, *_rest in line if x is not None and y is not None]
        if len(clean_line) >= 2:
            cleaned.append(clean_line)

    if not cleaned:
        return None
    if len(cleaned) == 1:
        return {"type": "LineString", "coordinates": cleaned[0]}
    return {"type": "MultiLineString", "coordinates": cleaned}


def _station_record(row: pd.Series) -> dict:
    fields = [
        "station_id",
        "station_name",
        "province",
        "city",
        "cnty",
        "town",
        "lon",
        "lat",
        "rain_24h",
        "obs_count",
        "valid_pre_count",
        "start_time",
        "end_time",
    ]
    result = {}
    for field in fields:
        if field not in row.index:
            continue
        value = row.get(field)
        if isinstance(value, pd.Timestamp):
            result[field] = value.strftime("%Y-%m-%d %H:%M:%S")
        elif pd.isna(value):
            result[field] = None
        elif hasattr(value, "item"):
            result[field] = value.item()
        else:
            result[field] = value
    return result


def make_station_geojson(stations: list[dict]) -> dict:
    features = []
    for station in stations:
        lon = _safe_float(station.get("lon"))
        lat = _safe_float(station.get("lat"))
        if lon is None or lat is None:
            continue
        props = dict(station)
        props.pop("lon", None)
        props.pop("lat", None)
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": props,
            }
        )
    return {"type": "FeatureCollection", "features": features}


def find_direct_impact_edges(
    graph,
    impact_stations: list[dict],
    *,
    station_buffer_km: float,
) -> tuple[dict[tuple, dict], dict[str, dict]]:
    """用 pkl 中的河流折线/节点线，筛选站点 30km 缓冲区内的直接影响边。"""
    direct_edges: dict[tuple, dict] = {}
    direct_rivers: dict[str, dict] = {}

    for u, v, key, attr in iter_graph_edges(graph):
        lines = edge_to_lines(u, v, attr)
        if not lines:
            continue

        matched_stations = []
        min_dist = math.inf
        for station in impact_stations:
            lon = _safe_float(station.get("lon"))
            lat = _safe_float(station.get("lat"))
            if lon is None or lat is None:
                continue
            dist = point_to_lines_distance_km(lon, lat, lines)
            min_dist = min(min_dist, dist)
            if dist <= station_buffer_km:
                item = dict(station)
                item["distance_to_river_km"] = round(dist, 3)
                matched_stations.append(item)

        if not matched_stations:
            continue

        river_name = get_edge_river_name(attr) or "未知"
        edge_id = make_edge_id(u, v, key)
        direct_edges[edge_id] = {
            "u": u,
            "v": v,
            "key": key,
            "attr": attr,
            "lines": lines,
            "river_name": river_name,
            "min_station_distance_km": round(min_dist, 3),
            "trigger_stations": matched_stations,
        }

        river_item = direct_rivers.setdefault(
            river_name,
            {
                "river_name": river_name,
                "edge_count": 0,
                "trigger_stations": [],
                "min_station_distance_km": math.inf,
            },
        )
        river_item["edge_count"] += 1
        river_item["min_station_distance_km"] = min(
            river_item["min_station_distance_km"],
            min_dist,
        )
        known = {str(s.get("station_id")) for s in river_item["trigger_stations"]}
        for station in matched_stations:
            station_id = str(station.get("station_id"))
            if station_id not in known:
                river_item["trigger_stations"].append(station)
                known.add(station_id)

    for item in direct_rivers.values():
        item["min_station_distance_km"] = round(float(item["min_station_distance_km"]), 3)

    return direct_edges, direct_rivers


def trace_downstream_edges(
    graph,
    direct_edges: dict[tuple, dict],
    *,
    downstream_km: float,
) -> dict[tuple, dict]:
    """从直接影响边的终点向下游追踪 downstream_km 公里。"""
    if downstream_km <= 0 or not direct_edges:
        return {}

    start_nodes = {item["v"] for item in direct_edges.values()}
    heap_counter = count()
    best_dist: dict[Any, float] = {node: 0.0 for node in start_nodes}
    heap = [(0.0, next(heap_counter), node) for node in start_nodes]
    downstream_edges: dict[tuple, dict] = {}

    while heap:
        curr_dist, _seq, curr_node = heapq.heappop(heap)
        if curr_dist > best_dist.get(curr_node, math.inf):
            continue
        if curr_dist > downstream_km:
            continue

        for u, v, key, attr in iter_out_edges(graph, curr_node):
            edge_id = make_edge_id(u, v, key)
            edge_len = get_edge_length_km(attr) or 0.0
            next_dist = curr_dist + edge_len
            if edge_id not in direct_edges:
                lines = edge_to_lines(u, v, attr)
                if lines:
                    downstream_edges.setdefault(
                        edge_id,
                        {
                            "u": u,
                            "v": v,
                            "key": key,
                            "attr": attr,
                            "lines": lines,
                            "river_name": get_edge_river_name(attr) or "未知",
                            "downstream_start_distance_km": round(curr_dist, 3),
                            "downstream_end_distance_km": round(next_dist, 3),
                        },
                    )

            if next_dist <= downstream_km and next_dist < best_dist.get(v, math.inf):
                best_dist[v] = next_dist
                heapq.heappush(heap, (next_dist, next(heap_counter), v))

    return downstream_edges


def build_local_river_geojson(
    direct_edges: dict[tuple, dict],
    downstream_edges: dict[tuple, dict],
) -> dict:
    features = []

    for edge_id, item in direct_edges.items():
        geometry = _line_to_geometry(item["lines"])
        if not geometry:
            continue
        attr = item.get("attr") or {}
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "impact_type": "direct_buffer",
                    "river_name": item.get("river_name"),
                    "edge_id": repr(edge_id),
                    "edge_key": item.get("key"),
                    "length_km": round(float(get_edge_length_km(attr) or 0.0), 3),
                    "min_station_distance_km": item.get("min_station_distance_km"),
                    "trigger_station_count": len(item.get("trigger_stations", [])),
                    "trigger_stations": item.get("trigger_stations", []),
                    "objectid": attr.get("objectid") or attr.get("OBJECTID") or attr.get("id"),
                    "source": "local_pkl",
                },
            }
        )

    for edge_id, item in downstream_edges.items():
        geometry = _line_to_geometry(item["lines"])
        if not geometry:
            continue
        attr = item.get("attr") or {}
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "impact_type": "downstream_50km",
                    "river_name": item.get("river_name"),
                    "edge_id": repr(edge_id),
                    "edge_key": item.get("key"),
                    "length_km": round(float(get_edge_length_km(attr) or 0.0), 3),
                    "downstream_start_distance_km": item.get("downstream_start_distance_km"),
                    "downstream_end_distance_km": item.get("downstream_end_distance_km"),
                    "objectid": attr.get("objectid") or attr.get("OBJECTID") or attr.get("id"),
                    "source": "local_pkl",
                },
            }
        )

    features.sort(
        key=lambda f: (
            0 if f["properties"]["impact_type"] == "direct_buffer" else 1,
            f["properties"].get("river_name") or "",
            f["properties"].get("edge_id") or "",
        )
    )
    return {"type": "FeatureCollection", "features": features}


def build_local_test_outputs(
    *,
    csv_path: str,
    graph_path: str,
    output_dir: str,
    rain_threshold_mm: float = 50.0,
    station_buffer_km: float = 30.0,
    downstream_km: float = 50.0,
    top_station_limit: int = 100,
) -> dict:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    station_24h_df = aggregate_5min_station_pre_to_24h(csv_path)
    impact_df = station_24h_df[
        (station_24h_df["rain_24h"] >= rain_threshold_mm)
        & station_24h_df["lon"].notna()
        & station_24h_df["lat"].notna()
    ].copy()

    impact_stations = [_station_record(row) for _, row in impact_df.iterrows()]
    top_stations = [
        _station_record(row)
        for _, row in station_24h_df.head(max(int(top_station_limit), 0)).iterrows()
    ]

    graph = get_graph(graph_path=graph_path, force_reload=True)

    direct_edges, direct_rivers = find_direct_impact_edges(
        graph,
        impact_stations,
        station_buffer_km=station_buffer_km,
    )
    downstream_edges = trace_downstream_edges(
        graph,
        direct_edges,
        downstream_km=downstream_km,
    )

    river_geojson = build_local_river_geojson(direct_edges, downstream_edges)
    station_geojson = make_station_geojson(impact_stations)

    river_geojson_path = output / "impact_rivers_local.geojson"
    station_geojson_path = output / "impact_stations_local.geojson"
    top_csv_path = output / "rain24h_top_stations.csv"
    summary_path = output / "summary.json"

    river_geojson_path.write_text(
        json.dumps(river_geojson, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    station_geojson_path.write_text(
        json.dumps(station_geojson, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    station_24h_df.head(max(int(top_station_limit), 0)).to_csv(
        top_csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    direct_river_list = sorted(direct_rivers.values(), key=lambda x: x["river_name"])
    downstream_rivers = sorted(
        {item["river_name"] for item in downstream_edges.values() if item.get("river_name")}
    )

    summary = {
        "status": "ok",
        "params": {
            "csv_path": csv_path,
            "graph_path": graph_path,
            "rain_threshold_mm": rain_threshold_mm,
            "station_buffer_km": station_buffer_km,
            "downstream_km": downstream_km,
        },
        "time_range": {
            "start_time": _json_default(station_24h_df["start_time"].min()),
            "end_time": _json_default(station_24h_df["end_time"].max()),
        },
        "station_summary": {
            "total_station_count": int(len(station_24h_df)),
            "impact_station_count": int(len(impact_stations)),
            "max_rain_24h": float(station_24h_df["rain_24h"].max() or 0.0),
        },
        "river_summary": {
            "direct_edge_count": len(direct_edges),
            "downstream_edge_count": len(downstream_edges),
            "geojson_feature_count": len(river_geojson["features"]),
            "direct_river_count": len(direct_river_list),
            "downstream_river_count": len(downstream_rivers),
        },
        "direct_rivers": direct_river_list,
        "downstream_rivers": downstream_rivers,
        "outputs": {
            "river_geojson": str(river_geojson_path),
            "station_geojson": str(station_geojson_path),
            "top_stations_csv": str(top_csv_path),
            "summary_json": str(summary_path),
        },
        "note": (
            "本地脚本使用 river_directed_v5.pkl 中的折线/节点坐标生成 GeoJSON；"
            "正式接口仍建议使用 PostGIS 真实河流表输出 ST_AsGeoJSON。"
        ),
    }

    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本地测试 24 小时降水影响河流 GeoJSON")
    parser.add_argument("--csv", default=DEFAULT_CSV_PATH, help="5 分钟降水 CSV 路径")
    parser.add_argument("--graph", default=DEFAULT_GRAPH_PATH, help="河网拓扑 pkl 路径")
    parser.add_argument(
        "--output-dir",
        default=str(Path(DEFAULT_CSV_PATH).with_suffix("")) + "_impact_output",
        help="输出目录",
    )
    parser.add_argument("--rain-threshold-mm", type=float, default=50.0, help="暴雨阈值，默认 50mm")
    parser.add_argument("--station-buffer-km", type=float, default=30.0, help="站点缓冲半径，默认 30km")
    parser.add_argument("--downstream-km", type=float, default=50.0, help="下游追踪距离，默认 50km")
    parser.add_argument("--top-station-limit", type=int, default=100, help="输出累计雨量前 N 个站点")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    summary = build_local_test_outputs(
        csv_path=args.csv,
        graph_path=args.graph,
        output_dir=args.output_dir,
        rain_threshold_mm=args.rain_threshold_mm,
        station_buffer_km=args.station_buffer_km,
        downstream_km=args.downstream_km,
        top_station_limit=args.top_station_limit,
    )

    print("本地测试完成")
    print(f"时间范围：{summary['time_range']['start_time']} -> {summary['time_range']['end_time']}")
    print(f"总站数：{summary['station_summary']['total_station_count']}")
    print(f"触发站数：{summary['station_summary']['impact_station_count']}")
    print(f"最大24小时雨量：{summary['station_summary']['max_rain_24h']} mm")
    print(f"直接影响河段：{summary['river_summary']['direct_edge_count']}")
    print(f"下游影响河段：{summary['river_summary']['downstream_edge_count']}")
    print(f"GeoJSON要素数：{summary['river_summary']['geojson_feature_count']}")
    print("输出文件：")
    for name, path in summary["outputs"].items():
        print(f"  - {name}: {path}")


if __name__ == "__main__":
    main()
