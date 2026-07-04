r"""检查河流表 geometry 字段是否可用。

用于定位：最近河流距离 None、所有缓冲半径命中 0 的问题。

运行前在 PowerShell 设置连接信息：
    $env:HHLY_DB_HOST="数据库地址"
    $env:HHLY_DB_PORT="端口"
    $env:HHLY_DB_NAME="库名"
    $env:HHLY_DB_USER="用户名"
    $env:HHLY_DB_PASSWORD="密码"

运行：
    python utils/inspect_river_table_geometry.py --table haihe_river_directed_full_v5
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor


DEFAULT_OUTPUT_DIR = r"C:\Users\gaozr\Downloads\24hourmindata_db_impact_output"
DEFAULT_SCHEMA = "public"
DEFAULT_TABLE = "haihe_river_directed_full_v5"


def _quote_ident(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(identifier or "")):
        raise ValueError(f"非法 SQL 标识符：{identifier!r}")
    return f'"{identifier}"'


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"缺少环境变量：{name}")
    return value


def _connect_db(args: argparse.Namespace):
    return psycopg2.connect(
        host=args.db_host,
        port=args.db_port,
        dbname=args.db_name,
        user=args.db_user,
        password=args.db_password,
        sslmode=args.db_sslmode,
        connect_timeout=args.db_connect_timeout,
    )


def _get_columns(cur, schema: str, table: str) -> list[dict]:
    cur.execute(
        """
        SELECT column_name, data_type, udt_name
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
    )
    return list(cur.fetchall())


def _get_geometry_columns(cur, schema: str, table: str, columns: list[dict]) -> list[str]:
    cur.execute(
        """
        SELECT f_geometry_column AS geom_col
        FROM geometry_columns
        WHERE f_table_schema = %s
          AND f_table_name = %s
        ORDER BY f_geometry_column
        """,
        (schema, table),
    )
    geom_cols = [row["geom_col"] for row in cur.fetchall()]

    for row in columns:
        if str(row.get("udt_name") or "").lower() == "geometry":
            name = row["column_name"]
            if name not in geom_cols:
                geom_cols.append(name)

    names = {row["column_name"] for row in columns}
    for name in ("geom", "geometry", "wkb_geometry", "the_geom", "shape", "line_geom"):
        if name in names and name not in geom_cols:
            geom_cols.append(name)
    return geom_cols


def _inspect_geometry_column(cur, schema: str, table: str, geom_col: str) -> dict:
    q_schema = _quote_ident(schema)
    q_table = _quote_ident(table)
    q_geom = _quote_ident(geom_col)
    cur.execute(
        f"""
        SELECT
            COUNT(*) AS row_count,
            COUNT({q_geom}) AS geom_count,
            COUNT(*) FILTER (WHERE {q_geom} IS NOT NULL AND ST_IsValid({q_geom})) AS valid_geom_count,
            ST_SRID({q_geom}) AS srid,
            GeometryType({q_geom}) AS geometry_type,
            ST_AsText(ST_Extent({q_geom})) AS extent_wkt
        FROM {q_schema}.{q_table}
        GROUP BY ST_SRID({q_geom}), GeometryType({q_geom})
        ORDER BY geom_count DESC NULLS LAST
        """
    )
    groups = list(cur.fetchall())
    total_geom_count = sum(int(row.get("geom_count") or 0) for row in groups)
    return {
        "column": geom_col,
        "total_geom_count": total_geom_count,
        "groups": groups,
    }


def inspect(args: argparse.Namespace) -> dict:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "inspect_river_table_geometry.json"

    conn = _connect_db(args)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            columns = _get_columns(cur, args.db_schema, args.table)
            geom_cols = _get_geometry_columns(cur, args.db_schema, args.table, columns)
            inspections = [
                _inspect_geometry_column(cur, args.db_schema, args.table, col)
                for col in geom_cols
            ]
    finally:
        conn.close()

    usable = [
        {"column": item["column"], "total_geom_count": item["total_geom_count"]}
        for item in inspections
        if int(item.get("total_geom_count") or 0) > 0
    ]
    report = {
        "status": "ok",
        "schema": args.db_schema,
        "table": args.table,
        "columns": columns,
        "geometry_columns": geom_cols,
        "usable_geometry_columns": usable,
        "inspections": inspections,
        "report_path": str(report_path),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检查河流表 geometry 字段")
    parser.add_argument("--table", default=os.getenv("HHLY_RIVER_TABLE_FULL", DEFAULT_TABLE))
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
    report = inspect(parse_args())
    print("河流表 geometry 字段检查完成")
    print(f"表：{report['schema']}.{report['table']}")
    print(f"识别到 geometry 字段：{report['geometry_columns']}")
    print(f"可用 geometry 字段：{report['usable_geometry_columns']}")
    print(f"报告：{report['report_path']}")
    if not report["geometry_columns"]:
        print("结论：没有识别到 geometry 字段。")
    elif not report["usable_geometry_columns"]:
        print("结论：识别到了 geometry 字段，但这些字段几何数量都是 0。")
    else:
        print(f"建议使用几何字段：{report['usable_geometry_columns'][0]['column']}")


if __name__ == "__main__":
    main()
