#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
在服务器上直接测试 MCP 工具（不启动 HTTP/SSE）。

用法（在项目根目录）：

  python scripts/test_emergency_management.py init
  python scripts/test_emergency_management.py upsert
  python scripts/test_emergency_management.py obs-pipeline --times 20250723080000
  python scripts/test_emergency_management.py obs-pipeline --times 20250723080000 --offline
  python scripts/test_emergency_management.py fc-pipeline --start-time 2026030800
  python scripts/test_emergency_management.py fc-pipeline --start-time 2026030800 --offline
  python scripts/test_emergency_management.py obs-compare --times 20250723080000

依赖：与 tools.py 相同（config.ini 中 [postgres]、MUSIC 等按实际环境）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


async def _call(name: str, arguments: dict, print_result: bool = True):
    from fastmcp import FastMCP
    from tools import register_tools

    mcp = FastMCP("haihe-emergency-test")
    register_tools(mcp)
    try:
        result = await mcp.call_tool(name, arguments or {}, run_middleware=False)
    except Exception as exc:
        payload = {"tool": name, "error": str(exc)}
        if print_result:
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return payload
    if getattr(result, "structured_content", None) is not None:
        payload = result.structured_content
        if print_result:
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return payload
    for block in getattr(result, "content", []) or []:
        if getattr(block, "type", None) == "text" and hasattr(block, "text"):
            if print_result:
                print(block.text)
            return block.text
    if print_result:
        print(result)
    return result


def _pick_payload(data):
    if isinstance(data, list):
        if data and isinstance(data[0], dict):
            return data[0]
        return {}
    if isinstance(data, dict):
        return data
    return {}


def _has_error(payload: dict) -> bool:
    return isinstance(payload, dict) and bool(payload.get("error"))


def _offline_observation_records() -> list[dict]:
    # 本机离线联调样例：2个站达到暴雨阈值且有小时雨量，1个站未达标。
    return [
        {
            "Station_Id_C": "OFF001",
            "Station_levl": "11",
            "Lat": 39.10,
            "Lon": 117.20,
            "PRE_1h": 0.6,
            "PRE_12h": 62.0,
            "PRE_24h": 88.0,
            "Year": "2026",
            "Mon": "03",
            "Day": "08",
            "Hour": "08",
            "Station_Name": "离线样例站1",
            "Province": "天津市",
            "City": "天津市",
            "Cnty": "和平区",
        },
        {
            "Station_Id_C": "OFF002",
            "Station_levl": "12",
            "Lat": 39.12,
            "Lon": 117.23,
            "PRE_1h": 0.4,
            "PRE_12h": 56.0,
            "PRE_24h": 64.0,
            "Year": "2026",
            "Mon": "03",
            "Day": "08",
            "Hour": "08",
            "Station_Name": "离线样例站2",
            "Province": "天津市",
            "City": "天津市",
            "Cnty": "河西区",
        },
        {
            "Station_Id_C": "OFF003",
            "Station_levl": "13",
            "Lat": 39.25,
            "Lon": 117.45,
            "PRE_1h": 0.0,
            "PRE_12h": 12.0,
            "PRE_24h": 18.0,
            "Year": "2026",
            "Mon": "03",
            "Day": "08",
            "Hour": "08",
            "Station_Name": "离线样例站3",
            "Province": "天津市",
            "City": "天津市",
            "Cnty": "武清区",
        },
    ]


def _run_obs_pipeline_offline(args) -> dict:
    import haihe_mcp_tools as hm
    from constants import DEFAULT_THRESHOLDS_MM

    records = _offline_observation_records()
    filtered = hm._observation_filter_core(records=records, allowed_station_levels=args.allowed_station_levels)
    evaluated = hm._observation_evaluate_core(
        records=filtered["records"],
        allowed_station_levels=filtered["allowed_station_levels"],
        neighbor_km=50.0,
        sustain_hourly_threshold_mm=0.1,
        rainstorm_12h=DEFAULT_THRESHOLDS_MM["rainstorm_12h"],
        rainstorm_24h=DEFAULT_THRESHOLDS_MM["rainstorm_24h"],
        severe_rainstorm_24h=DEFAULT_THRESHOLDS_MM["severe_rainstorm_24h"],
        extraordinary_24h=DEFAULT_THRESHOLDS_MM["extraordinary_24h"],
    )
    result = hm._observation_report_core(
        evaluation=evaluated,
        basin_codes=args.basin_codes,
        times=args.times,
        allowed_station_levels=filtered["allowed_station_levels"],
        include_records=True,
        records=records,
    )
    result["offline_mode"] = True
    result["offline_note"] = "使用脚本内置样例站点数据，不访问MUSIC内网服务。"
    return result


