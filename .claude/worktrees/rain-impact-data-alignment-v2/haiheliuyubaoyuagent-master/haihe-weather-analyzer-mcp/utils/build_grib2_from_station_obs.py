#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按实例 GRIB2 模板，基于站点实测造一批“同格式”GRIB2。

核心原则：
1) 模板决定格式（网格、参数、编码、step 等）
2) 仅改时间与数值场（values）
3) 输出文件命名沿用模板：YYYYMMDDHHMMSS-XXh-oper-fc.grib2

注意：
- 需要 Python eccodes 绑定：pip install eccodes
- 建议服务器已安装 ecCodes 运行库
"""

from __future__ import annotations

import argparse
import configparser
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import numpy as np
import geopandas as gpd
from shapely import contains_xy
from eccodes import (
    codes_get,
    codes_get_array,
    codes_grib_new_from_file,
    codes_release,
    codes_set,
    codes_set_values,
    codes_write,
)


STEP_RE = re.compile(r"-(\d+)h-", re.IGNORECASE)
LEADING_TIME_RE = re.compile(r"^\d{14}")


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _iter_template_files(template_dir: Path) -> Iterable[Path]:
    files = sorted(template_dir.glob("*.grib2"))
    if not files:
        raise FileNotFoundError(f"模板目录无 grib2 文件: {template_dir}")
    return files


def _step_hours_from_name(name: str) -> int:
    m = STEP_RE.search(name)
    if m:
        return int(m.group(1))
    return 0


def _new_name(template_name: str, new_times: str) -> str:
    # 例：20260504000000-24h-oper-fc.grib2 -> 20250722000000-24h-oper-fc.grib2
    if LEADING_TIME_RE.match(template_name):
        return LEADING_TIME_RE.sub(new_times, template_name, count=1)
    return f"{new_times}-{template_name}"


def _load_station_slots(path: Path) -> List[Dict[str, Any]]:
    root = json.loads(path.read_text(encoding="utf-8"))
    slots = root.get("slots") or []
    return [s for s in slots if bool(s.get("ok")) and isinstance(s.get("payload"), dict)]


def _load_boundary_shp_from_config() -> Optional[str]:
    cfg = configparser.ConfigParser()
    cfg_path = Path(__file__).resolve().parents[1] / "config.ini"
    if not cfg_path.is_file():
        return None
    cfg.read(cfg_path, encoding="utf-8")
    if not cfg.has_section("paths"):
        return None
    p = (cfg.get("paths", "boundary_shp", fallback="") or "").strip()
    return p or None


def _extract_station_points(slot: Dict[str, Any]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rows = (slot.get("payload") or {}).get("list") or []
    sx: List[float] = []
    sy: List[float] = []
    sv: List[float] = []
    for r in rows:
        lat = _safe_float(r.get("lat"), default=np.nan)
        lon = _safe_float(r.get("lon"), default=np.nan)
        mm24 = _safe_float(r.get("rainfall_mm"), default=np.nan)
        if np.isnan(lat) or np.isnan(lon) or np.isnan(mm24):
            continue
        sx.append(float(lon))
        sy.append(float(lat))
        sv.append(max(0.0, float(mm24)))
    if not sx:
        return np.array([]), np.array([]), np.array([])
    return np.asarray(sx), np.asarray(sy), np.asarray(sv)


def _idw_interpolate_to_grid(
    x: np.ndarray,
    y: np.ndarray,
    sx: np.ndarray,
    sy: np.ndarray,
    sv: np.ndarray,
    *,
    power: float = 2.0,
    max_neighbors: int = 6,
    chunk_size: int = 30_000,
) -> np.ndarray:
    if sx.size == 0:
        return np.zeros_like(x, dtype=np.float64)
    k = max(1, min(int(max_neighbors), int(sx.size)))
    out = np.zeros_like(x, dtype=np.float32)
    n = x.size
    for i in range(0, n, chunk_size):
        j = min(n, i + chunk_size)
        xx = x[i:j][:, None].astype(np.float32, copy=False)
        yy = y[i:j][:, None].astype(np.float32, copy=False)
        sx32 = sx.astype(np.float32, copy=False)
        sy32 = sy.astype(np.float32, copy=False)
        sv32 = sv.astype(np.float32, copy=False)
        d2 = (xx - sx32[None, :]) ** 2 + (yy - sy32[None, :]) ** 2

        if k < sx.size:
            idx = np.argpartition(d2, kth=k - 1, axis=1)[:, :k]
            d2k = np.take_along_axis(d2, idx, axis=1)
            svk = sv32[idx]
        else:
            d2k = d2
            svk = np.broadcast_to(sv32[None, :], d2.shape)

        near = d2k.min(axis=1) < 1e-12
        d2k = np.maximum(d2k, 1e-12)
        w = 1.0 / np.power(d2k, power / 2.0)
        val = (w * svk).sum(axis=1) / np.maximum(w.sum(axis=1), 1e-12)
        if np.any(near):
            nearest_idx = np.argmin(d2k, axis=1)
            val[near] = svk[np.arange(svk.shape[0]), nearest_idx][near]
        out[i:j] = val
    return out


def _build_basin_mask(boundary_shp: Optional[str], lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    if not boundary_shp:
        return np.ones_like(lon, dtype=bool)
    shp = Path(boundary_shp)
    if not shp.is_file():
        return np.ones_like(lon, dtype=bool)
    gdf = gpd.read_file(shp)
    if gdf.empty:
        return np.ones_like(lon, dtype=bool)
    geom = gdf.unary_union
    return np.asarray(contains_xy(geom, lon, lat), dtype=bool)


def _slot_mm24(slot: Dict[str, Any], agg: str) -> float:
    rows = (slot.get("payload") or {}).get("list") or []
    vals = [_safe_float(r.get("rainfall_mm"), 0.0) for r in rows]
    vals = [x for x in vals if x >= 0.0]
    if not vals:
        return 0.0
    if agg == "max":
        return max(vals)
    if agg == "p90":
        vals2 = sorted(vals)
        idx = max(0, min(len(vals2) - 1, int(round(0.9 * (len(vals2) - 1)))))
        return vals2[idx]
    return sum(vals) / len(vals)


def _mm_for_step(mm24: float, step_h: int, mode: str) -> float:
    # step 累计量（mm），基于 24h 实测构造
    if step_h <= 0:
        return 0.0
    if mode == "linear24":
        # 0-24h 线性增长，24h 后保持
        return mm24 * min(step_h, 24) / 24.0
    # rate：按 24h 平均雨强外推到 step_h
    return (mm24 / 24.0) * step_h


def _set_time_keys(gid: Any, dt: datetime) -> None:
    ymd = int(dt.strftime("%Y%m%d"))
    hm = int(dt.strftime("%H%M"))
    # 主键
    try:
        codes_set(gid, "dataDate", ymd)
    except Exception:
        pass
    try:
        codes_set(gid, "dataTime", hm)
    except Exception:
        pass
    # 常见备选键
    for k, v in (
        ("year", dt.year),
        ("month", dt.month),
        ("day", dt.day),
        ("hour", dt.hour),
        ("minute", dt.minute),
    ):
        try:
            codes_set(gid, k, v)
        except Exception:
            pass


def _write_one_from_template(
    template_file: Path,
    out_file: Path,
    base_dt: datetime,
    values_mm: np.ndarray,
) -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with template_file.open("rb") as fin, out_file.open("wb") as fout:
        try:
            gid = codes_grib_new_from_file(fin)
        except Exception as e:
            raise RuntimeError(f"模板读取失败: {template_file} | {e}") from e
        if gid is None:
            raise RuntimeError(f"模板为空: {template_file}")
        try:
            _set_time_keys(gid, base_dt)
            npts = int(codes_get(gid, "numberOfPoints"))
            units = str(codes_get(gid, "units"))
            vals = np.asarray(values_mm, dtype=np.float64)
            if vals.size != npts:
                raise RuntimeError(f"栅格点数不匹配: values={vals.size}, npts={npts}, file={template_file}")
            # 若单位是 m（常见 tp），把 mm -> m
            if units.strip().lower() == "m":
                vals = vals / 1000.0
            codes_set_values(gid, vals.tolist())
            codes_write(gid, fout)
        finally:
            codes_release(gid)


def main() -> None:
    parser = argparse.ArgumentParser(description="按模板GRIB2 + 站点实测批量造数")
    parser.add_argument("--template-dir", required=True, help="实例 grib2 模板目录（如 20260504）")
    parser.add_argument("--station-json", required=True, help="station_rain_api_*.json")
    parser.add_argument("--out-dir", required=True, help="输出目录")
    parser.add_argument("--boundary-shp", default="", help="流域边界shp，用于裁剪（默认读 config.ini 的 paths.boundary_shp）")
    parser.add_argument("--agg", choices=["avg", "max", "p90"], default="p90", help="把站点转单值方式")
    parser.add_argument(
        "--step-mode",
        choices=["linear24", "rate"],
        default="linear24",
        help="把24h值映射到各step：linear24(24h封顶)/rate(线性外推)",
    )
    parser.add_argument("--time-prefix", default="2025", help="仅处理指定年份前缀时次（默认 2025）")
    parser.add_argument("--times", default="", help="仅处理指定时次（YYYYMMDDHHMMSS），为空则按 time-prefix")
    parser.add_argument("--steps", default="", help="仅处理指定时效（如 6,12,24,48,72），为空则模板目录全处理")
    args = parser.parse_args()

    template_dir = Path(args.template_dir)
    out_dir = Path(args.out_dir)
    station_path = Path(args.station_json)

    templates = list(_iter_template_files(template_dir))
    slots = _load_station_slots(station_path)
    if args.times:
        slots = [s for s in slots if str(s.get("times") or "") == str(args.times).strip()]
    else:
        slots = [s for s in slots if str(s.get("times") or "").startswith(str(args.time_prefix))]
    if not slots:
        raise RuntimeError(f"未找到 times 以 {args.time_prefix} 开头的可用时次")

    step_filter: Optional[set[int]] = None
    if str(args.steps).strip():
        step_filter = {int(x.strip()) for x in str(args.steps).split(",") if x.strip().isdigit()}

    boundary_shp = (args.boundary_shp or "").strip() or (_load_boundary_shp_from_config() or "")

    total = 0
    failed = 0
    mask_cache: Dict[str, np.ndarray] = {}
    template_lonlat_cache: Dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for s in slots:
        times = str(s.get("times"))
        base_dt = datetime.strptime(times, "%Y%m%d%H%M%S")
        sx, sy, sv24 = _extract_station_points(s)
        if sx.size == 0:
            print(f"[WARN] 时次 {times} 无有效站点经纬度/雨量，跳过")
            continue
        mm24_scalar = _slot_mm24(s, args.agg)
        for tpl in templates:
            step_h = _step_hours_from_name(tpl.name)
            if step_filter is not None and step_h not in step_filter:
                continue
            out_name = _new_name(tpl.name, times)
            out_file = out_dir / out_name
            try:
                key = str(tpl.resolve())
                if key not in template_lonlat_cache:
                    with tpl.open("rb") as fin:
                        gid0 = codes_grib_new_from_file(fin)
                        if gid0 is None:
                            raise RuntimeError(f"模板为空: {tpl}")
                        try:
                            lon = np.asarray(codes_get_array(gid0, "longitudes"), dtype=np.float64)
                            lat = np.asarray(codes_get_array(gid0, "latitudes"), dtype=np.float64)
                        finally:
                            codes_release(gid0)
                    template_lonlat_cache[key] = (lon, lat)
                lon, lat = template_lonlat_cache[key]
                if key not in mask_cache:
                    mask_cache[key] = _build_basin_mask(boundary_shp, lon, lat)
                mask = mask_cache[key]

                # 站点24h插值场 -> 时效换算
                field24 = _idw_interpolate_to_grid(lon, lat, sx, sy, sv24, power=2.0, max_neighbors=8)
                if float(mm24_scalar) == 0.0:
                    field24 = np.zeros_like(field24)
                scale = _mm_for_step(24.0, step_h, args.step_mode) / 24.0 if step_h > 0 else 0.0
                field_step = field24 * scale
                field_step = np.where(mask, field_step, 0.0)

                _write_one_from_template(
                    template_file=tpl,
                    out_file=out_file,
                    base_dt=base_dt,
                    values_mm=field_step,
                )
                total += 1
            except Exception as e:
                failed += 1
                print(f"[WARN] 生成失败，已跳过: {tpl.name} @ {times} | {e}")

    summary = {
        "template_dir": str(template_dir),
        "station_json": str(station_path),
        "out_dir": str(out_dir),
        "times_count": len(slots),
        "template_count": len(templates),
        "generated_files": total,
        "failed_files": failed,
        "agg": args.agg,
        "step_mode": args.step_mode,
        "boundary_shp": boundary_shp,
        "times": args.times or None,
        "steps": sorted(step_filter) if step_filter else None,
        "time_prefix": args.time_prefix,
        "note": "格式来自模板；值场为站点IDW插值，并按流域边界裁剪。",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "_summary_build_grib2.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
