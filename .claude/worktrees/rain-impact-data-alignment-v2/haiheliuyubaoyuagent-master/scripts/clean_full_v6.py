#!/usr/bin/env python3
"""Clean haihe_river_directed_full_v6 so each objectid maps to one connected segment.

Connects to PostgreSQL using environment variables, reads the table named by
``RIVER_TABLE_FULL`` (defaulting to the project constant), and writes a new
staging table named by ``RIVER_TABLE_FULL_OUTPUT`` (defaulting to
``{input}_cleaned``).

Cleaning steps:

1. Group input rows by ``(objectid, src_name)``.
2. Combine all line geometries within each ``(objectid, src_name)`` group
   (using ``shapely.ops.unary_union``) so connected rows are treated as one
   shape before component analysis.
3. Split the combined geometry into connected components using a coordinate
   tolerance of ``1e-8`` degrees.
4. Each connected component becomes a candidate segment. The longest candidate
   for an objectid keeps the original ``objectid``; all other candidates receive
   new sequential objectids.
5. Write all cleaned segments to the output table with columns
   ``objectid``, ``src_name``, ``is_luan``, ``geom``, and ``original_objectid``.
6. Write a JSON remap log named ``{output_table}_remap.json``.

Rows are streamed from PostgreSQL ordered by ``(objectid, src_name)`` and
processed one original ``objectid`` at a time, so memory usage stays bounded
by the largest single-objectid group rather than the full table.

The original table is never modified.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values
from shapely import from_wkb, to_wkb
from shapely.errors import ShapelyError, TopologicalError
from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge, unary_union

from _river_common import (
    DEFAULT_INPUT_TABLE,
    DEFAULT_LUAN_OBJECTIDS,
    CommonError,
    _has_column,
    _require_env_vars,
    _scrub_message,
    _validate_identifier,
)

TOLERANCE_DEG = 1e-8


class CleanError(CommonError):
    """Raised for known, non-recoverable cleaning failures."""


def _discover_geometry_column(conn, table: str, schema: str = "public") -> tuple[str, int]:
    """Discover the name and SRID of the geometry column for the given table."""
    _validate_identifier(table)
    _validate_identifier(schema)

    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                    SELECT f_geometry_column, srid
                    FROM geometry_columns
                    WHERE f_table_schema = %s AND f_table_name = %s
                    LIMIT 1
                """,
                [schema, table],
            )
            row = cur.fetchone()
            if row:
                return row[0], int(row[1])
        except psycopg2.Error:
            conn.rollback()

        cur.execute(
            """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                  AND data_type = 'USER-DEFINED' AND udt_name = 'geometry'
                LIMIT 1
            """,
            [schema, table],
        )
        row = cur.fetchone()
        if row:
            geom_col = row[0]
            try:
                cur.execute(
                    sql.SQL("SELECT ST_SRID({geom}) FROM {table} LIMIT 1").format(
                        geom=sql.Identifier(geom_col),
                        table=sql.Identifier(table),
                    )
                )
                srid_row = cur.fetchone()
                srid = int(srid_row[0]) if srid_row and srid_row[0] is not None else 4326
            except psycopg2.Error:
                conn.rollback()
                srid = 4326
            return geom_col, srid

    raise CleanError(f"could not find a geometry column for table {schema}.{table}")


def _snap(value: float) -> int:
    """Snap a coordinate to an integer grid with TOLERANCE_DEG spacing."""
    return round(value / TOLERANCE_DEG)


def _extract_segments(geom_wkb: bytes | memoryview) -> list[LineString]:
    """Return individual LineString segments from a WKB geometry."""
    if not geom_wkb:
        return []

    if isinstance(geom_wkb, memoryview):
        geom_wkb = bytes(geom_wkb)

    geom = from_wkb(geom_wkb)
    if geom is None:
        return []

    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type == "MultiLineString":
        return [LineString(part.coords) for part in geom.geoms]

    return []


