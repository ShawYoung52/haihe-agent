from __future__ import annotations

from functools import lru_cache
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from .config import Settings
from .geometry import geometry_from_row


@lru_cache(maxsize=4)
def get_engine(database_url: str) -> Engine:
    if not database_url:
        raise ValueError(
            "未配置数据库：请设置环境变量 WMS_DATABASE_URL，"
            "或在项目根目录提供可读的 config.ini（含 [postgres]）"
        )
    return create_engine(database_url, pool_pre_ping=True)


def fetch_sql_text(settings: Settings, sql_id: str) -> str:
    engine = get_engine(settings.database_url)
    query = text(
        f"select {settings.sql_text_column} from {settings.sql_table} "
        f"where cast({settings.sql_id_column} as text) = cast(:sql_id as text)"
    )
    with engine.connect() as conn:
        row = conn.execute(query, {"sql_id": sql_id}).first()
    if row is None:
        raise KeyError(f"sql_id {sql_id!r} was not found")
    return str(row[0])


def fetch_geometries(
    settings: Settings,
    sql_text: str,
    bbox: tuple[float, float, float, float],
    srid: int,
) -> list[dict[str, Any]]:
    minx, miny, maxx, maxy = bbox
    params = {
        "minx": minx,
        "miny": miny,
        "maxx": maxx,
        "maxy": maxy,
        "bbox_minx": minx,
        "bbox_miny": miny,
        "bbox_maxx": maxx,
        "bbox_maxy": maxy,
        "srid": srid,
    }
    engine = get_engine(settings.database_url)
    geometries: list[dict[str, Any]] = []
    max_features = int(settings.max_features)
    with engine.connect() as conn:
        result = conn.execute(text(sql_text), params)
        rows = result.fetchmany(max_features + 1)
        if len(rows) > max_features:
            rows = rows[:max_features]
        for row in rows:
            geometry = geometry_from_row(row)
            if geometry:
                geometries.append(geometry)
    return geometries
