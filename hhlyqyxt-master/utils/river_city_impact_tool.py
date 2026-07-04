"""直接/间接影响河流涉及城市统计工具。

输入影响河流 GeoJSON，按 impact_type 区分：
- direct_buffer/direct：直接影响河流；
- downstream_50km/indirect/downstream：间接影响河流。

然后与行政区划面表做 ST_Intersects，返回直接河流影响哪些市、间接河流影响哪些市。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable


def _quote_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name or "")):
        raise ValueError(f"非法 SQL 标识符：{name!r}")
    return f'"{name}"'


def _split_table_name(table: str, default_schema: str = "public") -> tuple[str, str]:
    text = str(table or "").strip()
    if not text:
        raise ValueError("表名不能为空")
    if "." in text:
        schema, name = text.split(".", 1)
    else:
        schema, name = default_schema, text
    return schema.strip(), name.strip()


def _first_existing(columns: set[str], candidates: Iterable[str]) -> str | None:
    lower_map = {c.lower(): c for c in columns}
    for item in candidates:
        if item in columns:
            return item
        real = lower_map.get(str(item).lower())
        if real:
            return real
    return None


def _default_engine():
    try:
        from utils.db import engine  # type: ignore
    except Exception:
        from .db import engine  # type: ignore
    return engine


def _load_geojson_file(geojson_path: str | Path) -> dict:
    path = Path(geojson_path)
    if not path.exists():
        raise FileNotFoundError(f"影响河流 GeoJSON 文件不存在：{path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise ValueError(f"不是合法的 FeatureCollection GeoJSON：{path}")
    return data


def _extract_river_geojson(
    impact_result: dict | None = None,
    river_geojson: dict | None = None,
    geojson_path: str | Path | None = None,
) -> dict:
    if geojson_path:
        return _load_geojson_file(geojson_path)
    if river_geojson:
        return river_geojson
    if not impact_result:
        return {"type": "FeatureCollection", "features": []}
    if impact_result.get("type") == "FeatureCollection":
        return impact_result
    return impact_result.get("river_geojson") or {"type": "FeatureCollection", "features": []}


def _normalize_impact_type(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if text in {"direct", "direct_buffer", "buffer", "直接", "直接影响"}:
        return "direct"
    if text in {"indirect", "downstream", "downstream_50km", "间接", "间接影响", "下游"}:
        return "indirect"
    if "direct" in text:
        return "direct"
    if "downstream" in text or "indirect" in text:
        return "indirect"
    return "unknown"


def _feature_rows_from_geojson(river_geojson: dict) -> list[tuple]:
    rows = []
    for idx, feature in enumerate(river_geojson.get("features", []) or [], start=1):
        if not isinstance(feature, dict):
            continue
        geom = feature.get("geometry")
        if not geom:
            continue
        props = feature.get("properties") or {}
        impact_type = _normalize_impact_type(props.get("impact_type"))
        if impact_type == "unknown":
            continue
        river_name = props.get("river_name") or props.get("rivername") or props.get("name") or "未知"
        objectid = props.get("objectid") or props.get("id") or ""
        edge_key = props.get("edge_key") or f"feature_{idx}"
        rows.append((idx, impact_type, str(river_name), str(objectid), str(edge_key), json.dumps(geom, ensure_ascii=False)))
    return rows


def _table_columns(cur, schema: str, table: str) -> set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
        (schema, table),
    )
    return {str(row[0]) for row in cur.fetchall()}


def _admin_exprs(columns: set[str], alias: str = "a") -> tuple[str, str, str]:
    province_col = _first_existing(columns, ["province_name", "province", "prov_name", "sheng"])
    city_col = _first_existing(columns, ["city_name", "city", "prefecture", "shi", "name", "NAME"])
    county_col = _first_existing(columns, ["county_name", "cnty", "county", "district_name", "xian"])

    def expr(col: str | None) -> str:
        if not col:
            return "NULL::text"
        return f"NULLIF(TRIM({alias}.{_quote_ident(col)}::text), '')"

    return expr(province_col), expr(city_col), expr(county_col)


def _city_item(row: dict) -> dict:
    city_name = row.get("city_name") or row.get("county_name") or row.get("province_name") or "未知"
    return {
        "city_name": city_name,
        "province_name": row.get("province_name"),
        "county_names": row.get("county_names") or [],
        "river_count": int(row.get("river_count") or 0),
        "feature_count": int(row.get("feature_count") or 0),
        "rivers": row.get("rivers") or [],
    }


def get_direct_indirect_river_impact_cities(
    *,
    geojson_path: str | Path | None = None,
    impact_result: dict | None = None,
    river_geojson: dict | None = None,
    engine: Any = None,
    admin_table: str = "haihe_admin_division",
    admin_schema: str = "public",
    admin_geom_col: str | None = None,
) -> dict:
    """统计直接河流、间接河流分别影响哪些市。

    Args:
        geojson_path: 已生成的影响河流 GeoJSON 文件路径，例如 impact_rivers_postgis.geojson。
        impact_result: build_rain24h_impact_river_geojson 或本地测试脚本得到的完整结果。
        river_geojson: 影响河流 FeatureCollection。优先级：geojson_path > river_geojson > impact_result。
        engine: 可选 SQLAlchemy engine；不传时使用 utils.db.engine。
        admin_table: 行政区划面表，默认 haihe_admin_division。
        admin_schema: admin_table 不带 schema 时使用的默认 schema。
        admin_geom_col: 行政区划面几何字段；不传时自动识别 geom/geometry/wkb_geometry/the_geom。

    Returns:
        dict，包含 direct / indirect 两组城市清单。
    """
    fc = _extract_river_geojson(
        impact_result=impact_result,
        river_geojson=river_geojson,
        geojson_path=geojson_path,
    )
    feature_rows = _feature_rows_from_geojson(fc)
    if not feature_rows:
        return {
            "status": "ok",
            "direct": {"city_count": 0, "cities": []},
            "indirect": {"city_count": 0, "cities": []},
            "direct_city_names": [],
            "indirect_city_names": [],
            "all_city_names": [],
            "message": "没有可统计的影响河流 GeoJSON 要素。",
        }

    engine = engine or _default_engine()
    raw_conn = engine.raw_connection()
    schema, table = _split_table_name(admin_table, default_schema=admin_schema)

    try:
        with raw_conn.cursor() as cur:
            columns = _table_columns(cur, schema, table)
            if not columns:
                raise ValueError(f"未找到行政区划表：{schema}.{table}")
            geom_col = admin_geom_col or _first_existing(columns, ["geom", "geometry", "wkb_geometry", "the_geom"])
            if not geom_col:
                raise ValueError(f"行政区划表 {schema}.{table} 未找到几何字段")

            province_expr, city_expr, county_expr = _admin_exprs(columns, alias="a")
            cur.execute("DROP TABLE IF EXISTS tmp_impact_river_features")
            cur.execute("""
                CREATE TEMP TABLE tmp_impact_river_features(
                    feature_id integer,
                    impact_type text,
                    river_name text,
                    objectid text,
                    edge_key text,
                    geom geometry(Geometry,4326)
                ) ON COMMIT DROP
            """)
            cur.executemany(
                """
                INSERT INTO tmp_impact_river_features
                VALUES(%s,%s,%s,%s,%s,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))
                """,
                feature_rows,
            )

            q_schema = _quote_ident(schema)
            q_table = _quote_ident(table)
            q_geom = _quote_ident(geom_col)
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
                    JOIN {q_schema}.{q_table} a
                      ON ST_Intersects(f.geom, a.{q_geom})
                    WHERE f.geom IS NOT NULL
                      AND a.{q_geom} IS NOT NULL
                      AND NOT ST_IsEmpty(a.{q_geom})
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
            colnames = [desc[0] for desc in cur.description]
            rows = [dict(zip(colnames, row)) for row in cur.fetchall()]
    finally:
        raw_conn.close()

    direct_items = []
    indirect_items = []
    for row in rows:
        item = _city_item(row)
        if row.get("impact_type") == "direct":
            direct_items.append(item)
        elif row.get("impact_type") == "indirect":
            indirect_items.append(item)

    direct_names = sorted({x["city_name"] for x in direct_items if x.get("city_name")})
    indirect_names = sorted({x["city_name"] for x in indirect_items if x.get("city_name")})
    all_names = sorted(set(direct_names) | set(indirect_names))

    return {
        "status": "ok",
        "params": {
            "admin_table": f"{schema}.{table}",
            "admin_geom_col": geom_col,
            "direct_rule": "impact_type=direct_buffer/direct 的河流空间相交城市",
            "indirect_rule": "impact_type=downstream_50km/indirect 的河流空间相交城市",
        },
        "direct": {
            "city_count": len(direct_names),
            "cities": direct_items,
        },
        "indirect": {
            "city_count": len(indirect_names),
            "cities": indirect_items,
        },
        "direct_city_names": direct_names,
        "indirect_city_names": indirect_names,
        "all_city_names": all_names,
        "message": f"直接河流影响 {len(direct_names)} 个市，间接河流影响 {len(indirect_names)} 个市。",
    }


# 简短别名，方便智能体工具层引用。
get_river_impact_cities = get_direct_indirect_river_impact_cities