def _split_connected_components(segments: list[LineString]) -> list[MultiLineString]:
    """Split segments into connected components using TOLERANCE_DEG endpoint snapping.

    Returns a list of MultiLineString geometries, one per connected component.
    """
    segments = [seg for seg in segments if not seg.is_empty and len(seg.coords) >= 2]
    if not segments:
        return []

    # Node key: snapped endpoint coordinates.
    node_to_segments: dict[tuple[int, int], list[int]] = defaultdict(list)
    segment_nodes: list[list[tuple[int, int]]] = []

    for idx, seg in enumerate(segments):
        coords = list(seg.coords)
        start = (_snap(coords[0][0]), _snap(coords[0][1]))
        end = (_snap(coords[-1][0]), _snap(coords[-1][1]))
        segment_nodes.append([start, end])
        node_to_segments[start].append(idx)
        node_to_segments[end].append(idx)

    visited = [False] * len(segments)
    components: list[MultiLineString] = []

    for start_idx in range(len(segments)):
        if visited[start_idx]:
            continue

        stack = [start_idx]
        component_indices: list[int] = []
        while stack:
            idx = stack.pop()
            if visited[idx]:
                continue
            visited[idx] = True
            component_indices.append(idx)
            for node in segment_nodes[idx]:
                for neighbor in node_to_segments[node]:
                    if not visited[neighbor]:
                        stack.append(neighbor)

        if len(component_indices) == 1:
            merged = segments[component_indices[0]]
        else:
            merged = linemerge(MultiLineString([segments[i] for i in component_indices]))
        if merged.geom_type == "LineString":
            components.append(MultiLineString([merged]))
        else:
            components.append(merged)

    return components


def _component_to_linestring(component: MultiLineString, original_objectid: int) -> LineString | None:
    """Convert a connected component to a single LineString for storage.

    Most components are already simple paths; if a component is branched,
    ``linemerge`` is attempted and the longest resulting part is kept.
    """
    if component.is_empty:
        return None

    if component.geom_type == "LineString":
        return component

    merged = linemerge(component)
    if merged.geom_type == "LineString":
        return merged

    parts = [g for g in merged.geoms if g.geom_type == "LineString"]
    if not parts:
        return None

    if len(parts) > 1:
        print(
            f"Warning: objectid {original_objectid} has a branched component; "
            f"keeping the longest of {len(parts)} parts",
            file=sys.stderr,
        )
    return max(parts, key=lambda g: g.length)


def _fetch_rows(conn, table: str, geom_col: str, has_is_luan: bool):
    """Yield (objectid, src_name, is_luan, geom_wkb) rows from the source table."""
    _validate_identifier(table)
    _validate_identifier(geom_col)

    if has_is_luan:
        query = sql.SQL("""
            SELECT objectid, src_name, is_luan, ST_AsBinary({geom}) AS geom_wkb
            FROM {table}
            WHERE objectid IS NOT NULL
            ORDER BY objectid, src_name
        """).format(
            geom=sql.Identifier(geom_col),
            table=sql.Identifier(table),
        )
    else:
        query = sql.SQL("""
            SELECT objectid, src_name, NULL::boolean AS is_luan, ST_AsBinary({geom}) AS geom_wkb
            FROM {table}
            WHERE objectid IS NOT NULL
            ORDER BY objectid, src_name
        """).format(
            geom=sql.Identifier(geom_col),
            table=sql.Identifier(table),
        )

    with conn.cursor() as cur:
        cur.execute(query)
        yield from cur


