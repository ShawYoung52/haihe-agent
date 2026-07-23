#!/usr/bin/env python3
"""Rebuild river_directed_v6.pkl from a cleaned full_v6 river table.

Connects to PostgreSQL using environment variables, reads the table named by
``RIVER_TABLE_FULL`` (defaulting to the project constant), and writes a new
serialized ``networkx.DiGraph`` pickle to ``RIVER_GRAPH_PATH`` (defaulting to
the directory of config.ini's ``[paths] graph`` entry plus the constant
``DIRECTED_GRAPH_FILENAME``).

The directed graph is built by:

1. Fetching all rows as ``(objectid, src_name, is_luan, ST_AsBinary(geom))``.
2. Extracting line endpoints and snapping them to an 8-digit precision grid.
3. Splitting every line at all graph nodes using Shapely ``snap``/``split``.
4. Building an undirected graph from the split segments.
5. For each connected component, choosing the easternmost leaf node as the
   outlet and running BFS to direct edges away from the outlet (downstream).
6. Writing a ``networkx.DiGraph`` whose edges carry the same attributes as the
   original production pickle, including ``OBJECTID``, ``is_luan``,
   endpoint coordinates, and geometry length in kilometres.
"""

from __future__ import annotations

import configparser
import math
import os
import pickle
import sys
from collections import defaultdict, deque
from pathlib import Path

import networkx as nx
import psycopg2
from psycopg2 import sql
from shapely import from_wkb
from shapely.errors import ShapelyError, TopologicalError
from shapely.geometry import LineString, MultiLineString, MultiPoint
from shapely.ops import snap, split

from _river_common import (
    DIRECTED_GRAPH_FILENAME,
    DEFAULT_LUAN_OBJECTIDS,
    RIVER_TABLE_FULL,
    CommonError,
    _has_column,
    _require_env_vars,
    _scrub_message,
    _validate_identifier,
)

# 8-digit degree precision used for graph node keys.
COORD_PRECISION = 1e-8
# Tolerance passed to shapely.ops.snap so rounded nodes split lines reliably.
SNAP_TOLERANCE = 1e-7
# Approximate kilometres per degree of latitude / longitude at the equator.
KM_PER_DEG = 111.32


class BuildError(CommonError):
    """Raised for known, non-recoverable build failures."""


def _config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "haihe-weather-analyzer-mcp" / "config.ini"


def _resolve_graph_path() -> Path:
    """Return the output pickle path.

    Prefers ``RIVER_GRAPH_PATH``. Otherwise derives the path from
    ``config.ini``'s ``[paths] graph`` entry by replacing the filename with
    ``DIRECTED_GRAPH_FILENAME``.
    """
    env_path = os.environ.get("RIVER_GRAPH_PATH")
    if env_path:
        return Path(env_path)

    config_file = _config_path()
    if config_file.exists():
        config = configparser.ConfigParser()
        config.read(config_file, encoding="utf-8")
        if config.has_option("paths", "graph"):
            config_graph = config.get("paths", "graph").strip()
            if config_graph:
                return Path(config_graph).parent / DIRECTED_GRAPH_FILENAME

    raise BuildError(
        "RIVER_GRAPH_PATH is not set and no [paths] graph value found in config.ini"
    )


def _edge_length_km(line: LineString) -> float:
    """Return the approximate length of a line in kilometres.

    The geometry is assumed to be in EPSG:4326.  Segment lengths are estimated
    using the mean-latitude conversion so that longitude degrees are scaled by
    ``cos(lat)``.
    """
    total = 0.0
    coords = list(line.coords)
    for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
        lat = math.radians((y1 + y2) / 2.0)
        dx = (x2 - x1) * KM_PER_DEG * math.cos(lat)
        dy = (y2 - y1) * KM_PER_DEG
        total += math.hypot(dx, dy)
    return total


def _node_key(coord: tuple[float, float]) -> tuple[float, float]:
    """Snap a coordinate to the 8-digit precision grid."""
    return (round(float(coord[0]) / COORD_PRECISION) * COORD_PRECISION,
            round(float(coord[1]) / COORD_PRECISION) * COORD_PRECISION)


def _extract_linestrings(geom_wkb: bytes | memoryview) -> list[LineString]:
    """Return individual LineString segments from a WKB geometry."""
    if not geom_wkb:
        return []
    if isinstance(geom_wkb, memoryview):
        geom_wkb = bytes(geom_wkb)

    geom = from_wkb(geom_wkb)
    if geom is None or geom.is_empty:
        return []

    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type == "MultiLineString":
        return [LineString(part.coords) for part in geom.geoms]

    return []


