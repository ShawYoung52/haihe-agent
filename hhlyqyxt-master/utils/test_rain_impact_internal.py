"""内网测试牵引智能体暴雨影响河流工具。

用法（在 hhlyqyxt-master 目录下执行）：
    $env:HHLY_DB_HOST="<内网PG地址>"
    $env:HHLY_DB_PORT="5432"
    $env:HHLY_DB_NAME="hhly"
    $env:HHLY_DB_USER="postgres"
    $env:HHLY_DB_PASSWORD="<密码>"
    $env:HHLY_GRAPH_PATH="E:/tj/line/result/river_directed_v6.pkl"
    python utils/test_rain_impact_internal.py --csv "C:/Users/.../24hourmindata.csv" --output "C:/Users/.../rain_impact_result.json"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for item in (CURRENT_DIR, PROJECT_ROOT):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

import rainfall_impact_geojson as rig


def main():
    parser = argparse.ArgumentParser(description="内网测试暴雨影响河流工具")
    parser.add_argument("--csv", required=True, help="5 分钟降水 CSV 路径")
    parser.add_argument("--output", default="rain_impact_result.json", help="结果 JSON 输出路径")
    parser.add_argument("--threshold", type=float, default=50.0, help="24h 降雨阈值 mm")
    parser.add_argument("--direct-match-km", type=float, default=10.0, help="直接河段匹配距离 km")
    parser.add_argument("--station-buffer-km", type=float, default=30.0, help="站点缓冲区 km")
    parser.add_argument("--downstream-km", type=float, default=50.0, help="下游追踪距离 km")
    parser.add_argument("--db-host", default=os.getenv("HHLY_DB_HOST", "10.226.107.130"))
    parser.add_argument("--db-port", default=os.getenv("HHLY_DB_PORT", "5432"))
    parser.add_argument("--db-name", default=os.getenv("HHLY_DB_NAME", "postgres"))
    parser.add_argument("--db-user", default=os.getenv("HHLY_DB_USER", "postgres"))
    parser.add_argument("--db-password", default=os.getenv("HHLY_DB_PASSWORD", "postgres"))
    parser.add_argument("--graph-path", default=os.getenv("HHLY_GRAPH_PATH", "/home/ev/haiheliuyubaoyuagent/yx-test/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/test-data/river_directed_v6.pkl"))
    parser.add_argument("--river-table", default=rig.DEFAULT_RIVER_TABLE)
    args = parser.parse_args()

    if not args.db_host or not args.db_password:
        parser.error("请通过环境变量 HHLY_DB_HOST / HHLY_DB_PASSWORD 或命令行参数传入内网数据库信息")

    pg_conf = {
        "host": args.db_host,
        "port": args.db_port,
        "dbname": args.db_name,
        "user": args.db_user,
        "password": args.db_password,
        "sslmode": "disable",
        "connect_timeout": 30,
    }

    df = rig.aggregate_5min_station_pre_to_24h(args.csv)
    stations = [rig._station_record(row) for _, row in df.iterrows() if row["rain_24h"] >= args.threshold]
    print(f"CSV 站点数：{len(df)}，达到阈值 {args.threshold}mm 的站点数：{len(stations)}")

    if not stations:
        print("无触发站点，直接退出")
        return

    result = rig.build_rainstorm_impact_thematic_map(
        stations,
        pg_conf=pg_conf,
        graph_path=args.graph_path,
        rainfall_threshold_mm=args.threshold,
        station_buffer_km=args.station_buffer_km,
        downstream_km=args.downstream_km,
        direct_match_km=args.direct_match_km,
        river_table=args.river_table,
    )

    summary = result.get("river_summary", {})
    start_stats = result.get("downstream_start_stats", {})
    print("\n=== 结果摘要 ===")
    print(f"直接河段 feature 数：{summary.get('direct_feature_count', 0)}")
    print(f"下游边数：{summary.get('downstream_edge_count', 0)}")
    print(f"触发站点数：{len(stations)}")
    print(f"GeoJSON feature 总数：{summary.get('geojson_feature_count', 0)}")
    print(f"受影响河流：{result.get('affected_rivers', [])}")
    print(f"\n=== 下游起点统计 ===")
    print(json.dumps(start_stats, ensure_ascii=False, indent=2))

    output_path = Path(args.output)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "downstream_start_stats": start_stats,
                "river_summary": summary,
                "affected_rivers": result.get("affected_rivers", []),
                "impact_stations": result.get("impact_stations", []),
                "station_geojson": result.get("station_geojson", {"type": "FeatureCollection", "features": []}),
                "river_geojson": result.get("river_geojson", {"type": "FeatureCollection", "features": []}),
            },
            f,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    print(f"\n结果已保存：{output_path}")

    # 单独保存可直接被 QGIS 加载的 GeoJSON 文件
    river_geojson = result.get("river_geojson", {"type": "FeatureCollection", "features": []})
    station_geojson = result.get("station_geojson", {"type": "FeatureCollection", "features": []})
    river_path = output_path.with_suffix(output_path.suffix + ".river.geojson")
    station_path = output_path.with_suffix(output_path.suffix + ".station.geojson")
    with open(river_path, "w", encoding="utf-8") as f:
        json.dump(river_geojson, f, ensure_ascii=False, indent=2, default=str)
    with open(station_path, "w", encoding="utf-8") as f:
        json.dump(station_geojson, f, ensure_ascii=False, indent=2, default=str)
    print(f"河流 GeoJSON：{river_path}")
    print(f"站点 GeoJSON：{station_path}")


if __name__ == "__main__":
    main()