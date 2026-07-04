r"""诊断 24 小时降水影响河流没有命中的原因。

当 test_rainfall_impact_geojson_db_local.py 输出：
    直接影响河段：0
    GeoJSON要素数：0

优先运行本脚本，它会检查：
1. CSV 聚合后达到阈值的触发站；
2. PostGIS 完整河流表字段、数量、范围；
3. 每个触发站距离最近河流的距离；
4. 不同缓冲半径下可以命中的河段数。

运行：
    cd hhlyqyxt-master
    $env:HHLY_DB_PASSWORD="你的数据库密码"
    python utils/diagnose_rainfall_impact_nohit.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values


CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rainfall_impact_geojson import aggregate_5min_station_pre_to_24h  # noqa: E402


DEFAULT_CSV_PATH = r"C:\Users\gaozr\Downloads\24hourmindata.csv"
DEFAULT_OUTPUT_DIR = r"C:\Users\gaozr\Downloads\24hourmindata_db_impact_output"
DEFAULT_DB_HOST = "211.157.132.19"
DEFAULT_DB_PORT = 48091
DEFAULT_DB_NAME = "hhly"
DEFAULT_DB_USER = "postgres"
DEFAULT_DB_SCHEMA = "public"
DEFAULT_DB_SRID = 4326
DEFAULT_RIVER_TABLE_FULL = "haihe_river_directed_full_v5"
DEFAULT_DB_SSLMODE = "disable"
DEFAULT_DB_CONNECT_TIMEOUT = 5


def _env(name: str, default: Any) -> Any:
    return os.getenv(name, default)


def _quote_ident(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(identifier or "")):
        raise ValueError(f"非法 SQL 标识符：{identifier!r}")
    return f'"{identifier}"'


def _pick_first_existing(columns: set[str], candidates: Iterable[str]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _river_name_sql_expr(columns: set[str], alias: str | None = None) -> str:
    fields = [
        c for c in ("river_name", "rivername", "src_name", "name")
        if c in columns
    ]
    if not fields:
        raise ValueError("河流表未找到河名字段：river_name/rivername/src_name/name")
    prefix = f"{_quote_ident(alias)}." if alias else ""
    parts = [f"NULLIF(TRIM({prefix}{_quote_ident(c)}::text), '')" for c in fields]
    return f"COALESCE({', '.join(parts)}, '未知')"


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)


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


def _get_table_columns(cur, *, schema: str, table: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
        """,
        (schema, table),
    )
    return {str(row["column_name"]) for row in cur.fetchall()}


def _create_temp_station_table(cur, stations: list[dict], *, srid: int) -> None:
    cur.execute(
        f"""
        CREATE TEMP TABLE tmp_diag_stations (
            station_id TEXT,
            station_name TEXT,
            province TEXT,
            city TEXT,
            cnty TEXT,
            town TEXT,
            lon DOUBLE PRECISION,
            lat DOUBLE PRECISION,
            rain_24h DOUBLE PRECISION,
            geom geometry(Point, {int(srid)})
        ) ON COMMIT DROP
        """
    )
    rows = []
    for s in stations:
        rows.append(
            (
                str(s.get("station_id") or ""),
                str(s.get("station_name") or ""),
                str(s.get("province") or ""),
                str(s.get("city") or ""),
                str(s.get("cnty") or ""),
                str(s.get("town") or ""),
                float(s["lon"]),
                float(s["lat"]),
                float(s.get("rain_24h") or 0.0),
                float(s["lon"]),
                float(s["lat"]),
            )
        )
    execute_values(
        cur,
        """
        INSERT INTO tmp_diag_stations (
            station_id, station_name, province, city, cnty, town, lon, lat, rain_24h, geom
        ) VALUES %s
        """,
        rows,
        template=(
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            f"ST_SetSRID(ST_MakePoint(%s,%s),{int(srid)}))"
        ),
    )


def _station_record(row: pd.Series) -> dict:
    fields = [
        "station_id",
        "station_name",
        "province",
        "city",
        "cnty",
        "town",
        "lon",
        "lat",
        "rain_24h",
        "obs_count",
        "valid_pre_count",
        "start_time",
        "end_time",
    ]
    out = {}
    for f in fields:
        if f not in row.index:
            continue
        value = row.get(f)
        if isinstance(value, pd.Timestamp):
            out[f] = value.strftime("%Y-%m-%d %H:%M:%S")
        elif pd.isna(value):
            out[f] = None
        elif hasattr(value, "item"):
            out[f] = value.item()
        else:
            out[f] = value
    return out


def _query_table_overview(cur, *, schema: str, table: str, geom_col: str) -> dict:
    q_schema = _quote_ident(schema)
    q_table = _quote_ident(table)
    q_geom = _quote_ident(geom_col)
    cur.execute(
        f"""
        SELECT
            COUNT(*) AS row_count,
            COUNT({q_geom}) AS geom_count,
            ST_SRID({q_geom}) AS srid,
            ST_AsText(ST_Extent({q_geom})) AS extent_wkt
        FROM {q_schema}.{q_table}
        WHERE {q_geom} IS NOT NULL
        GROUP BY ST_SRID({q_geom})
        ORDER BY geom_count DESC
        LIMIT 5
        """
    )
    return {"srid_groups": list(cur.fetchall())}


