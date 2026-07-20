#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
海河流域降水预报产品图绘制脚本（EC AIFS GeoTIFF, UTC）。

核心能力：
1) 从指定目录自动选取最新 tiff（或手动指定 tiff）
2) 使用海河流域边界（shp/geojson）裁剪栅格
3) 输出接近业务模板风格的降水分级图（PNG）

示例（扁平 output 下 tiff）：
python draw_haihe_precip_product.py \
  --input-dir /home/ev/data/ec/EC_AIFS/output/ \
  --basin-vector /home/ev/data/海河流域边界.shp \
  --output /home/ev/data/products/haihe_precip.png \
  --lead-hours 24

按日目录 GRIB（{EC_AIFS}/{年}/{YYYYMMDD}/）时，--input-dir 填 EC_AIFS 根目录，并指定 --start-utc YYYYmmddHH，
脚本会按与 haihe_mcp_tools 相同规则查找 *-{N}h-oper-fc.grib2 或 ec_*_rain_total_{N}h.tif；亦可设环境变量 EC_AIFS_ROOT。
"""

from __future__ import annotations

import argparse
import configparser
import importlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap
except Exception as exc:
    raise RuntimeError("缺少 matplotlib 依赖，无法绘图。") from exc

try:
    from matplotlib import font_manager
except Exception:
    font_manager = None

try:
    # 用动态导入规避编辑器对 osgeo 的静态解析报错（运行时行为不变）
    gdal = importlib.import_module("osgeo.gdal")
    ogr = importlib.import_module("osgeo.ogr")
    osr = importlib.import_module("osgeo.osr")
except Exception as exc:
    raise RuntimeError("缺少 GDAL（osgeo）依赖，无法读取/裁剪栅格和矢量。") from exc

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:
    psycopg2 = None
    RealDictCursor = None


gdal.UseExceptions()


@dataclass
class RasterData:
    array: np.ndarray
    geotransform: tuple
    projection_wkt: str
    nodata: Optional[float]
    xsize: int
    ysize: int


@dataclass(frozen=True)
class RenderThemeStyle:
    name: str
    figure_facecolor: str
    axes_facecolor: str
    title_color: str
    label_color: str
    grid_color: str
    boundary_color: str
    admin_line_color: str
    dry_band_color: str
    nodata_color: str
    cbar_edge_color: str


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="绘制海河流域降水预报产品图（UTC）")
    parser.add_argument(
        "--input-dir",
        default="/home/ev/data/ec/EC_AIFS/output/",
        help="EC 根目录：扁平 output 或按日存放时的 EC_AIFS 根（如 .../EC_AIFS），见 --start-utc",
    )
    parser.add_argument(
        "--tiff-file",
        default="",
        help="指定栅格文件（tif/grib2/.nc）；不传则：有 --start-utc 时按 EC 规范文件名查找，否则在 input-dir 取最新。传 .nc 时按滚动预报处理，用 --lead-hours 作为提取时效",
    )
    parser.add_argument(
        "--rolling-forecast-nc",
        default="",
        help="滚动预报 .nc 文件路径；设置后忽略 --tiff-file/--input-dir/--start-utc，用 --lead-hours 作为提取时效",
    )
    parser.add_argument("--basin-vector", required=True, help="海河流域边界文件（shp/geojson）")
    parser.add_argument("--output", default="haihe_precip_product.png", help="输出 PNG 文件路径")
    parser.add_argument("--lead-hours", type=int, default=24, help="预报时效（小时），默认 24")
    parser.add_argument(
        "--start-utc",
        default="",
        help="起报时间 UTC，格式 YYYYmmddHH；不传则尝试从文件名提取，失败则用当前 UTC",
    )
    parser.add_argument("--dpi", type=int, default=180, help="输出分辨率 DPI")
    parser.add_argument("--config", default="config.ini", help="数据库配置文件路径（用于叠加行政区划）")
    parser.add_argument("--font-path", default="", help="中文字体文件路径（ttf/ttc，优先使用）")
    parser.add_argument("--no-admin-overlay", action="store_true", help="不叠加数据库行政区划")
    parser.add_argument(
        "--admin-level",
        default="city_adcode",
        choices=["all", "city", "district", "city_district", "hybrid", "city_adcode"],
        help="层级：city_adcode=六位 adcode 筛地级市；表无 adcode 时配合默认市界 ST_Union 仍可得正常市域线划",
    )
    parser.add_argument(
        "--admin-max-features",
        type=int,
        default=8000,
        help="与当前视窗 bbox 相交的行政区划最多取多少条（原先硬编码 300 会导致只显示一部分）",
    )
    parser.add_argument(
        "--admin-simplify-deg",
        type=float,
        default=0.002,
        help="行政区划几何抽稀（度，约0.002≈220m）。0 表示不抽稀；越小线越细、点越多",
    )
    g_basin = parser.add_mutually_exclusive_group()
    g_basin.add_argument(
        "--admin-by-basin",
        dest="admin_by_basin",
        action="store_true",
        help="按海河流域面与库 ST_Intersects 查区划（默认开启，出图与流域对齐）",
    )
    g_basin.add_argument(
        "--no-admin-by-basin",
        dest="admin_by_basin",
        action="store_false",
        help="不按流域面过滤，仅用当前图框 bbox 查区划",
    )
    g_union = parser.add_mutually_exclusive_group()
    g_union.add_argument(
        "--admin-city-union",
        dest="admin_city_union",
        action="store_true",
        help="库内按省+市 ST_Union 再抽稀得市界（默认开启，适用于乡镇表且 adcode 为空）",
    )
    g_union.add_argument(
        "--no-admin-city-union",
        dest="admin_city_union",
        action="store_false",
        help="关闭市界合并，按库内逐条几何查询（adcode 完整时可用）",
    )
    parser.set_defaults(admin_by_basin=True, admin_city_union=True)
    parser.add_argument(
        "--admin-metro-prefixes",
        default="11,12",
        help="hybrid 模式下按 adcode 前两位视为直辖市、细分到区级，逗号分隔，默认 11,12（北京、天津）",
    )
    parser.add_argument(
        "--force-display-latlon-swap",
        action="store_true",
        help="强制按「横轴纬度、纵轴经度」的 GeoTIFF 纠正为显示用经纬度（解决轴数字与标签反了）",
    )
    parser.add_argument(
        "--admin-query-buffer-ratio",
        type=float,
        default=0.22,
        help="查库用的经纬度包络相对当前图框再外扩的比例(0~1)，解决区划贴不满图框边缘；0 不扩",
    )
    parser.add_argument(
        "--map-basin-padding",
        type=float,
        default=0.12,
        help="最终出图视域相对流域包络各方向留白比例（避免大查询框+equal aspect 导致四周大块空白）",
    )
    parser.add_argument(
        "--precip-mm-factor",
        type=float,
        default=0.0,
        help="栅格值乘系数后当 mm 填色；0=自动(若掩膜内最大<0.02 则 x1000，按米→毫米)",
    )
    parser.add_argument(
        "--theme",
        default="light",
        choices=["light", "dark", "both"],
        help="出图主题：light=浅色，dark=深色，both=同时输出浅色+深色（深色自动附加 _dark 后缀）",
    )
    parser.add_argument(
        "--transparent-background",
        action="store_true",
        help="输出透明背景 PNG（适合叠加到底图；无降水和 nodata 档位也透明）",
    )
    parser.add_argument(
        "--color-scheme",
        default="default",
        choices=["default", "cma"],
        help="降水色标方案：default=现有方案，cma=中国气象局专题图分级色标",
    )
    return parser.parse_args()


def _extent_to_lonlat_ranges(extent: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """imshow extent=(left,right,bottom,top)，在经纬度栅格下即 (lon0,lon1,lat0,lat1)，排序成标准范围。"""
    left, right, bottom, top = extent
    lon_lo, lon_hi = sorted((left, right))
    lat_lo, lat_hi = sorted((bottom, top))
    return lon_lo, lon_hi, lat_lo, lat_hi


def _pad_lonlat_rect(
    lon_lo: float,
    lon_hi: float,
    lat_lo: float,
    lat_hi: float,
    pad_ratio: float,
) -> tuple[float, float, float, float]:
    """经纬度矩形按 pad_ratio 各向扩展（用于最终视域）。"""
    pr = max(0.0, float(pad_ratio))
    cx = 0.5 * (lon_lo + lon_hi)
    cy = 0.5 * (lat_lo + lat_hi)
    w = (lon_hi - lon_lo) * (1.0 + 2.0 * pr)
    h = (lat_hi - lat_lo) * (1.0 + 2.0 * pr)
    w = max(w, 0.08)
    h = max(h, 0.08)
    return cx - 0.5 * w, cx + 0.5 * w, cy - 0.5 * h, cy + 0.5 * h


def _intersect_lonlat_rects(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> Optional[tuple[float, float, float, float]]:
    lon_lo, lon_hi, lat_lo, lat_hi = max(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), min(a[3], b[3])
    if lon_lo >= lon_hi or lat_lo >= lat_hi:
        return None
    return (lon_lo, lon_hi, lat_lo, lat_hi)


def _layer_envelope_wgs84_lonlat(layer) -> Optional[tuple[float, float, float, float]]:
    """矢量图层包络四角变换到 WGS84，得 (lon_min, lon_max, lat_min, lat_max)，不依赖纬经启发式。"""
    try:
        xmin, xmax, ymin, ymax = layer.GetExtent()
        src = layer.GetSpatialRef()
        if src is None:
            return None
        src = src.Clone()
        dst = osr.SpatialReference()
        dst.ImportFromEPSG(4326)
        try:
            oams = getattr(osr, "OAMS_TRADITIONAL_GIS_ORDER", None)
            if oams is not None:
                dst.SetAxisMappingStrategy(oams)
                src.SetAxisMappingStrategy(oams)
        except Exception:
            pass
        trans = osr.CoordinateTransformation(src, dst)
        lons: list[float] = []
        lats: list[float] = []
        for x, y in ((xmin, ymin), (xmin, ymax), (xmax, ymin), (xmax, ymax)):
            try:
                pt = trans.TransformPoint(float(x), float(y))
                lons.append(float(pt[0]))
                lats.append(float(pt[1]))
            except Exception:
                continue
        if len(lons) < 2:
            return None
        return (min(lons), max(lons), min(lats), max(lats))
    except Exception:
        return None


def _expand_query_bbox(
    lon_min: float, lon_max: float, lat_min: float, lat_max: float, ratio: float
) -> tuple[float, float, float, float]:
    """以图框中心为基准外扩 (1+ratio)，便于 bbox 与屏幕边距内仍能查到行政区。"""
    r = max(0.0, float(ratio))
    if r <= 0:
        return lon_min, lat_min, lon_max, lat_max
    cx = 0.5 * (lon_min + lon_max)
    cy = 0.5 * (lat_min + lat_max)
    w = (lon_max - lon_min) * (1.0 + r)
    h = (lat_max - lat_min) * (1.0 + r)
    w = max(w, 0.05)
    h = max(h, 0.05)
    return cx - 0.5 * w, cy - 0.5 * h, cx + 0.5 * w, cy + 0.5 * h


def _parse_admin_metro_prefixes(s: str) -> frozenset[str]:
    out: list[str] = []
    for part in (s or "").split(","):
        p = part.strip()
        if len(p) >= 2 and p[:2].isdigit():
            out.append(p[:2])
    return frozenset(out) if out else frozenset({"11", "12"})


def _resolve_ec_nested_input_dir(base_dir: str, start_utc_compact: str) -> str:
    """
    若 GRIB/TIF 按 {根}/{年}/{YYYYMMDD}/ 存放，且 base 为 EC_AIFS 根（而非某日目录），
    则根据 start_utc 前 8 位定位到当日子目录（与 haihe_mcp_tools._ec_daily_search_directories 一致）。
    """
    base_dir = os.path.normpath((base_dir or "").strip())
    if not base_dir or not os.path.isdir(base_dir):
        return base_dir
    bn = os.path.basename(base_dir)
    if len(bn) == 8 and bn.isdigit():
        return base_dir
    st = (start_utc_compact or "").strip()
    if len(st) < 8 or not st[:8].isdigit():
        return base_dir
    y, ymd = st[:4], st[:8]
    cand = os.path.join(base_dir, y, ymd)
    return cand if os.path.isdir(cand) else base_dir


def _pick_latest_raster(input_dir: str) -> str:
    p = Path(input_dir)
    if not p.exists():
        raise FileNotFoundError(f"输入目录不存在: {input_dir}")
    globs = ("*.tif", "*.tiff", "*.grib2", "*.grb2", "*.grib")
    candidates = sorted(
        [x for pat in globs for x in p.glob(pat)],
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"目录中未找到 tif/grib: {input_dir}")
    return str(candidates[0])


def _extract_utc_from_name(path: str) -> Optional[datetime]:
    name = Path(path).name
    m = re.search(r"(20\d{8})(\d{2})", name)
    if not m:
        return None
    dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H")
    return dt.replace(tzinfo=timezone.utc)


def _resolve_cycle_utc(path: str, start_utc_arg: str) -> datetime:
    if start_utc_arg:
        dt = datetime.strptime(start_utc_arg, "%Y%m%d%H")
        return dt.replace(tzinfo=timezone.utc)
    parsed = _extract_utc_from_name(path)
    if parsed:
        return parsed
    return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _open_raster(path: str) -> RasterData:
    ds = gdal.Open(path)
    if ds is None:
        raise RuntimeError(f"无法打开栅格: {path}")
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray().astype(np.float32)
    nodata = band.GetNoDataValue()
    gt = ds.GetGeoTransform()
    proj = ds.GetProjection()
    xsize = ds.RasterXSize
    ysize = ds.RasterYSize
    ds = None
    return RasterData(
        array=arr, geotransform=gt, projection_wkt=proj, nodata=nodata, xsize=xsize, ysize=ysize
    )


def _open_rolling_forecast_nc(path: str, hour: int) -> RasterData:
    """从滚动预报 .nc 提取指定时效的 TP1H 2D 场，构造 RasterData 供渲染管线使用。

    NetCDF 结构：TP1H (time, lat, lon)，lat 升序、lon 升序。GDAL 惯例 row 0 = max lat，
    故对数组做垂直翻转；geotransform 按像元中心约定推算边界。
    """
    import xarray as xr  # 懒导入
    ds = xr.open_dataset(path, engine="netcdf4", decode_times=False)
    try:
        if hour not in ds["time"].values:
            raise ValueError(f"滚动预报 .nc 中无 time={hour} 时效: {path}")
        tp = ds["TP1H"].sel(time=hour).load()
    finally:
        ds.close()
    lat = tp["lat"].values  # 升序
    lon = tp["lon"].values  # 升序
    arr = tp.values.astype(np.float32)  # (lat, lon)
    arr = arr[::-1, :]  # row 0 = max lat
    lon_res = float(lon[1] - lon[0])
    lat_res = float(lat[1] - lat[0])
    # 像元中心→边界：GT[0] = lon_min - res/2, GT[3] = lat_max + res/2
    gt = (
        float(lon[0]) - lon_res / 2,
        lon_res,
        0.0,
        float(lat[-1]) + lat_res / 2,
        0.0,
        -lat_res,
    )
    return RasterData(
        array=arr,
        geotransform=gt,
        projection_wkt=wgs84_projection_wkt_traditional(),
        nodata=None,
        xsize=int(arr.shape[1]),
        ysize=int(arr.shape[0]),
    )


def _open_raster_or_nc(path: str, hour: Optional[int] = None) -> RasterData:
    """按文件扩展名分发：.nc 用滚动预报读取器（需 hour），其余用 GDAL。"""
    if path.lower().endswith(".nc"):
        if hour is None:
            raise ValueError("打开 .nc 需要指定 hour 参数")
        return _open_rolling_forecast_nc(path, hour)
    return _open_raster(path)


def _open_vector_layer(path: str):
    ds = ogr.Open(path)
    if ds is None:
        raise RuntimeError(f"无法打开矢量边界文件: {path}")
    layer = ds.GetLayer(0)
    if layer is None:
        raise RuntimeError(f"矢量图层为空: {path}")
    return ds, layer


def _apply_traditional_gis_order_srs(srs) -> None:
    """GDAL3+ 地理坐标默认轴序与经典 GeoTIFF(easting,northing) 不一致时会导致栅格化掩膜全 0。"""
    try:
        oams = getattr(osr, "OAMS_TRADITIONAL_GIS_ORDER", None)
        if oams is not None:
            srs.SetAxisMappingStrategy(oams)
    except Exception:
        pass


def wgs84_projection_wkt_traditional() -> str:
    """WGS84 WKT，轴序与 IDW/Affine(lon,lat) 栅格一致，供实况 GeoTIFF 与 RasterizeLayer 共用。"""
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    _apply_traditional_gis_order_srs(srs)
    return srs.ExportToWkt()


def _geographic_wkt_force_traditional_gis(proj_wkt: str) -> str:
    """将地理坐标系的 WKT 统一为 traditional GIS 轴序，避免 RasterizeLayer 与 GeoTransform 错位。"""
    if not (proj_wkt or "").strip():
        return proj_wkt
    srs = osr.SpatialReference()
    if srs.ImportFromWkt(proj_wkt) != 0:
        return proj_wkt
    try:
        if srs.IsGeographic():
            _apply_traditional_gis_order_srs(srs)
            return srs.ExportToWkt()
    except Exception:
        pass
    return proj_wkt


def _reproject_vector_to_raster(vector_ds, layer, raster_wkt: str):
    raster_srs = osr.SpatialReference()
    raster_srs.ImportFromWkt(_geographic_wkt_force_traditional_gis(raster_wkt))
    _apply_traditional_gis_order_srs(raster_srs)

    src_srs = layer.GetSpatialRef()
    if src_srs is None:
        raise RuntimeError("矢量数据缺少坐标系，无法与栅格对齐。")
    src_srs = src_srs.Clone()
    _apply_traditional_gis_order_srs(src_srs)

    if src_srs.IsSame(raster_srs):
        return vector_ds, layer

    mem_driver = ogr.GetDriverByName("MEM")
    out_ds = mem_driver.CreateDataSource("wrk")
    out_layer = out_ds.CreateLayer("basin", srs=raster_srs, geom_type=ogr.wkbMultiPolygon)
    out_layer.CreateField(ogr.FieldDefn("id", ogr.OFTInteger))
    trans = osr.CoordinateTransformation(src_srs, raster_srs)

    layer.ResetReading()
    fid = 1
    for feat in layer:
        geom = feat.GetGeometryRef()
        if geom is None:
            continue
        g = geom.Clone()
        g.Transform(trans)
        out_feat = ogr.Feature(out_layer.GetLayerDefn())
        out_feat.SetField("id", fid)
        out_feat.SetGeometry(g)
        out_layer.CreateFeature(out_feat)
        out_feat = None
        fid += 1

    return out_ds, out_layer


def _rasterize_mask(layer, xsize: int, ysize: int, gt: tuple, proj_wkt: str) -> np.ndarray:
    proj_use = _geographic_wkt_force_traditional_gis(proj_wkt)
    mem = gdal.GetDriverByName("MEM").Create("", xsize, ysize, 1, gdal.GDT_Byte)
    mem.SetGeoTransform(gt)
    mem.SetProjection(proj_use)
    band = mem.GetRasterBand(1)
    band.Fill(0)
    gdal.RasterizeLayer(mem, [1], layer, burn_values=[1], options=["ALL_TOUCHED=TRUE"])
    mask = band.ReadAsArray().astype(bool)
    mem = None
    return mask


def _format_title(start_utc: datetime, lead_hours: int) -> tuple[str, str]:
    """English map title (forecast)."""
    end_utc = start_utc + timedelta(hours=lead_hours)
    line1 = "Haihe Basin Precipitation Forecast"
    line2 = f"{start_utc:%Y-%m-%d %H}:00 - {end_utc:%Y-%m-%d %H}:00 (UTC)"
    return line1, line2


def _format_period_en(start_utc: datetime, lead_hours: int) -> str:
    end_utc = start_utc + timedelta(hours=lead_hours)
    return f"{start_utc:%Y-%m-%d %H}:00 - {end_utc:%Y-%m-%d %H}:00 (UTC)"


def _coerce_precip_display_floor_mm(raw) -> float:
    """JSON/队列可能传入 str；非法或非正则当 0（关闭微量屏蔽）。"""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return v if v > 0.0 else 0.0


def _normalize_theme_name(theme: str) -> str:
    t = (theme or "light").strip().lower()
    return t if t in {"light", "dark", "both"} else "light"


def _resolve_theme_style(theme: str, transparent_background: bool) -> RenderThemeStyle:
    t = _normalize_theme_name(theme)
    if t == "dark":
        style = RenderThemeStyle(
            name="dark",
            figure_facecolor="#10151f",
            axes_facecolor="#10151f",
            title_color="#eaf2ff",
            label_color="#d6e4ff",
            grid_color="#7f92b3",
            boundary_color="#ffb1b1",
            admin_line_color="#9bb1d3",
            dry_band_color="#1f2b3d",
            nodata_color="#1f2b3d",
            cbar_edge_color="#c9d8f3",
        )
    else:
        style = RenderThemeStyle(
            name="light",
            figure_facecolor="#ffffff",
            axes_facecolor="#f0f0f0",
            title_color="#1a1a1a",
            label_color="#1f1f1f",
            grid_color="#9aa5b1",
            boundary_color="#a40000",
            admin_line_color="#5a5a5a",
            dry_band_color="#f0f0f0",
            nodata_color="#f0f0f0",
            cbar_edge_color="#4f4f4f",
        )
    if not transparent_background:
        return style
    return RenderThemeStyle(
        name=style.name,
        figure_facecolor="none",
        axes_facecolor="none",
        title_color=style.title_color,
        label_color=style.label_color,
        grid_color=style.grid_color,
        boundary_color=style.boundary_color,
        admin_line_color=style.admin_line_color,
        dry_band_color="#00000000",
        nodata_color="#00000000",
        cbar_edge_color=style.cbar_edge_color,
    )


def _build_cmap(
    max_mm: float = 0.0,
    dry_band_color: str = "#f0f0f0",
    nodata_color: str = "#f0f0f0",
    color_scheme: str = "default",
):
    scheme = (color_scheme or "default").strip().lower()
    if scheme == "cma":
        # CMA 降水专题图分级（mm）
        levels = [0.0, 0.1, 10.0, 25.0, 50.0, 100.0, 250.0, 10000.0]
        colors = [
            "#ffffffff",  # 无降水
            "#a6f28eff",  # 0-10
            "#3bb941ff",  # 10-25
            "#61b8ffff",  # 25-50
            "#0001fcff",  # 50-100
            "#fc00f9ff",  # 100-250
            "#7f0141ff",  # >=250
        ]
        cmap = ListedColormap(colors)
        cmap.set_bad(nodata_color)
        norm = BoundaryNorm(levels, cmap.N, clip=True)
        return levels, cmap, norm

    # max<=50mm 用细分级：若仅因局地 ≥10mm（如 IDW 牛眼）就切到强降水色标，「0.1–10mm」会占一整档，
    # 场里大量像元落在该档 → 整块均匀浅绿；提高阈值并补 10–20、20–50 可避免此现象。
    # 边界用 <=50：恰为 50mm 时仍应用细分级末档 [20,50]，避免误切粗分级。
    if not np.isfinite(max_mm):
        max_mm = 0.0
    if max_mm <= 50.0:
        levels = [0.0, 0.1, 0.5, 1, 2, 5, 10, 20, 50]
        colors = [
            dry_band_color,  # 0-0.1
            "#d9f7cf",  # 0.1-0.5
            "#9be56d",  # 0.5-1
            "#49c64d",  # 1-2
            "#1ca8e3",  # 2-5
            "#2e57ff",  # 5-10
            "#7ed957",  # 10-20
            "#2db84d",  # 20-50
        ]
    else:
        # 常规强降水分级（单位 mm）
        levels = [0.0, 0.1, 10, 25, 50, 100, 250, 1000]
        colors = [
            dry_band_color,  # 0-0.1mm
            "#c9f7c5",  # 0.1-10
            "#7ed957",  # 10-25
            "#2db84d",  # 25-50
            "#00a2ff",  # 50-100
            "#1737ff",  # 100-250
            "#d50000",  # >=250
        ]
    cmap = ListedColormap(colors)
    cmap.set_bad(nodata_color)
    norm = BoundaryNorm(levels, cmap.N, clip=True)
    return levels, cmap, norm


def _draw_boundary(ax, layer, swap_xy: bool, line_color: str):
    layer.ResetReading()
    for feat in layer:
        geom = feat.GetGeometryRef()
        if geom is None:
            continue
        geom_type = geom.GetGeometryType()
        if geom_type in (ogr.wkbPolygon, ogr.wkbPolygon25D):
            _draw_polygon(ax, geom, swap_xy, line_color)
        elif geom_type in (ogr.wkbMultiPolygon, ogr.wkbMultiPolygon25D):
            for i in range(geom.GetGeometryCount()):
                _draw_polygon(ax, geom.GetGeometryRef(i), swap_xy, line_color)


def _draw_polygon(ax, poly_geom, swap_xy: bool, line_color: str):
    ring = poly_geom.GetGeometryRef(0)
    if ring is None:
        return
    pts = ring.GetPoints()
    if not pts:
        return
    if swap_xy:
        xs = [p[1] for p in pts]
        ys = [p[0] for p in pts]
    else:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
    ax.plot(xs, ys, color=line_color, linewidth=1.2, zorder=8)


def _set_font(font_path: str = "") -> bool:
    """Titles/axes use English by default (DejaVu). Optional font_path for admin overlay labels."""
    if font_manager is not None and font_path and os.path.exists(font_path):
        try:
            font_manager.fontManager.addfont(font_path)
            name = font_manager.FontProperties(fname=font_path).get_name()
            matplotlib.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            matplotlib.rcParams["axes.unicode_minus"] = False
            return True
        except Exception:
            pass
    matplotlib.rcParams["font.sans-serif"] = ["DejaVu Sans", "Arial", "Helvetica", "sans-serif"]
    matplotlib.rcParams["axes.unicode_minus"] = False
    return False


def _normalize_pg_row(row: dict) -> dict:
    """RealDictCursor 列名可能是大小写混合，统一成小写键。"""
    return {str(k).lower(): v for k, v in row.items()}


def _row_text(row: dict, *keys: str) -> str:
    for k in keys:
        v = row.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def _normalize_adcode_6(row: dict) -> str:
    """六位县级/国测 adcode，整数列可能无前导零；列名各地库不一致。"""
    ad = _row_text(
        row,
        "adcode",
        "ad_code",
        "xzqdm",
        "zone_code",
        "areacode",
        "area_code",
        "region_code",
        "district_code",
        "code_id",
        "bm",
    )
    if not ad:
        return ""
    if ad.isdigit():
        z = ad.zfill(6)
        return z[-6:] if len(z) > 6 else z
    return ad


def _is_prefecture_level_adcode(ad6: str) -> bool:
    """
    地级市及直辖市整市范围（不含区县）：
    - 110000/120000/310000/500000 直辖市
    - 其它：末两位为 00、且非 xx0000 省码 → 地级
    """
    if len(ad6) != 6 or not ad6.isdigit():
        return False
    if ad6 in ("110000", "120000", "310000", "500000"):
        return True
    if ad6[2:6] == "0000":
        return False
    return ad6[4:6] == "00"


def _match_admin_hybrid_metro_district_else_city(row: dict, metro_adcode_prefixes: frozenset[str]) -> bool:
    """
    直辖市（默认 adcode 11 北京、12 天津）：保留到区/县级（有区县名）。
    其它地区：只要地级市界（有市名、无区县名）。
    """
    row = _normalize_pg_row(row)
    city_name = _row_text(row, "city_name", "cityname", "city", "shi_name")
    county_name = _row_text(row, "county_name", "countyname", "district_name", "qu_name", "xian_name")
    ad = _normalize_adcode_6(row)
    pref = ad[:2] if ad.isdigit() and len(ad) >= 2 else ""
    if pref in metro_adcode_prefixes:
        return bool(county_name)
    return bool(city_name) and not county_name


def _match_admin_level_by_names(
    row: dict, admin_level: str, metro_adcode_prefixes: frozenset[str] | None = None
) -> bool:
    """
    根据 city_name / county_name（及常见别名）判断层级：
    - 仅有市级字段、无区县字段 → 市级
    - 有区县字段 → 区县级
    """
    if admin_level == "all":
        return True

    row = _normalize_pg_row(row)

    if admin_level == "city_adcode":
        return _is_prefecture_level_adcode(_normalize_adcode_6(row))

    city_name = _row_text(row, "city_name", "cityname", "city", "shi_name")
    county_name = _row_text(row, "county_name", "countyname", "district_name", "qu_name", "xian_name")

    is_city = bool(city_name) and not county_name
    is_district = bool(county_name)

    if admin_level == "hybrid":
        return _match_admin_hybrid_metro_district_else_city(row, metro_adcode_prefixes or frozenset({"11", "12"}))
    if admin_level == "city":
        return is_city
    if admin_level == "district":
        return is_district
    return is_city or is_district


def _geojson_extract_outer_rings(geom_obj: dict) -> list[list[list[float]]]:
    """从 GeoJSON 中提取外环（支持 Polygon / MultiPolygon / GeometryCollection）。"""
    if not geom_obj or not isinstance(geom_obj, dict):
        return []
    t = geom_obj.get("type")
    coords = geom_obj.get("coordinates")
    rings: list[list[list[float]]] = []
    if t == "Polygon" and coords:
        rings.append(coords[0])
    elif t == "MultiPolygon" and coords:
        for poly in coords:
            if poly:
                rings.append(poly[0])
    elif t == "GeometryCollection":
        for g in geom_obj.get("geometries") or []:
            rings.extend(_geojson_extract_outer_rings(g))
    return rings


def _ring_abs_area_deg2(ring: list) -> float:
    if len(ring) < 3:
        return 0.0
    xs = [float(p[0]) for p in ring]
    ys = [float(p[1]) for p in ring]
    if xs[0] != xs[-1] or ys[0] != ys[-1]:
        xs.append(xs[0])
        ys.append(ys[0])
    s = 0.0
    for i in range(len(xs) - 1):
        s += xs[i] * ys[i + 1] - xs[i + 1] * ys[i]
    return abs(s) * 0.5


def _largest_ring_per_city_from_rows(rows: list) -> tuple[list[list[list[float]]], int]:
    """
    库表多为区县碎面且无标准市级 adcode 时：按市名（+省名）分组，每组只保留外环面积最大的一个面，近似「市界」。
    """
    best: dict[str, tuple[float, list]] = {}
    for row in rows:
        row_n = _normalize_pg_row(row)
        city = _row_text(row_n, "city_name", "cityname", "shi_name")
        prov = _row_text(row_n, "province_name", "prov_name", "sheng_name", "sf_name")
        key = f"{prov}|{city}" if city else f"_ad|{_normalize_adcode_6(row_n)[:4]}"
        gj = row_n.get("geom_json")
        if not gj:
            continue
        try:
            geom_obj = json.loads(gj)
        except Exception:
            continue
        rings = _geojson_extract_outer_rings(geom_obj)
        for ring in rings:
            if not ring:
                continue
            ar = _ring_abs_area_deg2(ring)
            prev = best.get(key)
            if prev is None or ar > prev[0]:
                best[key] = (ar, ring)
    out = [t[1] for t in best.values()]
    return out, len(best)


def _rows_to_polygons(
    rows: list,
    admin_level: str,
    metro_prefixes: frozenset[str] | None = None,
) -> tuple[list[list[list[float]]], int]:
    """按层级筛选并解析几何，返回 (rings, 通过层级筛选的行数)。"""
    polygons: list[list[list[float]]] = []
    passed_level_cnt = 0
    for row in rows:
        row_n = _normalize_pg_row(row)
        if not _match_admin_level_by_names(row_n, admin_level, metro_prefixes):
            continue
        passed_level_cnt += 1
        geom_json = row_n.get("geom_json")
        if not geom_json:
            continue
        try:
            geom_obj = json.loads(geom_json)
        except Exception:
            continue
        for ring in _geojson_extract_outer_rings(geom_obj):
            if ring:
                polygons.append(ring)
    return polygons, passed_level_cnt


def fetch_admin_clip_union_wkt(
    config_path: str,
    basin_wkt: str,
    *,
    admin_city_union: bool = True,
    max_features: int = 50_000,
    admin_simplify_deg: float = 0.002,
) -> Optional[str]:
    """
    从 PostGIS 取与流域面相交的行政区划并集（与市界 ST_Union×流域 逻辑一致），
    返回 WGS84 WKT，供实况 IDW 按行政区裁剪（流域外 / 行政外 nodata）。
    """
    if psycopg2 is None or RealDictCursor is None:
        print("[admin_clip] psycopg2 不可用，无法生成行政区裁剪面。")
        return None
    if not os.path.exists(config_path):
        print(f"[admin_clip] 配置文件不存在: {config_path}")
        return None
    bw = (basin_wkt or "").strip()
    if not bw:
        print("[admin_clip] basin_wkt 为空，跳过。")
        return None

    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding="utf-8")
    if "postgres" not in cfg:
        print("[admin_clip] config.ini 缺少 [postgres]。")
        return None
    pg = cfg["postgres"]
    schema = pg.get("schema", "public")
    srid = pg.getint("srid", 4326)
    simp = float(admin_simplify_deg)
    lim = max(5000, min(int(max_features) * 50, 150_000))
    geom_to_4326 = (
        "ST_Transform(CASE WHEN ST_SRID(a.geom) = 0 "
        f"THEN ST_SetSRID(a.geom, {srid}) ELSE a.geom END, 4326)"
    )
    basin4326 = """
        ST_Multi(
            ST_CollectionExtract(
                ST_MakeValid(ST_GeomFromText(%(basin_wkt)s, 4326)),
                3
            )
        )
    """
    sql_by_city = f"""
        WITH basin4326 AS ( SELECT {basin4326} AS g ),
        f AS (
            SELECT
                a.province_name,
                COALESCE(
                    NULLIF(btrim(a.city_name::text), ''),
                    NULLIF(btrim(a.county_name::text), ''),
                    NULLIF(btrim(a.name::text), ''),
                    '_unk'
                ) AS city_name,
                {geom_to_4326} AS gg
            FROM {schema}.haihe_admin_division a
            CROSS JOIN basin4326 b
            WHERE {geom_to_4326} && b.g
              AND ST_Intersects({geom_to_4326}, b.g)
        ),
        by_city AS (
            SELECT ST_Union(gg) AS u FROM f GROUP BY province_name, city_name
        ),
        merged AS ( SELECT ST_UnaryUnion(ST_Collect(u)) AS g FROM by_city )
        SELECT ST_AsText(
            ST_Multi(
                ST_CollectionExtract(
                    ST_MakeValid(
                        CASE WHEN %(simp)s::double precision > 0
                            THEN coalesce(ST_SimplifyPreserveTopology(g, %(simp)s::double precision), g)
                            ELSE g
                        END
                    ),
                    3
                )
            )
        ) AS clip_wkt
        FROM merged WHERE g IS NOT NULL
    """
    sql_flat = f"""
        WITH basin4326 AS ( SELECT {basin4326} AS g ),
        f AS (
            SELECT {geom_to_4326} AS gg
            FROM {schema}.haihe_admin_division a
            CROSS JOIN basin4326 b
            WHERE {geom_to_4326} && b.g
              AND ST_Intersects({geom_to_4326}, b.g)
            LIMIT %(lim)s
        ),
        merged AS ( SELECT ST_UnaryUnion(ST_Collect(gg)) AS g FROM f )
        SELECT ST_AsText(
            ST_Multi(
                ST_CollectionExtract(
                    ST_MakeValid(
                        CASE WHEN %(simp)s::double precision > 0
                            THEN coalesce(ST_SimplifyPreserveTopology(g, %(simp)s::double precision), g)
                            ELSE g
                        END
                    ),
                    3
                )
            )
        ) AS clip_wkt
        FROM merged WHERE g IS NOT NULL
    """
    sql_use = sql_by_city if admin_city_union else sql_flat
    conn = None
    try:
        conn = psycopg2.connect(
            host=pg.get("host", "127.0.0.1"),
            port=pg.getint("port", 5432),
            dbname=pg.get("dbname"),
            user=pg.get("user"),
            password=pg.get("password"),
            sslmode=pg.get("sslmode", "disable"),
            connect_timeout=pg.getint("connect_timeout", 5),
        )
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            params = {"basin_wkt": bw, "simp": simp, "lim": lim}
            cur.execute(sql_use, params)
            row = cur.fetchone()
        if not row:
            print("[admin_clip] 查询无行返回。")
            return None
        wkt = row.get("clip_wkt") or row.get("CLIP_WKT")
        if wkt is None:
            return None
        s = str(wkt).strip()
        if not s or s.upper() == "NONE":
            return None
        print(f"[admin_clip] 行政区并集 WKT 已生成（city_union={admin_city_union}，条带 limit={lim}）")
        return s
    except Exception as exc:
        print(f"[admin_clip] 查询失败: {exc}")
        return None
    finally:
        if conn is not None:
            conn.close()


def _mem_polygon_layer_from_wkt(wkt: str, proj_wkt: str):
    """构造单要素内存图层供 Rasterize；失败返回 (None, None)。"""
    if not (wkt or "").strip():
        return None, None
    g = ogr.CreateGeometryFromWkt(wkt.strip())
    if g is None:
        return None, None
    gt = g.GetGeometryType()
    if gt in (ogr.wkbPolygon, ogr.wkbPolygon25D):
        poly = g
        g = ogr.Geometry(ogr.wkbMultiPolygon)
        g.AddGeometry(poly)
    drv = ogr.GetDriverByName("MEM")
    ds = drv.CreateDataSource("admin_clip")
    srs = osr.SpatialReference()
    srs.ImportFromWkt(_geographic_wkt_force_traditional_gis(proj_wkt))
    _apply_traditional_gis_order_srs(srs)
    lyr = ds.CreateLayer("clip", srs=srs, geom_type=ogr.wkbMultiPolygon)
    feat = ogr.Feature(lyr.GetLayerDefn())
    feat.SetGeometry(g)
    lyr.CreateFeature(feat)
    return ds, lyr


def _raster_copy_apply_mask_nodata(
    arr: np.ndarray, mask: np.ndarray, nodata: float = -9999.0
) -> np.ndarray:
    out = arr.astype(np.float32, copy=True)
    out[~mask] = nodata
    return out


def _load_admin_divisions_from_db(
    config_path: str,
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    admin_level: str,
    max_features: int,
    admin_simplify_deg: float,
    basin_wkt: str,
    metro_prefixes: frozenset[str] | None = None,
    admin_city_union: bool = False,
) -> list[list[list[float]]]:
    if psycopg2 is None or RealDictCursor is None:
        print("[admin] psycopg2 不可用，跳过行政区划叠加。")
        return []
    if not os.path.exists(config_path):
        print(f"[admin] 配置文件不存在: {config_path}")
        return []

    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding="utf-8")
    if "postgres" not in cfg:
        print("[admin] config.ini 缺少 [postgres]，跳过行政区划叠加。")
        return []

    pg = cfg["postgres"]
    ac_col = pg.get("admin_adcode_column", "adcode").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", ac_col):
        print(f"[admin] admin_adcode_column 非法 {ac_col!r}，改用 adcode")
        ac_col = "adcode"
    adcode_sql = f"a.{ac_col} AS adcode"
    if not admin_city_union:
        print(f"[admin] SQL 取 adcode 列: a.{ac_col}")
    # 防止 numpy.float64 进入 SQL 参数导致 "模式 np 不存在"
    min_x = float(min_x)
    min_y = float(min_y)
    max_x = float(max_x)
    max_y = float(max_y)
    schema = pg.get("schema", "public")
    srid = pg.getint("srid", 4326)
    simp = float(admin_simplify_deg)
    # 统一到 WGS84；geom 无 SRID 时用 config.ini 的 srid 再变换（常见 4490/4326）
    geom_to_4326 = (
        "ST_Transform(CASE WHEN ST_SRID(a.geom) = 0 "
        f"THEN ST_SetSRID(a.geom, {srid}) ELSE a.geom END, 4326)"
    )
    geom_json_sql = f"""
        ST_AsGeoJSON(
            CASE WHEN %(simp)s::double precision > 0
                THEN ST_SimplifyPreserveTopology({geom_to_4326}, %(simp)s::double precision)
                ELSE {geom_to_4326}
            END
        )
    """
    sql_bbox = f"""
        WITH bbox AS (
            SELECT ST_MakeEnvelope(%(min_x)s, %(min_y)s, %(max_x)s, %(max_y)s, 4326) AS g
        )
        SELECT
            {adcode_sql},
            a.city_name,
            a.county_name,
            a.name,
            {geom_json_sql} AS geom_json
        FROM {schema}.haihe_admin_division a
        CROSS JOIN bbox b
        WHERE {geom_to_4326} && b.g
          AND ST_Intersects({geom_to_4326}, b.g)
        ORDER BY a.id
        LIMIT %(max_features)s
    """
    sql_basin = f"""
        WITH basin4326 AS (
            SELECT ST_Multi(
                ST_CollectionExtract(
                    ST_MakeValid(ST_GeomFromText(%(basin_wkt)s, 4326)),
                    3
                )
            ) AS g
        )
        SELECT
            {adcode_sql},
            a.city_name,
            a.county_name,
            a.name,
            ST_AsGeoJSON(
                CASE WHEN %(simp)s::double precision > 0
                    THEN ST_SimplifyPreserveTopology(
                        ST_CollectionExtract(
                            ST_MakeValid(ST_Intersection({geom_to_4326}, b.g)),
                            3
                        ),
                        %(simp)s::double precision
                    )
                    ELSE ST_CollectionExtract(
                        ST_MakeValid(ST_Intersection({geom_to_4326}, b.g)),
                        3
                    )
                END
            ) AS geom_json
        FROM {schema}.haihe_admin_division a
        CROSS JOIN basin4326 b
        WHERE {geom_to_4326} && b.g
          AND ST_Intersects({geom_to_4326}, b.g)
          AND NOT ST_IsEmpty(ST_Intersection({geom_to_4326}, b.g))
        ORDER BY a.id
        LIMIT %(max_features)s
    """
    sql_all = f"""
        SELECT
            {adcode_sql},
            a.city_name,
            a.county_name,
            a.name,
            {geom_json_sql} AS geom_json
        FROM {schema}.haihe_admin_division a
        ORDER BY a.id
        LIMIT %(max_features)s
    """
    union_json = f"""
        ST_AsGeoJSON(
            CASE WHEN %(simp)s::double precision > 0
                THEN ST_SimplifyPreserveTopology(ST_Union(gg), %(simp)s::double precision)
                ELSE ST_Union(gg)
            END
        )
    """
    sql_basin_union = f"""
        WITH basin4326 AS (
            SELECT ST_Multi(
                ST_CollectionExtract(
                    ST_MakeValid(ST_GeomFromText(%(basin_wkt)s, 4326)),
                    3
                )
            ) AS g
        ),
        f AS (
            SELECT
                a.province_name,
                COALESCE(
                    NULLIF(btrim(a.city_name::text), ''),
                    NULLIF(btrim(a.county_name::text), ''),
                    NULLIF(btrim(a.name::text), ''),
                    '_unk'
                ) AS city_name,
                ST_CollectionExtract(
                    ST_MakeValid(ST_Intersection({geom_to_4326}, b.g)),
                    3
                ) AS gg
            FROM {schema}.haihe_admin_division a
            CROSS JOIN basin4326 b
            WHERE {geom_to_4326} && b.g
              AND ST_Intersects({geom_to_4326}, b.g)
              AND NOT ST_IsEmpty(ST_Intersection({geom_to_4326}, b.g))
        )
        SELECT
            province_name,
            city_name,
            {union_json} AS geom_json
        FROM f
        GROUP BY province_name, city_name
        ORDER BY province_name, city_name
        LIMIT %(max_features)s
    """
    sql_bbox_union = f"""
        WITH bbox AS (
            SELECT ST_MakeEnvelope(%(min_x)s, %(min_y)s, %(max_x)s, %(max_y)s, 4326) AS g
        ),
        f AS (
            SELECT
                a.province_name,
                COALESCE(
                    NULLIF(btrim(a.city_name::text), ''),
                    NULLIF(btrim(a.county_name::text), ''),
                    NULLIF(btrim(a.name::text), ''),
                    '_unk'
                ) AS city_name,
                {geom_to_4326} AS gg
            FROM {schema}.haihe_admin_division a
            CROSS JOIN bbox b
            WHERE {geom_to_4326} && b.g
              AND ST_Intersects({geom_to_4326}, b.g)
        )
        SELECT
            province_name,
            city_name,
            {union_json} AS geom_json
        FROM f
        GROUP BY province_name, city_name
        ORDER BY province_name, city_name
        LIMIT %(max_features)s
    """
    cap = max(1, min(int(max_features), 100_000))
    params = {
        "min_x": min_x,
        "min_y": min_y,
        "max_x": max_x,
        "max_y": max_y,
        "max_features": cap,
        "simp": simp,
    }

    polygons: list[list[list[float]]] = []
    conn = None
    try:
        conn = psycopg2.connect(
            host=pg.get("host", "127.0.0.1"),
            port=pg.getint("port", 5432),
            dbname=pg.get("dbname"),
            user=pg.get("user"),
            password=pg.get("password"),
            sslmode=pg.get("sslmode", "disable"),
            connect_timeout=pg.getint("connect_timeout", 5),
        )
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            bw = (basin_wkt or "").strip()
            union_handled = False
            rows: list = []

            if admin_city_union:
                print(
                    "[admin] 乡镇表市界：库内 ST_Union(province_name,city_name) "
                    f"（--admin-city-union；simplify={simp}°）"
                )
                u_params = {**params, "simp": simp}
                if bw:
                    cur.execute(
                        sql_basin_union,
                        {"basin_wkt": bw, "max_features": cap, "simp": simp},
                    )
                    rows = cur.fetchall()
                    print(f"[admin] ST_Union×流域相交 → {len(rows)} 个市（省+市分组）")
                if len(rows) == 0:
                    print("[admin] ST_Union 改用图框 bbox …")
                    cur.execute(sql_bbox_union, u_params)
                    rows = cur.fetchall()
                    print(f"[admin] ST_Union×bbox → {len(rows)} 个市")
                if rows:
                    polygons, _ = _rows_to_polygons(rows, "all", metro_prefixes)
                    union_handled = True
                    print(f"[admin] ST_Union 可绘制环数: {len(polygons)}")
                else:
                    print("[admin] ST_Union 无结果，回退乡镇逐条查询")

            if not union_handled:
                if bw:
                    cur.execute(sql_basin, {"basin_wkt": bw, "max_features": cap, "simp": simp})
                    rows = cur.fetchall()
                    print(f"[admin] 流域面相交(4326)返回 {len(rows)} 条（LIMIT {cap}；simplify={simp}°）")
                else:
                    rows = []

                if len(rows) == 0:
                    print("[admin] 改用图框 bbox（4326 + ST_Transform(geom,4326)）查询区划…")
                    cur.execute(sql_bbox, params)
                    rows = cur.fetchall()
                    print(f"[admin] bbox 查询返回 {len(rows)} 条")

                if len(rows) >= cap:
                    print(
                        f"[admin] 提示: 已达到上限 {cap}，可提高 --admin-max-features（最大 100000）"
                    )

                if not rows:
                    # 兜底：仍无数据则取表内前 N 条（仅应急，几何应在 WGS84）
                    cur.execute(
                        sql_all,
                        {"max_features": min(cap * 2, 100_000), "simp": simp},
                    )
                    rows = cur.fetchall()
                    print(f"[admin] 全表 LIMIT 兜底返回 {len(rows)} 条（请检查库连通与表数据）")

                polygons, passed_level_cnt = _rows_to_polygons(rows, admin_level, metro_prefixes)

                if admin_level == "city_adcode" and rows and (passed_level_cnt == 0 or len(polygons) == 0):
                    print("[admin] adcode 规则未筛到市级面，尝试仅用 city_name（无 county）…")
                    polygons, passed_level_cnt = _rows_to_polygons(rows, "city", metro_prefixes)

                if admin_level != "all" and rows and (passed_level_cnt == 0 or len(polygons) == 0):
                    sample = [_normalize_adcode_6(_normalize_pg_row(r)) for r in rows[:5]]
                    print(f"[admin] adcode 样例(前5条): {sample}")
                    if sample and all(not s for s in sample):
                        ks = list(_normalize_pg_row(rows[0]).keys())
                        print(
                            "[admin] 提示: adcode 全空（与库注释「源数据未提供」一致），"
                            "乡镇表可加 --admin-city-union；或补全 adcode 列"
                        )
                        print(f"[admin] 首行字段键={ks}")
                    agg, n_grp = _largest_ring_per_city_from_rows(rows)
                    if len(agg) >= 6:
                        polygons = agg
                        print(
                            f"[admin] 按 city_name(+省) 聚合：每市保留最大面，共 {len(polygons)} 个（分组 {n_grp}）"
                        )
                    else:
                        print(
                            f"[admin] 聚合仅 {len(agg)} 个面，回退 all（共 {len(rows)} 条）— 建议检查 city_name 字段"
                        )
                        polygons, _ = _rows_to_polygons(rows, "all", metro_prefixes)

                if rows and len(polygons) == 0:
                    sample = _normalize_pg_row(rows[0])
                    gj = sample.get("geom_json")
                    preview = (gj[:120] + "…") if isinstance(gj, str) and len(gj) > 120 else gj
                    print(f"[admin] 警告: 仍无可用面，首条 geom_json 预览: {preview!r}")
    except Exception as exc:
        print(f"[admin] 查询失败: {exc}")
        return []
    finally:
        if conn is not None:
            conn.close()
    print(f"[admin] 可绘制 polygon 数量: {len(polygons)}")
    return polygons


def _draw_admin_polygons(ax, polygons: list[list[list[float]]], line_color: str):
    for ring in polygons:
        if not ring:
            continue
        xs = [pt[0] for pt in ring]
        ys = [pt[1] for pt in ring]
        ax.plot(xs, ys, color=line_color, linewidth=0.45, zorder=5, alpha=0.85)


def _output_path_with_suffix(output_path: str, suffix: str) -> str:
    p = Path(output_path)
    return str(p.with_name(f"{p.stem}{suffix}{p.suffix}"))

def _extent_from_gt(gt: tuple, xsize: int, ysize: int):
    xmin = gt[0]
    xmax = gt[0] + xsize * gt[1]
    ymax = gt[3]
    ymin = gt[3] + ysize * gt[5]
    return xmin, xmax, ymin, ymax


def _georef_first_axis_is_latitude(xmin: float, xmax: float, ymin: float, ymax: float) -> bool:
    """
    部分 EC GeoTIFF 第一坐标沿列变化为纬度、第二坐标沿行为经度。
    matplotlib 期望 extent = (lon 左, lon 右, lat 下, lat 上)，否则轴标签与数值反了。
    """
    cx = 0.5 * (xmin + xmax)
    cy = 0.5 * (ymin + ymax)
    return 20.0 < cx < 55.0 and 95.0 < cy < 135.0


def _extent_looks_like_lonlat_degrees(xmin: float, xmax: float, ymin: float, ymax: float) -> bool:
    """矢量图层包络是否像经纬度（度），用于区分投影坐标（米）。"""
    if not all(np.isfinite([xmin, xmax, ymin, ymax])):
        return False
    lo_x, hi_x = sorted((xmin, xmax))
    lo_y, hi_y = sorted((ymin, ymax))
    if max(abs(lo_x), abs(hi_x), abs(lo_y), abs(hi_y)) > 1000:
        return False
    if hi_x - lo_x > 360 or hi_y - lo_y > 170:
        return False
    return -180 <= lo_x and hi_x <= 180 and -90 <= lo_y and hi_y <= 90


def _layer_extent_lonlat_swap_hint(layer) -> Optional[bool]:
    """
    仅当 extent 像经纬度（度）时：判断包络 x 向是否更像纬度（顶点常以 纬、经 存为 x、y）。
    投影坐标下返回 None，调用方按「不交换」解读 extent，且不得影响栅格转置。
    """
    try:
        xmin_b, xmax_b, ymin_b, ymax_b = layer.GetExtent()
    except Exception:
        return None
    if not _extent_looks_like_lonlat_degrees(xmin_b, xmax_b, ymin_b, ymax_b):
        return None
    cx = 0.5 * (xmin_b + xmax_b)
    cy = 0.5 * (ymin_b + ymax_b)
    return bool(20.0 < cx < 55.0 and 95.0 < cy < 135.0)


def _make_display_raster_array(
    raster: RasterData,
    mask: np.ndarray,
    precip_mm_factor: float,
    strict_basin_mask: bool = False,
) -> tuple[np.ndarray, np.ndarray]:
    """复制栅格、掩膜、单位换算（毫米），再交给地理转置逻辑。
    strict_basin_mask：为 True 时绝不启用「掩膜全空则显示整幅栅格」回退，保证产品图仅流域内有填色候选。
    """
    data = raster.array.astype(np.float64).copy()
    msk = mask.copy()
    # 降水产品中 nodata 常见异常为 0；若盲目按 0 掩膜会把无雨区全部清空。
    if raster.nodata is not None:
        nd = float(raster.nodata)
        if abs(nd) > 1e-12:
            data[np.isclose(data, nd)] = np.nan
        else:
            print("[raster] 提示: nodata=0，已忽略该 nodata 掩膜（避免把无雨区误删）")
    data[data < 0] = np.nan

    if precip_mm_factor > 0:
        data *= precip_mm_factor
        print(f"[raster] 降水量乘 --precip-mm-factor={precip_mm_factor} 作为 mm")
    else:
        masked = np.where(msk, data, np.nan)
        if np.isfinite(masked).any():
            dmx = np.nanmax(masked)
            if np.isfinite(dmx) and 0 < dmx < 0.02:
                data *= 1000.0
                print(f"[raster] 自动 x1000（流域内最大 {dmx:.6g}，按米→毫米）")
        else:
            # 掩膜与栅格无重叠时，流域内统计不到；按整幅判断米→毫米，避免回退后仍全是接近 0
            gmax = float(np.nanmax(data))
            if np.isfinite(gmax) and 0 < gmax < 0.02:
                data *= 1000.0
                print(f"[raster] 掩膜与栅格无有效交叠，按整幅最大 {gmax:.6g} 自动 x1000（米→毫米）")

    masked = np.where(msk, data, np.nan)
    if np.isfinite(masked).any():
        data = masked
    elif strict_basin_mask and np.isfinite(data).any():
        # 实况 IDW 等在写 TIF 时已 _apply_basin_nodata；若此处矢量栅格化与格网错位，
        # np.where(msk,…) 会误删全域。strict 下信任「非 nodata」像元作为流域内候选。
        print(
            "[raster] 流域矢量栅格化与栅格未对齐，strict_basin_mask 下改用 TIF 非 nodata 像元作显示范围"
        )
        msk = np.isfinite(data)
        data = np.where(msk, data, np.nan)
    elif strict_basin_mask:
        print(
            "[raster] 流域掩膜与栅格无有效交叠且无有效像元，strict_basin_mask 下整图无填色"
        )
        data = masked
    else:
        # 流域掩膜全空时，回退到整幅栅格，避免整图留白（预报等产品保留）。
        print("[raster] 流域掩膜后全空，已回退显示整幅栅格以减少留白")
        msk = np.isfinite(data)

    fin = np.isfinite(data)
    if fin.any():
        print(
            f"[raster] 有效像元 {int(fin.sum())}，值域 mm 约 [{np.nanmin(data):.6g}, {np.nanmax(data):.6g}]"
        )
    else:
        print("[raster] 警告: 掩膜后无有效像元（检查流域/GeoTIFF 与 --force-display-latlon-swap）")
    return data.astype(np.float32), msk


def _prepare_raster_for_display(
    data: np.ndarray,
    mask: np.ndarray,
    gt: tuple,
    xsize: int,
    ysize: int,
    force_latlon_swap: bool = False,
) -> tuple[np.ndarray, np.ndarray, tuple[float, float, float, float], bool]:
    """
    返回 (显示用数组, mask, extent, vector_xy_swap)。
    extent 为 (lon_min, lon_max, lat_min, lat_max)。
    vector_xy_swap：矢量坐标若为 (纬度, 经度) 存点，绘图时需对调为 (经度, 纬度)。
    """
    xmin, xmax, ymin, ymax = _extent_from_gt(gt, xsize, ysize)
    if not force_latlon_swap and not _georef_first_axis_is_latitude(xmin, xmax, ymin, ymax):
        return data, mask, (xmin, xmax, ymin, ymax), False
    lon_min, lon_max = sorted([ymin, ymax])
    lat_min, lat_max = sorted([xmin, xmax])
    arr = np.transpose(data)
    msk = np.transpose(mask)
    return arr, msk, (lon_min, lon_max, lat_min, lat_max), True


def _prepare_raster_with_swap_fallback(
    data: np.ndarray,
    msk: np.ndarray,
    gt: tuple,
    xsize: int,
    ysize: int,
    force_display_latlon_swap: bool,
) -> tuple[np.ndarray, np.ndarray, tuple[float, float, float, float], bool]:
    d2, m2, ext, vs = _prepare_raster_for_display(
        data, msk, gt, xsize, ysize, force_latlon_swap=force_display_latlon_swap
    )
    if force_display_latlon_swap and not np.isfinite(d2).any():
        print(
            "[raster] --force-display-latlon-swap 转置后无有效像元，已自动按未转置重试"
            "（若经纬轴仍反，可去掉该开关或核对 GeoTIFF 地理参考）"
        )
        d2, m2, ext, vs = _prepare_raster_for_display(
            data, msk, gt, xsize, ysize, force_latlon_swap=False
        )
    return d2, m2, ext, vs


def _basin_union_wkt_wgs84(layer) -> str:
    """流域图层转为 WGS84 多边形 WKT，供 PostGIS ST_Intersects。"""
    layer.ResetReading()
    src = layer.GetSpatialRef()
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(4326)
    try:
        oams = getattr(osr, "OAMS_TRADITIONAL_GIS_ORDER", None)
        if oams is not None:
            dst.SetAxisMappingStrategy(oams)
            if src is not None:
                src.SetAxisMappingStrategy(oams)
    except Exception:
        pass
    trans = osr.CoordinateTransformation(src, dst) if src is not None else None
    union = None
    for feat in layer:
        g = feat.GetGeometryRef()
        if g is None:
            continue
        gg = g.Clone()
        if trans is not None:
            gg.Transform(trans)
        union = gg if union is None else union.Union(gg)
    if union is None:
        return ""
    return union.ExportToWkt()


def _draw_product(
    raster: RasterData,
    mask: np.ndarray,
    layer,
    output_path: str,
    title1: str,
    title2: str,
    start_utc: datetime,
    lead_hours: int,
    dpi: int,
    font_path: str,
    config_path: str,
    no_admin_overlay: bool,
    admin_level: str,
    admin_max_features: int,
    admin_simplify_deg: float,
    basin_wkt_for_admin: str,
    metro_prefixes: frozenset[str],
    force_display_latlon_swap: bool,
    admin_query_buffer_ratio: float,
    map_basin_padding: float,
    precip_mm_factor: float,
    admin_city_union: bool,
    title_line3: str = "EC AIFS",
    precip_display_floor_mm: float = 0.0,
    strict_basin_mask: bool = False,
    theme: str = "light",
    transparent_background: bool = False,
    color_scheme: str = "default",
):
    _set_font(font_path)
    src_data, src_msk = _make_display_raster_array(
        raster, mask, precip_mm_factor, strict_basin_mask=strict_basin_mask
    )
    # 栅格是否转置仅由 GeoTIFF 地理参考 / --force-display-latlon-swap 决定，勿用语矢量 extent 覆盖，
    # 否则会出现掩膜与数组错位（掩膜全空）、或色阶全 0。
    data, disp_msk, extent, _ = _prepare_raster_with_swap_fallback(
        src_data,
        src_msk,
        raster.geotransform,
        raster.xsize,
        raster.ysize,
        force_display_latlon_swap,
    )
    floor_mm = _coerce_precip_display_floor_mm(precip_display_floor_mm)
    if floor_mm > 0:
        data = np.asarray(data, dtype=np.float64, copy=True)
        data[np.isfinite(data) & (data < floor_mm)] = np.nan
    if strict_basin_mask:
        m2 = np.asarray(disp_msk, dtype=bool)
        data = np.asarray(data, dtype=np.float64, copy=True)
        if not m2.any():
            data.fill(np.nan)
        else:
            data[~m2] = np.nan
    layer_extent_swap = _layer_extent_lonlat_swap_hint(layer)
    layer_swap = bool(layer_extent_swap) if layer_extent_swap is not None else False
    # 与 imshow 完全一致的经纬范围（区划查询、最终视域均以此为基准，避免矢量 extent 启发式与栅格转置打架）
    rad_lon_lo, rad_lon_hi, rad_lat_lo, rad_lat_hi = _extent_to_lonlat_ranges(extent)
    r_pad = _pad_lonlat_rect(rad_lon_lo, rad_lon_hi, rad_lat_lo, rad_lat_hi, map_basin_padding)
    be = _layer_envelope_wgs84_lonlat(layer)
    if be:
        b_pad = _pad_lonlat_rect(be[0], be[1], be[2], be[3], map_basin_padding)
        inner = _intersect_lonlat_rects(r_pad, b_pad)
        if inner is not None:
            fin_x0, fin_x1, fin_y0, fin_y1 = inner
        else:
            fin_x0, fin_x1, fin_y0, fin_y1 = r_pad
    else:
        fin_x0, fin_x1, fin_y0, fin_y1 = r_pad

    if np.isfinite(data).any():
        max_mm = float(np.nanmax(data))
        if not np.isfinite(max_mm):
            max_mm = 0.0
    else:
        max_mm = 0.0
    theme_style = _resolve_theme_style(theme, transparent_background)
    levels, cmap, norm = _build_cmap(
        max_mm,
        dry_band_color=theme_style.dry_band_color,
        nodata_color=theme_style.nodata_color,
        color_scheme=color_scheme,
    )
    fig = plt.figure(figsize=(12.5, 8))
    ax = fig.add_subplot(111)
    fig.patch.set_facecolor(theme_style.figure_facecolor)
    ax.set_facecolor(theme_style.axes_facecolor)

    im = ax.imshow(
        data,
        extent=extent,
        origin="upper",
        cmap=cmap,
        norm=norm,
        interpolation="nearest",
        zorder=2,
    )
    # 勿对 imshow 使用矢量 set_clip_path：matplotlib 中 AxesImage 与 transData 组合常见
    # 裁切错误导致整块栅格不可见；流域外显示依赖 TIF nodata + _make_display_raster_array 掩膜。
    _draw_boundary(ax, layer, layer_swap, theme_style.boundary_color)

    # 叠加数据库行政区划：bbox 必须用与 imshow extent 相同的经纬顺序，勿用 ax.get_xlim（易被矢量 extent 误设成轴颠倒）
    if not no_admin_overlay:
        try:
            q_lo_lon, q_lo_lat, q_hi_lon, q_hi_lat = _expand_query_bbox(
                rad_lon_lo,
                rad_lon_hi,
                rad_lat_lo,
                rad_lat_hi,
                admin_query_buffer_ratio,
            )
            if admin_query_buffer_ratio > 0:
                print(
                    f"[admin] 查询 bbox 外扩 {admin_query_buffer_ratio * 100:.0f}%："
                    f"lon [{q_lo_lon:.3f},{q_hi_lon:.3f}] lat [{q_lo_lat:.3f},{q_hi_lat:.3f}]"
                )
            admin_polygons = _load_admin_divisions_from_db(
                config_path,
                q_lo_lon,
                q_lo_lat,
                q_hi_lon,
                q_hi_lat,
                admin_level,
                admin_max_features,
                admin_simplify_deg,
                basin_wkt_for_admin,
                metro_prefixes,
                admin_city_union=admin_city_union,
            )
            _draw_admin_polygons(ax, admin_polygons, theme_style.admin_line_color)
        except Exception:
            pass

    cap3 = (title_line3 or "EC AIFS").strip() or "EC AIFS"
    ax.set_title(f"{title1}\n{title2}\n{cap3}", fontsize=20, pad=16, color=theme_style.title_color)
    ax.set_xlabel("Longitude", color=theme_style.label_color)
    ax.set_ylabel("Latitude", color=theme_style.label_color)
    ax.tick_params(axis="both", colors=theme_style.label_color)
    cbar_label = "Precipitation (mm)"
    ax.grid(linestyle="--", linewidth=0.4, color=theme_style.grid_color, alpha=0.3, zorder=1)

    cbar = fig.colorbar(im, ax=ax, fraction=0.032, pad=0.02, ticks=levels[1:-1])
    cbar.set_label(cbar_label, color=theme_style.label_color)
    cbar.ax.tick_params(colors=theme_style.label_color)
    try:
        cbar.outline.set_edgecolor(theme_style.cbar_edge_color)
    except Exception:
        pass

    # 最终视域对齐「流域+边距」，避免大查询框与 equal aspect 在四周留出大块空白
    ax.set_xlim(fin_x0, fin_x1)
    ax.set_ylim(fin_y0, fin_y1)
    ax.set_aspect("equal", adjustable="box")
    try:
        ax.margins(0)
    except Exception:
        pass

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    fig.savefig(
        output_path,
        dpi=dpi,
        bbox_inches="tight",
        transparent=bool(transparent_background),
        facecolor=theme_style.figure_facecolor,
    )
    plt.close(fig)


def run_draw_haihe_precip_product(
    tiff_path: str,
    basin_vector: str,
    output_path: str,
    lead_hours: int,
    *,
    start_utc_arg: str = "",
    config: str = "config.ini",
    dpi: int = 180,
    font_path: str = "",
    no_admin_overlay: bool = False,
    admin_level: str = "city_adcode",
    admin_max_features: int = 8000,
    admin_simplify_deg: float = 0.002,
    admin_by_basin: bool = True,
    admin_city_union: bool = True,
    admin_metro_prefixes: str = "11,12",
    force_display_latlon_swap: bool = False,
    admin_query_buffer_ratio: float = 0.22,
    map_basin_padding: float = 0.12,
    precip_mm_factor: float = 0.0,
    title1: Optional[str] = None,
    title2: Optional[str] = None,
    title_line3: Optional[str] = None,
    precip_display_floor_mm: float = 0.0,
    strict_basin_mask: bool = False,
    clip_mask: str = "basin",
    admin_clip_wkt: Optional[str] = None,
    theme: str = "light",
    transparent_background: bool = False,
    color_scheme: str = "default",
) -> None:
    """
    供队列/服务调用：从已确定的 GeoTIFF/GRIB 路径生成单张海河流域降水产品 PNG。
    start_utc_arg：起报 UTC，格式 YYYYmmddHH；建议与 EC 起报一致，用于标题与周期文案。
    title1/title2：optional English lines overriding the default forecast title.
    title_line3：标题第三行，默认 EC AIFS；实况图可传 station IDW 说明等（全英文）。
    precip_display_floor_mm：小于该值（mm）的有效像元在填色前视为无显示，减轻 IDW 微量噪声底；0 表示关闭。
    strict_basin_mask：为 True 时禁用掩膜为空时的整幅栅格回退，且按流域掩膜再次抠除流域外像元（实况产品建议 True）。
    clip_mask：basin=按 boundary_shp 栅格化掩膜；admin=按 admin_clip_wkt（库内行政区并集）掩膜与画外廓。
    admin_clip_wkt：clip_mask=admin 时由队列传入 PostGIS 生成的 MULTIPOLYGON WKT。
    theme：light/dark/both；both 会在 output_path 产出 light，并额外生成 *_dark.png。
    transparent_background：为 True 时输出透明背景 PNG，便于叠加展示。
    color_scheme：色标方案，default=现有方案，cma=中国气象局专题图分级色标。
    """
    if not os.path.exists(tiff_path):
        raise FileNotFoundError(f"tiff 不存在: {tiff_path}")
    if not os.path.exists(basin_vector):
        raise FileNotFoundError(f"流域边界文件不存在: {basin_vector}")

    start_utc = _resolve_cycle_utc(tiff_path, start_utc_arg)
    if title1 is not None and title2 is not None:
        t1, t2 = str(title1), str(title2)
    else:
        t1, t2 = _format_title(start_utc, lead_hours)
    cap3 = "EC AIFS" if title_line3 is None else str(title_line3)
    if tiff_path.lower().endswith(".nc"):
        cap3 = "滚动预报" if title_line3 is None else str(title_line3)

    raster = _open_raster_or_nc(tiff_path, hour=lead_hours)
    vds, layer = _open_vector_layer(basin_vector)
    rvds, rlayer = _reproject_vector_to_raster(vds, layer, raster.projection_wkt)

    cm = (clip_mask or "basin").strip().lower()
    clip_ds_admin = None
    if cm == "admin" and (admin_clip_wkt or "").strip():
        clip_ds_admin, clip_lyr = _mem_polygon_layer_from_wkt(
            admin_clip_wkt.strip(), raster.projection_wkt
        )
        if clip_lyr is None:
            print("[draw] admin_clip_wkt 无法解析，回退流域掩膜与边界")
            mask = _rasterize_mask(rlayer, raster.xsize, raster.ysize, raster.geotransform, raster.projection_wkt)
            boundary_layer = rlayer
        else:
            mask = _rasterize_mask(
                clip_lyr,
                raster.xsize,
                raster.ysize,
                raster.geotransform,
                raster.projection_wkt,
            )
            boundary_layer = clip_lyr
            if int(np.count_nonzero(mask)) == 0:
                print("[draw] 行政区掩膜为 0，回退流域掩膜与边界")
                mask = _rasterize_mask(
                    rlayer, raster.xsize, raster.ysize, raster.geotransform, raster.projection_wkt
                )
                boundary_layer = rlayer
                clip_ds_admin = None
    else:
        if cm == "admin":
            print("[draw] clip_mask=admin 但缺少 admin_clip_wkt，使用流域掩膜")
        mask = _rasterize_mask(rlayer, raster.xsize, raster.ysize, raster.geotransform, raster.projection_wkt)
        boundary_layer = rlayer

    # 与 PostGIS 库中 haihe_admin_division(4326) 求交：用 shapefile 原始图层转 WGS84，
    # 避免经「矢量→栅格 CRS」重投影链后再转 4326 时与库中几何轴序/变换不一致导致相交 0 条。
    basin_wkt_admin = ""
    if admin_by_basin:
        basin_wkt_admin = _basin_union_wkt_wgs84(layer)

    metro_p = _parse_admin_metro_prefixes(admin_metro_prefixes)
    theme_norm = _normalize_theme_name(theme)
    render_jobs: list[tuple[str, str]] = []
    if theme_norm == "both":
        render_jobs.append(("light", output_path))
        render_jobs.append(("dark", _output_path_with_suffix(output_path, "_dark")))
    else:
        render_jobs.append((theme_norm, output_path))

    for one_theme, one_out in render_jobs:
        _draw_product(
            raster,
            mask,
            boundary_layer,
            one_out,
            t1,
            t2,
            start_utc,
            lead_hours,
            dpi,
            (font_path or "").strip(),
            config,
            no_admin_overlay,
            admin_level,
            admin_max_features,
            admin_simplify_deg,
            basin_wkt_admin,
            metro_p,
            force_display_latlon_swap,
            admin_query_buffer_ratio,
            map_basin_padding,
            precip_mm_factor,
            admin_city_union,
            title_line3=cap3,
            precip_display_floor_mm=precip_display_floor_mm,
            strict_basin_mask=strict_basin_mask,
            theme=one_theme,
            transparent_background=transparent_background,
            color_scheme=color_scheme,
        )
    if theme_norm == "both":
        print(f"[draw] 已输出浅色版: {output_path}")
        print(f"[draw] 已输出深色版: {_output_path_with_suffix(output_path, '_dark')}")

    if clip_ds_admin is not None:
        clip_ds_admin = None
    rvds = None
    vds = None


def main():
    args = _parse_args()
    # 滚动预报 .nc 模式：跳过 EC 文件发现，直接用 .nc + lead-hours
    rolling_nc = (args.rolling_forecast_nc or "").strip()
    if rolling_nc:
        if not os.path.isfile(rolling_nc):
            raise FileNotFoundError(f"滚动预报 .nc 文件不存在: {rolling_nc}")
        print(f"[paths] 滚动预报模式: {rolling_nc} hour={args.lead_hours}")
        run_draw_haihe_precip_product(
            rolling_nc,
            args.basin_vector,
            args.output,
            args.lead_hours,
            start_utc_arg=args.start_utc,
            config=args.config,
            dpi=args.dpi,
            font_path=args.font_path,
            no_admin_overlay=args.no_admin_overlay,
            admin_level=args.admin_level,
            admin_max_features=args.admin_max_features,
            admin_simplify_deg=args.admin_simplify_deg,
            admin_by_basin=args.admin_by_basin,
            admin_city_union=args.admin_city_union,
            admin_metro_prefixes=args.admin_metro_prefixes,
            force_display_latlon_swap=args.force_display_latlon_swap,
            admin_query_buffer_ratio=args.admin_query_buffer_ratio,
            map_basin_padding=args.map_basin_padding,
            precip_mm_factor=args.precip_mm_factor,
            theme=args.theme,
            transparent_background=args.transparent_background,
            color_scheme=args.color_scheme,
        )
        return
    ec_base = (args.input_dir or "").strip()
    su = (args.start_utc or "").strip()
    scan_dir = _resolve_ec_nested_input_dir(ec_base, su) if su else ec_base
    if su and scan_dir != ec_base:
        print(f"[paths] 按日 EC 子目录: {scan_dir}")

    tiff_path = (args.tiff_file or "").strip()
    if not tiff_path and su:
        try:
            from haihe_mcp_tools import _find_ec_precip_file, _parse_forecast_start_time

            parsed = _parse_forecast_start_time(su)
            found = _find_ec_precip_file(ec_base, parsed, int(args.lead_hours))
            if found and os.path.isfile(found):
                tiff_path = found
                print(f"[paths] 按起报+时效匹配 EC 文件: {tiff_path}")
        except Exception:
            pass
    if not tiff_path:
        tiff_path = _pick_latest_raster(scan_dir)
    if args.admin_by_basin:
        print("[admin] 使用流域面与库中行政区划求交查询（若失败则仍用 bbox）")
    metro_p = _parse_admin_metro_prefixes(args.admin_metro_prefixes)
    if args.admin_level == "hybrid":
        print(f"[admin] hybrid 直辖市 adcode 前缀: {sorted(metro_p)}")
    elif args.admin_level == "city_adcode":
        print("[admin] 行政区划层级：地级市 + 京津沪渝市域 (city_adcode，不含区县)")

    run_draw_haihe_precip_product(
        tiff_path,
        args.basin_vector,
        args.output,
        args.lead_hours,
        start_utc_arg=args.start_utc,
        config=args.config,
        dpi=args.dpi,
        font_path=args.font_path,
        no_admin_overlay=args.no_admin_overlay,
        admin_level=args.admin_level,
        admin_max_features=args.admin_max_features,
        admin_simplify_deg=args.admin_simplify_deg,
        admin_by_basin=args.admin_by_basin,
        admin_city_union=args.admin_city_union,
        admin_metro_prefixes=args.admin_metro_prefixes,
        force_display_latlon_swap=args.force_display_latlon_swap,
        admin_query_buffer_ratio=args.admin_query_buffer_ratio,
        map_basin_padding=args.map_basin_padding,
        precip_mm_factor=args.precip_mm_factor,
        theme=args.theme,
        transparent_background=args.transparent_background,
        color_scheme=args.color_scheme,
    )

    start_utc = _resolve_cycle_utc(tiff_path, args.start_utc)
    _, title2 = _format_title(start_utc, args.lead_hours)
    print("Done.")
    print(f"  tiff: {tiff_path}")
    print(f"  basin: {args.basin_vector}")
    print(f"  output: {args.output}")
    print(f"  period(UTC): {title2}")


if __name__ == "__main__":
    main()
