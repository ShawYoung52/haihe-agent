"""
将动态矢量查询登记为 WMS 可用的 SQL 模板（含 :srid / :minx / :miny / :maxx / :maxy），
供前端仅携带 sql id 请求地图服务，避免直传大体量 GeoJSON。

WMS 侧执行时需绑定命名参数，与 PostGIS 占位符一致：
  :srid, :minx, :miny, :maxx, :maxy
"""
from __future__ import annotations

import configparser
import json
import re
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2.extras import RealDictCursor


def _sql_string_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _sql_text_array_literal(names: List[str]) -> str:
    parts = [_sql_string_literal(str(x).strip()) for x in names if str(x).strip()]
    if not parts:
        return "ARRAY[]::text[]"
    return "ARRAY[" + ",".join(parts) + "]::text[]"


def _ident(qname: str) -> str:
    """限定 schema.table 等标识符，仅允许字母数字下划线。"""
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$", qname):
        raise ValueError(f"非法 SQL 标识符: {qname!r}")
    return qname


def load_pg_for_registry(config_path: str) -> Dict[str, Any]:
    cp = configparser.ConfigParser()
    cp.read(config_path, encoding="utf-8")
    if not cp.has_section("postgres"):
        raise ValueError("config.ini 缺少 [postgres] 配置")
    pg = cp["postgres"]
    schema = pg.get("schema", "public").strip() or "public"
    return {
        "host": pg.get("host", "127.0.0.1"),
        "port": pg.getint("port", 5432),
        "dbname": pg.get("dbname", "postgres"),
        "user": pg.get("user", "postgres"),
        "password": pg.get("password", ""),
        "sslmode": pg.get("sslmode", "prefer"),
        "connect_timeout": pg.getint("connect_timeout", 5),
        "schema": schema,
        "srid": pg.getint("srid", 4326),
        "river_table": (
            pg.get("river_table_full", "").strip()
            or pg.get("river_table", "haihe_river_directed_full_v2").strip()
            or "haihe_river_directed_full_v2"
        ),
        "admin_table": pg.get("admin_table", "haihe_admin_division").strip() or "haihe_admin_division",
        "gis_wms_sql_table": pg.get("gis_wms_sql_table", "hh_gis_wms_sql").strip() or "hh_gis_wms_sql",
        "zone256_table": (pg.get("zone256_table") or "haihe_246_zone").strip() or "haihe_246_zone",
    }


def _connect(pg: Dict[str, Any]):
    return psycopg2.connect(
        host=pg["host"],
        port=pg["port"],
        dbname=pg["dbname"],
        user=pg["user"],
        password=pg["password"],
        sslmode=pg["sslmode"],
        connect_timeout=pg["connect_timeout"],
    )


def ensure_wms_sql_table(schema: str, table_name: str, conn) -> None:
    t = _ident(f"{schema}.{table_name}")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {t} (
                id BIGSERIAL PRIMARY KEY,
                sql_text TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )


def register_wms_sql_text(config_path: str, sql_text: str) -> int:
    """写入 SQL 模板并返回 id（供 WMS ?layer_sql_id= 一类参数使用）。"""
    pg = load_pg_for_registry(config_path)
    schema = _ident(pg["schema"])
    tbl = _ident(pg["gis_wms_sql_table"])
    conn = _connect(pg)
    try:
        ensure_wms_sql_table(pg["schema"], pg["gis_wms_sql_table"], conn)
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    f"INSERT INTO {schema}.{tbl} (sql_text) VALUES (%s) RETURNING id",
                    (sql_text.strip(),),
                )
                row = cur.fetchone()
                if not row or row.get("id") is None:
                    raise RuntimeError("登记 WMS SQL 失败：未返回 id")
                return int(row["id"])
    finally:
        conn.close()


