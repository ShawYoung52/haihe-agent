r"""本地 DB 版测试入口：5 分钟 CSV -> 24 小时降水 -> 影响河流 GeoJSON。

只保留这一个入口文件。数据库连接不走 utils.db 的旧默认库，而是使用本文件参数/环境变量。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import pandas as pd
from sqlalchemy import create_engine

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for p in (PROJECT_ROOT, CURRENT_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import rainfall_impact_geojson as rig  # noqa: E402

DEFAULT_CSV_PATH = r"C:\Users\gaozr\Downloads\24hourmindata.csv"
DEFAULT_GRAPH_PATH = r"E:\tj\line\result\river_directed_v5.pkl"
DEFAULT_OUTPUT_DIR = r"C:\Users\gaozr\Downloads\24hourmindata_db_impact_output"
DEFAULT_DB_HOST = "211.157.132.19"
DEFAULT_DB_PORT = 48091
DEFAULT_DB_NAME = "hhly"
DEFAULT_DB_USER = "postgres"
DEFAULT_DB_SCHEMA = "public"
DEFAULT_RIVER_TABLE = "haihe_river_directed_full_v5"


def json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)


def make_engine(args: argparse.Namespace):
    if not args.db_password:
        raise ValueError("缺少数据库密码：请设置 HHLY_DB_PASSWORD 或传入 --db-password")
    password = quote_plus(str(args.db_password))
    url = f"postgresql+psycopg2://{args.db_user}:{password}@{args.db_host}:{args.db_port}/{args.db_name}"
    return create_engine(url, connect_args={"sslmode": args.db_sslmode, "connect_timeout": args.db_connect_timeout})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="真实河流 24 小时降水影响 GeoJSON")
    parser.add_argument("--csv", default=DEFAULT_CSV_PATH)
    parser.add_argument("--graph", default=DEFAULT_GRAPH_PATH)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--rain-threshold-mm", type=float, default=50.0)
    parser.add_argument("--station-buffer-km", type=float, default=30.0)
    parser.add_argument("--downstream-km", type=float, default=50.0)
    parser.add_argument("--db-host", default=os.getenv("HHLY_DB_HOST", DEFAULT_DB_HOST))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("HHLY_DB_PORT", DEFAULT_DB_PORT)))
    parser.add_argument("--db-name", default=os.getenv("HHLY_DB_NAME", DEFAULT_DB_NAME))
    parser.add_argument("--db-user", default=os.getenv("HHLY_DB_USER", DEFAULT_DB_USER))
    parser.add_argument("--db-password", default=os.getenv("HHLY_DB_PASSWORD", ""))
    parser.add_argument("--db-schema", default=os.getenv("HHLY_DB_SCHEMA", DEFAULT_DB_SCHEMA))
    parser.add_argument("--db-sslmode", default=os.getenv("HHLY_DB_SSLMODE", "disable"))
    parser.add_argument("--db-connect-timeout", type=int, default=int(os.getenv("HHLY_DB_CONNECT_TIMEOUT", "5")))
    parser.add_argument("--river-table", default=os.getenv("HHLY_RIVER_TABLE_FULL", DEFAULT_RIVER_TABLE))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    engine = make_engine(args)
    rig._get_engine = lambda: engine  # 避免走 utils.db 里的旧默认数据库

    result = rig.build_rain24h_impact_river_geojson(
        args.csv,
        rain_threshold_mm=args.rain_threshold_mm,
        station_buffer_km=args.station_buffer_km,
        downstream_km=args.downstream_km,
        river_table=args.river_table,
        schema=args.db_schema,
        graph_path=args.graph,
    )

    river_path = output / "impact_rivers_postgis.geojson"
    station_path = output / "impact_stations.geojson"
    summary_path = output / "summary.json"
    river_path.write_text(json.dumps(result.get("river_geojson", {"type": "FeatureCollection", "features": []}), ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    station_path.write_text(json.dumps(result.get("station_geojson", {"type": "FeatureCollection", "features": []}), ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    summary = {k: v for k, v in result.items() if k not in {"river_geojson", "station_geojson"}}
    summary["outputs"] = {"river_geojson": str(river_path), "station_geojson": str(station_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")

    print("DB 版本地测试完成")
    print(f"渲染河流表：{args.river_table}")
    print(f"数据库：{args.db_host}:{args.db_port}/{args.db_name}")
    print(f"输出文件：{river_path}")


if __name__ == "__main__":
    main()
