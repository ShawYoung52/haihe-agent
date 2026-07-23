from __future__ import annotations

import configparser
import json
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from haihe_mcp_tools import (
    DEFAULT_EC_OUTPUT_PATH,
    BusinessException,
    collect_ec_forecast_precip_files,
)

DEFAULT_PRODUCT_HOURS: Tuple[int, ...] = (12, 24, 36, 48, 60, 72)

# 与 run_draw_haihe_precip_product 关键字参数对齐；队列默认 admin_by_basin=True，其余同脚本默认值。
_ALLOWED_DRAW_OPTION_KEYS = frozenset(
    {
        "dpi",
        "font_path",
        "no_admin_overlay",
        "admin_level",
        "admin_max_features",
        "admin_simplify_deg",
        "admin_by_basin",
        "admin_city_union",
        "admin_metro_prefixes",
        "force_display_latlon_swap",
        "admin_query_buffer_ratio",
        "map_basin_padding",
        "precip_mm_factor",
        "precip_display_floor_mm",
        "strict_basin_mask",
        "clip_mask",
        "theme",
        "transparent_background",
        "color_scheme",
    }
)
_DEFAULT_QUEUE_DRAW_KWARGS: Dict[str, Any] = {
    "dpi": 180,
    "font_path": "",
    "no_admin_overlay": False,
    "admin_level": "city_adcode",
    "admin_max_features": 8000,
    "admin_simplify_deg": 0.002,
    "admin_by_basin": True,
    "admin_city_union": True,
    "admin_metro_prefixes": "11,12",
    "force_display_latlon_swap": False,
    "admin_query_buffer_ratio": 0.22,
    "map_basin_padding": 0.12,
    "precip_mm_factor": 0.0,
    "theme": "light",
    "transparent_background": False,
    "color_scheme": "default",
}

_ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONFIG = os.path.join(_ROOT_DIR, "config.ini")
_PRODUCTS_ROOT = os.path.normpath(
    os.getenv("FORECAST_PRODUCT_ROOT", os.path.join(_ROOT_DIR, "data", "forecast_products"))
)


def _read_default_paths(config_path: str) -> Tuple[str, str]:
    boundary = ""
    ec_out = DEFAULT_EC_OUTPUT_PATH
    if os.path.isfile(config_path):
        cp = configparser.ConfigParser()
        cp.read(config_path, encoding="utf-8")
        if cp.has_section("paths"):
            boundary = cp.get("paths", "boundary_shp", fallback="").strip()
            ec_out = cp.get("paths", "ecOutput", fallback=ec_out).strip() or ec_out
    return boundary, ec_out


@dataclass
class ForecastProductJob:
    job_id: str
    start_time: str
    hours: Tuple[int, ...]
    ec_output_path: str
    config_path: str
    basin_vector: str
    draw_options: Optional[Dict[str, Any]] = None
    status: str = "pending"  # pending | running | done | failed
    error: Optional[str] = None
    items: List[Dict[str, Any]] = field(default_factory=list)
    start_time_compact: str = ""
    created_at: str = ""
    finished_at: Optional[str] = None


_jobs_lock = threading.Lock()
_jobs: Dict[str, ForecastProductJob] = {}
_task_queue: "queue.Queue[str]" = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()
_pause_lock = threading.Lock()
_queue_paused = False


def set_forecast_product_queue_paused(paused: bool) -> None:
    """暂停后，工作线程不再从队列取新任务；已在执行中的单张图会跑完。"""
    global _queue_paused
    with _pause_lock:
        _queue_paused = bool(paused)


def is_forecast_product_queue_paused() -> bool:
    with _pause_lock:
        return _queue_paused


def forecast_product_queue_status() -> Dict[str, Any]:
    with _pause_lock:
        paused = _queue_paused
    try:
        qsz = _task_queue.qsize()
    except Exception:
        qsz = -1
    return {
        "paused": paused,
        "queued_jobs_approx": qsz,
        "products_root": _PRODUCTS_ROOT,
        "hint": "暂停仅阻止取新任务；彻底停止请结束 emergency_http_server 进程。",
    }