def build_river_names_geojson_wms_sql(pg: Dict[str, Any], river_names: List[str]) -> str:
    """与 _query_river_geometries_by_names 同源逻辑，输出按视口裁剪的几何列。"""
    cleaned = [str(x).strip() for x in river_names if str(x).strip()]
    if not cleaned:
        raise ValueError("river_names 为空")
    schema = _ident(pg["schema"])
    river_table = _ident(pg["river_table"])
    arr = _sql_text_array_literal(cleaned)
    return f"""
SELECT ST_Transform(t.geom_union, :srid) AS geom
FROM (
  SELECT
    COALESCE(NULLIF(TRIM(r.river_name), ''), NULLIF(TRIM(r.src_name), '')) AS river_name_key,
    ST_LineMerge(ST_UnaryUnion(ST_Collect(r.geom))) AS geom_union
  FROM {schema}.{river_table} r
  WHERE COALESCE(NULLIF(TRIM(r.river_name), ''), NULLIF(TRIM(r.src_name), '')) = ANY({arr})
  GROUP BY 1
) t
WHERE ST_Intersects(
  ST_Transform(t.geom_union, :srid),
  ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, :srid)
)
""".strip()


def build_region_rivers_scene_wms_sql(pg: Dict[str, Any], region_name: str, max_rivers: int) -> str:
    """与 _query_region_rivers_scene 同源：行政区边界 + 区内河流，合并为多行几何。"""
    name = str(region_name).strip()
    if not name:
        raise ValueError("region_name 为空")
    schema = _ident(pg["schema"])
    admin_table = _ident(pg["admin_table"])
    river_table = _ident(pg["river_table"])
    lim = max(1, min(int(max_rivers), 5000))
    lit = _sql_string_literal(name)
    admin_where = f"""
    COALESCE(adcode::text,'') = {lit}
    OR COALESCE(name,'') ILIKE {lit}
    OR COALESCE(city_name,'') ILIKE {lit}
    OR COALESCE(county_name,'') ILIKE {lit}
    OR COALESCE(name,'') ILIKE ('%%' || {lit} || '%%')
    OR COALESCE(city_name,'') ILIKE ('%%' || {lit} || '%%')
    OR COALESCE(county_name,'') ILIKE ('%%' || {lit} || '%%')
""".strip()
    return f"""
WITH region_match AS (
  SELECT ST_UnaryUnion(ST_Collect(geom)) AS geom
  FROM {schema}.{admin_table}
  WHERE
    {admin_where}
)
SELECT ST_Transform(u.g, :srid) AS geom
FROM (
  SELECT geom AS g
  FROM {schema}.{admin_table}
  WHERE
    {admin_where}
  ORDER BY id
  LIMIT 50
  UNION ALL
  SELECT t.geom_union AS g
  FROM (
    SELECT
      COALESCE(NULLIF(TRIM(r.river_name), ''), NULLIF(TRIM(r.src_name), '')) AS river_name_key,
      ST_LineMerge(ST_UnaryUnion(ST_Collect(r.geom))) AS geom_union,
      SUM(
        COALESCE(
          CASE WHEN to_jsonb(r) ? 'length_km' THEN (to_jsonb(r)->>'length_km')::double precision END,
          CASE WHEN to_jsonb(r) ? 'length_val' THEN (to_jsonb(r)->>'length_val')::double precision END,
          CASE WHEN to_jsonb(r) ? 'len_km' THEN (to_jsonb(r)->>'len_km')::double precision END,
          0
        )
      ) AS river_length_km
    FROM {schema}.{river_table} r
    JOIN region_match rm ON ST_Intersects(r.geom, rm.geom)
    GROUP BY 1
    ORDER BY river_length_km DESC
    LIMIT {lim}
  ) t
) u
WHERE u.g IS NOT NULL
  AND ST_Intersects(
    ST_Transform(u.g, :srid),
    ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, :srid)
  )
""".strip()