def _run_fc_pipeline_offline(args) -> dict:
    import haihe_mcp_tools as hm
    from constants import DEFAULT_THRESHOLDS_MM

    records = _offline_observation_records()
    total_count = len({r.get("Station_Id_C") for r in records})
    rain24 = {"OFF001": 92.0, "OFF002": 68.0, "OFF003": 22.0}
    rain12 = {"OFF001": 58.0, "OFF002": 52.0, "OFF003": 10.0}
    sustained_ids = {"OFF001", "OFF002"}
    checks = hm._forecast_evaluate_core(
        station_records=records,
        total_count=total_count,
        rain24=rain24,
        rain12=rain12,
        sustained_station_ids=sustained_ids,
        rainstorm_12h=DEFAULT_THRESHOLDS_MM["rainstorm_12h"],
        rainstorm_24h=DEFAULT_THRESHOLDS_MM["rainstorm_24h"],
        severe_rainstorm_24h=DEFAULT_THRESHOLDS_MM["severe_rainstorm_24h"],
        extraordinary_24h=DEFAULT_THRESHOLDS_MM["extraordinary_24h"],
        typhoon_landing_impact=False,
        typhoon_impact_increasing=False,
    )
    parsed = hm._parse_forecast_start_time(args.start_time)
    result = hm._forecast_report_core(
        checks=checks,
        parsed_start_time=parsed,
        basin_codes=args.basin_codes,
        ec_output_path=args.ec_output_path or "OFFLINE_DEMO_PATH",
        allowed_levels=[x.strip() for x in args.allowed_station_levels.split(",") if x.strip()],
        sample_method="nearest",
        typhoon_landing_impact=False,
        typhoon_impact_increasing=False,
        ec_files_paths={"6h": "offline_6h.tif", "12h": "offline_12h.tif", "24h": "offline_24h.tif"},
        sustain_source="6h",
        sustain_threshold_6h_mm=0.1,
        total_count=total_count,
        include_records=True,
        station_records=records,
    )
    result["offline_mode"] = True
    result["offline_note"] = "使用脚本内置样例站点与预报采样值，不访问MUSIC和EC内网文件。"
    return result


