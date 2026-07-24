"""内网离线服务器 暴雨影响河流 GeoJSON 传播时间验证脚本（无需 pytest）。

用法（在 hhlyqyxt-master 目录下执行）：
    python scripts/intranet_verify_rain_impact.py --csv /root/zm_code/yangxiao.csv --output /tmp/rain_impact_test.json

验证内容：
    1. 结果 JSON 是否包含 river_propagation 顶层字段
    2. GeoJSON 每条 feature 是否有 propagation_distance_km / propagation_time_hours
    3. feature properties 中 min_downstream_distance_km / trigger_stations 等字段不为 null
    4. 直接河段与下游河段各自的属性完整性
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

try:
    import rainfall_impact_geojson as rig
except ImportError:
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(_PROJECT_ROOT))
    import rainfall_impact_geojson as rig


def _sep(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def verify_top_level(result: dict) -> bool:
    """验证 1：结果 JSON 顶层字段。"""
    _sep("验证 1：顶层字段完整性")
    issues = 0

    for key in ("affected_rivers", "direct_rivers", "downstream_rivers", "river_propagation"):
        if key in result:
            val = result[key]
            if isinstance(val, list):
                print(f"  ✓ {key}: {len(val)} 项")
            elif isinstance(val, dict):
                rivers = val.get("rivers", val.get("rivers", []))
                velocity = val.get("flow_velocity_mps", "N/A")
                print(f"  ✓ {key}: flow_velocity_mps={velocity}, rivers={len(rivers)} 项")
        else:
            print(f"  ✗ {key}: 缺失!")
            issues += 1

    return issues == 0


def verify_geojson_properties(river_geojson: dict) -> bool:
    """验证 2-4：GeoJSON feature 属性完整性。"""
    _sep("验证 2-4：GeoJSON feature properties")
    features = river_geojson.get("features", [])
    if not features:
        print("  ✗ 无 GeoJSON features（可能无降雨站点触发阈值）")
        return False

    direct_count = 0
    downstream_count = 0
    null_prop_issues = 0
    null_prop_direct_issues = 0
    null_prop_downstream_issues = 0

    for feat in features:
        props = feat.get("properties", {})
        impact_type = props.get("impact_type")

        # 检查 per-edge 传播时间
        prop_dist = props.get("propagation_distance_km")
        prop_time = props.get("propagation_time_hours")
        if prop_dist is None or prop_time is None:
            null_prop_issues += 1
        else:
            pass  # OK

        if impact_type == "direct_buffer":
            direct_count += 1
            # 直接河段应有这些属性且非 null
            for key in ("min_station_distance_km", "trigger_station_count"):
                if props.get(key) is None:
                    null_prop_direct_issues += 1
                    print(f"  ✗ 直接河段 {props.get('river_name', '?')}: {key} = null")
            trig_stations = props.get("trigger_stations")
            if trig_stations is None:
                null_prop_direct_issues += 1
                print(f"  ✗ 直接河段 {props.get('river_name', '?')}: trigger_stations = null")
        elif impact_type == "downstream_50km":
            downstream_count += 1
            for key in ("min_downstream_distance_km", "end_downstream_distance_km",
                        "keep_km", "clip_fraction"):
                if props.get(key) is None:
                    null_prop_downstream_issues += 1
                    print(f"  ✗ 下游河段 {props.get('river_name', '?')}: {key} = null")

    print(f"  直接河段 features: {direct_count}")
    print(f"  下游河段 features: {downstream_count}")

    if null_prop_issues > 0:
        print(f"  ✗ {null_prop_issues} 条 feature 缺 propagation_distance_km/propagation_time_hours")
    else:
        print(f"  ✓ 所有 feature 均有 per-edge 传播时间属性")

    if null_prop_direct_issues == 0 and direct_count > 0:
        print(f"  ✓ 直接河段属性完整（{direct_count} 条）")
    if null_prop_downstream_issues == 0 and downstream_count > 0:
        print(f"  ✓ 下游河段属性完整（{downstream_count} 条）")

    total_null = null_prop_issues + null_prop_direct_issues + null_prop_downstream_issues
    if total_null == 0:
        print("  ✓ 所有 feature properties 无非 null 字段")
    return total_null == 0


def verify_propagation_consistency(result: dict) -> bool:
    """验证 5：river_propagation 汇总与 per-edge propagation 一致性。

    per-edge 边级别 propagation_distance：
    - 下游 feature → end_distance_km（累计下游距离）
    - 直接 feature → length_km（边本身长度）
    summary 河级别汇总：下游边取 max end_distance_km，纯直接边取 max length_km。
    比较时按同口径分两组：下游组比下游，直接（无下游河）组比直接。
    """
    _sep("验证 5：传播时间一致性")
    river_prop = result.get("river_propagation", {})
    river_geojson = result.get("river_geojson", {})
    prop_rivers = {r["river_name"]: r for r in river_prop.get("rivers", [])}

    # 按河名 + impact_type 分组 per-edge max
    downstream_max: dict[str, float] = {}
    direct_max: dict[str, float] = {}
    for feat in river_geojson.get("features", []):
        props = feat.get("properties", {})
        name = props.get("river_name", "")
        dist = props.get("propagation_distance_km", 0)
        if not (name and dist and math.isfinite(dist)):
            continue
        if props.get("impact_type") == "downstream_50km":
            downstream_max[name] = max(downstream_max.get(name, 0), dist)
        else:
            direct_max[name] = max(direct_max.get(name, 0), dist)

    issues = 0
    for name, summary in prop_rivers.items():
        has_ds = summary.get("has_downstream", False)
        expected = summary["propagation_distance_km"]
        if has_ds:
            actual = downstream_max.get(name, 0)
            # 下游边 end_distance_km ≤ downstream_km(50)，summary 口径一致
            if abs(actual - expected) > 1.0:
                print(f"  ✗ {name}: per-edge 下游 max={actual} vs summary={expected}, 偏差 > 1km")
                issues += 1
        else:
            actual = direct_max.get(name, 0)
            # 直接边取 max length_km；_feature_length_km（full_v6 len_km）与
            # get_edge_length_km（pkl/haversine）可能差少许，容差 5km
            if abs(actual - expected) > 5.0:
                print(f"  ✗ {name}: per-edge 直接 max={actual} vs summary={expected}, 偏差 > 5km")
                issues += 1

    if issues == 0:
        checked = len([n for n in prop_rivers if n in downstream_max or n in direct_max])
        print(f"  ✓ per-edge 与 summary 一致（{checked} 条河，按同口径对齐）")
    return issues == 0


def main():
    parser = argparse.ArgumentParser(description="暴雨影响河流 GeoJSON 传播时间内网验证")
    parser.add_argument("--csv", required=True, help="5 分钟降水 CSV 路径")
    parser.add_argument("--output", default="/tmp/rain_impact_verify.json", help="验证结果 JSON 输出路径")
    parser.add_argument("--threshold", type=float, default=50.0)
    parser.add_argument("--db-host", default="10.226.107.130")
    parser.add_argument("--db-port", default="5432")
    parser.add_argument("--db-name", default="postgres")
    parser.add_argument("--db-user", default="postgres")
    parser.add_argument("--db-password", default="")
    parser.add_argument("--graph-path", required=True, help="pkl 图路径")
    args = parser.parse_args()

    if not args.db_password:
        parser.error("请通过 --db-password 或环境变量传入数据库密码")

    print("暴雨影响河流 GeoJSON 传播时间内网验证")
    print(f"  CSV: {args.csv}")
    print(f"  Graph: {args.graph_path}")
    print(f"  Threshold: {args.threshold}mm")

    # Step 1: 聚合 CSV
    df = rig.aggregate_5min_station_pre_to_24h(args.csv)
    stations = [rig._station_record(row) for _, row in df.iterrows()
                if row["rain_24h"] >= args.threshold]
    print(f"\n  CSV 站点数: {len(df)}, 触发站点数: {len(stations)}")

    if not stations:
        print("无触发站点，跳过后续验证。")
        return 0

    # Step 2: 生成专题图
    pg_conf = {
        "host": args.db_host, "port": args.db_port, "dbname": args.db_name,
        "user": args.db_user, "password": args.db_password,
        "sslmode": "disable", "connect_timeout": 30,
    }

    result = rig.build_rainstorm_impact_thematic_map(
        stations, pg_conf=pg_conf, graph_path=args.graph_path,
        rainfall_threshold_mm=args.threshold,
    )

    # Step 3: 保存（附带传播时间）
    output_path = Path(args.output)
    river_geojson = result.get("river_geojson", {"type": "FeatureCollection", "features": []})
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "affected_rivers": result.get("affected_rivers", []),
            "direct_rivers": result.get("direct_rivers", []),
            "downstream_rivers": result.get("downstream_rivers", []),
            "river_propagation": result.get("river_propagation", {"rivers": []}),
            "river_geojson": river_geojson,
            "impact_stations": result.get("impact_stations", []),
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  结果已保存: {output_path}")

    # 验证
    results = [
        ("顶层字段", verify_top_level(result)),
        ("GeoJSON properties", verify_geojson_properties(river_geojson)),
        ("传播时间一致性", verify_propagation_consistency(result)),
    ]

    _sep("结果汇总")
    all_pass = True
    for name, ok in results:
        status = "✓ PASS" if ok else "✗ FAIL"
        if not ok:
            all_pass = False
        print(f"  {status} - {name}")

    if all_pass:
        print("\n全部验证通过。传播时间在顶层 JSON 和 GeoJSON feature 属性中均已输出，无 null 字段。")
    else:
        print("\n部分验证未通过。✗ 项对应字段可能为 null 或缺失，请检查代码是否为最新版本。")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
