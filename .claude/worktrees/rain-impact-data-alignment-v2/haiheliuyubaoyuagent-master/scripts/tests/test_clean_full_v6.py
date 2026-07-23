"""Unit tests for clean_full_v6 geometry helpers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from shapely.geometry import LineString, MultiLineString
from shapely import to_wkb

import clean_full_v6
from _river_common import DEFAULT_LUAN_OBJECTIDS
from clean_full_v6 import (
    _component_to_linestring,
    _extract_segments,
    _process_objectid_groups,
    _split_connected_components,
)


class TestExtractSegments:
    def test_linestring(self):
        line = LineString([(0, 0), (1, 1)])
        wkb = to_wkb(line)
        segments = _extract_segments(wkb)
        assert len(segments) == 1
        assert segments[0].equals_exact(line, 1e-9)

    def test_multilinestring(self):
        ml = MultiLineString([[(0, 0), (1, 1)], [(2, 2), (3, 3)]])
        wkb = to_wkb(ml)
        segments = _extract_segments(wkb)
        assert len(segments) == 2

    def test_empty_wkb(self):
        assert _extract_segments(b"") == []
        assert _extract_segments(None) == []

    def test_memoryview(self):
        line = LineString([(0, 0), (1, 1)])
        wkb = to_wkb(line)
        segments = _extract_segments(memoryview(wkb))
        assert len(segments) == 1


class TestSplitConnectedComponents:
    def test_single_linestring(self):
        segments = [LineString([(0, 0), (1, 1)])]
        components = _split_connected_components(segments)
        assert len(components) == 1

    def test_connected_lines(self):
        # Two lines that share an endpoint form one component.
        segments = [
            LineString([(0, 0), (1, 1)]),
            LineString([(1, 1), (2, 0)]),
        ]
        components = _split_connected_components(segments)
        assert len(components) == 1

    def test_disconnected_lines(self):
        segments = [
            LineString([(0, 0), (1, 1)]),
            LineString([(10, 10), (11, 11)]),
        ]
        components = _split_connected_components(segments)
        assert len(components) == 2

    def test_touching_within_tolerance(self):
        # Endpoints are within TOLERANCE_DEG (1e-8) but not exact.
        segments = [
            LineString([(0, 0), (1, 1)]),
            LineString([(1 + 5e-9, 1 + 5e-9), (2, 0)]),
        ]
        components = _split_connected_components(segments)
        assert len(components) == 1

    def test_empty_input(self):
        assert _split_connected_components([]) == []

    def test_empty_and_short_segments_are_skipped(self):
        # Empty/short segments used to cause an IndexError because
        # segment_nodes was indexed by original segment index.
        segments = [
            LineString(),
            LineString([(0, 0), (1, 1)]),
            LineString([(10, 10), (11, 11)]),
        ]
        components = _split_connected_components(segments)
        assert len(components) == 2


class TestProcessObjectidGroups:
    def test_single_group_keeps_original_id(self):
        groups = {
            "River A": [LineString([(0, 0), (1, 1)])],
        }
        cleaned, remap, kept, created, next_id = _process_objectid_groups(
            groups, 42, 1000
        )
        assert len(cleaned) == 1
        assert cleaned[0][0] == 42
        assert kept is True
        assert created == 0
        assert next_id == 1000
        assert remap[0]["reason"] == "kept_main"

    def test_longest_component_keeps_original_id(self):
        groups = {
            "main": [LineString([(0, 0), (10, 0)])],
            "branch": [LineString([(20, 0), (21, 0)])],
        }
        cleaned, remap, kept, created, next_id = _process_objectid_groups(
            groups, 7, 500
        )
        assert len(cleaned) == 2
        assert kept is True
        assert created == 1
        assert next_id == 501
        # The longest (main) keeps id 7; the shorter gets 500.
        by_new_id = {row[0]: row for row in cleaned}
        assert by_new_id[7][1] == "main"
        assert by_new_id[500][1] == "branch"
        reasons = {entry["new_objectid"]: entry["reason"] for entry in remap}
        assert reasons[7] == "kept_main"
        assert reasons[500] == "split_component"

    def test_combining_connected_rows(self):
        # Two rows with the same name that connect end-to-end should be merged
        # into a single component and keep the original id.
        groups = {
            "River B": [
                LineString([(0, 0), (1, 0)]),
                LineString([(1, 0), (2, 0)]),
            ],
        }
        cleaned, _remap, kept, created, next_id = _process_objectid_groups(
            groups, 10, 100
        )
        assert len(cleaned) == 1
        assert cleaned[0][0] == 10
        assert kept is True
        assert created == 0

    def test_splitting_disconnected_rows_same_name(self):
        # Two disconnected rows with the same name produce two components.
        groups = {
            "River C": [
                LineString([(0, 0), (1, 0)]),
                LineString([(10, 0), (11, 0)]),
            ],
        }
        cleaned, _remap, kept, created, next_id = _process_objectid_groups(
            groups, 20, 200
        )
        assert len(cleaned) == 2
        assert kept is True
        assert created == 1
        assert next_id == 201
        new_ids = {row[0] for row in cleaned}
        assert 20 in new_ids
        assert 200 in new_ids

    def test_empty_groups(self):
        cleaned, remap, kept, created, next_id = _process_objectid_groups(
            {}, 1, 100
        )
        assert cleaned == []
        assert remap == []
        assert kept is False
        assert created == 0
        assert next_id == 100


class TestComponentToLinestring:
    def test_linestring_component(self):
        line = LineString([(0, 0), (1, 1)])
        result = _component_to_linestring(MultiLineString([line]), 1)
        assert result.equals_exact(line, 1e-9)

    def test_empty_component(self):
        assert _component_to_linestring(MultiLineString(), 1) is None

    def test_branched_component_returns_linestring(self):
        # A branched MultiLineString: two paths from the same point.
        # ``linemerge`` may combine overlapping branches; the function should
        # still return a usable LineString rather than None.
        long_branch = LineString([(0, 0), (10, 0)])
        short_branch = LineString([(0, 0), (2, 0)])
        component = MultiLineString([long_branch, short_branch])
        result = _component_to_linestring(component, 1)
        assert result is not None
        assert result.geom_type == "LineString"
        assert result.length > 0


class TestIsLuanPropagation:
    def test_is_luan_true_propagates(self):
        groups = {"River": [LineString([(0, 0), (1, 1)])]}
        cleaned, remap, _kept, _created, _next = _process_objectid_groups(
            groups, 100, 1000, is_luan=True
        )
        assert len(cleaned) == 1
        assert cleaned[0][4] is True
        assert remap[0]["is_luan"] is True

    def test_is_luan_false_propagates(self):
        groups = {"River": [LineString([(0, 0), (1, 1)])]}
        cleaned, remap, _kept, _created, _next = _process_objectid_groups(
            groups, 100, 1000, is_luan=False
        )
        assert cleaned[0][4] is False
        assert remap[0]["is_luan"] is False

    def test_is_luan_none_uses_default_luan_objectids(self):
        # Use an objectid inside the default Luan range with no explicit flag.
        luan_objectid = next(iter(DEFAULT_LUAN_OBJECTIDS))
        groups = {"River": [LineString([(0, 0), (1, 1)])]}
        cleaned, remap, _kept, _created, _next = _process_objectid_groups(
            groups, luan_objectid, 1000, is_luan=None
        )
        assert cleaned[0][4] is True
        assert remap[0]["is_luan"] is True

    def test_is_luan_none_non_luan_objectid(self):
        # Use an objectid outside the default Luan range with no explicit flag.
        non_luan_objectid = (max(DEFAULT_LUAN_OBJECTIDS) + 1)
        groups = {"River": [LineString([(0, 0), (1, 1)])]}
        cleaned, remap, _kept, _created, _next = _process_objectid_groups(
            groups, non_luan_objectid, 1000, is_luan=None
        )
        assert cleaned[0][4] is False
        assert remap[0]["is_luan"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