def _dollar_quote_literal(text: str) -> str:
    """用于在 SQL 中安全嵌入 JSON 等任意文本。"""
    if "$" not in text:
        return "$$" + text + "$$"
    tag = "wms"
    for _ in range(256):
        delim = f"${tag}$"
        if delim not in text:
            return f"{delim}{text}{delim}"
        tag += "x"
    raise ValueError("无法为嵌入文本生成 dollar 引用")


def build_partition_stats_wms_sql_for_rows(pg: Dict[str, Any], rows: List[Dict[str, Any]]) -> str:
    """站点降水列表（与 _query_partition_stats_from_ranking 相同字段）嵌入为 json_to_recordset。"""
    payload: List[Dict[str, Any]] = []
    for r in rows:
        payload.append(
            {
                "station_id": str(r.get("station_id") or ""),
                "lon": float(r.get("lon") or 0.0),
                "lat": float(r.get("lat") or 0.0),
                "rainfall_mm": float(r.get("rainfall_mm") or 0.0),
            }
        )
    js = json.dumps(payload, ensure_ascii=False)
    lit = _dollar_quote_literal(js) + "::json"
    schema = _ident(pg["schema"])
    return f"""
WITH s AS (
  SELECT *
  FROM json_to_recordset({lit}) AS x(
    station_id text,
    lon double precision,
    lat double precision,
    rainfall_mm double precision
  )
),
z AS (
  SELECT
    'haihe_zone_9'::text AS partition_layer,
    gid,
    zone_code::text AS zone_code,
    zone_name::text AS zone_name,
    geom
  FROM {schema}.haihe_zone_9
  UNION ALL
  SELECT
    'haihe_zone_11'::text AS partition_layer,
    gid,
    zone_code::text AS zone_code,
    zone_name::text AS zone_name,
    geom
  FROM {schema}.haihe_zone_11
),
agg AS (
  SELECT
    z.partition_layer,
    z.gid AS partition_id,
    COALESCE(z.zone_code::text, z.gid::text) AS partition_code,
    COALESCE(z.zone_name, z.gid::text) AS partition_name,
    SUM(s.rainfall_mm) AS station_precip_sum_mm,
    MAX(s.rainfall_mm) AS station_precip_max_mm,
    COUNT(*)::int AS station_count,
    z.geom
  FROM z
  JOIN s ON ST_Intersects(z.geom, ST_SetSRID(ST_Point(s.lon, s.lat), 4326))
  GROUP BY z.partition_layer, z.gid, z.zone_code, z.zone_name, z.geom
)
SELECT ST_Transform(agg.geom, :srid) AS geom
FROM agg
WHERE ST_Intersects(
  ST_Transform(agg.geom, :srid),
  ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, :srid)
)
""".strip()


def build_admin_boundaries_by_names_wms_sql(
    pg: Dict[str, Any],
    region_names: List[str],
    *,
    admin_table: str = "haihe_admin_division_bak",
) -> str:
    """与 _query_admin_boundaries_by_region_names 一致的匹配规则，输出区县面几何。"""
    cleaned = [str(x).strip() for x in region_names if str(x).strip()]
    if not cleaned:
        raise ValueError("region_names 为空")
    schema = _ident(pg["schema"])
    tbl = _ident(admin_table)
    arr_sql = "ARRAY[" + ",".join(_sql_string_literal(x) for x in cleaned) + "]::text[]"
    return f"""
WITH q AS (
  SELECT DISTINCT NULLIF(TRIM(unnest({arr_sql})), '') AS region_name
),
m AS (
  SELECT
    q.region_name AS query_name,
    a.adcode,
    a.name,
    a.level,
    a.geom,
    CASE
      WHEN COALESCE(a.name,'') = q.region_name THEN 1
      WHEN REPLACE(COALESCE(a.name,''), '市', '') = REPLACE(q.region_name, '市', '') THEN 2
      WHEN REPLACE(COALESCE(a.name,''), '区', '') = REPLACE(q.region_name, '区', '') THEN 3
      ELSE 99
    END AS hit_rank
  FROM q
  JOIN {schema}.{tbl} a
    ON q.region_name IS NOT NULL
   AND (
      COALESCE(a.name,'') = q.region_name
      OR REPLACE(COALESCE(a.name,''), '市', '') = REPLACE(q.region_name, '市', '')
      OR REPLACE(COALESCE(a.name,''), '区', '') = REPLACE(q.region_name, '区', '')
    )
  WHERE
    NOT (
      LOWER(COALESCE(a.level, '')) LIKE 'town%%'
      OR COALESCE(a.level, '') IN ('town', 'street', '乡镇', '街道')
    )
),
picked AS (
  SELECT DISTINCT ON (query_name)
    query_name, geom
  FROM m
  ORDER BY
    query_name,
    hit_rank,
    CASE
      WHEN LOWER(COALESCE(level,'')) IN ('district', 'county') OR COALESCE(level,'') IN ('区', '区县') THEN 1
      WHEN LOWER(COALESCE(level,'')) = 'city' OR COALESCE(level,'') IN ('市') THEN 2
      ELSE 9
    END,
    adcode
)
SELECT ST_Transform(picked.geom, :srid) AS geom
FROM picked
WHERE ST_Intersects(
  ST_Transform(picked.geom, :srid),
  ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, :srid)
)
""".strip()


