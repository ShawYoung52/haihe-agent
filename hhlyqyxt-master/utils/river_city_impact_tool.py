"""统计直接/间接影响河流涉及城市。

输入已生成的影响河流 GeoJSON：
- impact_type=direct_buffer/direct 归为直接影响；
- impact_type=downstream_50km/downstream/indirect 归为间接影响。

函数只负责一件事：河流 GeoJSON 与行政区划面表空间相交，并按城市聚合。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def _quote_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name or "")):
        raise ValueError(f"非法 SQL 标识符：{name!r}")
    return f'"{name}"'


def _get_default_engine():
    try:
        from utils.db import engine  # type: ignore
    except ImportError:
        from .db import engine  # type: ignore
    return engine


def _load_feature_collection(geojson_path: str | Path) -> dict:
    path = Path(geojson_path)
    if not path.exists():
        raise FileNotFoundError(f"影响河流 GeoJSON 不存在：{path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise ValueError(f"文件不是 FeatureCollection GeoJSON：{path}")
    return data


def _classify_impact_type(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"direct", "direct_buffer", "直接", "直接影响"}:
        return "direct"
    if text in {"downstream", "downstream_50km", "indirect", "下游", "间接", "间接影响"}:
        return "indirect"
    return None


def _make_feature_rows(feature_collection: dict) -> list[tuple]:
    rows: list[tuple] = []
    for index, feature in enumerate(feature_collection.get("features", []) or [], start=1):
        if not isinstance(feature, dict) or not feature.get("geometry"):
            continue
        props = feature.get("properties") or {}
        impact_type = _classify_impact_type(props.get("impact_type"))
        if not impact_type:
            continue
        river_name = props.get("river_name") or props.get("rivername") or props.get("name") or "未知"
        rows.append((index, impact_type, str(river_name), json.dumps(feature["geometry"], ensure_ascii=False)))
    return rows


def _fetch_table_columns(cur, schema: str, table: str) -> set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
        (schema, table),
    )
    return {str(row[0]) for row in cur.fetchall()}


def _pick_column(columns: set[str], candidates: tuple[str, ...]) -> str | None:
    lower_to_real = {column.lower(): column for column in columns}
    for candidate in candidates:
        if candidate in columns:
            return candidate
        if candidate.lower() in lower_to_real:
            return lower_to_real[candidate.lower()]
    return None


def _admin_column_expressions(columns: set[str]) -> tuple[str, str, str]:
    province = _pick_column(columns, ("province_name", "province", "prov_name"))
    city = _pick_column(columns, ("city_name", "city", "name", "NAME"))
    county = _pick_column(columns, ("county_name", "cnty", "county", "district_name"))

    def expr(column: str | None) -> str:
        return "NULL::text" if not column else f"NULLIF(TRIM(a.{_quote_ident(column)}::text), '')"

    return expr(province), expr(city), expr(county)


def _create_feature_temp_table(cur, feature_rows: list[tuple]) -> None:
    cur.execute("DROP TABLE IF EXISTS tmp_impact_river_features")
    cur.execute("""
        CREATE TEMP TABLE tmp_impact_river_features(
            feature_id integer,
            impact_type text,
            river_name text,
            geom geometry(Geometry,4326)
        ) ON COMMIT DROP
    """)
    cur.executemany(
        """
        INSERT INTO tmp_impact_river_features
        VALUES (%s,%s,%s,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))
        """,
        feature_rows,
    )


def _query_city_impacts(cur, admin_schema: str, admin_table: str, admin_geom_col: str) -> list[dict]:
    columns = _fetch_table_columns(cur, admin_schema, admin_table)
    if not columns:
        raise ValueError(f"未找到行政区划表：{admin_schema}.{admin_table}")
    if admin_geom_col not in columns:
        raise ValueError(f"行政区划表缺少几何字段：{admin_geom_col}")

    province_expr, city_expr, county_expr = _admin_column_expressions(columns)
    cur.execute(f"""
        WITH hits AS (
            SELECT
                f.impact_type,
                f.river_name,
                f.feature_id,
                {province_expr} AS province_name,
                COALESCE({city_expr}, {county_expr}, {province_expr}, '未知') AS city_name,
                {county_expr} AS county_name
            FROM tmp_impact_river_features f
            JOIN {_quote_ident(admin_schema)}.{_quote_ident(admin_table)} a
              ON ST_Intersects(f.geom, a.{_quote_ident(admin_geom_col)})
            WHERE f.geom IS NOT NULL
              AND a.{_quote_ident(admin_geom_col)} IS NOT NULL
              AND NOT ST_IsEmpty(a.{_quote_ident(admin_geom_col)})
        )
        SELECT
            impact_type,
            province_name,
            city_name,
            array_remove(array_agg(DISTINCT county_name), NULL) AS county_names,
            COUNT(DISTINCT river_name) AS river_count,
            COUNT(DISTINCT feature_id) AS feature_count,
            array_agg(DISTINCT river_name ORDER BY river_name) AS rivers
        FROM hits
        GROUP BY impact_type, province_name, city_name
        ORDER BY impact_type, province_name, city_name
    """)
    names = [desc[0] for desc in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _format_city_item(row: dict) -> dict:
    return {
        "city_name": row.get("city_name") or "未知",
        "province_name": row.get("province_name"),
        "county_names": row.get("county_names") or [],
        "river_count": int(row.get("river_count") or 0),
        "feature_count": int(row.get("feature_count") or 0),
        "rivers": row.get("rivers") or [],
    }


def _group_city_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    direct: list[dict] = []
    indirect: list[dict] = []
    for row in rows:
        item = _format_city_item(row)
        if row.get("impact_type") == "direct":
            direct.append(item)
        elif row.get("impact_type") == "indirect":
            indirect.append(item)
    return direct, indirect


def get_river_impact_cities(
    geojson_path: str | Path,
    *,
    engine: Any = None,
    admin_table: str = "haihe_admin_division",
    admin_schema: str = "public",
    admin_geom_col: str = "geom",
) -> dict:
    """根据影响河流 GeoJSON 统计直接/间接影响城市。

    Args:
        geojson_path: 已生成的 impact_rivers_postgis.geojson 路径。
        engine: SQLAlchemy engine；不传则使用 utils.db.engine。
        admin_table: 行政区划面表名。
        admin_schema: 行政区划表 schema。
        admin_geom_col: 行政区划几何字段名。
    """
    feature_rows = _make_feature_rows(_load_feature_collection(geojson_path))
    if not feature_rows:
        return _empty_result("没有可统计的直接/间接影响河流要素。")

    db_engine = engine or _get_default_engine()
    connection = db_engine.raw_connection()
    try:
        with connection.cursor() as cur:
            _create_feature_temp_table(cur, feature_rows)
            city_rows = _query_city_impacts(cur, admin_schema, admin_table, admin_geom_col)
    finally:
        connection.close()

    direct, indirect = _group_city_rows(city_rows)
    return _build_result(direct, indirect, admin_schema, admin_table, admin_geom_col)


def _empty_result(message: str) -> dict:
    return {
        "status": "ok",
        "direct": {"city_count": 0, "cities": []},
        "indirect": {"city_count": 0, "cities": []},
        "direct_city_names": [],
        "indirect_city_names": [],
        "all_city_names": [],
        "message": message,
    }


def _build_result(direct: list[dict], indirect: list[dict], schema: str, table: str, geom_col: str) -> dict:
    direct_names = sorted({item["city_name"] for item in direct})
    indirect_names = sorted({item["city_name"] for item in indirect})
    return {
        "status": "ok",
        "params": {
            "admin_table": f"{schema}.{table}",
            "admin_geom_col": geom_col,
        },
        "direct": {"city_count": len(direct_names), "cities": direct},
        "indirect": {"city_count": len(indirect_names), "cities": indirect},
        "direct_city_names": direct_names,
        "indirect_city_names": indirect_names,
        "all_city_names": sorted(set(direct_names) | set(indirect_names)),
        "message": f"直接河流影响 {len(direct_names)} 个市，间接河流影响 {len(indirect_names)} 个市。",
    }


# 兼容上一次给出的函数名。
get_direct_indirect_river_impact_cities = get_river_impact_cities