def _ensure_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return

        def _loop() -> None:
            while True:
                while is_forecast_product_queue_paused():
                    time.sleep(0.25)
                try:
                    job_id = _task_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if is_forecast_product_queue_paused():
                    _task_queue.put(job_id)
                    _task_queue.task_done()
                    time.sleep(0.25)
                    continue
                try:
                    _run_job(job_id)
                except Exception as exc:  # noqa: BLE001
                    with _jobs_lock:
                        j = _jobs.get(job_id)
                        if j:
                            j.status = "failed"
                            j.error = str(exc)
                            j.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                finally:
                    _task_queue.task_done()

        t = threading.Thread(target=_loop, name="forecast-product-worker", daemon=True)
        t.start()
        _worker_started = True


def _cycle_output_dir(compact: str) -> str:
    safe = "".join(c for c in compact if c.isalnum()) or "unknown"
    return os.path.join(_PRODUCTS_ROOT, safe)


def _run_job(job_id: str) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.status = "running"
        job.error = None
        job.items = []

    try:
        meta = collect_ec_forecast_precip_files(job.start_time, job.ec_output_path, job.hours)
    except BusinessException as e:
        with _jobs_lock:
            j = _jobs.get(job_id)
            if j:
                j.status = "failed"
                j.error = str(e)
                j.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return

    compact = str(meta.get("start_time_compact") or "")
    ec_files = meta.get("ec_files") or {}
    start_utc_arg = compact[:10] if len(compact) >= 10 else compact

    out_dir = _cycle_output_dir(compact)
    os.makedirs(out_dir, exist_ok=True)

    items: List[Dict[str, Any]] = []
    for h in job.hours:
        key = f"{h}h"
        tiff_path = ec_files.get(key)
        png_name = f"haihe_precip_{h}h.png"
        png_path = os.path.join(out_dir, png_name)
        rel_url_path = f"/emergency/forecast/products/png?start_time_compact={compact}&lead_hours={h}"
        one: Dict[str, Any] = {
            "lead_hours": h,
            "tiff_path": tiff_path,
            "png_path": None,
            "png_path_dark": None,
            "png_relative": f"{compact}/{png_name}",
            "url_hint": rel_url_path,
            "ok": False,
            "message": "",
        }
        if not tiff_path or not os.path.isfile(tiff_path):
            one["message"] = f"未找到 {key} 对应栅格文件"
            items.append(one)
            continue
        try:
            from draw_haihe_precip_product import run_draw_haihe_precip_product

            kw = dict(_DEFAULT_QUEUE_DRAW_KWARGS)
            for k, v in (job.draw_options or {}).items():
                if k in _ALLOWED_DRAW_OPTION_KEYS:
                    kw[k] = v
            run_draw_haihe_precip_product(
                tiff_path,
                job.basin_vector,
                png_path,
                h,
                start_utc_arg=start_utc_arg,
                config=job.config_path,
                **kw,
            )
            one["ok"] = True
            one["png_path"] = os.path.abspath(png_path)
            if str(kw.get("theme", "light")).strip().lower() == "both":
                stem, ext = os.path.splitext(png_path)
                dark_path = f"{stem}_dark{ext}"
                if os.path.isfile(dark_path):
                    one["png_path_dark"] = os.path.abspath(dark_path)
            one["message"] = "ok"
        except Exception as exc:  # noqa: BLE001
            one["message"] = str(exc)
        items.append(one)

    manifest = {
        "start_time": meta.get("start_time"),
        "start_time_compact": compact,
        "ec_output_path": job.ec_output_path,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "job_id": job_id,
        "items": items,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    with _jobs_lock:
        j = _jobs.get(job_id)
        if j:
            ok_count = sum(1 for it in items if bool(it.get("ok")))
            if ok_count == len(items) and len(items) > 0:
                j.status = "done"
            elif ok_count == 0:
                j.status = "failed"
                if not j.error:
                    j.error = "任务内全部时效出图失败"
            else:
                j.status = "partial_failed"
                if not j.error:
                    j.error = f"部分时效失败: success={ok_count}, failed={len(items) - ok_count}"
            j.items = items
            j.start_time_compact = compact
            j.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def wait_forecast_product_queue_idle() -> None:
    """阻塞直到当前已入队任务全部执行完毕（单线程 worker 每 job 结束会 task_done）。"""
    _task_queue.join()


def enqueue_forecast_product_job(
    start_time: str,
    hours: Optional[Tuple[int, ...]] = None,
    ec_output_path: Optional[str] = None,
    config_path: Optional[str] = None,
    basin_vector: Optional[str] = None,
    draw_options: Optional[Dict[str, Any]] = None,
) -> ForecastProductJob:
    """
    入队生成 12/24/36/48/60/72h（默认）等产品图。EC 资料为 6h 起报时次，各时效文件需已落盘。
    """
    cfg = config_path or _DEFAULT_CONFIG
    b_default, ec_default = _read_default_paths(cfg)
    basin = (basin_vector or b_default).strip()
    if not basin or not os.path.isfile(basin):
        raise ValueError(f"流域边界文件无效或未配置: {basin or '(空)'}，请在 config.ini [paths] boundary_shp 或传 basin_vector")

    ec_path = (ec_output_path or ec_default).strip() or ec_default
    hrs = tuple(hours) if hours else DEFAULT_PRODUCT_HOURS

    _ensure_worker()
    job_id = uuid.uuid4().hex
    opts: Optional[Dict[str, Any]] = None
    if draw_options:
        opts = {k: v for k, v in draw_options.items() if k in _ALLOWED_DRAW_OPTION_KEYS}
    job = ForecastProductJob(
        job_id=job_id,
        start_time=start_time.strip(),
        hours=hrs,
        ec_output_path=ec_path,
        config_path=os.path.abspath(cfg),
        basin_vector=os.path.abspath(basin),
        draw_options=opts,
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    with _jobs_lock:
        _jobs[job_id] = job
    _task_queue.put(job_id)
    return job


def get_forecast_product_job(job_id: str) -> Optional[ForecastProductJob]:
    with _jobs_lock:
        j = _jobs.get(job_id)
        if not j:
            return None
        return ForecastProductJob(
            job_id=j.job_id,
            start_time=j.start_time,
            hours=j.hours,
            ec_output_path=j.ec_output_path,
            config_path=j.config_path,
            basin_vector=j.basin_vector,
            draw_options=dict(j.draw_options) if j.draw_options is not None else None,
            status=j.status,
            error=j.error,
            items=list(j.items),
            start_time_compact=j.start_time_compact,
            created_at=j.created_at,
            finished_at=j.finished_at,
        )


def job_to_dict(j: ForecastProductJob) -> Dict[str, Any]:
    return {
        "job_id": j.job_id,
        "start_time": j.start_time,
        "hours": list(j.hours),
        "status": j.status,
        "error": j.error,
        "items": j.items,
        "start_time_compact": j.start_time_compact,
        "created_at": j.created_at,
        "finished_at": j.finished_at,
        "products_root": _PRODUCTS_ROOT,
        "draw_options": j.draw_options or {},
    }


def resolve_png_path(start_time_compact: str, lead_hours: int) -> Optional[str]:
    """若磁盘上已存在对应 PNG，返回绝对路径。"""
    safe = "".join(c for c in start_time_compact if c.isalnum())
    if not safe:
        return None
    name = f"haihe_precip_{int(lead_hours)}h.png"
    p = os.path.join(_PRODUCTS_ROOT, safe, name)
    if os.path.isfile(p):
        return os.path.abspath(p)
    return None


def read_manifest(start_time_compact: str) -> Optional[Dict[str, Any]]:
    safe = "".join(c for c in start_time_compact if c.isalnum())
    if not safe:
        return None
    mp = os.path.join(_PRODUCTS_ROOT, safe, "manifest.json")
    if not os.path.isfile(mp):
        return None
    try:
        with open(mp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def products_root() -> str:
    return _PRODUCTS_ROOT
