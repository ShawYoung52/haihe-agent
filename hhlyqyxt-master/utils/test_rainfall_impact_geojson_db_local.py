r"""本地 DB 版测试：5 分钟 CSV -> 24 小时降水 -> PostGIS 真实河流 GeoJSON。

保留用途：
- 本地读取 5 分钟站点降水 CSV；
- 聚合为站点 24 小时累计雨量；
- 筛选达到暴雨阈值的站点；
- 用 PostGIS 完整河流表做 30km 直接影响河段查询；
- 用 river_directed_v5.pkl 做下游 50km 拓扑追踪；
- 回表输出真实河流 GeoJSON，给前端渲染。

运行：
    cd hhlyqyxt-master
    $env:HHLY_DB_PASSWORD="你的数据库密码"
    python utils/test_rainfall_impact_geojson_db_local.py

输出目录默认：
    C:\Users\gaozr\Downloads\24hourmindata_db_impact_output
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

from rainfall_impact_geojson import (  # noqa: E402
    _collect_downstream_impacts,
    _make_station_geojson,
    _station_record,
    aggregate_5min_station_pre_to_24h,
)

DEFAULT_CSV_PATH = r"C:\Users\gaozr\Downloads\24hourmindata.csv"
DEFAULT_GRAPH_PATH = r"E:\tj\line\result\river_directed_v5.pkl"
DEFAULT_OUTPUT_DIR = r"C:\Users\gaozr\Downloads\24hourmindata_db_impact_output"

DEFAULT_DB_HOST = "211.157.132.19"
DEFAULT_DB_PORT = 48091
DEFAULT_DB_NAME = "hhly"
DEFAULT_DB_USER = "postgres"
DEFAULT_DB_SCHEMA = "public"
DEFAULT_DB_SRID = 4326
DEFAULT_RIVER_TABLE_FULL = "haihe_river_directed_full_v5"
DEFAULT_RIVER_GEOM_COLUMN = "geom"
DEFAULT_DB_SSLMODE = "disable"
DEFAULT_DB_CONNECT_TIMEOUT = 5


def _env(name: str, default: Any) -> Any:
    return os.getenv(name, default)


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)


def _quote_ident(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(identifier or "")):
        raise ValueError(f"非法 SQL 标识符：{identifier!r}")
    return f'"{identifier}"'


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


def _station_sql_rows(stations: list[dict]) -> list[tuple]:
    rows = []
    for station in stations:
        lon = float(station["lon"])
        lat = float(station["lat"])
        rows.append(
            (
                str(station.get("station_id") or ""),
                str(station.get("station_name") or ""),
                str(station.get("province") or ""),
                str(station.get("city") or ""),
                str(station.get("cnty") or ""),
                str(station.get("town") or ""),
                lon,
                lat,
                float(station.get("rain_24h") or 0.0),
                int(station.get("obs_count") or 0),
                str(station.get("start_time") or ""),
                str(station.get("end_time") or ""),
                lon,
                lat,
            )
        )
    return rows


def _station_values_template(srid: int) -> str:
    return (
        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
        f"ST_SetSRID(ST_MakePoint(%s,%s),{int(srid)}))"
    )


def _station_cte_columns() -> str:
    return """
        station_id, station_name, province, city, cnty, town,
        lon, lat, rain_24h, obs_count, start_time, end_time, geom
    """


def _validate_geometry_column(columns: set[str], geom_column: str) -> str:
    if geom_column in columns:
        return geom_column
    fallback = _pick_first_existing(columns, ("geom", "geometry", "wkb_geometry", "the_geom"))
    if fallback:
        return fallback
    raise ValueError(f"河流表未找到几何字段：{geom_column}")


def _query_direct_rows(
    cur,
    *,
    schema: str,
    table: str,
    columns: set[str],
    geom_column: str,
    srid: int,
    stations: list[dict],
    buffer_km: float,
) -> list[dict]:
    if not stations:
        return []

    geom_col = _validate_geometry_column(columns, geom_column)
    objectid_col = _pick_first_existing(columns, ("objectid", "OBJECTID", "id", "gid"))
    id_col = _pick_first_existing(columns, ("id", "gid"))
    river_expr = _river_name_sql_expr(columns, alias="r")

    q_schema = _quote_ident(schema)
    q_table = _quote_ident(table)
    q_geom = _quote_ident(geom_col)
    objectid_expr = f"r.{_quote_ident(objectid_col)}::text" if objectid_col else "NULL::text"
    id_expr = f"r.{_quote_ident(id_col)}::text" if id_col else objectid_expr

    sql = f"""
        WITH stations ({_station_cte_columns()}) AS (VALUES %s)
        SELECT
            {id_expr} AS id,
            {objectid_expr} AS objectid,
            {river_expr} AS river_name,
            ST_AsGeoJSON(r.{q_geom}) AS geom_json,
            ST_Length(r.{q_geom}::geography) / 1000.0 AS length_km,
            MIN(ST_Distance(r.{q_geom}::geography, s.geom::geography) / 1000.0) AS min_station_distance_km,
            COUNT(DISTINCT s.station_id) AS trigger_station_count,
            jsonb_agg(
                DISTINCT jsonb_build_object(
                    'station_id', s.station_id,
                    'station_name', s.station_name,
                    'province', s.province,
                    'city', s.city,
                    'cnty', s.cnty,
                    'town', s.town,
                    'lon', s.lon,
                    'lat', s.lat,
                    'rain_24h', s.rain_24h,
                    'distance_to_river_km', ROUND((ST_Distance(r.{q_geom}::geography, s.geom::geography) / 1000.0)::numeric, 3)
                )
            ) AS trigger_stations
        FROM {q_schema}.{q_table} r
        JOIN stations s
          ON ST_DWithin(r.{q_geom}::geography, s.geom::geography, %s)
        WHERE r.{q_geom} IS NOT NULL
          AND NOT ST_IsEmpty(r.{q_geom})
        GROUP BY r.{q_geom}, {id_expr}, {objectid_expr}, {river_expr}
        ORDER BY min_station_distance_km, river_name, objectid
    """
    rows = _station_sql_rows(stations)
    result = execute_values(
        cur,
        sql,
        rows,
        template=_station_values_template(srid),
        page_size=max(len(rows), 1),
        fetch=True,
    )
    return list(result or [])


def _query_downstream_rows(
    cur,
    *,
    schema: str,
    table: str,
    columns: set[str],
    geom_column: str,
    objectids: Iterable[str],
    river_names: Iterable[str],
    allow_name_fallback: bool,
) -> tuple[list[dict], str]:
    objectid_values = sorted({str(x).strip() for x in objectids if str(x).strip()})
    name_values = sorted({str(x).strip() for x in river_names if str(x).strip()})

    geom_col = _validate_geometry_column(columns, geom_column)
    objectid_col = _pick_first_existing(columns, ("objectid", "OBJECTID", "id", "gid"))
    id_col = _pick_first_existing(columns, ("id", "gid"))
    river_expr = _river_name_sql_expr(columns, alias="r")

    where_parts = []
    params: dict[str, Any] = {}
    match_mode = "objectid"

    if objectid_values and objectid_col:
        where_parts.append(f"r.{_quote_ident(objectid_col)}::text = ANY(%(objectids)s)")
        params["objectids"] = objectid_values
    if objectid_values and id_col and id_col != objectid_col:
        where_parts.append(f"r.{_quote_ident(id_col)}::text = ANY(%(objectids)s)")
        params["objectids"] = objectid_values

    if allow_name_fallback and name_values:
        where_parts.append(f"{river_expr} = ANY(%(river_names)s)")
        params["river_names"] = name_values
        match_mode = "objectid_or_name_fallback" if objectid_values else "river_name_fallback"

    if not where_parts:
        return [], "none"

    q_schema = _quote_ident(schema)
    q_table = _quote_ident(table)
    q_geom = _quote_ident(geom_col)
    objectid_expr = f"r.{_quote_ident(objectid_col)}::text" if objectid_col else "NULL::text"
    id_expr = f"r.{_quote_ident(id_col)}::text" if id_col else objectid_expr
    where_sql = " OR ".join(f"({part})" for part in where_parts)

    cur.execute(
        f"""
        SELECT
            {id_expr} AS id,
            {objectid_expr} AS objectid,
            {river_expr} AS river_name,
            ST_AsGeoJSON(r.{q_geom}) AS geom_json,
            ST_Length(r.{q_geom}::geography) / 1000.0 AS length_km
        FROM {q_schema}.{q_table} r
        WHERE r.{q_geom} IS NOT NULL
          AND NOT ST_IsEmpty(r.{q_geom})
          AND ({where_sql})
        ORDER BY river_name, objectid
        """,
        params,
    )
    return list(cur.fetchall()), match_mode


def _row_geometry(row: dict) -> dict | None:
    geom = row.get("geom_json")
    if not geom:
        return None
    try:
        return json.loads(geom) if isinstance(geom, str) else geom
    except Exception:
        return None


def _build_direct_feature(row: dict) -> dict | None:
    geometry = _row_geometry(row)
    if not geometry:
        return None
    trigger_stations = row.get("trigger_stations") or []
    min_distance = row.get("min_station_distance_km")
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "impact_type": "direct_buffer",
            "river_name": row.get("river_name"),
            "id": row.get("id"),
            "objectid": row.get("objectid"),
            "length_km": round(float(row.get("length_km") or 0.0), 3),
            "min_station_distance_km": round(float(min_distance), 3) if min_distance is not None else None,
            "trigger_station_count": int(row.get("trigger_station_count") or len(trigger_stations)),
            "trigger_stations": trigger_stations,
            "geometry_source": "postgis",
        },
    }


def _build_downstream_feature(row: dict, downstream_map: dict[str, dict], *, match_mode: str) -> dict | None:
    geometry = _row_geometry(row)
    if not geometry:
        return None
    river_name = str(row.get("river_name") or "未知")
    info = downstream_map.get(river_name, {})
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "impact_type": "downstream_50km",
            "river_name": river_name,
            "id": row.get("id"),
            "objectid": row.get("objectid"),
            "length_km": round(float(row.get("length_km") or 0.0), 3),
            "min_downstream_distance_km": info.get("min_distance_km"),
            "source_rivers": info.get("source_rivers", []),
            "geometry_source": "postgis",
            "geometry_match_mode": match_mode,
        },
    }


def _write_empty_result(
    *,
    station_24h_df: pd.DataFrame,
    river_geojson_path: Path,
    station_geojson_path: Path,
    top_csv_path: Path,
    summary_path: Path,
) -> dict:
    river_geojson = {"type": "FeatureCollection", "features": []}
    river_geojson_path.write_text(json.dumps(river_geojson, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "status": "ok",
        "message": "没有站点达到降雨阈值，未生成影响河流。",
        "station_summary": {
            "total_station_count": int(len(station_24h_df)),
            "impact_station_count": 0,
            "max_rain_24h": float(station_24h_df["rain_24h"].max() or 0.0),
        },
        "outputs": {
            "river_geojson": str(river_geojson_path),
            "station_geojson": str(station_geojson_path),
            "top_stations_csv": str(top_csv_path),
            "summary_json": str(summary_path),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def build_db_test_outputs(args: argparse.Namespace) -> dict:
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    station_24h_df = aggregate_5min_station_pre_to_24h(args.csv)
    impact_df = station_24h_df[
        (station_24h_df["rain_24h"] >= args.rain_threshold_mm)
        & station_24h_df["lon"].notna()
        & station_24h_df["lat"].notna()
    ].copy()
    impact_stations = [_station_record(row) for _, row in impact_df.iterrows()]

    river_geojson_path = output / "impact_rivers_postgis.geojson"
    station_geojson_path = output / "impact_stations.geojson"
    top_csv_path = output / "rain24h_top_stations.csv"
    summary_path = output / "summary.json"

    station_geojson_path.write_text(
        json.dumps(_make_station_geojson(impact_stations), ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    station_24h_df.head(max(args.top_station_limit, 0)).to_csv(
        top_csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    if not impact_stations:
        return _write_empty_result(
            station_24h_df=station_24h_df,
            river_geojson_path=river_geojson_path,
            station_geojson_path=station_geojson_path,
            top_csv_path=top_csv_path,
            summary_path=summary_path,
        )

    conn = _connect_db(args)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            columns = _get_table_columns(cur, schema=args.db_schema, table=args.river_table_full)
            if not columns:
                raise ValueError(f"未找到河流表：{args.db_schema}.{args.river_table_full}")

            direct_rows = _query_direct_rows(
                cur,
                schema=args.db_schema,
                table=args.river_table_full,
                columns=columns,
                geom_column=args.river_geom_column,
                srid=args.db_srid,
                stations=impact_stations,
                buffer_km=args.station_buffer_km,
            )
            direct_rivers = sorted({str(r.get("river_name") or "").strip() for r in direct_rows if r.get("river_name")})
            direct_objectids = {str(r.get("objectid") or "").strip() for r in direct_rows if r.get("objectid")}

            downstream_map, downstream_objectids = _collect_downstream_impacts(
                direct_rivers,
                downstream_km=args.downstream_km,
                graph_path=args.graph,
            )
            downstream_objectids = {x for x in downstream_objectids if x not in direct_objectids}

            downstream_rows, match_mode = _query_downstream_rows(
                cur,
                schema=args.db_schema,
                table=args.river_table_full,
                columns=columns,
                geom_column=args.river_geom_column,
                objectids=downstream_objectids,
                river_names=downstream_map.keys(),
                allow_name_fallback=args.allow_name_fallback,
            )
    finally:
        conn.close()

    features = []
    seen = set()
    for row in direct_rows:
        feature = _build_direct_feature(row)
        if not feature:
            continue
        key = (feature["properties"].get("objectid"), feature["properties"].get("river_name"), "direct")
        if key not in seen:
            seen.add(key)
            features.append(feature)

    for row in downstream_rows:
        objectid = str(row.get("objectid") or "").strip()
        if objectid and objectid in direct_objectids:
            continue
        feature = _build_downstream_feature(row, downstream_map, match_mode=match_mode)
        if not feature:
            continue
        key = (feature["properties"].get("objectid"), feature["properties"].get("river_name"), "downstream")
        if key not in seen:
            seen.add(key)
            features.append(feature)

    features.sort(
        key=lambda f: (
            0 if f["properties"].get("impact_type") == "direct_buffer" else 1,
            f["properties"].get("river_name") or "",
            f["properties"].get("objectid") or "",
        )
    )
    river_geojson_path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )

    downstream_river_list = sorted(downstream_map.values(), key=lambda x: x["river_name"])
    summary = {
        "status": "ok",
        "params": {
            "csv_path": args.csv,
            "graph_path": args.graph,
            "db_host": args.db_host,
            "db_port": args.db_port,
            "db_name": args.db_name,
            "db_schema": args.db_schema,
            "river_table_full": args.river_table_full,
            "river_geom_column": args.river_geom_column,
            "rain_threshold_mm": args.rain_threshold_mm,
            "station_buffer_km": args.station_buffer_km,
            "downstream_km": args.downstream_km,
            "allow_name_fallback": args.allow_name_fallback,
        },
        "time_range": {
            "start_time": _json_default(station_24h_df["start_time"].min()),
            "end_time": _json_default(station_24h_df["end_time"].max()),
        },
        "station_summary": {
            "total_station_count": int(len(station_24h_df)),
            "impact_station_count": int(len(impact_stations)),
            "max_rain_24h": float(station_24h_df["rain_24h"].max() or 0.0),
        },
        "river_summary": {
            "direct_segment_count": len(direct_rows),
            "direct_river_count": len(direct_rivers),
            "downstream_river_count": len(downstream_map),
            "downstream_db_segment_count": len(downstream_rows),
            "geojson_feature_count": len(features),
            "downstream_geometry_match_mode": match_mode,
        },
        "direct_rivers": direct_rivers,
        "direct_segments": [
            {
                "river_name": row.get("river_name"),
                "objectid": row.get("objectid"),
                "min_station_distance_km": round(float(row.get("min_station_distance_km") or 0.0), 3),
                "trigger_station_count": int(row.get("trigger_station_count") or 0),
            }
            for row in direct_rows
        ],
        "downstream_rivers": downstream_river_list,
        "outputs": {
            "river_geojson": str(river_geojson_path),
            "station_geojson": str(station_geojson_path),
            "top_stations_csv": str(top_csv_path),
            "summary_json": str(summary_path),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DB 版本地测试：24 小时降水影响河流 GeoJSON")
    parser.add_argument("--csv", default=DEFAULT_CSV_PATH, help="5 分钟降水 CSV 路径")
    parser.add_argument("--graph", default=DEFAULT_GRAPH_PATH, help="河网拓扑 pkl 路径")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="输出目录")
    parser.add_argument("--rain-threshold-mm", type=float, default=50.0, help="暴雨阈值，默认 50mm")
    parser.add_argument("--station-buffer-km", type=float, default=30.0, help="站点缓冲半径，默认 30km")
    parser.add_argument("--downstream-km", type=float, default=50.0, help="下游追踪距离，默认 50km")
    parser.add_argument("--top-station-limit", type=int, default=100, help="累计雨量 TOP N 输出")
    parser.add_argument("--allow-name-fallback", action="store_true", help="objectid 不匹配时，允许按河名回查下游几何。")
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
    parser.add_argument("--river-geom-column", default=_env("HHLY_RIVER_GEOM_COLUMN", DEFAULT_RIVER_GEOM_COLUMN))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = build_db_test_outputs(args)

    print("DB 版本地测试完成")
    print(f"时间范围：{summary['time_range']['start_time']} -> {summary['time_range']['end_time']}")
    print(f"总站数：{summary['station_summary']['total_station_count']}")
    print(f"触发站数：{summary['station_summary']['impact_station_count']}")
    print(f"最大24小时雨量：{summary['station_summary']['max_rain_24h']} mm")
    print(f"直接影响河段：{summary['river_summary']['direct_segment_count']}")
    print(f"直接影响河流：{summary['river_summary']['direct_river_count']}")
    print(f"下游影响河流：{summary['river_summary']['downstream_river_count']}")
    print(f"下游DB河段：{summary['river_summary']['downstream_db_segment_count']}")
    print(f"GeoJSON要素数：{summary['river_summary']['geojson_feature_count']}")
    if summary.get("direct_segments"):
        print("直接影响河段明细：")
        for row in summary["direct_segments"]:
            print(
                f"  - {row['river_name']} objectid={row['objectid']} "
                f"distance={row['min_station_distance_km']}km stations={row['trigger_station_count']}"
            )
    print("输出文件：")
    for name, path in summary["outputs"].items():
        print(f"  - {name}: {path}")


if __name__ == "__main__":
    main()