def _collect_nodes(lines: list[tuple[int, str, bool, LineString]]) -> set[tuple[float, float]]:
    """Collect all endpoints as 8-digit precision node keys."""
    nodes: set[tuple[float, float]] = set()
    for _objectid, _name, _is_luan, line in lines:
        if line.is_empty or len(line.coords) < 2:
            continue
        coords = list(line.coords)
        nodes.add(_node_key(coords[0]))
        nodes.add(_node_key(coords[-1]))
    return nodes


def _split_at_nodes(
    lines: list[tuple[int, str, bool, LineString]],
    nodes: set[tuple[float, float]],
) -> list[tuple[int, str, bool, LineString]]:
    """Split every line at all graph nodes using shapely snap/split."""
    if not nodes:
        return []

    multipoint = MultiPoint([node for node in nodes])
    split_lines: list[tuple[int, str, bool, LineString]] = []

    for objectid, name, is_luan, line in lines:
        if line.is_empty or len(line.coords) < 2:
            continue

        try:
            snapped = snap(line, multipoint, SNAP_TOLERANCE)
            parts = split(snapped, multipoint)
        except (ShapelyError, ValueError, TopologicalError) as exc:
            # If snapping/splitting fails, keep the original line.
            print(
                f"Warning: failed to split objectid {objectid}: {exc}; "
                "keeping the original line",
                file=sys.stderr,
            )
            split_lines.append((objectid, name, is_luan, line))
            continue

        for part in _extract_parts(parts):
            if part.is_empty or len(part.coords) < 2:
                continue
            split_lines.append((objectid, name, is_luan, part))

    return split_lines