def main() -> None:
    os.chdir(_ROOT)
    parser = argparse.ArgumentParser(description="应急事件管理本地测试")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="创建/补全应急相关表")

    p_up = sub.add_parser("upsert", help="写入一条示例事件（可改参数）")
    p_up.add_argument("--forecast-time", default="2026030800")
    p_up.add_argument("--event-level", default="IV")
    p_up.add_argument("--zone-code", default="Z01")
    p_up.add_argument("--city-name", default="天津市")
    p_up.add_argument("--response-window-hours", type=int, default=3)

    p_obs = sub.add_parser("obs-pipeline", help="执行观测判定四段链路")
    p_obs.add_argument("--times", required=True, help="实况时次，如 20250723080000")
    p_obs.add_argument("--basin-codes", default="HHLY")
    p_obs.add_argument("--allowed-station-levels", default="")
    p_obs.add_argument("--offline", action="store_true", help="离线模式：使用内置样例数据，不访问内网接口")

    p_fc = sub.add_parser("fc-pipeline", help="执行预报判定四段链路")
    p_fc.add_argument("--start-time", required=True, help="预报起报时次，如 2026030800")
    p_fc.add_argument("--basin-codes", default="HHLY")
    p_fc.add_argument("--ec-output-path", default="")
    p_fc.add_argument("--allowed-station-levels", default="")
    p_fc.add_argument("--offline", action="store_true", help="离线模式：使用内置样例数据，不访问内网接口")

    p_cmp = sub.add_parser("obs-compare", help="对比观测旧聚合 vs 新四段结果")
    p_cmp.add_argument("--times", required=True, help="实况时次，如 20250723080000")
    p_cmp.add_argument("--basin-codes", default="HHLY")
    p_cmp.add_argument("--allowed-station-levels", default="")
    p_cmp.add_argument("--offline", action="store_true", help="离线模式：使用内置样例数据做一致性对比")

    args = parser.parse_args()

    if args.cmd == "init":
        asyncio.run(_call("init_emergency_management_tables", {}))
    elif args.cmd == "upsert":
        station = json.dumps(
            {"stations": [{"Station_Id_C": "TEST001", "Lat": 39.1, "Lon": 117.2, "forecast_rain_mm": 55.0}]},
            ensure_ascii=False,
        )
        asyncio.run(
            _call(
                "upsert_emergency_event_management",
                {
                    "forecast_time": args.forecast_time,
                    "event_level": args.event_level,
                    "event_type": "rainstorm",
                    "zone_code": args.zone_code,
                    "city_name": args.city_name,
                    "response_window_hours": args.response_window_hours,
                    "source_kind": "forecast",
                    "station_distribution_json": station,
                    "station_json_dir": os.path.join(_ROOT, "data", "emergency_station_snapshots"),
                },
            )
        )
    elif args.cmd == "obs-pipeline":
        if args.offline:
            print(json.dumps(_run_obs_pipeline_offline(args), ensure_ascii=False, indent=2, default=str))
            return

        async def _run_obs_pipeline():
            fetched = await _call(
                "fetch_haihe_observation_response_inputs",
                {"basin_codes": args.basin_codes, "times": args.times},
                print_result=False,
            )
            fetched = _pick_payload(fetched)
            if _has_error(fetched):
                print(json.dumps(fetched, ensure_ascii=False, indent=2, default=str))
                return
            filtered = await _call(
                "filter_haihe_observation_response_records",
                {
                    "records": fetched.get("records", []),
                    "allowed_station_levels": args.allowed_station_levels,
                },
                print_result=False,
            )
            filtered = _pick_payload(filtered)
            if _has_error(filtered):
                print(json.dumps(filtered, ensure_ascii=False, indent=2, default=str))
                return
            evaluated = await _call(
                "evaluate_haihe_observation_response_records",
                {
                    "records": filtered.get("records", []),
                    "allowed_station_levels": args.allowed_station_levels,
                },
                print_result=False,
            )
            evaluated = _pick_payload(evaluated)
            if _has_error(evaluated):
                print(json.dumps(evaluated, ensure_ascii=False, indent=2, default=str))
                return
            reported = await _call(
                "report_haihe_observation_response",
                {
                    "evaluation": evaluated,
                    "basin_codes": args.basin_codes,
                    "times": args.times,
                    "allowed_station_levels": args.allowed_station_levels,
                },
                print_result=False,
            )
            payload = _pick_payload(reported)
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        asyncio.run(_run_obs_pipeline())
    elif args.cmd == "fc-pipeline":
        if args.offline:
            print(json.dumps(_run_fc_pipeline_offline(args), ensure_ascii=False, indent=2, default=str))
            return

        async def _run_fc_pipeline():
            fetch_args = {
                "start_time": args.start_time,
                "basin_codes": args.basin_codes,
                "allowed_station_levels": args.allowed_station_levels,
            }
            if args.ec_output_path:
                fetch_args["ec_output_path"] = args.ec_output_path
            fetched = await _call(
                "fetch_haihe_forecast_response_inputs",
                fetch_args,
                print_result=False,
            )
            fetched = _pick_payload(fetched)
            if _has_error(fetched):
                print(json.dumps(fetched, ensure_ascii=False, indent=2, default=str))
                return
            filtered = await _call(
                "filter_haihe_forecast_response_inputs",
                {
                    "station_records": fetched.get("station_records", []),
                    "ec_files": fetched.get("ec_files", {}),
                },
                print_result=False,
            )
            filtered = _pick_payload(filtered)
            if _has_error(filtered):
                print(json.dumps(filtered, ensure_ascii=False, indent=2, default=str))
                return
            evaluated = await _call(
                "evaluate_haihe_forecast_response_inputs",
                {
                    "station_records": fetched.get("station_records", []),
                    "total_station_count": fetched.get("total_station_count", 0),
                    "rain24": filtered.get("rain24", {}),
                    "rain12": filtered.get("rain12", {}),
                    "sustained_station_ids": filtered.get("sustained_station_ids", []),
                },
                print_result=False,
            )
            evaluated = _pick_payload(evaluated)
            if _has_error(evaluated):
                print(json.dumps(evaluated, ensure_ascii=False, indent=2, default=str))
                return
            report_args = {
                "checks": evaluated.get("checks", []),
                "start_time": args.start_time,
                "basin_codes": args.basin_codes,
                "allowed_station_levels": args.allowed_station_levels,
                "ec_files": fetched.get("ec_files", {}),
                "sustain_source": filtered.get("sustain_source", "6h"),
                "sustain_threshold_6h_mm": filtered.get("sustain_threshold_6h_mm", 0.1),
                "total_station_count": fetched.get("total_station_count", 0),
                "station_records": fetched.get("station_records", []),
            }
            if args.ec_output_path:
                report_args["ec_output_path"] = args.ec_output_path
            reported = await _call(
                "report_haihe_forecast_response",
                report_args,
                print_result=False,
            )
            payload = _pick_payload(reported)
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        asyncio.run(_run_fc_pipeline())
    elif args.cmd == "obs-compare":
        if args.offline:
            import haihe_mcp_tools as hm

            records = _offline_observation_records()
            allowed_list = [x.strip() for x in args.allowed_station_levels.split(",") if x.strip()]
            old_result = hm.evaluate_observation_response(records, allowed_station_levels=allowed_list)
            new_result = _run_obs_pipeline_offline(args)
            compare = {
                "old_level": old_result.get("level"),
                "new_level": new_result.get("level"),
                "old_triggered": old_result.get("triggered"),
                "new_triggered": new_result.get("triggered"),
                "same_level": old_result.get("level") == new_result.get("level"),
                "same_triggered": old_result.get("triggered") == new_result.get("triggered"),
                "offline_mode": True,
            }
            print(json.dumps({"compare": compare, "old": old_result, "new": new_result}, ensure_ascii=False, indent=2, default=str))
            return

        async def _run_obs_compare():
            old_result = await _call(
                "evaluate_haihe_emergency_response",
                {
                    "basin_codes": args.basin_codes,
                    "times": args.times,
                    "allowed_station_levels": args.allowed_station_levels,
                },
                print_result=False,
            )
            old_result = _pick_payload(old_result)

            fetched = _pick_payload(await _call(
                "fetch_haihe_observation_response_inputs",
                {"basin_codes": args.basin_codes, "times": args.times},
                print_result=False,
            ))
            filtered = _pick_payload(await _call(
                "filter_haihe_observation_response_records",
                {"records": fetched.get("records", []), "allowed_station_levels": args.allowed_station_levels},
                print_result=False,
            ))
            evaluated = _pick_payload(await _call(
                "evaluate_haihe_observation_response_records",
                {"records": filtered.get("records", []), "allowed_station_levels": args.allowed_station_levels},
                print_result=False,
            ))
            new_result = _pick_payload(await _call(
                "report_haihe_observation_response",
                {
                    "evaluation": evaluated,
                    "basin_codes": args.basin_codes,
                    "times": args.times,
                    "allowed_station_levels": args.allowed_station_levels,
                },
                print_result=False,
            ))

            compare = {
                "old_level": old_result.get("level"),
                "new_level": new_result.get("level"),
                "old_triggered": old_result.get("triggered"),
                "new_triggered": new_result.get("triggered"),
                "same_level": old_result.get("level") == new_result.get("level"),
                "same_triggered": old_result.get("triggered") == new_result.get("triggered"),
            }
            print(json.dumps({"compare": compare, "old": old_result, "new": new_result}, ensure_ascii=False, indent=2, default=str))
        asyncio.run(_run_obs_compare())


if __name__ == "__main__":
    main()
