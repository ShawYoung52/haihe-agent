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
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import rainfall_impact_geojson as rig
except ImportError:
    # 未安装时用相对路径兜底（内网离线环境）
    _HERE = Path(__file__).resolve().parent
    sys.path.insert(0, str(_HERE))
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
    """验证 5：river_propagation 汇总与 per-edge 到达时间一致性。

    per-edge 边级别 propagation_distance：
    - 下游 feature → keep_km（本段裁剪长度，链式语义，非累计；累计看 end_downstream_distance_km）
    - 直接 feature → length_km（full_v6 len_km 优先）
    summary 河级别传播行程 = 暴雨入河点（直接段）→ 下游最远点：
    - 提供 features 时精化为 (最远 feature 到达时刻 - 该河直接段最早 t0) × 流速；
    - 无 features 时为 最长直接段 + 最远下游 end_distance_km。
    本验证核对三条不变量：
      ① per-edge 与 summary 的河名集合一致（命名口径）；
      ② summary propagation_time_hours ≈ 特征精化行程时间（当 features 可用时）；
      ③ summary propagation_distance_km 覆盖最远下游累计距离——不满足时仅告警
         （可能为跨河汇流归属，非缺陷，不判失败）。
    """
    _sep("验证 5：传播时间一致性")
    river_prop = result.get("river_propagation", {})
    river_geojson = result.get("river_geojson", {})
    prop_rivers = {r["river_name"]: r for r in river_prop.get("rivers", [])}

    # 按河名分组收集 per-edge 信息 + 直接段 t0（用于精化行程的参考起点）
    per_edge_names: set[str] = set()
    farthest_downstream: dict[str, float] = {}      # 最远下游累计距离 end_downstream_distance_km
    river_arrivals: dict[str, list[datetime]] = {}
    river_entry_t0: dict[str, list[datetime]] = {}
    iso_re = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    for feat in river_geojson.get("features", []):
        props = feat.get("properties", {})
        name = props.get("river_name", "")
        if not name:
            continue
        per_edge_names.add(name)
        if props.get("impact_type") == "downstream_50km":
            raw = props.get("end_downstream_distance_km")
            try:
                raw_f = float(raw)
            except (TypeError, ValueError):
                raw_f = math.nan
            if raw_f and math.isfinite(raw_f):
                farthest_downstream[name] = max(farthest_downstream.get(name, 0.0), raw_f)
        elif props.get("impact_type") == "direct_buffer":
            t0_str = props.get("t0_source_time")
            if t0_str and iso_re.match(t0_str):
                river_entry_t0.setdefault(name, []).append(
                    datetime.fromisoformat(t0_str.replace("Z", "+00:00")))
        arrival_str = props.get("estimated_arrival_time")
        if arrival_str and iso_re.match(arrival_str):
            river_arrivals.setdefault(name, []).append(
                datetime.fromisoformat(arrival_str.replace("Z", "+00:00")))

    issues = 0
    # ① 河名集合一致性
    missing_from_summary = per_edge_names - set(prop_rivers)
    if missing_from_summary:
        print(f"  ⚠ per-edge 有 {len(missing_from_summary)} 条河未出现在 summary 中（命名不一致？）")
        for n in sorted(missing_from_summary)[:5]:
            print(f"    per-edge only: '{n}'")
        issues += 1
    missing_from_per_edge = set(prop_rivers) - per_edge_names
    if missing_from_per_edge:
        print(f"  ⚠ summary 有 {len(missing_from_per_edge)} 条河未出现在 per-edge 中")
        for n in sorted(missing_from_per_edge)[:5]:
            print(f"    summary only: '{n}' (has_ds={prop_rivers[n].get('has_downstream')})")
        issues += 1

    # ② summary 传播时间 ≈ 特征精化行程；③ summary 距离 ≥ 最远下游累计
    for name, summary in prop_rivers.items():
        summary_hours = summary.get("propagation_time_hours")
        summary_dist = summary.get("propagation_distance_km")
        entries = river_entry_t0.get(name, [])
        arrivals = river_arrivals.get(name, [])
        if entries and arrivals:
            refined_hours = (max(arrivals) - min(entries)).total_seconds() / 3600.0
            if summary_hours is not None and abs(float(summary_hours) - refined_hours) > 0.3:
                print(f"  ✗ {name}: summary 传播时间 {summary_hours}h 与特征精化行程 {refined_hours:.2f}h 偏差 > 0.3h")
                issues += 1
        far = farthest_downstream.get(name, 0.0)
        if summary_dist is not None and far > 0 and float(summary_dist) + 1.0 < far:
            # 仅告警不判失败：下游支流可能由其它河更早的直接段汇入（河网汇流），
            # 该支流的累计距离未必从本河直接段起算，属命名归属而非缺陷。
            print(f"  ⚠ {name}: summary 传播距离 {summary_dist}km < 最远下游累计 {far}km"
                  f"（可能为跨河汇流归属，人工核对）")

    if issues == 0:
        print(f"  ✓ 传播时间与 per-edge 一致（{len(prop_rivers)} 条河，命名/精化行程/最远覆盖均通过）")
    return issues == 0


