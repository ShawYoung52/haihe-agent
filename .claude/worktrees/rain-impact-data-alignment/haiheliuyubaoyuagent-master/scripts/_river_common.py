"""Shared helpers for river data cleaning and pickle rebuild scripts.

This module centralises code that would otherwise be duplicated between
``clean_full_v6.py`` and ``regenerate_river_pkl.py``.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2 import sql


class CommonError(Exception):
    """Raised for known, non-recoverable failures in river scripts."""


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
            raise CommonError(f"could not load constants module from {constants_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("haihe_weather_analyzer_mcp.constants", module)
        spec.loader.exec_module(module)
    except (FileNotFoundError, ImportError, SyntaxError) as exc:
        raise CommonError(
            f"failed to load project constants from {constants_path}: {exc}"
        ) from exc
    return module


_constants = _load_constants_module()
DEFAULT_INPUT_TABLE = _constants.RIVER_TABLE_FULL
DIRECTED_GRAPH_FILENAME = _constants.DIRECTED_GRAPH_FILENAME
RIVER_TABLE_FULL = _constants.RIVER_TABLE_FULL


def _load_luan_objectids() -> frozenset[int]:
    """Return the default Luan objectid set.

    Tries to load the objectid keys from ``_DEFAULT_LUAN_NAME_MAPPING`` in the
    traction-agent module so the fallback stays in sync. If that file is
    unavailable or has no mapping, falls back to the historically known range
    (objectids 1 through 21).
    """
    mapping_path = (
        Path(__file__).resolve().parent.parent.parent
        / "hhlyqyxt-master"
        / "utils"
        / "rainfall_impact_geojson.py"
    )
    try:
        spec = importlib.util.spec_from_file_location(
            "rainfall_impact_geojson_fallback", mapping_path
        )
        if spec is not None and spec.loader is not None:
            module = importlib.util.module_from_spec(spec)
            sys.modules.setdefault("rainfall_impact_geojson_fallback", module)
            spec.loader.exec_module(module)
            mapping = getattr(module, "_DEFAULT_LUAN_NAME_MAPPING", {})
            if mapping:
                return frozenset(
                    int(k) for k in mapping.keys() if str(k).isdigit()
                )
    except Exception:
        pass
    # Fallback: objectids 1-21 are the Luan River system per the
    # traction-agent's default _DEFAULT_LUAN_NAME_MAPPING.
    return frozenset(range(1, 22))


DEFAULT_LUAN_OBJECTIDS = _load_luan_objectids()

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
# IPv4 address with optional port, e.g. 211.157.132.19:48091
_REDACT_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b")


def _validate_identifier(name: str) -> None:
    if len(name) > 63:
        raise CommonError(f"identifier '{name}' exceeds PostgreSQL's 63-byte limit")
    if not _IDENTIFIER_RE.match(name):
        raise CommonError(f"'{name}' is not a valid PostgreSQL identifier")


def _scrub_message(message: str) -> str:
    """Remove IP:port patterns from database error messages."""
    return _REDACT_RE.sub("<redacted>", message)


def _require_env_vars() -> dict[str, Any]:
    """Return connection kwargs from environment, validating required variables."""
    required = ["PGHOST", "PGDATABASE", "PGUSER"]
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise CommonError(f"missing required environment variables: {', '.join(missing)}")

    kwargs: dict[str, Any] = {
        "host": os.environ["PGHOST"],
        "dbname": os.environ["PGDATABASE"],
        "user": os.environ["PGUSER"],
        "password": os.environ.get("PGPASSWORD", ""),
    }
    port = os.environ.get("PGPORT", "").strip()
    if port:
        kwargs["port"] = int(port)
    return kwargs


def _has_column(conn, table: str, column: str, schema: str = "public") -> bool:
    """Return True if ``table`` has a column named ``column``."""
    _validate_identifier(table)
    _validate_identifier(column)
    _validate_identifier(schema)

    with conn.cursor() as cur:
        cur.execute(
            """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s AND column_name = %s
                LIMIT 1
            """,
            [schema, table, column],
        )
        return cur.fetchone() is not None