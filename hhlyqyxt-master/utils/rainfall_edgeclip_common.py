"""边级裁剪影响河流公共函数。"""
from __future__ import annotations

import heapq
import math
import re
from itertools import count
from typing import Any, Iterable

from rainfall_impact_geojson import (
    _edge_objectid_key,
    get_edge_length_km,
    get_edge_river_name,
    get_graph,
    iter_graph_edges,
    iter_out_edges,
)


def quote_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name or "")):
        raise ValueError(f"非法 SQL 标识符：{name!r}")
    return f'"{name}"'


def pick_first(columns: set[str], names: Iterable[str]) -> str | None:
    for name in names:
        if name in columns:
            return name
    return None


def river_name_expr(columns: set[str], alias: str = "r") -> str:
    fields = [c for c in ("river_name", "rivername", "src_name", "name") if c in columns]
    if not fields:
        return "'未知'"
    prefix = f"{quote_ident(alias)}."
    parts = [f"NULLIF(TRIM({prefix}{quote_ident(c)}::text), '')" for c in fields]
    return f"COALESCE({', '.join(parts)}, '未知')"


def validate_geom_column(columns: set[str], preferred: str) -> str:
    if preferred in columns:
        return preferred
    fallback = pick_first(columns, ("geom", "geometry", "wkb_geometry", "the_geom"))
    if fallback:
        return fallback
    raise ValueError(f"河流边表未找到几何字段：{preferred}")


def parse_node_xy(node: Any) -> tuple[float | None, float | None]:
    try:
        if isinstance(node, str) and "," in node:
            x, y = node.split(",", 1)
            return float(x), float(y)
        if isinstance(node, (tuple, list)) and len(node) >= 2:
            return float(node[0]), float(node[1])
    except Exception:
        pass
    return None, None


def graph_edges_by_objectid(graph_path: str | None) -> dict[str, list[dict]]:
    graph = get_graph(graph_path)
    result: dict[str, list[dict]] = {}
    for u, v, key, attr in iter_graph_edges(graph):
        objectid = _edge_objectid_key(attr)
        if not objectid:
            continue
        from_x, from_y = parse_node_xy(u)
        to_x, to_y = parse_node_xy(v)
        result.setdefault(objectid, []).append({
            "u": u,
            "v": v,
            "key": key,
            "attr": attr,
            "river_name": get_edge_river_name(attr),
            "from_x": from_x,
            "from_y": from_y,
            "to_x": to_x,
            "to_y": to_y,
        })
    return result


def direct_seed_nodes(direct_rows: list[dict], graph_path: str | None) -> tuple[dict[Any, float], set[str], set[str]]:
    edge_map = graph_edges_by_objectid(graph_path)
    node_dist: dict[Any, float] = {}
    direct_objectids: set[str] = set()
    direct_rivers: set[str] = set()
    for row in direct_rows:
        objectid = str(row.get("objectid") or "").strip()
        river_name = str(row.get("river_name") or "").strip()
        if objectid:
            direct_objectids.add(objectid)
        if river_name:
            direct_rivers.add(river_name)
        remain = float(row.get("remaining_after_buffer_km") or 0.0)
        for edge in edge_map.get(objectid, []):
            node = edge.get("v")
            if node is not None and remain < node_dist.get(node, math.inf):
                node_dist[node] = remain
    return node_dist, direct_objectids, direct_rivers


def collect_downstream_segments(
    start_node_dist: dict[Any, float],
    *,
    graph_path: str | None,
    direct_objectids: set[str],
    direct_rivers: set[str],
    downstream_km: float,
) -> tuple[dict[str, dict], list[dict]]:
    limit = float(downstream_km)
    if not start_node_dist or limit <= 0:
        return {}, []

    graph = get_graph(graph_path)
    heap_counter = count()
    best_dist = dict(start_node_dist)
    heap = [(float(dist), next(heap_counter), node) for node, dist in start_node_dist.items()]
    heapq.heapify(heap)
    river_map: dict[str, dict] = {}
    segment_map: dict[tuple[str, str], dict] = {}

    while heap:
        curr_dist, _seq, curr_node = heapq.heappop(heap)
        if curr_dist > best_dist.get(curr_node, math.inf) or curr_dist >= limit:
            continue

        for u, next_node, _key, attr in iter_out_edges(graph, curr_node):
            objectid = _edge_objectid_key(attr)
            river_name = get_edge_river_name(attr)
            edge_len = max(float(get_edge_length_km(attr, attr_name="length_km") or 0.0), 0.0)
            next_dist = curr_dist + edge_len
            keep_km = max(min(limit - curr_dist, edge_len), 0.0) if edge_len > 0 else 0.0
            clip_fraction = 1.0 if edge_len <= 0 else max(min(keep_km / edge_len, 1.0), 0.0)
            is_direct = bool(objectid and objectid in direct_objectids) or bool(river_name and river_name in direct_rivers)

            if objectid and river_name and not is_direct and clip_fraction > 0:
                from_x, from_y = parse_node_xy(u)
                to_x, to_y = parse_node_xy(next_node)
                key = (str(objectid), river_name)
                old = segment_map.get(key)
                if old is None or curr_dist < old["min_distance_km"] or clip_fraction > old["clip_fraction"]:
                    segment_map[key] = {
                        "objectid": str(objectid),
                        "river_name": river_name,
                        "min_distance_km": round(float(curr_dist), 3),
                        "end_distance_km": round(float(curr_dist + keep_km), 3),
                        "clip_fraction": round(float(clip_fraction), 8),
                        "from_x": from_x,
                        "from_y": from_y,
                        "to_x": to_x,
                        "to_y": to_y,
                    }
                item = river_map.setdefault(river_name, {"river_name": river_name, "min_distance_km": math.inf})
                if curr_dist < item["min_distance_km"]:
                    item["min_distance_km"] = curr_dist

            if next_dist <= limit and next_dist < best_dist.get(next_node, math.inf):
                best_dist[next_node] = next_dist
                heapq.heappush(heap, (next_dist, next(heap_counter), next_node))

    for item in river_map.values():
        item["min_distance_km"] = round(float(item["min_distance_km"]), 3)
    segments = sorted(segment_map.values(), key=lambda x: (x["min_distance_km"], x["river_name"], x["objectid"]))
    return river_map, segments
