#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按时间窗批量调用实况应急判定（与 emergency_http_server /emergency/observation 同源逻辑）。

必须在项目根目录执行，或任意目录执行均可（已自动把项目根加入 sys.path）：

  python scripts/test_observation_window_case.py
  python scripts/test_observation_window_case.py --start 2023072900 --end 2023080118 --step-hours 6
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main() -> None:
    parser = argparse.ArgumentParser(description="实况应急：多时次窗口测试（MUSIC 实况）")
    parser.add_argument(
        "--start",
        default="2023072900",
        help="起始时次 YYYYmmddHH（北京时间整点），默认 2023-07-29 00 时",
    )
    parser.add_argument(
        "--end",
        default="2023080118",
        help="结束时次 YYYYmmddHH，默认 2023-08-01 18 时",
    )
    parser.add_argument(
        "--step-hours",
        type=int,
        default=6,
        help="步长（小时），默认 6",
    )
    parser.add_argument(
        "--basin-codes",
        default="HHLY",
    )
    parser.add_argument(
        "--allowed-station-levels",
        default="",
    )
    args = parser.parse_args()

    os.chdir(_ROOT)

    from emergency_response_interface import query_haihe_emergency_observation

    def _parse_compact(s: str) -> datetime:
        s = "".join(c for c in s if c.isdigit())
        if len(s) == 10:
            return datetime.strptime(s, "%Y%m%d%H")
        raise SystemExit(f"无效 --start/--end: {s!r}，需 YYYYmmddHH 共 10 位数字")

    start = _parse_compact(args.start)
    end = _parse_compact(args.end)
    step = timedelta(hours=max(1, int(args.step_hours)))

    cur = start
    results: list[tuple[str, bool | None, str | None]] = []
    while cur <= end:
        times = cur.strftime("%Y%m%d%H") + "0000"
        print(f"== times={times} ==")
        try:
            res = query_haihe_emergency_observation(
                times=times,
                basin_codes=str(args.basin_codes),
                neighbor_km=50.0,
                sustain_hourly_threshold_mm=0.1,
                allowed_station_levels=str(args.allowed_station_levels),
                include_evidence=True,
            )
        except Exception as e:
            print(f"ERROR: {e}")
            results.append((times, None, None))
            cur += step
            continue
        print(res)
        results.append((times, res.get("reached"), res.get("level")))
        cur += step

    print("\nSummary:")
    for t, reached, lvl in results:
        print(f"{t}: reached={reached}, level={lvl}")

    by_lvl: dict[str, int] = {}
    for _, reached, lvl in results:
        if reached and lvl:
            by_lvl[str(lvl)] = by_lvl.get(str(lvl), 0) + 1
    if by_lvl:
        print("Reached counts by level:", by_lvl)


if __name__ == "__main__":
    main()