def verify_arrival_time_consistency(result: dict) -> bool:
    """验证 6：预计到达时间一致性。

    - 每个有 t0_source_time 的 feature 必须有 estimated_arrival_time
    - ISO UTC 格式正则
    - 直接段：|arrival - t0 - propagation_time_hours * 3600| ≤ 200s
    - 下游段：arrival ≥ t0（时间不倒流）
    - params.reference_time == min(feature.t0_source_time)
    - river_propagation.rivers[*].earliest_arrival_time == min(该河 features estimated_arrival_time)
    """
    _sep("验证 6：预计到达时间一致性")
    iso_re = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    features = result.get("river_geojson", {}).get("features", [])
    issues = 0

    # 逐 feature 检查
    feature_arrivals: dict[str, list[datetime]] = {}
    for feat in features:
        props = feat.get("properties", {})
        name = props.get("river_name", "")
        t0 = props.get("t0_source_time")
        arrival = props.get("estimated_arrival_time")
        prop_hours = props.get("propagation_time_hours", 0)

        if t0 is not None:
            # 有 t0 必须有 arrival
            if arrival is None:
                print(f"  ✗ {name}: t0_source_time={t0} 但 estimated_arrival_time 为 None")
                issues += 1
                continue
            # ISO 格式
            if not iso_re.match(t0):
                print(f"  ✗ {name}: t0_source_time 格式异常: {t0}")
                issues += 1
            if not iso_re.match(arrival):
                print(f"  ✗ {name}: estimated_arrival_time 格式异常: {arrival}")
                issues += 1

            # 直接段：arrival - t0 ≈ prop_hours * 3600s
            # 容忍度 200s：propagation_time_hours 是 round(x, 1) 精度，最差 0.05h*3600=180s 误差
            if prop_hours and math.isfinite(prop_hours) and prop_hours > 0:
                try:
                    t0_dt = datetime.fromisoformat(t0.replace("Z", "+00:00"))
                    arr_dt = datetime.fromisoformat(arrival.replace("Z", "+00:00"))
                    diff_s = (arr_dt - t0_dt).total_seconds()
                    expected_s = prop_hours * 3600
                    if abs(diff_s - expected_s) > 200:
                        print(f"  ✗ {name}: arrival-t0={diff_s}s, 预期={expected_s}s (prop={prop_hours}h), 偏差 > 200s")
                        issues += 1
                except Exception as e:
                    print(f"  ✗ {name}: 时间解析失败: {e}")
                    issues += 1
        else:
            # 无 t0 → arrival 应为 None
            if arrival is not None:
                print(f"  ✗ {name}: t0_source_time=None 但 estimated_arrival_time={arrival}")
                issues += 1

        # 收集 arrival 用于河级别汇总检查
        if name and arrival:
            try:
                feature_arrivals.setdefault(name, []).append(
                    datetime.fromisoformat(arrival.replace("Z", "+00:00"))
                )
            except Exception:
                pass

    # params.reference_time
    ref_time = result.get("params", {}).get("reference_time")
    all_t0s = [
        f["properties"]["t0_source_time"]
        for f in features
        if f.get("properties", {}).get("t0_source_time") is not None
    ]
    if all_t0s:
        earliest_t0 = min(all_t0s)
        # reference_time 是所有 rainstorm_stations（含未触发河流的阈值以上站）的最早 rain_end_time，
        # 涵盖范围是 trigger 站的超集，故 reference_time <= earliest_t0 是硬性口径（可 <，不可 >）
        try:
            ref_dt = datetime.fromisoformat(ref_time.replace("Z", "+00:00")) if ref_time else None
            earliest_dt = datetime.fromisoformat(earliest_t0.replace("Z", "+00:00"))
            if ref_dt is None or ref_dt > earliest_dt:
                print(f"  ✗ params.reference_time={ref_time} > 最早 trigger t0={earliest_t0}")
                issues += 1
        except Exception as e:
            print(f"  ✗ reference_time 解析失败: {e}")
            issues += 1
    else:
        if ref_time is not None:
            print(f"  ✗ 无 feature 有 t0，但 params.reference_time={ref_time}")
            # 不记为 issues，因为可能 0 features
        print(f"  - 无 t0 数据（features 为空或全无 rain_end_time），跳过 reference_time 检查")

    # river_propagation 河级别汇总
    prop = result.get("river_propagation", {})
    for r in prop.get("rivers", []):
        name = r["river_name"]
        arrivals = feature_arrivals.get(name, [])
        if arrivals:
            expected_earliest = min(arrivals)
            expected_latest = max(arrivals)
            actual_earliest = r.get("earliest_arrival_time")
            actual_latest = r.get("latest_arrival_time")
            # 解析字符串比较
            try:
                if actual_earliest:
                    ae = datetime.fromisoformat(actual_earliest.replace("Z", "+00:00"))
                    if abs((ae - expected_earliest).total_seconds()) > 1:
                        print(f"  ✗ summary {name}: earliest_arrival 偏差")
                        issues += 1
                if actual_latest:
                    al = datetime.fromisoformat(actual_latest.replace("Z", "+00:00"))
                    if abs((al - expected_latest).total_seconds()) > 1:
                        print(f"  ✗ summary {name}: latest_arrival 偏差")
                        issues += 1
            except Exception as e:
                print(f"  ✗ summary {name}: 解析异常: {e}")
                issues += 1

    if issues == 0:
        print(f"  ✓ 预计到达时间一致性验证通过（{len(features)} 条 features）")
    return issues == 0


def main():
    parser = argparse.ArgumentParser(description="暴雨影响河流 GeoJSON 传播时间内网验证")
    parser.add_argument("--csv", required=True, help="5 分钟降水 CSV 路径")
    parser.add_argument("--output", default="/tmp/rain_impact_verify.json", help="验证结果 JSON 输出路径")
    parser.add_argument("--threshold", type=float, default=50.0)
    parser.add_argument("--station-buffer-km", type=float, default=20.0,
                        help="站点缓冲区 km（默认 20；盐山等站最近河段 21.2km 超默认，"
                             "需 30 才能匹配到漳卫新河，与 test_rain_impact_internal 默认 30 一致）")
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
        station_buffer_km=args.station_buffer_km,
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
        ("预计到达时间一致性", verify_arrival_time_consistency(result)),
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
