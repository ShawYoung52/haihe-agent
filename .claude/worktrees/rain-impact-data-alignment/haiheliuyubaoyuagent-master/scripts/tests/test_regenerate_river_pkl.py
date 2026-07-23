"""Unit tests for regenerate_river_pkl graph-building helpers."""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from shapely.geometry import LineString, MultiPoint

import regenerate_river_pkl
from regenerate_river_pkl import (
    COORD_PRECISION,
    _build_directed_graph,
    _build_undirected_graph,
    _edge_length_km,
    _node_key,
    _split_at_nodes,
)


class TestNodeKey:
    def test_snaps_to_precision_grid(self):
        coord = (1.234567890123, 9.876543210987)
        key = _node_key(coord)
        assert key[0] == pytest.approx(round(coord[0] / COORD_PRECISION) * COORD_PRECISION)
        assert key[1] == pytest.approx(round(coord[1] / COORD_PRECISION) * COORD_PRECISION)

    def test_identical_coordinates_same_key(self):
        assert _node_key((1.0, 2.0)) == _node_key((1.0, 2.0))

    def test_coordinates_within_precision_same_key(self):
        # Two coordinates within half the precision should round to the same key.
        assert _node_key((1.0, 2.0)) == _node_key((1.0 + COORD_PRECISION / 4, 2.0))


class TestEdgeLengthKm:
    def test_length_equator_one_degree(self):
        line = LineString([(0, 0), (1, 0)])
        assert _edge_length_km(line) == pytest.approx(111.32, rel=1e-3)

    def test_length_north_south_one_degree(self):
        line = LineString([(0, 0), (0, 1)])
        assert _edge_length_km(line) == pytest.approx(111.32, rel=1e-3)

    def test_length_45_degrees_latitude(self):
        # At 45N one degree of longitude is shorter than one degree of latitude.
        line = LineString([(0, 45), (1, 45)])
        expected = 111.32 * math.cos(math.radians(45))
        assert _edge_length_km(line) == pytest.approx(expected, rel=1e-3)


class TestSplitAtNodes:
    def test_splits_line_at_internal_node(self):
        # Line from (0,0) to (2,0) with a node at (1,0) should split into two.
        lines = [(1, "river", False, LineString([(0, 0), (2, 0)]))]
        nodes = {(0, 0), (1, 0), (2, 0)}
        split = _split_at_nodes(lines, nodes)
        assert len(split) == 2
        new_coords = {tuple(part.coords) for _, _, _, part in split}
        assert {((0, 0), (1, 0)), ((1, 0), (2, 0))} == new_coords

    def test_returns_empty_for_empty_nodes(self):
        lines = [(1, "river", False, LineString([(0, 0), (1, 0)]))]
        assert _split_at_nodes(lines, set()) == []

    def test_keeps_original_when_snap_fails(self):
        # Empty/short lines should be skipped without crashing.
        lines = [
            (1, "river", False, LineString()),
            (2, "river", False, LineString([(0, 0), (1, 0)])),
        ]
        nodes = {(0, 0), (1, 0)}
        split = _split_at_nodes(lines, nodes)
        assert len(split) == 1
        assert split[0][0] == 2


class TestBuildUndirectedGraph:
    def test_simple_line_becomes_edge(self):
        lines = [(42, "river", False, LineString([(0, 0), (1, 0)]))]
        graph = _build_undirected_graph(_split_at_nodes(lines, _collect_nodes(lines)))
        assert graph.number_of_nodes() == 2
        assert graph.number_of_edges() == 1
        edge = graph.edges(data=True)
        attrs = next(iter(edge))
        assert attrs[2]["objectid"] == "42"
        assert attrs[2]["src_name"] == "river"
        assert attrs[2]["is_luan"] is False

    def test_closed_loop_is_ignored(self):
        # A closed ring has identical start/end endpoints, so _collect_nodes()
        # only sees one node and the loop is not split. _build_undirected_graph()
        # ignores the resulting self-loop, leaving the graph with no edges.
        lines = [(7, "loop", False, LineString([(0, 0), (1, 0), (1, 1), (0, 0)]))]
        graph = _build_undirected_graph(_split_at_nodes(lines, _collect_nodes(lines)))
        # No edges remain because the closed ring collapses to a single-node self-loop.
        assert graph.number_of_edges() == 0


class TestBuildDirectedGraph:
    def test_directs_away_from_easternmost_outlet(self):
        # Component: (0,0) -- (1,0) -- (2,0). Easternmost outlet is (2,0).
        # BFS starts at the outlet, so edges are directed away from it:
        # (2,0) -> (1,0) -> (0,0).
        lines = [
            (1, "river", False, LineString([(0, 0), (1, 0)])),
            (1, "river", False, LineString([(1, 0), (2, 0)])),
        ]
        nodes = _collect_nodes(lines)
        split = _split_at_nodes(lines, nodes)
        undirected = _build_undirected_graph(split)
        directed = _build_directed_graph(undirected)

        assert directed.number_of_edges() == 2
        assert directed.has_edge(_node_key((2, 0)), _node_key((1, 0)))
        assert directed.has_edge(_node_key((1, 0)), _node_key((0, 0)))

    def test_preserves_edge_attributes(self):
        lines = [(99, "Luan", True, LineString([(0, 0), (1, 0)]))]
        nodes = _collect_nodes(lines)
        split = _split_at_nodes(lines, nodes)
        undirected = _build_undirected_graph(split)
        directed = _build_directed_graph(undirected)

        edge = next(iter(directed.edges(data=True)))
        assert edge[2]["OBJECTID"] == 99
        assert edge[2]["src_name"] == "Luan"
        assert edge[2]["is_luan"] is True
        assert edge[2]["length"] > 0
        assert edge[2]["len_m"] == pytest.approx(edge[2]["length"] * 1000)
        assert edge[2]["len_km"] == pytest.approx(edge[2]["length"])


# Re-export helper to avoid importing it twice.
def _collect_nodes(lines):
    return regenerate_river_pkl._collect_nodes(lines)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])