def _query_nearest_rivers(
    cur,
    *,
    schema: str,
    table: str,
    columns: set[str],
    nearest_limit: int,
) -> list[dict]:
    geom_col = _pick_first_existing(columns, ("geom", "geometry"))
    if not geom_col:
        raise ValueError(f"{schema}.{table} 未找到 geom/geometry 字段")
    id_col = _pick_first_existing(columns, ("id", "gid"))
    objectid_col = _pick_first_existing(columns, ("objectid", "OBJECTID", "id", "gid"))
    river_expr = _river_name_sql_expr(columns, alias="r")
    q_schema = _quote_ident(schema)
    q_table = _quote_ident(table)
    q_geom = _quote_ident(geom_col)
    id_expr = f"r.{_quote_ident(id_col)}::text" if id_col else "NULL::text"
    objectid_expr = f"r.{_quote_ident(objectid_col)}::text" if objectid_col else id_expr

    cur.execute(
        f"""
        SELECT
            s.station_id,
            s.station_name,
            s.province,
            s.city,
            s.cnty,
            s.town,
            s.lon,
            s.lat,
            s.rain_24h,
            near.id,
            near.objectid,
            near.river_name,
            near.distance_km
        FROM tmp_diag_stations s
        CROSS JOIN LATERAL (
            SELECT
                {id_expr} AS id,
                {objectid_expr} AS objectid,
                {river_expr} AS river_name,
                ST_Distance(r.{q_geom}::geography, s.geom::geography) / 1000.0 AS distance_km
            FROM {q_schema}.{q_table} r
            WHERE r.{q_geom} IS NOT NULL
            ORDER BY ST_Distance(r.{q_geom}::geography, s.geom::geography)
            LIMIT %s
        ) near
        ORDER BY s.rain_24h DESC, s.station_id, near.distance_km
        """,
        (int(nearest_limit),),
    )
    rows = list(cur.fetchall())
    for row in rows:
        if row.get("distance_km") is not None:
            row["distance_km"] = round(float(row["distance_km"]), 3)
    return rows


def _query_buffer_counts(
    cur,
    *,
    schema: str,
    table: str,
    columns: set[str],
    buffers_km: list[float],
) -> list[dict]:
    geom_col = _pick_first_existing(columns, ("geom", "geometry"))
    if not geom_col:
        raise ValueError(f"{schema}.{table} 未找到 geom/geometry 字段")
    q_schema = _quote_ident(schema)
    q_table = _quote_ident(table)
    q_geom = _quote_ident(geom_col)
    results = []
    for buffer_km in buffers_km:
        cur.execute(
            f"""
            SELECT COUNT(DISTINCT r.ctid) AS segment_count
            FROM {q_schema}.{q_table} r
            JOIN tmp_diag_stations s
              ON ST_DWithin(r.{q_geom}::geography, s.geom::geography, %s)
            WHERE r.{q_geom} IS NOT NULL
            """,
            (float(buffer_km) * 1000.0,),
        )
        row = cur.fetchone() or {}
        results.append({"buffer_km": float(buffer_km), "segment_count": int(row.get("segment_count") or 0)})
    return results


