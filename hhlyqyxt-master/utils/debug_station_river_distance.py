r"""最小化调试：单个站点到河流表最近距离。

用途：绕开 24 小时聚合、临时表、多轮逻辑，直接验证：
- 给定站点 lon/lat；
- 给定河流表 geom；
- PostGIS 是否能算出最近河流与距离。

运行前请通过 HHLY_DB_* 环境变量设置数据库连接信息。
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Iterable

import psycopg2
from psycopg2.extras import RealDictCursor


DEFAULT_OUTPUT_DIR = r"C:\Users\gaozr\Downloads\24hourmindata_db_impact_output"
DEFAULT_SCHEMA = "public"
DEFAULT_TABLE = "haihe_river_directed_full_v5"
DEFAULT_GEOM_COLUMN = "geom"
DEFAULT_LON = 113.3617
DEFAULT_LAT = 36.2147
DEFAULT_SRID = 4326


def _quote_ident(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(identifier or "")):
        raise ValueError(f"非法 SQL 标识符：{identifier!r}")
    return f'"{identifier}"'


def _connect_db(args: argparse.Namespace):
    if not args.db_password:
        raise ValueError("缺少数据库密码：请设置 HHLY_DB_PASSWORD 或传入 --db-password")
    return psycopg2.connect(
        host=args.db_host,
        port=args.db_port,
        dbname=args.db_name,
        user=args.db_user,
        password=args.db_password,
        sslmode=args.db_sslmode,
        connect_timeout=args.db_connect_timeout,
    )


def _get_columns(cur, schema: str, table: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
        """,
        (schema, table),
    )
    return {row["column_name"] for row in cur.fetchall()}


