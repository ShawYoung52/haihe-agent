#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
预报应急响应：任务队列 + 退避重试（常驻 worker）

说明
----
同事说的「堆栈」在业务里通常指：**待处理任务堆积在队列里**，由常驻进程按顺序处理；
缺 EC 文件时不退出，而是**重新入队并延迟再试**，避免「单次定时任务跑一次就结束」漏判。

用法（示例）
-----------
在项目根目录执行：

  python scripts/forecast_emergency_queue_worker.py \\
    --job 2025072302 --job 2025072308 \\
    --ec-output-path /home/ev/data/ec/EC_AIFS/output/ \\
    --output-dir /var/log/haihe_forecast_results

或用 JSONL 任务文件（每行一个 JSON，至少含 start_time）：

  python scripts/forecast_emergency_queue_worker.py --job-file pending_jobs.jsonl

建议用 systemd/supervisor 保活本进程；cron 只负责往 job-file 追加任务或调用 API 入队。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Any, Deque, Dict, List, Optional

# 保证从项目根目录直接运行脚本能 import haihe_mcp_tools
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from exception.CustomException import BusinessException
except Exception:

    class BusinessException(Exception):
        pass

# 不依赖 haihe_mcp_tools 是否导出 DEFAULT_EC_OUTPUT_PATH（服务器上旧版文件可能没有该常量）
_DEFAULT_EC_OUTPUT_PATH = os.getenv("EC_OUTPUT_PATH", "/home/ev/data/ec/EC_AIFS/output/")
# 按日存储 GRIB：{EC_AIFS_ROOT}/{年}/{YYYYMMDD}/ ，见 haihe_mcp_tools._ec_daily_search_directories

try:
    from haihe_mcp_tools import evaluate_haihe_forecast_emergency_response_core  # noqa: E402
except ImportError as exc:
    missing = getattr(exc, "name", None) or str(exc)
    raise SystemExit(
        "无法从 haihe_mcp_tools 导入 evaluate_haihe_forecast_emergency_response_core。\n"
        f"详情: {missing}\n"
        "请把本仓库里更新后的 haihe_mcp_tools.py 同步到服务器（需包含预报判定 core 函数）。"
    ) from exc


def _china_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def _backoff_seconds(attempt: int, base: float, cap: float) -> float:
    return min(cap, base * (2 ** max(0, attempt - 1)))


def _is_retryable_missing_data(exc: BaseException) -> bool:
    if isinstance(exc, BusinessException):
        msg = str(exc)
        return "未找到" in msg and "起报" in msg
    return False


def _load_jobs_from_file(path: str) -> List[Dict[str, Any]]:
    jobs: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{line_no} JSON 无效: {e}") from e
            if "start_time" not in obj:
                raise ValueError(f"{path}:{line_no} 缺少 start_time")
            jobs.append(obj)
    return jobs


def _default_job(start_time: str) -> Dict[str, Any]:
    return {"start_time": start_time}


