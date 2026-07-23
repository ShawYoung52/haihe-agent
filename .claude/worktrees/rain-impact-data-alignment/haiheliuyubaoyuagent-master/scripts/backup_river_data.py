#!/usr/bin/env python3
"""Backup the river network table and pkl graph for data-alignment tasks.

Reads connection parameters from environment variables and creates timestamped
backups of the PostgreSQL table and the serialized graph pickle. Each run
produces a new backup; previous backups are never overwritten.

Defaults for the source table and pickle path are read from
``haihe-weather-analyzer-mcp/config.ini`` if it exists, with ``RIVER_TABLE_FULL``
and ``RIVER_GRAPH_PATH`` environment variables taking precedence.
"""

import configparser
import importlib.util
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import psycopg2
from psycopg2 import sql


def _load_constants_module():
    """Load project constants from the hyphenated package directory."""
    constants_path = (
        Path(__file__).resolve().parent.parent
        / "haihe-weather-analyzer-mcp"
        / "constants.py"
    )
    spec = importlib.util.spec_from_file_location("haihe_weather_analyzer_mcp.constants", constants_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("haihe_weather_analyzer_mcp.constants", module)
    spec.loader.exec_module(module)
    return module


_constants = _load_constants_module()
DIRECTED_GRAPH_FILENAME = _constants.DIRECTED_GRAPH_FILENAME
RIVER_TABLE_FULL = _constants.RIVER_TABLE_FULL


# Fallback defaults used when neither config.ini nor environment variables set them.
_DEFAULT_SOURCE_TABLE = RIVER_TABLE_FULL

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
# IPv4 address with optional port, e.g. 211.157.132.19:48091
_REDACT_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b")


class BackupError(Exception):
    """Raised for known, non-recoverable backup failures."""


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "haihe-weather-analyzer-mcp" / "config.ini"


def _load_config_defaults() -> tuple[str | None, str | None]:
    """Return (source_table, graph_path) from config.ini, or (None, None)."""
    config_file = _config_path()
    if not config_file.exists():
        return None, None

    config = configparser.ConfigParser()
    config.read(config_file, encoding="utf-8")

    source_table = None
    if config.has_option("postgres", "river_table_full"):
        source_table = config.get("postgres", "river_table_full").strip() or None

    graph_path = None
    if config.has_option("paths", "graph"):
        graph_path = config.get("paths", "graph").strip() or None

    return source_table, graph_path


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise BackupError(f"environment variable {name} is not set")
    return value


def _validate_identifier(name: str) -> None:
    if len(name) > 63:
        raise BackupError(f"identifier '{name}' exceeds PostgreSQL's 63-byte limit")
    if not _IDENTIFIER_RE.match(name):
        raise BackupError(f"'{name}' is not a valid PostgreSQL identifier")


def _resolve_graph_path(config_graph_value: str | None) -> Path:
    """Return the graph pickle path, preferring RIVER_GRAPH_PATH env var.

    When the env var is unset, derive the path from config.ini's ``[paths] graph``
    entry (treated as a file path) by replacing the filename with the constant
    ``DIRECTED_GRAPH_FILENAME``.
    """
    env_path = os.environ.get("RIVER_GRAPH_PATH")
    if env_path:
        return Path(env_path)

    if config_graph_value:
        return Path(config_graph_value).parent / DIRECTED_GRAPH_FILENAME

    raise BackupError(
        "RIVER_GRAPH_PATH is not set and no [paths] graph value found in config.ini"
    )


def _scrub_message(message: str) -> str:
    """Remove IP:port patterns from database error messages."""
    return _REDACT_RE.sub("<redacted>", message)


def _backup_table(conn, source_table: str, backup_table: str) -> int:
    """Create a backup table using CREATE TABLE ... AS TABLE ... and return row count."""
    _validate_identifier(source_table)
    _validate_identifier(backup_table)

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("CREATE TABLE {} AS TABLE {}").format(
                sql.Identifier(backup_table),
                sql.Identifier(source_table),
            )
        )
        cur.execute(
            sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(backup_table))
        )
        row_count = cur.fetchone()[0]
    return row_count


def _drop_backup_table(conn, backup_table: str) -> None:
    """Drop a backup table; used to keep DB/pickle backups in sync on failure."""
    _validate_identifier(backup_table)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(backup_table))
        )


def _backup_pickle(source_path: Path, backup_path: Path) -> None:
    """Copy the pickle file to a timestamped backup path."""
    shutil.copy2(source_path, backup_path)


def main() -> int:
    config_source_table, config_graph_path = _load_config_defaults()

    try:
        host = _require_env("PGHOST")
        port = _require_env("PGPORT")
        database = _require_env("PGDATABASE")
        user = _require_env("PGUSER")
        password = _require_env("PGPASSWORD")

        source_table = os.environ.get(
            "RIVER_TABLE_FULL", config_source_table or _DEFAULT_SOURCE_TABLE
        )
        _validate_identifier(source_table)

        source_pkl = _resolve_graph_path(config_graph_path)

        timestamp = _timestamp()
        backup_table = f"{source_table}_bak_{timestamp}"
        _validate_identifier(backup_table)

        backup_pkl = source_pkl.with_suffix(f".bak_{timestamp}.pkl")

        print(f"Backing up river data at {timestamp}")
        print(f"  Source table: {source_table}")
        print(f"  Source pkl:   {source_pkl}")

        if not source_pkl.exists():
            raise BackupError(f"source pickle not found: {source_pkl}")

        conn = None
        try:
            conn = psycopg2.connect(
                host=host,
                port=port,
                dbname=database,
                user=user,
                password=password,
            )
            row_count = _backup_table(conn, source_table, backup_table)
            conn.commit()
            print(f"  Backup table: {backup_table} ({row_count:,} rows)")

            try:
                _backup_pickle(source_pkl, backup_pkl)
                print(f"  Backup pkl:   {backup_pkl}")
            except OSError as exc:
                print(
                    f"Error: pickle backup failed: {exc}; dropping {backup_table}",
                    file=sys.stderr,
                )
                _drop_backup_table(conn, backup_table)
                conn.commit()
                raise BackupError(f"pickle backup failed: {exc}") from exc
        except psycopg2.Error:
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    except psycopg2.Error as exc:
        print(f"Error: database backup failed: {_scrub_message(str(exc))}", file=sys.stderr)
        return 1
    except BackupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print("Backup completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
