#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按时间窗批量生成“实况应急验证”产品图（基于 shapefile 边界裁剪）。

默认时段与 scripts/test_observation_window_case.py 保持一致：
  start=2023072900, end=2023080118, step_hours=6

示例：
  python scripts/render_observation_window_shp_maps.py --basin-vector "D:/data/haihe_boundary.shp"
  python scripts/render_observation_window_shp_maps.py --start 2023072900 --end 2023080118 --step-hours 6 --accum-hours 24,48
  python scripts/render_observation_window_shp_maps.py --judgment-out ./logs/observation_judgment.json

后台跑日志（建议 -u 无缓冲，便于 tail -f 实时看）：
  nohup python -u scripts/render_observation_window_shp_maps.py ... > ./logs/obs_shp_maps.log 2>&1 &
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Any, List

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _force_line_buffered_stdio() -> None:
    """重定向到文件时避免 stdout 块缓冲，便于 nohup + tail -f 立即看到 print。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(line_buffering=True)
            except Exception:
                pass


def _parse_accum_hours(text: str) -> tuple[int, ...]:
    vals: List[int] = []
    for part in (text or "").split(","):
        s = part.strip()
        if not s:
            continue
        try:
            v = int(s)
        except ValueError as e:
            raise SystemExit(f"非法 --accum-hours: {s!r}") from e
        if v <= 0:
            raise SystemExit(f"--accum-hours 仅支持正整数，收到: {v}")
        vals.append(v)
    if not vals:
        return (24,)
    return tuple(sorted(set(vals)))


def _collect_observation_judgments(
    jobs: list[Any],
    *,
    neighbor_km: float,
    sustain_hourly_threshold_mm: float,
    include_evidence: bool,
) -> list[dict[str, Any]]:
    """与 /emergency/observation 同源：query_haihe_emergency_observation。"""
    from emergency_response_interface import query_haihe_emergency_observation
    from observation_product_queue import times_compact_from_times

    rows: list[dict[str, Any]] = []
    for j in jobs:
        entry: dict[str, Any] = {
            "times": j.times,
            "times_compact": times_compact_from_times(j.times),
            "basin_codes": j.basin_codes,
            "allowed_station_levels": j.allowed_station_levels,
        }
        try:
            entry["judgment"] = query_haihe_emergency_observation(
                times=str(j.times).strip(),
                basin_codes=str(j.basin_codes),
                neighbor_km=float(neighbor_km),
                sustain_hourly_threshold_mm=float(sustain_hourly_threshold_mm),
                allowed_station_levels=str(j.allowed_station_levels),
                include_evidence=bool(include_evidence),
            )
            entry["judgment_error"] = None
        except Exception as exc:  # noqa: BLE001
            entry["judgment"] = None
            entry["judgment_error"] = str(exc)
        rows.append(entry)
    return rows


def _wait_jobs_done(job_ids: list[str], poll_seconds: float = 2.0) -> list[dict]:
    from observation_product_queue import get_observation_product_job, obs_job_to_dict

    pending = set(job_ids)
    done: list[dict] = []
    while pending:
        finished_now: list[str] = []
        for job_id in list(pending):
            job = get_observation_product_job(job_id)
            if not job:
                continue
            if job.status in ("done", "error"):
                finished_now.append(job_id)
                done.append(obs_job_to_dict(job))
        for jid in finished_now:
            pending.discard(jid)
        if pending:
            time.sleep(max(0.5, float(poll_seconds)))
    return done


def _iter_slots(start: str, end: str, step_hours: int, max_jobs: int) -> list[datetime]:
    from observation_product_queue import parse_observation_anchor_naive_local

    dt_start = parse_observation_anchor_naive_local(start)
    dt_end = parse_observation_anchor_naive_local(end)
    if dt_end < dt_start:
        raise ValueError("end 必须 >= start")
    step = max(1, int(step_hours))
    cap = min(max(1, int(max_jobs)), 1000)
    slots: list[datetime] = []
    cur = dt_start
    while cur <= dt_end:
        slots.append(cur)
        cur += timedelta(hours=step)
    if len(slots) > cap:
        raise ValueError(f"时次数量 {len(slots)} 超过 max_jobs={cap}，请增大 step_hours 或提高 max_jobs")
    return slots


def _collect_observation_judgments_only(
    *,
    start: str,
    end: str,
    step_hours: int,
    max_jobs: int,
    basin_codes: str,
    allowed_station_levels: str,
    neighbor_km: float,
    sustain_hourly_threshold_mm: float,
    include_evidence: bool,
) -> list[dict[str, Any]]:
    from emergency_response_interface import query_haihe_emergency_observation
    from observation_product_queue import format_music_times_string, times_compact_from_times

    rows: list[dict[str, Any]] = []
    for dt in _iter_slots(start, end, step_hours, max_jobs):
        ts = format_music_times_string(dt)
        entry: dict[str, Any] = {
            "times": ts,
            "times_compact": times_compact_from_times(ts),
            "basin_codes": basin_codes,
            "allowed_station_levels": allowed_station_levels,
        }
        try:
            entry["judgment"] = query_haihe_emergency_observation(
                times=ts,
                basin_codes=basin_codes,
                neighbor_km=float(neighbor_km),
                sustain_hourly_threshold_mm=float(sustain_hourly_threshold_mm),
                allowed_station_levels=allowed_station_levels,
                include_evidence=bool(include_evidence),
            )
            entry["judgment_error"] = None
        except Exception as exc:  # noqa: BLE001
            entry["judgment"] = None
            entry["judgment_error"] = str(exc)
        rows.append(entry)
    return rows


def main() -> None:
    _force_line_buffered_stdio()
    parser = argparse.ArgumentParser(description="批量生成实况应急窗口时次的 shp 裁剪产品图")
    parser.add_argument("--start", default="2023072900", help="起始时次 YYYYmmddHH（北京时间整点）")
    parser.add_argument("--end", default="2023080118", help="结束时次 YYYYmmddHH（北京时间整点）")
    parser.add_argument("--step-hours", type=int, default=6, help="步长小时，默认 6")
    parser.add_argument("--accum-hours", default="24", help="累计时长，逗号分隔，如 12,24,48")
    parser.add_argument("--basin-codes", default="HHLY", help="流域编码，默认 HHLY")
    parser.add_argument("--allowed-station-levels", default="", help="站点等级白名单")
    parser.add_argument(
        "--basin-vector",
        default="/home/ev/data/海河流域边界.shp",
        help="shapefile 路径（.shp）。默认 /home/ev/data/海河流域边界.shp；为空时回退读取 config.ini [paths] boundary_shp",
    )
    parser.add_argument("--config", default=os.path.join(_ROOT, "config.ini"), help="config.ini 路径")
    parser.add_argument("--max-jobs", type=int, default=300, help="最大任务数保护，默认 300")
    parser.add_argument(
        "--judgment-out",
        default="",
        help="实况应急判定汇总 JSON 路径（与 HTTP /emergency/observation 同源）；不传则只出图",
    )
    parser.add_argument(
        "--neighbor-km",
        type=float,
        default=50.0,
        help="邻站合并半径(km)，与接口默认一致",
    )
    parser.add_argument(
        "--sustain-hourly-threshold-mm",
        type=float,
        default=0.1,
        help="持续降水小时阈值(mm)，与接口默认一致",
    )
    parser.add_argument(
        "--include-evidence",
        action="store_true",
        help="判定 JSON 中包含 evidence 明细",
    )
    parser.add_argument(
        "--judgment-only",
        action="store_true",
        help="只生成判定 JSON，不入队出图",
    )
    args = parser.parse_args()

    os.chdir(_ROOT)
    accum_hours = _parse_accum_hours(args.accum_hours)

    from observation_product_queue import (
        enqueue_observation_product_jobs_range,
        observation_product_queue_status,
    )

    judgment_out = (args.judgment_out or "").strip()
    if args.judgment_only:
        if not judgment_out:
            raise SystemExit("开启 --judgment-only 时必须传 --judgment-out")
        print("[judgment] 仅判定模式：不出图，开始逐时次生成 JSON …")
        judgment_rows = _collect_observation_judgments_only(
            start=str(args.start),
            end=str(args.end),
            step_hours=int(args.step_hours),
            max_jobs=int(args.max_jobs),
            basin_codes=str(args.basin_codes),
            allowed_station_levels=str(args.allowed_station_levels),
            neighbor_km=float(args.neighbor_km),
            sustain_hourly_threshold_mm=float(args.sustain_hourly_threshold_mm),
            include_evidence=bool(args.include_evidence),
        )
        results = []
        print(f"[judgment] 已完成 {len(judgment_rows)} 个时次判定")
    else:
        jobs = enqueue_observation_product_jobs_range(
            start=args.start,
            end=args.end,
            step_hours=int(args.step_hours),
            max_jobs=int(args.max_jobs),
            accum_hours=accum_hours,
            basin_codes=str(args.basin_codes),
            allowed_station_levels=str(args.allowed_station_levels),
            config_path=str(args.config),
            basin_vector=str(args.basin_vector) if args.basin_vector else None,
            draw_options={},
        )

        job_ids = [j.job_id for j in jobs]
        print(f"[enqueue] 已入队 {len(job_ids)} 个时次任务，accum_hours={list(accum_hours)}")
        print(f"[queue] root={observation_product_queue_status().get('products_root')}")

        judgment_rows = None
        if judgment_out:
            print("[judgment] 正在拉取各时次实况判定（与 /emergency/observation 同源）…")
            judgment_rows = _collect_observation_judgments(
                jobs,
                neighbor_km=float(args.neighbor_km),
                sustain_hourly_threshold_mm=float(args.sustain_hourly_threshold_mm),
                include_evidence=bool(args.include_evidence),
            )

        results = _wait_jobs_done(job_ids)

    done_cnt = sum(1 for x in results if x.get("status") == "done")
    err_items = [x for x in results if x.get("status") == "error"]
    print(f"[result] done={done_cnt}, error={len(err_items)}")
    for x in sorted(results, key=lambda r: str(r.get("times_compact") or "")):
        t = x.get("times_compact")
        st = x.get("status")
        if st == "done":
            items = x.get("items") or []
            pngs = [str(it.get("path")) for it in items if str(it.get("path", "")).lower().endswith(".png")]
            print(f"  - {t}: done, png={len(pngs)}")
            for p in pngs:
                print(f"      {p}")
        else:
            print(f"  - {t}: error={x.get('error')}")

    if judgment_out and judgment_rows is not None:
        if args.judgment_only:
            slots = [{**jrow, "map_job": None} for jrow in judgment_rows]
        else:
            id_to_map = {str(x.get("job_id")): x for x in results if x.get("job_id")}
            slots = []
            for j, jrow in zip(jobs, judgment_rows):
                mj = id_to_map.get(str(j.job_id), {})
                slots.append(
                    {
                        **jrow,
                        "map_job": {
                            "job_id": j.job_id,
                            "status": mj.get("status"),
                            "error": mj.get("error"),
                            "times_compact": mj.get("times_compact"),
                            "items": mj.get("items"),
                            "finished_at": mj.get("finished_at"),
                        },
                    }
                )
        payload = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "meta": {
                "start": args.start,
                "end": args.end,
                "step_hours": int(args.step_hours),
                "accum_hours": list(accum_hours),
                "basin_codes": str(args.basin_codes),
                "allowed_station_levels": str(args.allowed_station_levels),
                "basin_vector": str(args.basin_vector) if args.basin_vector else None,
                "neighbor_km": float(args.neighbor_km),
                "sustain_hourly_threshold_mm": float(args.sustain_hourly_threshold_mm),
                "include_evidence": bool(args.include_evidence),
            },
            "slots": slots,
        }
        out_abs = os.path.abspath(judgment_out)
        out_dir = os.path.dirname(out_abs)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(out_abs, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[judgment] 已写入 {out_abs}")


if __name__ == "__main__":
    main()