def _process_objectid_groups(
    groups: dict[str, list[LineString]],
    original_objectid: int,
    next_objectid: int,
    is_luan: bool | None = None,
) -> tuple[list[tuple[int, str, LineString, int, bool]], list[dict], bool, int, int]:
    """Combine, split, and assign final objectids for one original objectid.

    ``groups`` maps ``src_name`` to the list of LineString segments that share
    that name for the current ``original_objectid``. All groups are processed
    together so the longest connected component across every name can keep the
    original objectid.

    ``is_luan`` is propagated from the source row or derived from the known
    Luan objectid range when the source table does not expose the column.

    Returns ``(cleaned_rows, remap_log, kept_original, new_ids_created,
    next_objectid)``.
    """

    def _linestrings_from_combined(geom) -> list[LineString]:
        """Extract individual LineStrings from a unary_union result."""
        if geom is None:
            return []
        if geom.geom_type == "LineString":
            return [geom]
        if geom.geom_type == "MultiLineString":
            return [LineString(part.coords) for part in geom.geoms]
        if geom.geom_type == "GeometryCollection":
            result: list[LineString] = []
            for part in geom.geoms:
                if part.geom_type == "LineString":
                    result.append(part)
                elif part.geom_type == "MultiLineString":
                    result.extend(LineString(sub.coords) for sub in part.geoms)
            return result
        return []

    candidates: list[tuple[int, str, LineString]] = []

    for src_name, segments in groups.items():
        try:
            if len(segments) == 1:
                combined_segments = segments
            else:
                combined = unary_union(segments)
                combined_segments = _linestrings_from_combined(combined)
                if not combined_segments:
                    combined_segments = segments
            components = _split_connected_components(combined_segments)
            for component in components:
                line = _component_to_linestring(component, original_objectid)
                if line is None or line.is_empty or len(line.coords) < 2:
                    continue
                candidates.append((original_objectid, src_name, line))
        except (ShapelyError, TopologicalError, ValueError) as exc:
            print(
                f"Warning: skipping ({original_objectid}, {src_name}): "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            continue

    if not candidates:
        return [], [], False, 0, next_objectid

    longest_idx = max(range(len(candidates)), key=lambda i: candidates[i][2].length)

    cleaned_rows: list[tuple[int, str, LineString, int, bool]] = []
    remap_log: list[dict] = []
    kept_original = False
    new_ids_created = 0

    derived_is_luan = (
        bool(is_luan)
        if is_luan is not None
        else (original_objectid in DEFAULT_LUAN_OBJECTIDS)
    )

    for idx, (orig_objectid, src_name, geometry) in enumerate(candidates):
        is_longest = idx == longest_idx
        if is_longest:
            new_objectid = orig_objectid
            reason = "kept_main"
            kept_original = True
        else:
            new_objectid = next_objectid
            next_objectid += 1
            reason = "split_component"
            new_ids_created += 1

        cleaned_rows.append((new_objectid, src_name, geometry, orig_objectid, derived_is_luan))
        remap_log.append(
            {
                "original_objectid": orig_objectid,
                "new_objectid": new_objectid,
                "src_name": src_name,
                "is_luan": derived_is_luan,
                "reason": reason,
            }
        )

    return cleaned_rows, remap_log, kept_original, new_ids_created, next_objectid


def _create_output_table(conn, output_table: str, srid: int = 4326) -> None:
    """Create the cleaned output table with a LineString geometry column."""
    _validate_identifier(output_table)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("DROP TABLE IF EXISTS {table}").format(
                table=sql.Identifier(output_table)
            )
        )
        cur.execute(
            sql.SQL(
                """
                CREATE TABLE {table} (
                    objectid INTEGER PRIMARY KEY,
                    src_name TEXT,
                    is_luan BOOLEAN,
                    geom GEOMETRY(LINESTRING, {srid}),
                    original_objectid INTEGER
                )
                """
            ).format(table=sql.Identifier(output_table), srid=sql.Literal(srid))
        )


def _insert_cleaned_rows(
    conn, output_table: str, cleaned_rows, srid: int = 4326
) -> int:
    """Bulk-insert cleaned rows into the output table."""
    _validate_identifier(output_table)
    if not cleaned_rows:
        return 0

    values = [
        (
            new_objectid,
            src_name,
            is_luan,
            to_wkb(geometry, hex=True, include_srid=True),
            original_objectid,
        )
        for new_objectid, src_name, geometry, original_objectid, is_luan in cleaned_rows
    ]

    template = sql.SQL(
        "( %s, %s, %s, ST_SetSRID(%s::geometry, {}), %s)"
    ).format(sql.Literal(srid)).as_string(conn)

    with conn.cursor() as cur:
        execute_values(
            cur,
            sql.SQL(
                """
                INSERT INTO {table} (objectid, src_name, is_luan, geom, original_objectid)
                VALUES %s
                """
            ).format(table=sql.Identifier(output_table)),
            values,
            template=template,
        )
    return len(cleaned_rows)


