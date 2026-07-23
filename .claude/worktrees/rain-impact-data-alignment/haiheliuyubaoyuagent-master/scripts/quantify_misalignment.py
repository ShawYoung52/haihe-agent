#!/usr/bin/env python3
"""Quantify baseline misalignment in the full river network table.

Connects to PostgreSQL using environment variables, reads the table named by
``RIVER_TABLE_FULL`` (defaulting to the project constant), and produces a JSON
report of geometry/name anomalies. The report is written to the current working
directory as ``{table}_baseline_stats.json``.

Anomalies detected:

* ``duplicate_rows``: the same ``objectid`` appears in more than one row.
* ``multi_part``: an ``objectid`` has a ``MULTILINESTRING`` geometry with more
  than one disconnected part.
* ``multi_name``: the same ``objectid`` is associated with more than one
  distinct ``src_name`` value.
* ``null_objectid_rows``: rows where ``objectid`` is NULL (skipped from other
  counts).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import psycopg2
from psycopg2 import sql
from shapely import from_wkb


def _load_constants_module():
    """Load project constants from the hyphenated package directory."""
    constants_path = (
        Path(__file__).resolve().parent.parent
        / "haihe-weather-analyzer-mcp"
        / "constants.py"
    )
    try:
        spec = importlib.util.spec_from_file_location(
            "haihe_weather_analyzer_mcp.constants", constants_path
        )
        if spec is None or spec.loader is None:
            raise QuantifyError(
                f"could not load constants module from {constants_path}"
            )
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("haihe_weather_analyzer_mcp.constants", module)
        spec.loader.exec_module(module)
    except (FileNotFoundError, ImportError, SyntaxError) as exc:
        raise QuantifyError(
            f"failed to load project constants from {constants_path}: {exc}"
        ) from exc
    return module


_constants = _load_constants_module()
_DEFAULT_TABLE = _constants.RIVER_TABLE_FULL

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_REDACT_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b")


class QuantifyError(Exception):
    """Raised for known, non-recoverable quantification failures."""


def _validate_identifier(name: str) -> None:
    if len(name) > 63:
        raise QuantifyError(f"identifier '{name}' exceeds PostgreSQL's 63-byte limit")
    if not _IDENTIFIER_RE.match(name):
        raise QuantifyError(f"'{name}' is not a valid PostgreSQL identifier")


def _scrub_message(message: str) -> str:
    """Remove IP:port patterns from database error messages."""
    return _REDACT_RE.sub("<redacted>", message)


def _count_geometry_parts(geom_wkb: bytes | memoryview | None) -> int:
    """Return the number of disconnected linestring parts in a WKB geometry."""
    if not geom_wkb:
        return 0

    # psycopg2 returns a memoryview; Shapely needs a contiguous bytes buffer.
    if isinstance(geom_wkb, memoryview):
        geom_wkb = bytes(geom_wkb)

    try:
        geom = from_wkb(geom_wkb)
    except Exception:
        return 0

    if geom is None:
        return 0
    if geom.geom_type == "MultiLineString":
        return len(geom.geoms)
    return 1


def _discover_geometry_column(conn, table: str, schema: str = "public") -> str:
    """Discover the name of the geometry column for the given table."""
    _validate_identifier(table)
    _validate_identifier(schema)

    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                    SELECT f_geometry_column
                    FROM geometry_columns
                    WHERE f_table_schema = %s AND f_table_name = %s
                    LIMIT 1
                """,
                [schema, table],
            )
            row = cur.fetchone()
            if row:
                return row[0]
        except psycopg2.Error:
            # Roll back the failed savepoint so the fallback query can run.
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
            return row[0]

    raise QuantifyError(
        f"could not find a geometry column for table {schema}.{table}"
    )


def _fetch_rows(conn, table: str, geom_col: str):
    """Yield (objectid, src_name, part_count) rows from the source table."""
    _validate_identifier(table)
    _validate_identifier(geom_col)

    query = sql.SQL("""
        SELECT objectid, src_name, ST_AsBinary({geom}) AS geom_wkb
        FROM {table}
    """).format(
        geom=sql.Identifier(geom_col),
        table=sql.Identifier(table),
    )

    with conn.cursor() as cur:
        cur.execute(query)
        for objectid, src_name, geom_wkb in cur:
            if objectid is None:
                continue
            part_count = _count_geometry_parts(geom_wkb)
            yield objectid, src_name, part_count


