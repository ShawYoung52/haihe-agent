#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内网站点降雨数据导出脚本（通过实况接口，导出到服务器本地）

默认调用：
  /emergency/rainfall/station-ranking

示例：
python utils/export_station_rainfall_internal.py --start "2024-07-22 00:00:00" --end "2024-07-26 23:00:00"
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "station_exports"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from emergency_scenario_client import emergency_http_base_url, fetch_scenario_get


def _parse_dt(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


def _iter_times(start_dt: datetime, end_dt: datetime, step_hours: int) -> List[str]:
    out: List[str] = []
    cur = start_dt
    while cur <= end_dt:
        out.append(cur.strftime("%Y%m%d%H0000"))
        cur += timedelta(hours=step_hours)
    return out


def _fetch_station_ranking(
    base_url: str,
    times: str,
    *,
    basin_codes: str,
    sort_by: str,
    allowed_station_levels: str,
    limit: int,
    min_mm: float,
    timeout_sec: int,
) -> Dict[str, Any]:
    params = {
        "times": times,
        "basin_codes": basin_codes,
        "sort_by": sort_by,
        "allowed_station_levels": allowed_station_levels,
        "limit": int(limit),
        "min_mm": float(min_mm),
    }
    # 不需要场景图层参数，纯数据接口
    return fetch_scenario_get(
        base_url=base_url,
        route="/emergency/rainfall/station-ranking",
        params=params,
        timeout_sec=timeout_sec,
        map_render="",
    )


def _build_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    times = str(payload.get("times") or "")
    sort_by = str(payload.get("sort_by") or "")
    out: List[Dict[str, Any]] = []
    for row in payload.get("list") or []:
        out.append(
            {
                "times": times,
                "sort_by": sort_by,
                "rank": row.get("rank"),
                "station_id": row.get("station_id"),
                "station": row.get("station"),
                "rainfall_mm": row.get("rainfall_mm"),
                "station_level": row.get("station_level"),
                "lat": row.get("lat"),
                "lon": row.get("lon"),
                "city": row.get("city"),
                "cnty": row.get("cnty"),
            }
        )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="通过内网实况接口导出站点降雨数据")
    parser.add_argument("--start", required=True, help="开始时间，格式: YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--end", required=True, help="结束时间，格式: YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--step-hours", type=int, default=1, help="采样步长小时，默认 1")
    parser.add_argument("--base-url", default=emergency_http_base_url(), help="内网服务地址")
    parser.add_argument("--basin-codes", default="HHLY", help="流域编码，默认 HHLY")
    parser.add_argument(
        "--sort-by",
        default="PRE_24h",
        choices=["PRE_1h", "PRE_3h", "PRE_6h", "PRE_12h", "PRE_24h", "PRE"],
        help="降雨要素",
    )
    parser.add_argument("--allowed-station-levels", default="", help="站点等级过滤")
    parser.add_argument("--limit", type=int, default=500, help="每个时次最大站点数")
    parser.add_argument("--min-mm", type=float, default=0.0, help="最小降雨阈值")
    parser.add_argument("--timeout-sec", type=int, default=120, help="单次请求超时秒数")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="导出目录（服务器本地）")
    args = parser.parse_args()

    start_dt = _parse_dt(args.start)
    end_dt = _parse_dt(args.end)
    if end_dt < start_dt:
        raise ValueError("end 不能早于 start")
    if args.step_hours <= 0:
        raise ValueError("step-hours 必须大于 0")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    times_list = _iter_times(start_dt, end_dt, args.step_hours)
    all_rows: List[Dict[str, Any]] = []
    raw_slots: List[Dict[str, Any]] = []
    ok_count = 0
    fail_count = 0

    for t in times_list:
        try:
            payload = _fetch_station_ranking(
                base_url=str(args.base_url).rstrip("/"),
                times=t,
                basin_codes=str(args.basin_codes),
                sort_by=str(args.sort_by),
                allowed_station_levels=str(args.allowed_station_levels),
                limit=int(args.limit),
                min_mm=float(args.min_mm),
                timeout_sec=int(args.timeout_sec),
            )
            rows = _build_rows(payload)
            all_rows.extend(rows)
            raw_slots.append({"times": t, "ok": True, "count": len(rows), "payload": payload})
            ok_count += 1
            print(f"[OK] {t} -> {len(rows)} 条")
        except Exception as e:
            raw_slots.append({"times": t, "ok": False, "error": str(e)})
            fail_count += 1
            print(f"[ERR] {t} -> {e}")

    stem = f"station_rain_api_{start_dt.strftime('%Y%m%d%H%M%S')}_{end_dt.strftime('%Y%m%d%H%M%S')}"
    csv_path = out_dir / f"{stem}.csv"
    json_path = out_dir / f"{stem}.json"

    fields = [
        "times",
        "sort_by",
        "rank",
        "station_id",
        "station",
        "rainfall_mm",
        "station_level",
        "lat",
        "lon",
        "city",
        "cnty",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in all_rows:
            writer.writerow({k: r.get(k) for k in fields})

    out_json = {
        "meta": {
            "base_url": str(args.base_url).rstrip("/"),
            "start": args.start,
            "end": args.end,
            "step_hours": args.step_hours,
            "basin_codes": args.basin_codes,
            "sort_by": args.sort_by,
            "allowed_station_levels": args.allowed_station_levels,
            "limit": args.limit,
            "min_mm": args.min_mm,
            "ok_times": ok_count,
            "fail_times": fail_count,
            "total_rows": len(all_rows),
        },
        "slots": raw_slots,
    }
    json_path.write_text(json.dumps(out_json, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[DONE] CSV: {csv_path}")
    print(f"[DONE] JSON: {json_path}")
    print(f"[DONE] 时次成功={ok_count}, 失败={fail_count}, 总记录={len(all_rows)}")


if __name__ == "__main__":
    main()
