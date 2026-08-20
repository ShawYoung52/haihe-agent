"""诊断 2026-08-19 暴雨影响河流"0 条河流"的原因。

用法（在 hhlyqyxt-master 目录下执行，参数已写死，无需复制粘贴任何参数）：
    python scripts/diag_rain_impact_0819.py

输出四段：
  1. CSV 24h 累计雨量 Top8 + 触发站点数
  2. pkl 河网图坐标范围（判断触发站是否在测试河网覆盖范围内）
  3. build_rainstorm_impact_thematic_map 匹配统计（候选行/直接段/下游/传播时间）
  4. 已有 /tmp/rain_impact_verify.json 内容
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_CURRENT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _CURRENT_DIR.parent
_UTILS_DIR = _PROJECT_ROOT / "utils"
for _item in (_CURRENT_DIR, _PROJECT_ROOT, _UTILS_DIR):
    if str(_item) not in sys.path:
        sys.path.insert(0, str(_item))

import pandas as pd  # noqa: E402

import rainfall_impact_geojson as rig  # noqa: E402

CSV = "/tmp/20260819_rain.csv"
VERIFY_JSON = "/tmp/rain_impact_verify.json"
GRAPH = ("/home/ev/haiheliuyubaoyuagent/yx-test/haiheliuyubaoyuagent-master/"
         "haihe-weather-analyzer-mcp/test-data/river_directed_v6.pkl")
DB_PASSWORD = "postgres"
THRESHOLD = 50.0


def _pg_conf() -> dict:
    return {
        "host": "10.226.107.130",
        "port": "5432",
        "dbname": "postgres",
        "user": "postgres",
        "password": DB_PASSWORD,
        "sslmode": "disable",
        "connect_timeout": 30,
    }


def section(title: str) -> None:
    print(f"\n{'=' * 60}\n  {title}\n{'=' * 60}")


def main() -> int:
    # ---------- 1. CSV 分析 ----------
    section("1. CSV 24h 累计雨量 Top8 + 触发站点")
    df = pd.read_csv(CSV, encoding="utf-8-sig")
    g = (
        df.groupby("Station_Id_C")
        .agg(rain_24h=("PRE", "sum"), lat=("Lat", "first"), lon=("Lon", "first"))
        .sort_values("rain_24h", ascending=False)
    )
    print(g.head(8))
    trigger = g[g["rain_24h"] >= THRESHOLD]
    print(f"触发(>= {THRESHOLD}mm)站点数: {len(trigger)}")
    for sid, row in trigger.iterrows():
        print(f"  触发站 {sid}: {row['rain_24h']:.1f}mm, lon={row['lon']:.4f}, lat={row['lat']:.4f}")

    # ---------- 2. pkl 图坐标范围 ----------
    section("2. pkl 河网图坐标范围")
    try:
        graph = rig.get_graph(GRAPH)
        lons: list[float] = []
        lats: list[float] = []
        for _u, _v, _k, _a, p1, p2 in rig._iter_edges_with_points(graph):
            lons.extend([p1[0], p2[0]])
            lats.extend([p1[1], p2[1]])
        if lons:
            print(f"  边坐标 lon: {min(lons):.4f} ~ {max(lons):.4f}")
            print(f"  边坐标 lat: {min(lats):.4f} ~ {max(lats):.4f}")
            for sid, row in trigger.iterrows():
                lon, lat = float(row["lon"]), float(row["lat"])
                inside = (min(lons) <= lon <= max(lons)) and (min(lats) <= lat <= max(lats))
                print(f"  触发站 {sid} ({lon:.4f}, {lat:.4f}) 是否在图范围内: {inside}")
        else:
            print("  图无边或坐标解析失败")
    except Exception as exc:  # noqa: BLE001 - 诊断脚本全量捕获
        print(f"  图加载失败: {exc}")

    # ---------- 3. build + 匹配统计 ----------
    section("3. build_rainstorm_impact_thematic_map 匹配统计")
    try:
        # rain_24h 是聚合结果列，必须经 aggregate_5min_station_pre_to_24h（与
        # test_rain_impact_internal 同款），不能直接读原始 CSV 行。
        agg_df = rig.aggregate_5min_station_pre_to_24h(CSV)
        stations = [
            rig._station_record(row)
            for _, row in agg_df.iterrows()
            if row["rain_24h"] >= THRESHOLD
        ]
        result = rig.build_rainstorm_impact_thematic_map(
            stations,
            pg_conf=_pg_conf(),
            graph_path=GRAPH,
            rainfall_threshold_mm=THRESHOLD,
        )
        print("affected_rivers:", result.get("affected_rivers"))
        print("downstream_start_stats:", json.dumps(
            result.get("downstream_start_stats", {}), ensure_ascii=False))
        print("river_summary:", json.dumps(
            result.get("river_summary", {}), ensure_ascii=False))
        print("river_propagation:", json.dumps(
            result.get("river_propagation", {}), ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001 - 诊断脚本每段独立容错
        import traceback
        print(f"  build 失败（不影响其它诊断段）: {exc}")
        traceback.print_exc()

    # ---------- 4. 已有 verify JSON ----------
    section("4. /tmp/rain_impact_verify.json 内容")
    try:
        with open(VERIFY_JSON, encoding="utf-8") as f:
            d = json.load(f)
        print("impact_stations:", json.dumps(
            d.get("impact_stations", []), ensure_ascii=False))
        print("affected_rivers:", d.get("affected_rivers"))
        print("direct_rivers:", d.get("direct_rivers"))
        print("downstream_rivers:", d.get("downstream_rivers"))
    except Exception as exc:  # noqa: BLE001
        print(f"  读取失败: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
