from __future__ import annotations

import configparser
import importlib
import json
import math
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from forecast_product_queue import (
    _ALLOWED_DRAW_OPTION_KEYS,
    _DEFAULT_QUEUE_DRAW_KWARGS,
)
from constants import DEFAULT_BASIN_CODES, DEFAULT_OBS_ELEMENTS, _looks_like_nine_zone_codes
from haihe_mcp_tools import (
    MusicClient,
    MusicConfig,
    deduplicate_latest_records,
    filter_records_by_station_levels,
    safe_float,
    station_id_of,
)
from observation_station_shapefile import write_observation_station_shapefile
from emergency_response_interface import (
    DEFAULT_HAIHE_BASIN_CODES,
    filter_records_by_nine_zone,
)

_ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONFIG = os.path.join(_ROOT_DIR, "config.ini")
_OBS_PRODUCTS_ROOT = os.path.normpath(
    os.getenv("OBSERVATION_PRODUCT_ROOT", os.path.join(_ROOT_DIR, "data", "observation_products"))
)
_DEFAULT_ACCUM_HOURS: Tuple[int, ...] = (1, 6, 12, 24)
_GRID_SIZE = 384
_IDW_POWER = 1.35
_IDW_MIN_DIST_KM = 2.0
_STATION_BBOX_PAD_DEG = 0.35
_MIN_STATIONS = 5

CST = timezone(timedelta(hours=8))


def products_root() -> str:
    return _OBS_PRODUCTS_ROOT


def times_compact_from_times(times: str) -> str:
    safe = "".join(c for c in (times or "") if c.isdigit())
    if len(safe) < 10:
        raise ValueError("times 需含至少 10 位数字，如 20250723080000")
    return safe


def parse_observation_anchor_naive_local(text: str) -> datetime:
    """
    将接口中的 start/end 解析为「墙上时钟」 naive datetime（与 MUSIC times 一致，按北京时次书写）。
    """
    digits = "".join(c for c in (text or "") if c.isdigit())
    if len(digits) >= 14:
        return datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
    if len(digits) >= 12:
        return datetime.strptime(digits[:12], "%Y%m%d%H%M")
    if len(digits) >= 10:
        return datetime.strptime(digits[:10], "%Y%m%d%H")
    raise ValueError(f"无法解析时次: {text!r}，至少 10 位数字，如 2026030908 或 20260309080000")