def main() -> None:
    parser = argparse.ArgumentParser(description="预报应急响应：队列 + 退避重试 worker")
    parser.add_argument(
        "--job",
        action="append",
        default=[],
        metavar="START_TIME",
        help="起报时次，可多次指定，如 --job 2025072302",
    )
    parser.add_argument("--job-file", default=None, help="JSONL 任务文件，每行含 start_time 等字段")
    parser.add_argument("--ec-output-path", default=_DEFAULT_EC_OUTPUT_PATH)
    parser.add_argument("--basin-codes", default="HHLY")
    parser.add_argument("--allowed-station-levels", default="")
    parser.add_argument("--sample-method", default="nearest", choices=["nearest", "bilinear"])
    parser.add_argument("--output-dir", default=None, help="成功时写入结果 JSON 的目录")
    parser.add_argument("--max-retries", type=int, default=96, help="单任务最大重试次数（缺文件时）")
    parser.add_argument("--backoff-base", type=float, default=30.0, help="首次重试等待秒数")
    parser.add_argument("--backoff-max", type=float, default=600.0, help="单次等待上限秒数")
    parser.add_argument("--idle-sleep", type=float, default=60.0, help="队列为空时休眠秒数")
    parser.add_argument("--reload-job-file-every", type=float, default=0.0, help=">0 时每隔 N 秒重新读取 job-file 追加新任务")
    parser.add_argument("--once", action="store_true", help="队列清空后退出（仍会对缺文件任务重试直至成功或超限）")
    args = parser.parse_args()

    queue: Deque[Dict[str, Any]] = deque()
    seen: set[str] = set()

    def enqueue(job: Dict[str, Any]) -> None:
        st = str(job.get("start_time", "")).strip()
        if not st:
            return
        key = json.dumps(job, sort_keys=True, ensure_ascii=False)
        if key in seen:
            return
        seen.add(key)
        job = dict(job)
        job.setdefault("attempts", 0)
        queue.append(job)

    for st in args.job:
        enqueue(_default_job(st))

    if args.job_file:
        for j in _load_jobs_from_file(args.job_file):
            enqueue(j)

    if not queue:
        print("队列为空：请提供 --job 或 --job-file", file=sys.stderr)
        sys.exit(1)

    last_reload = time.monotonic()
    output_dir = args.output_dir
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    print(f"[{_china_now().isoformat()}] worker 启动，待处理 {len(queue)} 个任务", flush=True)

    while True:
        if not queue:
            if args.once:
                break
            if args.job_file:
                try:
                    for j in _load_jobs_from_file(args.job_file):
                        enqueue(j)
                except OSError as e:
                    print(f"读取 job-file 失败: {e}", file=sys.stderr, flush=True)
            if not queue:
                if args.job_file and args.reload_job_file_every > 0:
                    time.sleep(args.reload_job_file_every)
                    continue
                break

        job = queue.popleft()
        job_key = json.dumps(job, sort_keys=True, ensure_ascii=False)
        seen.discard(job_key)
        attempts = int(job.get("attempts", 0))
        start_time = str(job["start_time"])

        try:
            result = evaluate_haihe_forecast_emergency_response_core(
                start_time=start_time,
                basin_codes=job.get("basin_codes", args.basin_codes),
                ec_output_path=job.get("ec_output_path", args.ec_output_path),
                allowed_station_levels=job.get("allowed_station_levels", args.allowed_station_levels),
                rainstorm_12h=float(job.get("rainstorm_12h", 50.0)),
                rainstorm_24h=float(job.get("rainstorm_24h", 50.0)),
                severe_rainstorm_24h=float(job.get("severe_rainstorm_24h", 100.0)),
                extraordinary_24h=float(job.get("extraordinary_24h", 250.0)),
                sustain_threshold_6h_mm=float(job.get("sustain_threshold_6h_mm", 0.1)),
                sample_method=str(job.get("sample_method", args.sample_method)),
                typhoon_landing_impact=bool(job.get("typhoon_landing_impact", False)),
                typhoon_impact_increasing=bool(job.get("typhoon_impact_increasing", False)),
                include_records=bool(job.get("include_records", False)),
            )
            ts = result.get("query", {}).get("start_time", start_time).replace(":", "").replace(" ", "_")
            print(
                f"[{_china_now().isoformat()}] 完成 {start_time} -> triggered={result.get('triggered')} level={result.get('level')}",
                flush=True,
            )
            if output_dir:
                out_path = os.path.join(output_dir, f"forecast_emergency_{ts}.json")
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                print(f"  已写入 {out_path}", flush=True)
        except BusinessException as e:
            if _is_retryable_missing_data(e):
                attempts += 1
                job["attempts"] = attempts
                if attempts > args.max_retries:
                    print(
                        f"[{_china_now().isoformat()}] 放弃 {start_time}：超过 max_retries={args.max_retries}，最后错误: {e}",
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    wait = _backoff_seconds(attempts, args.backoff_base, args.backoff_max)
                    print(
                        f"[{_china_now().isoformat()}] 待数据 {start_time}：第 {attempts} 次重试，{wait:.0f}s 后再入队。{e}",
                        flush=True,
                    )
                    time.sleep(wait)
                    new_key = json.dumps(job, sort_keys=True, ensure_ascii=False)
                    if new_key not in seen:
                        seen.add(new_key)
                        queue.append(job)
            else:
                print(f"[{_china_now().isoformat()}] 任务失败 {start_time}: {e}", file=sys.stderr, flush=True)
                raise
        except Exception as e:
            print(f"[{_china_now().isoformat()}] 任务异常 {start_time}: {e}", file=sys.stderr, flush=True)
            raise

        if args.job_file and args.reload_job_file_every > 0:
            if time.monotonic() - last_reload >= args.reload_job_file_every:
                last_reload = time.monotonic()
                try:
                    for j in _load_jobs_from_file(args.job_file):
                        enqueue(j)
                except OSError as e:
                    print(f"重读 job-file 失败: {e}", file=sys.stderr, flush=True)

    print(f"[{_china_now().isoformat()}] worker 结束", flush=True)


if __name__ == "__main__":
    main()
