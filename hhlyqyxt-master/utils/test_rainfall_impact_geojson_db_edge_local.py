r"""本地 DB 版测试：边级裁剪影响河流 GeoJSON。

入口文件。核心逻辑见 edgeclip_runner_small.py。
"""
from __future__ import annotations

import argparse
import os

from edgeclip_runner_small import run_edgeclip
from rainfall_edgeclip_connection import connect_from_env

DEFAULT_CSV_PATH = r"C:\Users\gaozr\Downloads\24hourmindata.csv"
DEFAULT_GRAPH_PATH = r"E:\tj\line\result\river_directed_v5.pkl"
DEFAULT_OUTPUT_DIR = r"C:\Users\gaozr\Downloads\24hourmindata_db_impact_output"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="边级裁剪 24 小时降水影响河流 GeoJSON")
    parser.add_argument("--csv", default=DEFAULT_CSV_PATH)
    parser.add_argument("--graph", default=DEFAULT_GRAPH_PATH)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--rain-threshold-mm", type=float, default=50.0)
    parser.add_argument("--station-buffer-km", type=float, default=30.0)
    parser.add_argument("--downstream-km", type=float, default=50.0)
    parser.add_argument("--db-schema", default=os.getenv("HHLY_DB_SCHEMA", "public"))
    parser.add_argument("--db-srid", type=int, default=int(os.getenv("HHLY_DB_SRID", "4326")))
    parser.add_argument("--river-edge-table", default=os.getenv("HHLY_RIVER_EDGE_TABLE", "haihe_river_directed_simple_v5"))
    parser.add_argument("--river-geom-column", default=os.getenv("HHLY_RIVER_GEOM_COLUMN", "geom"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    conn = connect_from_env()
    try:
        summary = run_edgeclip(
            conn=conn,
            csv_path=args.csv,
            graph_path=args.graph,
            output_dir=args.output_dir,
            db_schema=args.db_schema,
            db_srid=args.db_srid,
            river_edge_table=args.river_edge_table,
            river_geom_column=args.river_geom_column,
            rain_threshold_mm=args.rain_threshold_mm,
            station_buffer_km=args.station_buffer_km,
            downstream_km=args.downstream_km,
        )
    finally:
        conn.close()
    print("DB 版本地测试完成")
    print(f"总站数：{summary['station_summary']['total_station_count']}")
    print(f"触发站数：{summary['station_summary']['impact_station_count']}")
    print(f"最大24小时雨量：{summary['station_summary']['max_rain_24h']} mm")
    print(f"直接影响河段：{summary['river_summary']['direct_segment_count']}")
    print(f"直接影响河流：{summary['river_summary']['direct_river_count']}")
    print(f"下游图边段：{summary['river_summary']['downstream_graph_segment_count']}")
    print(f"下游DB河段：{summary['river_summary']['downstream_db_segment_count']}")
    print(f"GeoJSON要素数：{summary['river_summary']['geojson_feature_count']}")
    print("输出文件：")
    for name, path in summary["outputs"].items():
        print(f"  - {name}: {path}")


if __name__ == "__main__":
    main()