def diagnose(args: argparse.Namespace) -> dict:
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    station_24h_df = aggregate_5min_station_pre_to_24h(args.csv)
    impact_df = station_24h_df[
        (station_24h_df["rain_24h"] >= args.rain_threshold_mm)
        & station_24h_df["lon"].notna()
        & station_24h_df["lat"].notna()
    ].copy()
    impact_stations = [_station_record(row) for _, row in impact_df.iterrows()]

    nearest_csv_path = output / "diagnose_nearest_rivers.csv"
    report_path = output / "diagnose_nohit_report.json"

    report: dict[str, Any] = {
        "status": "ok",
        "params": {
            "csv_path": args.csv,
            "db_host": args.db_host,
            "db_port": args.db_port,
            "db_name": args.db_name,
            "db_schema": args.db_schema,
            "river_table_full": args.river_table_full,
            "rain_threshold_mm": args.rain_threshold_mm,
            "buffers_km": args.buffers_km,
            "nearest_limit": args.nearest_limit,
        },
        "station_summary": {
            "total_station_count": int(len(station_24h_df)),
            "impact_station_count": int(len(impact_stations)),
            "max_rain_24h": float(station_24h_df["rain_24h"].max() or 0.0),
        },
        "impact_stations": impact_stations,
    }

    if not impact_stations:
        report["conclusion"] = "没有站点达到阈值。"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        return report

    conn = _connect_db(args)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            columns = _get_table_columns(cur, schema=args.db_schema, table=args.river_table_full)
            report["table_columns"] = sorted(columns)
            if not columns:
                raise ValueError(f"未找到河流表：{args.db_schema}.{args.river_table_full}")
            geom_col = _pick_first_existing(columns, ("geom", "geometry"))
            if not geom_col:
                raise ValueError(f"{args.db_schema}.{args.river_table_full} 未找到 geom/geometry 字段")
            report["geometry_column"] = geom_col
            report["table_overview"] = _query_table_overview(
                cur,
                schema=args.db_schema,
                table=args.river_table_full,
                geom_col=geom_col,
            )

            _create_temp_station_table(cur, impact_stations, srid=args.db_srid)
            nearest_rows = _query_nearest_rivers(
                cur,
                schema=args.db_schema,
                table=args.river_table_full,
                columns=columns,
                nearest_limit=args.nearest_limit,
            )
            buffer_counts = _query_buffer_counts(
                cur,
                schema=args.db_schema,
                table=args.river_table_full,
                columns=columns,
                buffers_km=args.buffers_km,
            )
    finally:
        conn.close()

    report["nearest_rivers"] = nearest_rows[: args.nearest_limit * max(1, len(impact_stations))]
    report["buffer_counts"] = buffer_counts

    pd.DataFrame(nearest_rows).to_csv(nearest_csv_path, index=False, encoding="utf-8-sig")

    min_dist = None
    if nearest_rows:
        distances = [float(row["distance_km"]) for row in nearest_rows if row.get("distance_km") is not None]
        min_dist = min(distances) if distances else None
    report["min_nearest_distance_km"] = min_dist

    default_buffer_count = next((x["segment_count"] for x in buffer_counts if abs(x["buffer_km"] - 30.0) < 1e-9), None)
    if default_buffer_count == 0 and min_dist is not None:
        if min_dist > 30.0:
            report["conclusion"] = f"当前触发站最近河流距离约 {min_dist:.3f} km，超过 30km，所以直接影响河段为 0 属于筛选结果。"
        else:
            report["conclusion"] = "最近河流距离小于等于 30km 但 ST_DWithin 仍为 0，优先检查 geom SRID、经纬度顺序、geometry 是否有效。"
    elif default_buffer_count and default_buffer_count > 0:
        report["conclusion"] = "30km 缓冲区可命中河段；若主脚本仍为 0，需检查主脚本字段选择或连接参数。"
    else:
        report["conclusion"] = "未能判断，请查看 nearest_rivers 和 table_overview。"

    report["outputs"] = {
        "nearest_rivers_csv": str(nearest_csv_path),
        "diagnose_report_json": str(report_path),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="诊断降水影响河流直接命中为 0 的原因")
    parser.add_argument("--csv", default=DEFAULT_CSV_PATH)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--rain-threshold-mm", type=float, default=50.0)
    parser.add_argument("--nearest-limit", type=int, default=10)
    parser.add_argument(
        "--buffers-km",
        type=float,
        nargs="+",
        default=[10.0, 20.0, 30.0, 50.0, 80.0, 100.0, 150.0],
        help="逐个测试这些缓冲半径下命中的河段数。",
    )

    parser.add_argument("--db-host", default=_env("HHLY_DB_HOST", DEFAULT_DB_HOST))
    parser.add_argument("--db-port", type=int, default=int(_env("HHLY_DB_PORT", DEFAULT_DB_PORT)))
    parser.add_argument("--db-name", default=_env("HHLY_DB_NAME", DEFAULT_DB_NAME))
    parser.add_argument("--db-user", default=_env("HHLY_DB_USER", DEFAULT_DB_USER))
    parser.add_argument("--db-password", default=_env("HHLY_DB_PASSWORD", ""))
    parser.add_argument("--db-schema", default=_env("HHLY_DB_SCHEMA", DEFAULT_DB_SCHEMA))
    parser.add_argument("--db-srid", type=int, default=int(_env("HHLY_DB_SRID", DEFAULT_DB_SRID)))
    parser.add_argument("--db-sslmode", default=_env("HHLY_DB_SSLMODE", DEFAULT_DB_SSLMODE))
    parser.add_argument("--db-connect-timeout", type=int, default=int(_env("HHLY_DB_CONNECT_TIMEOUT", DEFAULT_DB_CONNECT_TIMEOUT)))
    parser.add_argument("--river-table-full", default=_env("HHLY_RIVER_TABLE_FULL", DEFAULT_RIVER_TABLE_FULL))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = diagnose(args)
    print("诊断完成")
    print(f"总站数：{report['station_summary']['total_station_count']}")
    print(f"触发站数：{report['station_summary']['impact_station_count']}")
    print(f"最大24小时雨量：{report['station_summary']['max_rain_24h']} mm")
    print(f"最近河流距离：{report.get('min_nearest_distance_km')} km")
    print("不同缓冲半径命中河段数：")
    for row in report.get("buffer_counts", []):
        print(f"  - {row['buffer_km']} km: {row['segment_count']}")
    print(f"结论：{report.get('conclusion')}")
    for name, path in report.get("outputs", {}).items():
        print(f"  - {name}: {path}")


if __name__ == "__main__":
    main()
