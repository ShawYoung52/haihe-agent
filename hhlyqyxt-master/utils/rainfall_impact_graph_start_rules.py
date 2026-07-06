"""暴雨影响河流下游追踪起点规则。

只允许从 PostGIS 已识别出的直接影响真实河段对应的 pkl 河网边开始追踪，
避免仅凭暴雨站 30km 范围内的 pkl 边扩大下游影响范围。
"""
from __future__ import annotations

from typing import Any


def install_strict_direct_part_start_rule() -> None:
    """收紧暴雨影响河流下游追踪起点。"""
    try:
        from . import rainfall_impact_geojson as core
    except Exception:  # pragma: no cover
        import rainfall_impact_geojson as core  # type: ignore

    def _find_direct_graph_starts(
        stations: list[dict],
        direct_rows: list[dict],
        graph_path: Any,
        buffer_km: float,
        direct_match_km: float,
    ) -> tuple[dict[Any, float], set[str]]:
        graph = core.get_graph(graph_path)
        direct_refs = core._direct_refs(direct_rows)
        if not direct_refs:
            return {}, set()

        starts: dict[Any, float] = {}
        direct_keys: set[str] = set()
        for u, v, key, attr in core.iter_graph_edges(graph):
            p1, p2 = core._edge_points(u, v)
            if p1 is None or p2 is None:
                continue
            if not core._edge_matches_direct_part(attr, p1, p2, direct_refs, direct_match_km):
                continue
            edge_key = core._edge_key(u, v, key, attr)
            direct_keys.add(edge_key)
            starts[v] = 0.0
        return starts, direct_keys

    core._find_direct_graph_starts = _find_direct_graph_starts
