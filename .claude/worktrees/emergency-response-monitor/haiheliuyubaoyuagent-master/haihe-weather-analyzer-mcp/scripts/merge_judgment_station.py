#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from typing import Any, Dict, List


def _read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _collect_json_files(directory: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not os.path.isdir(directory):
        return out
    for name in os.listdir(directory):
        if not name.endswith(".json"):
            continue
        key = os.path.splitext(name)[0]
        out[key] = os.path.join(directory, name)
    return out


def _safe_get_payload(doc: Dict[str, Any]) -> Any:
    if isinstance(doc, dict) and "payload" in doc:
        return doc.get("payload")
    return doc


def merge_dirs(judgment_dir: str, station_dir: str, output_file: str) -> Dict[str, Any]:
    judgment_files = _collect_json_files(judgment_dir)
    station_files = _collect_json_files(station_dir)
    all_times = sorted(set(judgment_files.keys()) | set(station_files.keys()))

    slots: List[Dict[str, Any]] = []
    for times in all_times:
        j_path = judgment_files.get(times)
        s_path = station_files.get(times)
        j_payload = _safe_get_payload(_read_json(j_path)) if j_path else None
        s_payload = _safe_get_payload(_read_json(s_path)) if s_path else None
        slot = {
            "times": times,
            "judgment": j_payload,
            "station_ranking": s_payload,
            "judgment_file": j_path,
            "station_file": s_path,
        }
        slots.append(slot)

    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "meta": {
            "judgment_dir": judgment_dir,
            "station_dir": station_dir,
            "slot_count": len(slots),
        },
        "slots": slots,
    }
    parent = os.path.dirname(output_file)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="按 times 合并判定与站点明细 JSON")
    parser.add_argument("--judgment-dir", required=True, help="判定结果目录（每个 times 一个 json）")
    parser.add_argument("--station-dir", required=True, help="站点明细目录（每个 times 一个 json）")
    parser.add_argument("--output-file", required=True, help="合并输出文件路径")
    args = parser.parse_args()
    merged = merge_dirs(args.judgment_dir, args.station_dir, args.output_file)
    print(
        json.dumps(
            {
                "ok": True,
                "slot_count": merged["meta"]["slot_count"],
                "output_file": args.output_file,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

