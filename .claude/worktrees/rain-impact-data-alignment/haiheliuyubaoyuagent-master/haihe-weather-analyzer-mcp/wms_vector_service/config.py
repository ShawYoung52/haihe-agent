from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus


IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


@dataclass(frozen=True)
class Settings:
    database_url: str
    sql_table: str = "hh_gis_wms_sql"
    sql_id_column: str = "id"
    sql_text_column: str = "sql_text"
    default_srid: int = 4326
    default_tile_size: int = 256
    max_features: int = 20000


def _checked_identifier(value: str, name: str) -> str:
    if not IDENTIFIER_RE.match(value):
        raise ValueError(f"{name} must be a simple SQL identifier, got {value!r}")
    return value


def _database_url_from_config_ini() -> str | None:
    """与 ``gis_wms_sql_registry`` / ``config.ini`` 中 [postgres] 一致，便于与本仓库应急 HTTP 共用库。"""
    root = Path(__file__).resolve().parents[1]
    ini_path = root / "config.ini"
    if not ini_path.is_file():
        return None
    cp = configparser.ConfigParser()
    cp.read(ini_path, encoding="utf-8")
    if not cp.has_section("postgres"):
        return None
    pg = cp["postgres"]
    user = quote_plus(pg.get("user", "postgres"))
    password = quote_plus(pg.get("password", ""))
    host = pg.get("host", "127.0.0.1").strip()
    port = pg.getint("port", 5432)
    dbname = quote_plus(pg.get("dbname", "postgres"))
    sslmode = pg.get("sslmode", "prefer").strip()
    q = f"sslmode={quote_plus(sslmode)}" if sslmode else ""
    base = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{dbname}"
    return f"{base}?{q}" if q else base


def load_settings() -> Settings:
    database_url = (os.environ.get("WMS_DATABASE_URL") or "").strip()
    if not database_url:
        database_url = _database_url_from_config_ini() or ""

    sql_table = (os.environ.get("WMS_SQL_TABLE") or "").strip() or "hh_gis_wms_sql"
    sql_id_column = (os.environ.get("WMS_SQL_ID_COLUMN") or "").strip() or "id"
    sql_text_column = (os.environ.get("WMS_SQL_TEXT_COLUMN") or "").strip() or "sql_text"

    _checked_identifier(sql_table, "WMS_SQL_TABLE")
    _checked_identifier(sql_id_column, "WMS_SQL_ID_COLUMN")
    _checked_identifier(sql_text_column, "WMS_SQL_TEXT_COLUMN")

    default_srid = int(os.environ.get("WMS_DEFAULT_SRID") or "4326")
    default_tile_size = int(os.environ.get("WMS_DEFAULT_TILE_SIZE") or "256")
    max_features = int(os.environ.get("WMS_MAX_FEATURES") or "20000")

    return Settings(
        database_url=database_url,
        sql_table=sql_table,
        sql_id_column=sql_id_column,
        sql_text_column=sql_text_column,
        default_srid=default_srid,
        default_tile_size=default_tile_size,
        max_features=max_features,
    )