def format_music_times_string(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M%S")


_RANGE_MAX_JOBS_HARD = 1000


def observation_end_time_utc(times: str) -> datetime:
    """将实况时次按 CST(UTC+8) 解析为「观测时刻」的 UTC。"""
    digits = times_compact_from_times(times)
    if len(digits) >= 14:
        dt_cst = datetime.strptime(digits[:14], "%Y%m%d%H%M%S").replace(tzinfo=CST)
    elif len(digits) >= 12:
        dt_cst = datetime.strptime(digits[:12], "%Y%m%d%H%M").replace(tzinfo=CST)
    else:
        dt_cst = datetime.strptime(digits[:10], "%Y%m%d%H").replace(tzinfo=CST)
    return dt_cst.astimezone(timezone.utc)


def _precip_field_for_accum_hours(h: int) -> str:
    if h == 1:
        return "PRE_1h"
    if h == 6:
        return "PRE_6h"
    if h == 12:
        return "PRE_12h"
    if h == 24:
        return "PRE_24h"
    if h == 36:
        return "PRE_36h_est"
    if h == 48:
        return "PRE_48h_est"
    raise ValueError(f"不支持的 accum_hours={h}，可选 1/6/12/24/36/48")


def _precip_value_for_accum_hours(record: Dict[str, Any], h: int) -> float:
    """
    36/48h 目前无直接观测字段，按 PRE_24h 线性外推为联调口径。
    """
    if h == 1:
        return safe_float(record.get("PRE_1h"))
    if h == 6:
        return safe_float(record.get("PRE_6h"))
    if h == 12:
        return safe_float(record.get("PRE_12h"))
    if h == 24:
        return safe_float(record.get("PRE_24h"))
    if h in (36, 48):
        return max(0.0, safe_float(record.get("PRE_24h")) * (float(h) / 24.0))
    raise ValueError(f"不支持的 accum_hours={h}，可选 1/6/12/24/36/48")


def _haversine_km_grid(
    lat_grid: np.ndarray, lon_grid: np.ndarray, slat: float, slon: float
) -> np.ndarray:
    r = 6371.0088
    phi1 = np.radians(lat_grid)
    phi2 = math.radians(slat)
    dphi = np.radians(slat - lat_grid)
    dlambda = np.radians(slon - lon_grid)
    a = np.sin(dphi / 2.0) ** 2 + np.cos(phi1) * math.cos(phi2) * np.sin(dlambda / 2.0) ** 2
    return 2 * r * np.arctan2(np.sqrt(a), np.sqrt(np.maximum(1.0 - a, 0.0)))


def _idw_surface(
    lon_min: float,
    lon_max: float,
    lat_min: float,
    lat_max: float,
    slon: np.ndarray,
    slat: np.ndarray,
    sval: np.ndarray,
    nx: int,
    ny: int,
    power: float,
) -> Tuple[np.ndarray, Tuple[float, float, float, float, float, float]]:
    dx = (lon_max - lon_min) / nx
    dy = (lat_max - lat_min) / ny
    lon_centers = lon_min + (np.arange(nx) + 0.5) * dx
    lat_centers = lat_max - (np.arange(ny) + 0.5) * dy
    lon_grid, lat_grid = np.meshgrid(lon_centers, lat_centers)
    acc = np.zeros((ny, nx), dtype=np.float64)
    wsum = np.zeros((ny, nx), dtype=np.float64)
    min_d_km = float(_IDW_MIN_DIST_KM)
    for k in range(len(slon)):
        d = _haversine_km_grid(lat_grid, lon_grid, float(slat[k]), float(slon[k]))
        d = np.maximum(d, min_d_km)
        w = 1.0 / (d**power)
        acc += w * float(sval[k])
        wsum += w
    out = np.full((ny, nx), -9999.0, dtype=np.float32)
    valid = wsum > 0
    out[valid] = (acc[valid] / wsum[valid]).astype(np.float32)
    geotransform = (lon_min, dx, 0.0, lat_max, 0.0, -dy)
    return out, geotransform


def _read_default_boundary(config_path: str) -> str:
    if os.path.isfile(config_path):
        cp = configparser.ConfigParser()
        cp.read(config_path, encoding="utf-8")
        if cp.has_section("paths"):
            return cp.get("paths", "boundary_shp", fallback="").strip()
    return ""


def _basin_lonlat_bbox(basin_vector: str, padding: float) -> Tuple[float, float, float, float]:
    from draw_haihe_precip_product import _layer_envelope_wgs84_lonlat, _open_vector_layer, _pad_lonlat_rect

    _, layer = _open_vector_layer(basin_vector)
    env = _layer_envelope_wgs84_lonlat(layer)
    if not env:
        raise RuntimeError(f"无法读取流域边界范围: {basin_vector}")
    lon_lo, lon_hi, lat_lo, lat_hi = env
    return _pad_lonlat_rect(lon_lo, lon_hi, lat_lo, lat_hi, padding)


def _expand_bbox_with_stations(
    lon_min: float,
    lon_max: float,
    lat_min: float,
    lat_max: float,
    lons: Sequence[float],
    lats: Sequence[float],
    buf_deg: float,
) -> Tuple[float, float, float, float]:
    if not lons or not lats:
        return lon_min, lon_max, lat_min, lat_max
    return (
        min(lon_min, min(lons) - buf_deg),
        max(lon_max, max(lons) + buf_deg),
        min(lat_min, min(lats) - buf_deg),
        max(lat_max, max(lats) + buf_deg),
    )


def _apply_basin_nodata_to_array(
    arr: np.ndarray,
    geotransform: Tuple[float, ...],
    projection_wkt: str,
    basin_vector: str,
    nodata: float = -9999.0,
) -> np.ndarray:
    """流域外像元置 nodata，避免掩膜与栅格错位时整幅回退显示成矩形。"""
    from draw_haihe_precip_product import _open_vector_layer, _reproject_vector_to_raster, _rasterize_mask

    ny, nx = arr.shape
    vds, layer = _open_vector_layer(basin_vector)
    rvds, rlayer = _reproject_vector_to_raster(vds, layer, projection_wkt)
    mask = _rasterize_mask(rlayer, nx, ny, geotransform, projection_wkt)
    inside = int(np.count_nonzero(mask))
    if inside == 0:
        print(
            "[observation/idw] 警告: 流域栅格掩膜为 0 像元，未对流域外置 nodata"
            "（请核对 boundary_shp 与 GeoTIFF 投影及格网对齐）"
        )
        rvds = None
        vds = None
        return arr
    tiny = max(64, nx * ny // 5000)
    if inside < tiny:
        print(
            f"[observation/idw] 提示: 流域掩膜仅 {inside} 像元（低于启发阈值 {tiny}），仍执行流域外 nodata"
        )
    out = arr.astype(np.float32, copy=True)
    out[~mask] = nodata
    rvds = None
    vds = None
    return out


def write_station_idw_geotiff(
    *,
    basin_vector: str,
    records: Sequence[Dict[str, Any]],
    accum_hours: int,
    out_tif_path: str,
    map_padding: float = 0.12,
    clip_mask: str = "basin",
    admin_union_wkt: Optional[str] = None,
) -> Dict[str, Any]:
    field_name = _precip_field_for_accum_hours(accum_hours)
    lons: List[float] = []
    lats: List[float] = []
    vals: List[float] = []
    for r in records:
        lat = safe_float(r.get("Lat"))
        lon = safe_float(r.get("Lon"))
        v = _precip_value_for_accum_hours(r, accum_hours)
        if not station_id_of(r) or abs(lat) < 0.01 or abs(lon) < 0.01:
            continue
        if v < 0:
            continue
        lats.append(lat)
        lons.append(lon)
        vals.append(v)
    meta = {
        "station_count": len(lons),
        "field": field_name,
        "accum_hours": accum_hours,
        "clip_mask": (clip_mask or "basin").strip().lower(),
    }
    if len(lons) < _MIN_STATIONS:
        raise ValueError(f"{field_name} 有效站点仅 {len(lons)} 个，不足 {_MIN_STATIONS}，无法稳定插值")

    lon_min, lon_max, lat_min, lat_hi = _basin_lonlat_bbox(basin_vector, map_padding)
    lon_min, lon_max, lat_min, lat_hi = _expand_bbox_with_stations(
        lon_min, lon_max, lat_min, lat_hi, lons, lats, _STATION_BBOX_PAD_DEG
    )
    arr, gt = _idw_surface(
        lon_min,
        lon_max,
        lat_min,
        lat_hi,
        np.asarray(lons, dtype=np.float64),
        np.asarray(lats, dtype=np.float64),
        np.asarray(vals, dtype=np.float64),
        _GRID_SIZE,
        _GRID_SIZE,
        _IDW_POWER,
    )

    gdal = importlib.import_module("osgeo.gdal")
    gdal.UseExceptions()
    from draw_haihe_precip_product import (
        _mem_polygon_layer_from_wkt,
        _raster_copy_apply_mask_nodata,
        _rasterize_mask,
        wgs84_projection_wkt_traditional,
    )

    wkt = wgs84_projection_wkt_traditional()
    ny, nx = arr.shape
    cm = (clip_mask or "basin").strip().lower()
    if cm == "admin" and (admin_union_wkt or "").strip():
        clip_ds, clip_lyr = _mem_polygon_layer_from_wkt(admin_union_wkt.strip(), wkt)
        if clip_lyr is None:
            print("[observation/idw] 行政区 WKT 无法解析，回退流域掩膜")
            arr = _apply_basin_nodata_to_array(arr, gt, wkt, basin_vector)
        else:
            rmask = _rasterize_mask(clip_lyr, nx, ny, gt, wkt)
            inside = int(np.count_nonzero(rmask))
            if inside == 0:
                print("[observation/idw] 警告: 行政区栅格掩膜为 0 像元，回退流域掩膜")
                arr = _apply_basin_nodata_to_array(arr, gt, wkt, basin_vector)
            else:
                arr = _raster_copy_apply_mask_nodata(arr, rmask)
                print(f"[observation/idw] 已按行政区裁剪栅格（掩膜内 {inside} 像元）")
            clip_ds = None
    else:
        if cm == "admin":
            print("[observation/idw] clip_mask=admin 但无行政区 WKT，使用流域掩膜")
        arr = _apply_basin_nodata_to_array(arr, gt, wkt, basin_vector)

    os.makedirs(os.path.dirname(os.path.abspath(out_tif_path)), exist_ok=True)
    drv = gdal.GetDriverByName("GTiff")
    if drv is None:
        raise RuntimeError("GDAL 无 GTiff 驱动")
    ds = drv.Create(out_tif_path, nx, ny, 1, gdal.GDT_Float32)
    if ds is None:
        raise RuntimeError(f"无法创建 GeoTIFF: {out_tif_path}")
    ds.SetGeoTransform(gt)
    ds.SetProjection(wkt)
    band = ds.GetRasterBand(1)
    band.WriteArray(arr)
    band.SetNoDataValue(-9999.0)
    band.FlushCache()
    ds.FlushCache()
    ds = None
    meta["tiff_path"] = os.path.abspath(out_tif_path)
    return meta


def _observation_title_lines(end_utc: datetime, accum_hours: int) -> Tuple[str, str, str]:
    start = end_utc - timedelta(hours=accum_hours)
    return (
        "Haihe Basin Precipitation (Observation)",
        f"{start:%Y-%m-%d %H}:00 - {end_utc:%Y-%m-%d %H}:00 (UTC) accumulated",
        "Station IDW (for display only; not analysis grid)",
    )


@dataclass
class ObservationProductJob:
    job_id: str
    times: str
    accum_hours: Tuple[int, ...]
    basin_codes: str
    allowed_station_levels: str
    config_path: str
    basin_vector: str
    draw_options: Optional[Dict[str, Any]] = None
    status: str = "pending"
    error: Optional[str] = None
    items: List[Dict[str, Any]] = field(default_factory=list)
    times_compact: str = ""
    created_at: str = ""
    finished_at: Optional[str] = None


_jobs_lock = threading.Lock()
_jobs: Dict[str, ObservationProductJob] = {}
_task_queue: "queue.Queue[str]" = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()
_pause_lock = threading.Lock()
_queue_paused = False


def set_observation_product_queue_paused(paused: bool) -> None:
    global _queue_paused
    with _pause_lock:
        _queue_paused = bool(paused)


def is_observation_product_queue_paused() -> bool:
    with _pause_lock:
        return _queue_paused


def observation_product_queue_status() -> Dict[str, Any]:
    with _pause_lock:
        paused = _queue_paused
    try:
        qsz = _task_queue.qsize()
    except Exception:
        qsz = -1
    return {
        "paused": paused,
        "queued_jobs_approx": qsz,
        "products_root": _OBS_PRODUCTS_ROOT,
        "hint": "Independent from the forecast queue; pause stops new observation render jobs only.",
    }


def _ensure_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return

        def _loop() -> None:
            while True:
                while is_observation_product_queue_paused():
                    time.sleep(0.25)
                try:
                    job_id = _task_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if is_observation_product_queue_paused():
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

        t = threading.Thread(target=_loop, name="observation-product-worker", daemon=True)
        t.start()
        _worker_started = True


def _cycle_output_dir(compact: str) -> str:
    safe = "".join(c for c in compact if c.isalnum()) or "unknown"
    return os.path.join(_OBS_PRODUCTS_ROOT, safe)


def _run_job(job_id: str) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.status = "running"
        job.error = None
        job.items = []

    compact = times_compact_from_times(job.times)
    out_dir = _cycle_output_dir(compact)
    os.makedirs(out_dir, exist_ok=True)

    client = MusicClient(MusicConfig())
    source_basin_codes = job.basin_codes
    use_nine_zone_filter = _looks_like_nine_zone_codes(job.basin_codes)
    if use_nine_zone_filter:
        source_basin_codes = DEFAULT_HAIHE_BASIN_CODES
    records_raw = client.get_surf_ele_in_basin_by_time(
        basin_codes=source_basin_codes,
        times=job.times,
        elements=DEFAULT_OBS_ELEMENTS,
    )
    if use_nine_zone_filter:
        records_raw = filter_records_by_nine_zone(
            records_raw,
            job.basin_codes,
            config_path=job.config_path,
        )
    levels_list = [x.strip() for x in job.allowed_station_levels.split(",") if x.strip()]
    records = filter_records_by_station_levels(records_raw, levels_list)
    records = deduplicate_latest_records(records)

    end_utc = observation_end_time_utc(job.times)

    kw = dict(_DEFAULT_QUEUE_DRAW_KWARGS)
    kw.setdefault("precip_display_floor_mm", 0.08)
    kw.setdefault("strict_basin_mask", True)
    kw.setdefault("clip_mask", "admin")
    for k, v in (job.draw_options or {}).items():
        if k in _ALLOWED_DRAW_OPTION_KEYS:
            kw[k] = v
    map_pad = float(kw.get("map_basin_padding", 0.12))
    clip_mask = str(kw.get("clip_mask", "admin")).strip().lower()
    if clip_mask not in ("basin", "admin"):
        clip_mask = "admin"

    admin_union_wkt: Optional[str] = None
    if clip_mask == "admin":
        from draw_haihe_precip_product import (
            _basin_union_wkt_wgs84,
            _open_vector_layer,
            fetch_admin_clip_union_wkt,
        )

        vtmp, ltmp = _open_vector_layer(job.basin_vector)
        bw_admin = _basin_union_wkt_wgs84(ltmp)
        vtmp = None
        admin_union_wkt = fetch_admin_clip_union_wkt(
            job.config_path,
            bw_admin,
            admin_city_union=bool(kw.get("admin_city_union", True)),
            max_features=int(kw.get("admin_max_features", 8000)),
            admin_simplify_deg=float(kw.get("admin_simplify_deg", 0.002)),
        )
        if not admin_union_wkt:
            print("[observation] 行政区裁剪面未取得，回退 clip_mask=basin")
            clip_mask = "basin"

    draw_kw = {k: v for k, v in kw.items() if k not in ("clip_mask",)}

    items: List[Dict[str, Any]] = []
    for h in job.accum_hours:
        png_name = f"haihe_obs_precip_{h}h.png"
        tif_name = f"haihe_obs_idw_{h}h.tif"
        shp_name = f"haihe_obs_station_{h}h.shp"
        png_path = os.path.join(out_dir, png_name)
        tif_path = os.path.join(out_dir, tif_name)
        shp_path = os.path.join(out_dir, shp_name)
        url_png = f"/emergency/observation/products/png?times_compact={compact}&accum_hours={h}"
        one: Dict[str, Any] = {
            "accum_hours": h,
            "tiff_path": None,
            "png_path": None,
            "png_path_dark": None,
            "shapefile_path": None,
            "png_relative": f"{compact}/{png_name}",
            "url_hint": url_png,
            "ok": False,
            "message": "",
        }
        try:
            write_station_idw_geotiff(
                basin_vector=job.basin_vector,
                records=records,
                accum_hours=h,
                out_tif_path=tif_path,
                map_padding=map_pad,
                clip_mask=clip_mask,
                admin_union_wkt=admin_union_wkt,
            )
            one["tiff_path"] = os.path.abspath(tif_path)
        except Exception as exc:  # noqa: BLE001
            one["message"] = str(exc)
            items.append(one)
            continue

        try:
            shp_meta = write_observation_station_shapefile(
                records=records,
                accum_hours=h,
                out_shp_path=shp_path,
                times=job.times,
            )
            one["shapefile_path"] = shp_meta.get("shapefile_path")
            one["shapefile_station_count"] = int(shp_meta.get("station_count", 0))
        except Exception as exc:  # noqa: BLE001
            one["shapefile_error"] = str(exc)

        t1, t2, t3 = _observation_title_lines(end_utc, h)
        start_window_utc = end_utc - timedelta(hours=h)
        start_utc_hint = start_window_utc.strftime("%Y%m%d%H")
        try:
            from draw_haihe_precip_product import run_draw_haihe_precip_product

            run_draw_haihe_precip_product(
                tif_path,
                job.basin_vector,
                png_path,
                h,
                start_utc_arg=start_utc_hint,
                config=job.config_path,
                title1=t1,
                title2=t2,
                title_line3=t3,
                clip_mask=clip_mask,
                admin_clip_wkt=admin_union_wkt if clip_mask == "admin" else None,
                **draw_kw,
            )
            one["ok"] = True
            one["png_path"] = os.path.abspath(png_path)
            if str(draw_kw.get("theme", "light")).strip().lower() == "both":
                from pathlib import Path

                dp = Path(png_path)
                dark_path = str(dp.with_name(f"{dp.stem}_dark{dp.suffix}"))
                if os.path.isfile(dark_path):
                    one["png_path_dark"] = os.path.abspath(dark_path)
            one["message"] = "ok"
        except Exception as exc:  # noqa: BLE001
            one["message"] = str(exc)
        items.append(one)

    manifest = {
        "kind": "observation",
        "times": job.times,
        "times_compact": compact,
        "basin_codes": job.basin_codes,
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
            j.times_compact = compact
            j.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def enqueue_observation_product_job(
    times: str,
    accum_hours: Optional[Tuple[int, ...]] = None,
    basin_codes: str = DEFAULT_BASIN_CODES,
    allowed_station_levels: str = "11,12,13,16",
    config_path: Optional[str] = None,
    basin_vector: Optional[str] = None,
    draw_options: Optional[Dict[str, Any]] = None,
) -> ObservationProductJob:
    cfg = config_path or _DEFAULT_CONFIG
    basin = (basin_vector or _read_default_boundary(cfg)).strip()
    if not basin or not os.path.isfile(basin):
        raise ValueError(
            f"流域边界无效: {basin or '(空)'}，请在 config.ini [paths] boundary_shp 或传入 basin_vector"
        )

    tc = times_compact_from_times(times)
    hrs = tuple(accum_hours) if accum_hours else _DEFAULT_ACCUM_HOURS
    for h in hrs:
        _precip_field_for_accum_hours(int(h))

    _ensure_worker()
    job_id = uuid.uuid4().hex
    opts: Optional[Dict[str, Any]] = None
    if draw_options:
        opts = {k: v for k, v in draw_options.items() if k in _ALLOWED_DRAW_OPTION_KEYS}
    job = ObservationProductJob(
        job_id=job_id,
        times=times.strip(),
        accum_hours=tuple(int(x) for x in hrs),
        basin_codes=basin_codes.strip(),
        allowed_station_levels=allowed_station_levels.strip(),
        config_path=os.path.abspath(cfg),
        basin_vector=os.path.abspath(basin),
        draw_options=opts,
        times_compact=tc,
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    with _jobs_lock:
        _jobs[job_id] = job
    _task_queue.put(job_id)
    return job


def enqueue_observation_product_jobs_range(
    start: str,
    end: str,
    step_hours: int = 6,
    max_jobs: int = 200,
    accum_hours: Optional[Tuple[int, ...]] = None,
    basin_codes: str = DEFAULT_BASIN_CODES,
    allowed_station_levels: str = "11,12,13,16",
    config_path: Optional[str] = None,
    basin_vector: Optional[str] = None,
    draw_options: Optional[Dict[str, Any]] = None,
) -> List[ObservationProductJob]:
    """
    按 [start, end] 闭区间、步长 step_hours 小时，为每个整点时次入队一个实况出图任务。
    max_jobs 为防止误扫过多时次；硬顶 _RANGE_MAX_JOBS_HARD。
    """
    dt_start = parse_observation_anchor_naive_local(start)
    dt_end = parse_observation_anchor_naive_local(end)
    if dt_end < dt_start:
        raise ValueError("end 必须 >= start")
    step = max(1, int(step_hours))
    cap = min(max(1, int(max_jobs)), _RANGE_MAX_JOBS_HARD)

    slots: List[datetime] = []
    cur = dt_start
    while cur <= dt_end:
        slots.append(cur)
        cur += timedelta(hours=step)

    if len(slots) > cap:
        raise ValueError(
            f"时次数量 {len(slots)} 超过 max_jobs={cap}，请增大 step_hours 或提高 max_jobs（<= {_RANGE_MAX_JOBS_HARD}）"
        )

    jobs: List[ObservationProductJob] = []
    for dt in slots:
        ts = format_music_times_string(dt)
        jobs.append(
            enqueue_observation_product_job(
                times=ts,
                accum_hours=accum_hours,
                basin_codes=basin_codes,
                allowed_station_levels=allowed_station_levels,
                config_path=config_path,
                basin_vector=basin_vector,
                draw_options=draw_options,
            )
        )
    return jobs


def get_observation_product_job(job_id: str) -> Optional[ObservationProductJob]:
    with _jobs_lock:
        j = _jobs.get(job_id)
        if not j:
            return None
        return ObservationProductJob(
            job_id=j.job_id,
            times=j.times,
            accum_hours=tuple(j.accum_hours),
            basin_codes=j.basin_codes,
            allowed_station_levels=j.allowed_station_levels,
            config_path=j.config_path,
            basin_vector=j.basin_vector,
            draw_options=dict(j.draw_options) if j.draw_options else None,
            status=j.status,
            error=j.error,
            items=list(j.items),
            times_compact=j.times_compact,
            created_at=j.created_at,
            finished_at=j.finished_at,
        )


def obs_job_to_dict(j: ObservationProductJob) -> Dict[str, Any]:
    return {
        "job_id": j.job_id,
        "times": j.times,
        "accum_hours": list(j.accum_hours),
        "status": j.status,
        "error": j.error,
        "items": j.items,
        "times_compact": j.times_compact,
        "created_at": j.created_at,
        "finished_at": j.finished_at,
        "products_root": _OBS_PRODUCTS_ROOT,
        "draw_options": j.draw_options or {},
    }


def resolve_observation_png_path(times_compact: str, accum_hours: int) -> Optional[str]:
    safe = "".join(c for c in times_compact if c.isalnum())
    if not safe:
        return None
    name = f"haihe_obs_precip_{int(accum_hours)}h.png"
    p = os.path.join(_OBS_PRODUCTS_ROOT, safe, name)
    if os.path.isfile(p):
        return os.path.abspath(p)
    return None


def resolve_observation_shapefile_path(times_compact: str, accum_hours: int) -> Optional[str]:
    safe = "".join(c for c in times_compact if c.isalnum())
    if not safe:
        return None
    name = f"haihe_obs_station_{int(accum_hours)}h.shp"
    p = os.path.join(_OBS_PRODUCTS_ROOT, safe, name)
    if os.path.isfile(p):
        return os.path.abspath(p)
    return None


def read_observation_manifest(times_compact: str) -> Optional[Dict[str, Any]]:
    safe = "".join(c for c in times_compact if c.isalnum())
    if not safe:
        return None
    mp = os.path.join(_OBS_PRODUCTS_ROOT, safe, "manifest.json")
    if not os.path.isfile(mp):
        return None
    try:
        with open(mp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