def _build_report(rows, table: str) -> dict:
    """Aggregate rows into a baseline misalignment report."""
    by_objectid = defaultdict(list)
    total_rows = 0
    null_objectid_rows = 0

    for objectid, src_name, part_count in rows:
        if objectid is None:
            null_objectid_rows += 1
            continue
        total_rows += 1
        by_objectid[objectid].append(
            {
                "src_name": src_name,
                "part_count": part_count,
            }
        )

    unique_objectids = len(by_objectid)
    duplicate_objectids = []
    multi_part_objectids = []
    multi_name_objectids = []
    details = []

    for objectid, entries in by_objectid.items():
        names = []
        seen_names = set()
        part_counts = []
        is_duplicate = len(entries) > 1
        is_multi_part = False
        issues = []

        for entry in entries:
            part_counts.append(entry["part_count"])
            if entry["part_count"] > 1:
                is_multi_part = True
            name = entry["src_name"]
            if name not in seen_names:
                seen_names.add(name)
                names.append(name)

        if is_duplicate:
            duplicate_objectids.append(objectid)
            issues.append("duplicate_rows")
        if is_multi_part:
            multi_part_objectids.append(objectid)
            issues.append("multi_part")
        if len(names) > 1:
            multi_name_objectids.append(objectid)
            issues.append("multi_name")

        if issues:
            details.append(
                {
                    "objectid": objectid,
                    "names": names,
                    "part_counts": part_counts,
                    "row_count": len(entries),
                    "issues": issues,
                }
            )

    return {
        "table": table,
        "total_rows": total_rows,
        "null_objectid_rows": null_objectid_rows,
        "unique_objectids": unique_objectids,
        "duplicate_objectids": duplicate_objectids,
        "multi_part_objectids": multi_part_objectids,
        "multi_name_objectids": multi_name_objectids,
        "details": details,
    }


def _require_env_vars():
    """Return connection kwargs from environment, validating required variables."""
    required = ["PGHOST", "PGDATABASE", "PGUSER"]
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise QuantifyError(
            f"missing required environment variables: {', '.join(missing)}"
        )

    return {
        "host": os.environ["PGHOST"],
        "port": os.environ.get("PGPORT", ""),
        "dbname": os.environ["PGDATABASE"],
        "user": os.environ["PGUSER"],
        "password": os.environ.get("PGPASSWORD", ""),
    }


def main() -> int:
    table = os.environ.get("RIVER_TABLE_FULL", _DEFAULT_TABLE)
    _validate_identifier(table)

    try:
        conn_kwargs = _require_env_vars()

        print(f"Quantifying misalignment for table: {table}")

        conn = psycopg2.connect(**conn_kwargs)

        try:
            geom_col = _discover_geometry_column(conn, table)
            print(f"  Geometry column: {geom_col}")

            rows = _fetch_rows(conn, table, geom_col)
            report = _build_report(rows, table)

            output_path = Path.cwd() / f"{table}_baseline_stats.json"
            try:
                with output_path.open("w", encoding="utf-8") as f:
                    json.dump(report, f, ensure_ascii=False, indent=2)
            except (OSError, TypeError) as exc:
                raise QuantifyError(
                    f"failed to write report to {output_path}: {exc}"
                ) from exc

            print(f"  Total rows: {report['total_rows']:,}")
            print(f"  Null objectid rows: {report['null_objectid_rows']:,}")
            print(f"  Unique objectids: {report['unique_objectids']:,}")
            print(f"  Duplicate objectids: {len(report['duplicate_objectids']):,}")
            print(f"  Multi-part objectids: {len(report['multi_part_objectids']):,}")
            print(f"  Multi-name objectids: {len(report['multi_name_objectids']):,}")
            print(f"  Report written to: {output_path}")
        finally:
            conn.close()

    except psycopg2.Error as exc:
        print(
            f"Error: database query failed: {_scrub_message(str(exc))}",
            file=sys.stderr,
        )
        return 1
    except QuantifyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
