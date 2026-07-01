#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 station_rain_api 导出结果转换为 rainfall_timeline 风格数据（按时次生成）。

输入：station_rain_api_*.json
输出：rainfall_timeline_YYYYMMDDHHMMSS.json（每个时次一份）
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List


def _safe_float(v: Any) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _derive_mm(mm24: float, hours: int) -> float:
    """
    仅有 PRE_24h 时，按比例构造 12h/6h（保序、可复现）：
    - PRE_12h ≈ PRE_24h * 0.60
    - PRE_6h  ≈ PRE_24h * 0.35
    """
    if hours == 24:
        return round(mm24, 1)
    if hours == 12:
        return round(mm24 * 0.60, 1)
    if hours == 6:
        return round(mm24 * 0.35, 1)
    return round(mm24, 1)


def _to_timeline_item(stations: List[Dict[str, Any]], hours: int, min_mm: float) -> Dict[str, Any]:
    field = f"PRE_{hours}h"
    rows: List[Dict[str, Any]] = []
    for r in stations:
        mm24 = _safe_float(r.get("rainfall_mm"))
        mm = _derive_mm(mm24, hours)
        if mm < min_mm:
            continue
        rows.append(
            {
                "station_id": str(r.get("station_id") or ""),
                "station_name": r.get("station") or "",
                "rainfall_mm": mm,
                "station_level": str(r.get("station_level") or ""),
                "lat": _safe_float(r.get("lat")),
                "lon": _safe_float(r.get("lon")),
                "city": r.get("city"),
                "cnty": r.get("cnty"),
            }
        )

    rows.sort(key=lambda x: _safe_float(x.get("rainfall_mm")), reverse=True)
    total_mm = round(sum(_safe_float(x.get("rainfall_mm")) for x in rows), 1)
    max_mm = round(_safe_float(rows[0]["rainfall_mm"]), 1) if rows else 0.0
    return {
        "accum_hours": hours,
        "field": field,
        "count": len(rows),
        "total_mm": total_mm,
        "max_mm": max_mm,
        "list": rows,
    }


def build_one(slot_payload: Dict[str, Any], min_mm: float, per_hours_limit: int) -> Dict[str, Any]:
    times = str(slot_payload.get("times") or "")
    basin_codes = str(slot_payload.get("basin_codes") or "HHLY")
    stations = list(slot_payload.get("list") or [])
    stations = stations[: max(1, int(per_hours_limit))]

    return {
        "times": times,
        "basin_codes": basin_codes,
        "min_mm": float(min_mm),
        "per_hours_limit": int(per_hours_limit),
        "accum_hours": [6, 12, 24],
        "list": [
            _to_timeline_item(stations, 6, min_mm),
            _to_timeline_item(stations, 12, min_mm),
            _to_timeline_item(stations, 24, min_mm),
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="按 station_rain_api 造 rainfall_timeline 格式数据")
    parser.add_argument("--input-json", required=True, help="station_rain_api_*.json 路径")
    parser.add_argument("--out-dir", required=True, help="输出目录")
    parser.add_argument("--min-mm", type=float, default=0.1, help="最小阈值，默认 0.1")
    parser.add_argument("--per-hours-limit", type=int, default=500, help="每个时次最多保留多少站点")
    args = parser.parse_args()

    in_path = Path(args.input_json)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    root = json.loads(in_path.read_text(encoding="utf-8"))
    slots = root.get("slots") or []
    ok_slots = [s for s in slots if bool(s.get("ok")) and isinstance(s.get("payload"), dict)]

    total = 0
    for s in ok_slots:
        payload = s["payload"]
        times = str(payload.get("times") or "").strip()
        if not times:
            continue
        one = build_one(
            slot_payload=payload,
            min_mm=float(args.min_mm),
            per_hours_limit=int(args.per_hours_limit),
        )
        fpath = out_dir / f"rainfall_timeline_{times}.json"
        fpath.write_text(json.dumps(one, ensure_ascii=False, indent=2), encoding="utf-8")
        total += 1

    summary = {
        "input": str(in_path),
        "out_dir": str(out_dir),
        "ok_slots": len(ok_slots),
        "generated_files": total,
        "note": "PRE_6h/PRE_12h 由 PRE_24h 按比例构造（0.35/0.60）。",
    }
    (out_dir / "_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