def _pick_first_existing(columns: set[str], candidates: Iterable[str]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _river_name_sql_expr(columns: set[str], alias: str = "r") -> str:
    fields = [c for c in ("river_name", "rivername", "src_name", "name") if c in columns]
    if not fields:
        return "'未知'"
    prefix = f"{_quote_ident(alias)}."
    parts = [f"NULLIF(TRIM({prefix}{_quote_ident(c)}::text), '')" for c in fields]
    return f"COALESCE({', '.join(parts)}, '未知')"


def debug_distance(args: argparse.Namespace) -> dict:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "debug_station_river_distance.json"

    q_schema = _quote_ident(args.db_schema)
    q_table = _quote_ident(args.table)
    q_geom = _quote_ident(args.geom_column)

    conn = _connect_db(args)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            columns = _get_columns(cur, args.db_schema, args.table)
            id_col = _pick_first_existing(columns, ("objectid", "OBJECTID", "id", "gid"))
            id_expr = f"r.{_quote_ident(id_col)}::text" if id_col else "NULL::text"
            name_expr = _river_name_sql_expr(columns, alias="r")

            cur.execute(
                f"""
                SELECT
                    COUNT(*) AS row_count,
                    COUNT({q_geom}) AS geom_count,
                    COUNT(*) FILTER (WHERE {q_geom} IS NOT NULL AND NOT ST_IsEmpty({q_geom})) AS non_empty_count,
                    ST_AsText(ST_Extent({q_geom})) AS extent_wkt
                FROM {q_schema}.{q_table}
                """
            )
            table_overview = dict(cur.fetchone() or {})

            cur.execute(
                f"""
                WITH p AS (
                    SELECT ST_SetSRID(ST_MakePoint(%s, %s), %s) AS geom
                )
                SELECT
                    {id_expr} AS objectid,
                    {name_expr} AS river_name,
                    GeometryType(r.{q_geom}) AS geometry_type,
                    ST_SRID(r.{q_geom}) AS srid,
                    ST_Distance(r.{q_geom}, p.geom) AS degree_distance,
                    ST_Distance(r.{q_geom}::geography, p.geom::geography) / 1000.0 AS km_distance,
                    ST_DWithin(r.{q_geom}::geography, p.geom::geography, %s) AS within_buffer,
                    ST_AsText(ST_ClosestPoint(r.{q_geom}, p.geom)) AS closest_point_wkt
                FROM {q_schema}.{q_table} r, p
                WHERE r.{q_geom} IS NOT NULL
                  AND NOT ST_IsEmpty(r.{q_geom})
                ORDER BY r.{q_geom} <-> p.geom
                LIMIT %s
                """,
                (float(args.lon), float(args.lat), int(args.srid), float(args.buffer_km) * 1000.0, int(args.limit)),
            )
            nearest_rows = list(cur.fetchall())

            cur.execute(
                f"""
                WITH p AS (
                    SELECT ST_SetSRID(ST_MakePoint(%s, %s), %s) AS geom
                )
                SELECT COUNT(*) AS within_count
                FROM {q_schema}.{q_table} r, p
                WHERE r.{q_geom} IS NOT NULL
                  AND NOT ST_IsEmpty(r.{q_geom})
                  AND ST_DWithin(r.{q_geom}::geography, p.geom::geography, %s)
                """,
                (float(args.lon), float(args.lat), int(args.srid), float(args.buffer_km) * 1000.0),
            )
            within_count = int((cur.fetchone() or {}).get("within_count") or 0)
    finally:
        conn.close()

    for row in nearest_rows:
        if row.get("degree_distance") is not None:
            row["degree_distance"] = round(float(row["degree_distance"]), 8)
        if row.get("km_distance") is not None:
            row["km_distance"] = round(float(row["km_distance"]), 3)

    report = {
        "status": "ok",
        "params": {
            "schema": args.db_schema,
            "table": args.table,
            "geom_column": args.geom_column,
            "lon": args.lon,
            "lat": args.lat,
            "srid": args.srid,
            "buffer_km": args.buffer_km,
        },
        "table_overview": table_overview,
        "within_count": within_count,
        "nearest_rows": nearest_rows,
        "report_path": str(report_path),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="调试单站点到河流表最近距离")
    parser.add_argument("--lon", type=float, default=DEFAULT_LON)
    parser.add_argument("--lat", type=float, default=DEFAULT_LAT)
    parser.add_argument("--srid", type=int, default=DEFAULT_SRID)
    parser.add_argument("--buffer-km", type=float, default=30.0)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--table", default=os.getenv("HHLY_RIVER_TABLE_FULL", DEFAULT_TABLE))
    parser.add_argument("--geom-column", default=os.getenv("HHLY_RIVER_GEOM_COLUMN", DEFAULT_GEOM_COLUMN))
    parser.add_argument("--db-schema", default=os.getenv("HHLY_DB_SCHEMA", DEFAULT_SCHEMA))
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--db-host", default=os.getenv("HHLY_DB_HOST", ""))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("HHLY_DB_PORT", "5432")))
    parser.add_argument("--db-name", default=os.getenv("HHLY_DB_NAME", ""))
    parser.add_argument("--db-user", default=os.getenv("HHLY_DB_USER", ""))
    parser.add_argument("--db-password", default=os.getenv("HHLY_DB_PASSWORD", ""))
    parser.add_argument("--db-sslmode", default=os.getenv("HHLY_DB_SSLMODE", "disable"))
    parser.add_argument("--db-connect-timeout", type=int, default=int(os.getenv("HHLY_DB_CONNECT_TIMEOUT", "5")))
    args = parser.parse_args()
    for attr, env_name in (
        ("db_host", "HHLY_DB_HOST"),
        ("db_name", "HHLY_DB_NAME"),
        ("db_user", "HHLY_DB_USER"),
        ("db_password", "HHLY_DB_PASSWORD"),
    ):
        if not getattr(args, attr):
            raise ValueError(f"缺少参数 --{attr.replace('_', '-')} 或环境变量 {env_name}")
    return args


def main() -> None:
    report = debug_distance(parse_args())
    print("单站点最近河流距离调试完成")
    print(f"站点坐标：lon={report['params']['lon']}, lat={report['params']['lat']}")
    print(f"表范围：{report['table_overview'].get('extent_wkt')}")
    print(f"{report['params']['buffer_km']} km 内命中河段数：{report['within_count']}")
    print("最近河段：")
    for i, row in enumerate(report.get("nearest_rows", []), start=1):
        print(
            f"  {i}. river={row.get('river_name')}, objectid={row.get('objectid')}, "
            f"type={row.get('geometry_type')}, srid={row.get('srid')}, "
            f"km={row.get('km_distance')}, degree={row.get('degree_distance')}, "
            f"within={row.get('within_buffer')}, closest={row.get('closest_point_wkt')}"
        )
    print(f"报告：{report['report_path']}")


if __name__ == "__main__":
    main()