def build_zone256_rivers_wms_sql(pg: Dict[str, Any], rows: List[Dict[str, Any]]) -> str:
    """
    站点降水落区对应的 246 分区并集，与全库河系求交，输出视口内河线几何（与分区统计同源点位嵌入）。
    """
    payload: List[Dict[str, Any]] = []
    for r in rows:
        payload.append(
            {
                "station_id": str(r.get("station_id") or ""),
                "lon": float(r.get("lon") or 0.0),
                "lat": float(r.get("lat") or 0.0),
                "rainfall_mm": float(r.get("rainfall_mm") or 0.0),
            }
        )
    js = json.dumps(payload, ensure_ascii=False)
    lit = _dollar_quote_literal(js) + "::json"
    schema = _ident(pg["schema"])
    river_table = _ident(pg["river_table"])
    zone_tbl = _ident(pg.get("zone256_table") or "haihe_246_zone")
    return f"""
WITH s AS (
  SELECT *
  FROM json_to_recordset({lit}) AS x(
    station_id text,
    lon double precision,
    lat double precision,
    rainfall_mm double precision
  )
),
hit_z AS (
  SELECT z.geom
  FROM {schema}.{zone_tbl} z
  JOIN s ON ST_Intersects(z.geom, ST_SetSRID(ST_Point(s.lon, s.lat), 4326))
),
union_zone AS (
  SELECT ST_UnaryUnion(ST_Collect(geom)) AS g FROM hit_z
),
rivers AS (
  SELECT
    COALESCE(NULLIF(TRIM(r.river_name), ''), NULLIF(TRIM(r.src_name), '')) AS river_key,
    ST_LineMerge(ST_UnaryUnion(ST_Collect(r.geom))) AS geom_union,
    SUM(
      COALESCE(
        CASE WHEN to_jsonb(r) ? 'length_km' THEN (to_jsonb(r)->>'length_km')::double precision END,
        CASE WHEN to_jsonb(r) ? 'length_val' THEN (to_jsonb(r)->>'length_val')::double precision END,
        CASE WHEN to_jsonb(r) ? 'len_km' THEN (to_jsonb(r)->>'len_km')::double precision END,
        0
      )
    ) AS river_length_km
  FROM {schema}.{river_table} r
  CROSS JOIN union_zone u
  WHERE u.g IS NOT NULL AND ST_Intersects(r.geom, u.g)
  GROUP BY 1
)
SELECT ST_Transform(rivers.geom_union, :srid) AS geom
FROM rivers
WHERE rivers.geom_union IS NOT NULL
  AND ST_Intersects(
    ST_Transform(rivers.geom_union, :srid),
    ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, :srid)
  )
""".strip()