def _write_remap_log(output_table: str, remap_log: list[dict]) -> Path:
    """Write the remap log JSON next to the current working directory."""
    output_path = Path.cwd() / f"{output_table}_remap.json"
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(remap_log, f, ensure_ascii=False, indent=2)
    return output_path


def main() -> int:
    input_table = os.environ.get("RIVER_TABLE_FULL", DEFAULT_INPUT_TABLE)
    _validate_identifier(input_table)

    output_table = os.environ.get(
        "RIVER_TABLE_FULL_OUTPUT", f"{input_table}_cleaned"
    )
    _validate_identifier(output_table)

    if output_table == input_table:
        raise CleanError(
            f"output table '{output_table}' must differ from input table"
        )

    try:
        conn_kwargs = _require_env_vars()

        print(f"Cleaning river table: {input_table}")
        print(f"  Output table: {output_table}")

        conn = psycopg2.connect(**conn_kwargs)
        try:
            geom_col, geom_srid = _discover_geometry_column(conn, input_table)
            print(f"  Geometry column: {geom_col} (SRID {geom_srid})")

            has_is_luan = _has_column(conn, input_table, "is_luan")
            print(f"  Source has is_luan: {has_is_luan}")

            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("SELECT MAX(objectid) FROM {table}").format(
                        table=sql.Identifier(input_table)
                    )
                )
                max_row = cur.fetchone()
                next_objectid = (max_row[0] or 0) + 1

            _create_output_table(conn, output_table, geom_srid)

            input_rows = 0
            output_rows = 0
            kept_objectids = 0
            new_ids_created = 0
            remap_log: list[dict] = []

            current_objectid: int | None = None
            current_groups: dict[str, list[LineString]] = {}
            current_is_luan: bool | None = None

            for objectid, src_name, is_luan, geom_wkb in _fetch_rows(
                conn, input_table, geom_col, has_is_luan
            ):
                input_rows += 1

                if objectid != current_objectid:
                    if current_objectid is not None:
                        (
                            cleaned,
                            remap,
                            kept,
                            created,
                            next_objectid,
                        ) = _process_objectid_groups(
                            current_groups,
                            current_objectid,
                            next_objectid,
                            current_is_luan,
                        )
                        if cleaned:
                            inserted = _insert_cleaned_rows(
                                conn, output_table, cleaned, geom_srid
                            )
                            output_rows += inserted
                            if kept:
                                kept_objectids += 1
                            new_ids_created += created
                            remap_log.extend(remap)
                    current_objectid = objectid
                    current_groups = {}
                    current_is_luan = None

                if is_luan is not None:
                    value = bool(is_luan)
                    # Rows for the same objectid may disagree on is_luan
                    # (e.g. duplicate/multipart geometries). We keep the last
                    # row's value and emit a warning so the conflict is visible.
                    if (
                        current_is_luan is not None
                        and current_is_luan != value
                    ):
                        print(
                            f"Warning: objectid {current_objectid} has conflicting "
                            f"is_luan values ({current_is_luan} vs {value}); using {value}",
                            file=sys.stderr,
                        )
                    current_is_luan = value

                segments = _extract_segments(geom_wkb)
                if segments:
                    current_groups.setdefault(src_name or "", []).extend(segments)

            if current_objectid is not None and current_groups:
                (
                    cleaned,
                    remap,
                    kept,
                    created,
                    next_objectid,
                ) = _process_objectid_groups(
                    current_groups,
                    current_objectid,
                    next_objectid,
                    current_is_luan,
                )
                if cleaned:
                    inserted = _insert_cleaned_rows(
                        conn, output_table, cleaned, geom_srid
                    )
                    output_rows += inserted
                    if kept:
                        kept_objectids += 1
                    new_ids_created += created
                    remap_log.extend(remap)

            conn.commit()

            remap_path = _write_remap_log(output_table, remap_log)

            print(f"  Input rows: {input_rows:,}")
            print(f"  Output rows: {output_rows:,}")
            print(f"  Original objectids kept: {kept_objectids:,}")
            print(f"  New objectids created: {new_ids_created:,}")
            print(f"  Remap log: {remap_path}")
        except Exception:
            conn.rollback()
            raise
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

    print("Cleaning completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
