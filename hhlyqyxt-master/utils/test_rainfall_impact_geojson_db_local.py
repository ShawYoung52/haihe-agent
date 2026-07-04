r"""本地 DB 版测试入口：5 分钟 CSV -> 24 小时降水 -> 影响河流 GeoJSON。

说明：这个入口只保留一个文件，不再新增 helper 文件。核心计算复用 rainfall_impact_geojson.py。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import pandas as pd

from rainfall_impact_geojson import build_rain24h_impact_river_geojson

DEFAULT_CSV_PATH = r"C:\Users\gaozr\Downloads\24hourmindata.csv"
DEFAULT_GRAPH_PATH = r"E:\tj\line\result\river_directed_v5.pkl"
DEFAULT_OUTPUT_DIR = r"C:\Users\gaozr\Downloads\24hourmindata_db_impact_output"
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="真实河流 24 小时降水影响 GeoJSON")
    parser.add_argument("--csv", default=DEFAULT_CSV_PATH)
    parser.add_argument("--graph", default=DEFAULT_GRAPH_PATH)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--rain-threshold-mm", type=float, default=50.0)
    parser.add_argument("--station-buffer-km", type=float, default=30.0)
    parser.add_argument("--downstream-km", type=float, default=50.0)
    parser.add_argument("--river-table", default=os.getenv("HHLY_RIVER_TABLE_FULL", DEFAULT_RIVER_TABLE))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    result = build_rain24h_impact_river_geojson(
        args.csv,
        rain_threshold_mm=args.rain_threshold_mm,
        station_buffer_km=args.station_buffer_km,
        downstream_km=args.downstream_km,
        river_table=args.river_table,
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
    print(f"输出文件：{river_path}")


if __name__ == "__main__":
    main()