def _extract_parts(geom) -> list[LineString]:
    """Extract LineString parts from a GeometryCollection/MultiLineString/LineString."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type in ("MultiLineString", "GeometryCollection"):
        result: list[LineString] = []
        for part in geom.geoms:
            if part.geom_type == "LineString":
                result.append(part)
            elif part.geom_type == "MultiLineString":
                result.extend(LineString(sub.coords) for sub in part.geoms)
        return result
    return []


def _build_undirected_graph(
    split_lines: list[tuple[int, str, bool, LineString]],
) -> nx.Graph:
    """Build an undirected graph from split line segments."""
    graph = nx.Graph()
    for objectid, name, is_luan, line in split_lines:
        coords = list(line.coords)
        start = _node_key(coords[0])
        end = _node_key(coords[-1])
        if start == end:
            # Ignore zero-length or closed loops that collapse to a single node.
            continue

        graph.add_edge(
            start,
            end,
            objectid=str(objectid),
            src_name=str(name) if name else "",
            is_luan=bool(is_luan),
            geom=line,
        )

    return graph


def _find_component_outlet(component: nx.Graph) -> tuple[float, float]:
    """Return the easternmost leaf node, or the easternmost node if no leaves."""
    nodes = list(component.nodes())
    leaves = [n for n in nodes if component.degree(n) <= 1]
    candidates = leaves if leaves else nodes
    # easternmost = max x, then max y as a tie-breaker.
    return max(candidates, key=lambda n: (n[0], n[1]))


def _direct_component(
    component: nx.Graph,
    outlet: tuple[float, float],
    digraph: nx.DiGraph,
) -> None:
    """BFS from outlet and add directed edges away from it into ``digraph``."""
    dist: dict[tuple[float, float], int] = {outlet: 0}
    queue: deque[tuple[float, float]] = deque([outlet])

    while queue:
        current = queue.popleft()
        current_dist = dist[current]
        for neighbor in component.neighbors(current):
            if neighbor not in dist:
                dist[neighbor] = current_dist + 1
                queue.append(neighbor)

    for u, v, attr in component.edges(data=True):
        du = dist.get(u)
        dv = dist.get(v)
        if du is None or dv is None:
            # Should not happen, but skip if it does.
            continue

        if du < dv:
            source, target = u, v
        elif du > dv:
            source, target = v, u
        else:
            # Same BFS level (cycle). Use a deterministic coordinate tie-breaker.
            source, target = (u, v) if u < v else (v, u)

        geom = attr["geom"]
        coords = list(geom.coords)
        # Ensure geometry is oriented from source to target.
        source_key = _node_key(coords[0])
        target_key = _node_key(coords[-1])
        if source_key != source or target_key != target:
            geom = LineString(coords[::-1])

        src_name = str(attr.get("src_name") or "")
        objectid = int(attr["objectid"])
        is_luan = bool(attr.get("is_luan"))
        length_km = _edge_length_km(geom)

        digraph.add_edge(
            source,
            target,
            OBJECTID=objectid,
            objectid=str(objectid),
            name=src_name,
            src_name=src_name,
            river_name=src_name,
            is_luan=is_luan,
            geom=geom,
            wkt=geom.wkt,
            length=float(length_km),
            len_m=float(length_km * 1000.0),
            len_km=float(length_km),
            from_x=float(source[0]),
            from_y=float(source[1]),
            to_x=float(target[0]),
            to_y=float(target[1]),
        )


def _build_directed_graph(undirected: nx.Graph) -> nx.DiGraph:
    """Direct every connected component away from its easternmost outlet."""
    digraph = nx.DiGraph()
    for component_nodes in nx.connected_components(undirected):
        component = undirected.subgraph(component_nodes).copy()
        outlet = _find_component_outlet(component)
        _direct_component(component, outlet, digraph)
    return digraph


def _fetch_rows(conn, table: str, has_is_luan: bool):
    """Yield (objectid, src_name, is_luan, geom_wkb) rows from the source table."""
    _validate_identifier(table)

    if has_is_luan:
        query = sql.SQL("""
            SELECT objectid, src_name, is_luan, ST_AsBinary(geom) AS geom_wkb
            FROM {table}
            WHERE objectid IS NOT NULL
              AND geom IS NOT NULL
              AND NOT ST_IsEmpty(geom)
            ORDER BY objectid
        """).format(table=sql.Identifier(table))
    else:
        query = sql.SQL("""
            SELECT objectid, src_name, NULL::boolean AS is_luan, ST_AsBinary(geom) AS geom_wkb
            FROM {table}
            WHERE objectid IS NOT NULL
              AND geom IS NOT NULL
              AND NOT ST_IsEmpty(geom)
            ORDER BY objectid
        """).format(table=sql.Identifier(table))

    with conn.cursor() as cur:
        cur.execute(query)
        yield from cur


def _ensure_output_dir(path: Path) -> None:
    """Create the parent directory for the output pickle if necessary."""
    path.parent.mkdir(parents=True, exist_ok=True)


def _backup_existing_pickle(path: Path) -> Path | None:
    """Back up an existing pickle before overwriting it.

    Returns the backup path, or None if no file existed.
    """
    if not path.exists():
        return None
    backup_path = path.with_suffix(".pkl.bak")
    # Rotate any existing backup so the latest one is always .pkl.bak.
    if backup_path.exists():
        backup_path.unlink()
    path.replace(backup_path)
    return backup_path


def main() -> int:
    source_table = os.environ.get("RIVER_TABLE_FULL", RIVER_TABLE_FULL)
    _validate_identifier(source_table)

    output_path = _resolve_graph_path()
    _ensure_output_dir(output_path)

    try:
        conn_kwargs = _require_env_vars()

        print(f"Rebuilding river pickle")
        print(f"  Source table: {source_table}")
        print(f"  Output path:  {output_path}")

        conn = psycopg2.connect(**conn_kwargs)
        try:
            has_is_luan = _has_column(conn, source_table, "is_luan")
            print(f"  Source has is_luan: {has_is_luan}")

            lines: list[tuple[int, str, bool, LineString]] = []
            for objectid, src_name, is_luan, geom_wkb in _fetch_rows(
                conn, source_table, has_is_luan
            ):
                derived_is_luan = (
                    bool(is_luan)
                    if is_luan is not None
                    else (int(objectid) in DEFAULT_LUAN_OBJECTIDS)
                )
                for line in _extract_linestrings(geom_wkb):
                    lines.append((int(objectid), str(src_name or ""), derived_is_luan, line))

            print(f"  Input line candidates: {len(lines):,}")

            nodes = _collect_nodes(lines)
            print(f"  Graph nodes (endpoints): {len(nodes):,}")

            split_lines = _split_at_nodes(lines, nodes)
            print(f"  Split line segments: {len(split_lines):,}")

            undirected = _build_undirected_graph(split_lines)
            print(f"  Undirected graph: {undirected.number_of_nodes():,} nodes, {undirected.number_of_edges():,} edges")

            directed = _build_directed_graph(undirected)
            print(f"  Directed graph:   {directed.number_of_nodes():,} nodes, {directed.number_of_edges():,} edges")

            backup_path = _backup_existing_pickle(output_path)
            if backup_path:
                print(f"  Backed up existing pickle to: {backup_path}")

            with output_path.open("wb") as f:
                pickle.dump(directed, f)

            print(f"  Saved: {output_path}")
        finally:
            conn.close()

    except psycopg2.Error as exc:
        print(
            f"Error: database operation failed: {_scrub_message(str(exc))}",
            file=sys.stderr,
        )
        return 1
    except CommonError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print("Rebuild completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
