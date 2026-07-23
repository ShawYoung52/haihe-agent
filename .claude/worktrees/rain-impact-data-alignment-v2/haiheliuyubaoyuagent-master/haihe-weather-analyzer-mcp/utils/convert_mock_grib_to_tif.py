#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import configparser
from pathlib import Path

import numpy as np
import geopandas as gpd
from shapely import contains_xy
import rasterio
from rasterio.features import geometry_mask
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject
import xarray as xr

DEFAULT_TARGET_CELL_DEG = 0.009
DEFAULT_RESAMPLE_METHOD = "cubic"


def _load_boundary_from_config() -> str:
    cfg = configparser.ConfigParser()
    cfg_path = Path(__file__).resolve().parents[1] / "config.ini"
    if not cfg_path.is_file():
        return ""
    cfg.read(cfg_path, encoding="utf-8")
    if not cfg.has_section("paths"):
        return ""
    return (cfg.get("paths", "boundary_shp", fallback="") or "").strip()


def _open_2d_da(grib_path: Path):
    ds = xr.open_dataset(grib_path, engine="cfgrib", backend_kwargs={"indexpath": ""})
    try:
        for _, v in ds.data_vars.items():
            da = v.squeeze(drop=True)
            while da.ndim > 2:
                da = da.isel({da.dims[0]: 0})
            if da.ndim == 2:
                return ds, da
        raise RuntimeError(f"未在文件中找到二维栅格变量: {grib_path}")
    except Exception:
        ds.close()
        raise


def _latlon_names(da):
    lat_name = "latitude" if "latitude" in da.coords else ("lat" if "lat" in da.coords else None)
    lon_name = "longitude" if "longitude" in da.coords else ("lon" if "lon" in da.coords else None)
    if lat_name is None or lon_name is None:
        for c in da.coords:
            cl = str(c).lower()
            if lat_name is None and "lat" in cl:
                lat_name = str(c)
            if lon_name is None and "lon" in cl:
                lon_name = str(c)
    if lat_name is None or lon_name is None:
        raise RuntimeError("无法识别经纬度坐标名")
    return lat_name, lon_name


def _build_mask_2d(lon: np.ndarray, lat: np.ndarray, boundary_shp: str) -> np.ndarray:
    if not boundary_shp:
        return np.ones((lat.size, lon.size), dtype=bool)
    shp = Path(boundary_shp)
    if not shp.is_file():
        return np.ones((lat.size, lon.size), dtype=bool)
    gdf = gpd.read_file(shp)
    if gdf.empty:
        return np.ones((lat.size, lon.size), dtype=bool)
    if gdf.crs is not None and str(gdf.crs).upper() not in {"EPSG:4326", "WGS84"}:
        gdf = gdf.to_crs(4326)
    geom = gdf.unary_union
    xx, yy = np.meshgrid(lon, lat)
    return np.asarray(contains_xy(geom, xx.ravel(), yy.ravel()), dtype=bool).reshape(xx.shape)


def _load_boundary_geom(boundary_shp: str):
    if not boundary_shp:
        return None
    shp = Path(boundary_shp)
    if not shp.is_file():
        return None
    gdf = gpd.read_file(shp)
    if gdf.empty:
        return None
    if gdf.crs is not None and str(gdf.crs).upper() not in {"EPSG:4326", "WGS84"}:
        gdf = gdf.to_crs(4326)
    return gdf.unary_union


def _mask_from_geom(
    shape: tuple[int, int],
    transform,
    geom,
    all_touched: bool = True,
) -> np.ndarray:
    if geom is None:
        return np.ones(shape, dtype=bool)
    # geometry_mask 返回 True=外部；这里转成 True=内部
    return ~geometry_mask(
        [geom.__geo_interface__],
        out_shape=shape,
        transform=transform,
        invert=False,
        all_touched=all_touched,
    )


def _crop_to_valid(
    arr: np.ndarray,
    mask: np.ndarray,
    west: float,
    north: float,
    dlon: float,
    dlat: float,
) -> tuple[np.ndarray, float, float]:
    ys, xs = np.where(mask)
    if ys.size == 0:
        return arr, west, north
    r0, r1 = int(ys.min()), int(ys.max())
    c0, c1 = int(xs.min()), int(xs.max())
    arr2 = arr[r0 : r1 + 1, c0 : c1 + 1]
    west2 = west + c0 * dlon
    north2 = north - r0 * dlat
    return arr2, west2, north2


def _resample_to_target_cell(
    arr: np.ndarray,
    west: float,
    north: float,
    dlon: float,
    dlat: float,
    nodata: float,
    target_cell_deg: float,
    resample_method: str,
) -> tuple[np.ndarray, float, float]:
    """仅在原网格更粗时，重采样到目标分辨率。"""
    target = max(float(target_cell_deg), 1e-5)
    src_cell = max(abs(dlon), abs(dlat))
    if src_cell <= target:
        return arr, dlon, dlat

    src_h, src_w = arr.shape
    east = west + src_w * dlon
    south = north - src_h * dlat
    dst_w = max(1, int(np.ceil((east - west) / target)))
    dst_h = max(1, int(np.ceil((north - south) / target)))
    dst_arr = np.full((dst_h, dst_w), float(nodata), dtype=np.float32)

    src_transform = from_origin(west, north, dlon, dlat)
    dst_dlon = (east - west) / dst_w
    dst_dlat = (north - south) / dst_h
    dst_transform = from_origin(west, north, dst_dlon, dst_dlat)
    method_norm = (resample_method or DEFAULT_RESAMPLE_METHOD).strip().lower()
    method_map = {
        "nearest": Resampling.nearest,
        "bilinear": Resampling.bilinear,
        "cubic": Resampling.cubic,
    }
    rs = method_map.get(method_norm, Resampling.cubic)

    reproject(
        source=arr,
        destination=dst_arr,
        src_transform=src_transform,
        src_crs="EPSG:4326",
        dst_transform=dst_transform,
        dst_crs="EPSG:4326",
        src_nodata=float(nodata),
        dst_nodata=float(nodata),
        resampling=rs,
    )
    return dst_arr, dst_dlon, dst_dlat


def _write_tif(
    grib_path: Path,
    tif_path: Path,
    boundary_shp: str = "",
    crop_to_mask: bool = True,
    target_cell_deg: float = DEFAULT_TARGET_CELL_DEG,
    mask_all_touched: bool = True,
    resample_method: str = DEFAULT_RESAMPLE_METHOD,
) -> None:
    ds, da = _open_2d_da(grib_path)
    try:
        lat_name, lon_name = _latlon_names(da)
        lat = np.asarray(da[lat_name].values, dtype=float)
        lon = np.asarray(da[lon_name].values, dtype=float)
        arr = np.asarray(da.values, dtype=np.float32)

        if lat.ndim != 1 or lon.ndim != 1:
            raise RuntimeError("当前脚本仅支持规则经纬度网格（一维lat/lon）")

        if lat[0] < lat[-1]:
            lat = lat[::-1]
            arr = arr[::-1, :]
        if lon[0] > lon[-1]:
            lon = lon[::-1]
            arr = arr[:, ::-1]

        dlat = abs(lat[0] - lat[1]) if lat.size > 1 else 0.25
        dlon = abs(lon[1] - lon[0]) if lon.size > 1 else 0.25
        west = float(lon.min()) - dlon / 2.0
        north = float(lat.max()) + dlat / 2.0
        # 按流域边界裁剪：流域外置 nodata
        nodata = np.float32(-9999.0)
        boundary_geom = _load_boundary_geom(boundary_shp)
        src_transform = from_origin(west, north, dlon, dlat)
        src_mask = _mask_from_geom(arr.shape, src_transform, boundary_geom, all_touched=mask_all_touched)
        arr = np.where(src_mask, arr, nodata).astype(np.float32)
        if crop_to_mask and boundary_geom is not None:
            arr, west, north = _crop_to_valid(arr, src_mask, west, north, dlon, dlat)
        src_cell = max(abs(dlon), abs(dlat))
        arr, dlon, dlat = _resample_to_target_cell(
            arr=arr,
            west=west,
            north=north,
            dlon=dlon,
            dlat=dlat,
            nodata=float(nodata),
            target_cell_deg=target_cell_deg,
            resample_method=resample_method,
        )
        # 关键：重采样后按目标网格重新掩膜，避免把粗网格台阶边界放大到 1km 图上
        transform = from_origin(west, north, dlon, dlat)
        dst_mask = _mask_from_geom(arr.shape, transform, boundary_geom, all_touched=mask_all_touched)
        arr = np.where(dst_mask, arr, nodata).astype(np.float32)
        if crop_to_mask and boundary_geom is not None:
            arr, west, north = _crop_to_valid(arr, dst_mask, west, north, dlon, dlat)
            transform = from_origin(west, north, dlon, dlat)
        print(
            f"[mock_tif] {grib_path.name}: cell {src_cell:.6f}° -> {max(abs(dlon), abs(dlat)):.6f}°, "
            f"size={arr.shape[1]}x{arr.shape[0]}, resample={resample_method}"
        )

        tif_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(
            tif_path,
            "w",
            driver="GTiff",
            height=arr.shape[0],
            width=arr.shape[1],
            count=1,
            dtype="float32",
            crs="EPSG:4326",
            transform=transform,
            compress="lzw",
            nodata=float(nodata),
        ) as dst:
            dst.write(arr, 1)
    finally:
        ds.close()


def main() -> None:
    p = argparse.ArgumentParser(description="把 mock grib2 转成 QGIS 可读 tif")
    p.add_argument("--grib-dir", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--prefix", default="20250722000000")
    p.add_argument("--steps", default="12,24,36,48,60,72")
    p.add_argument("--boundary-shp", default="", help="流域边界 shp 路径；不传则读 config.ini 的 paths.boundary_shp")
    p.add_argument("--no-crop", action="store_true", help="仅做掩膜，不裁剪范围")
    p.add_argument(
        "--target-cell-deg",
        type=float,
        default=DEFAULT_TARGET_CELL_DEG,
        help="目标像元大小（度），默认 0.009≈1km；仅当原始网格更粗时重采样",
    )
    p.add_argument(
        "--resample-method",
        choices=["nearest", "bilinear", "cubic"],
        default=DEFAULT_RESAMPLE_METHOD,
        help="重采样方法：nearest 最清晰，bilinear 最平滑，cubic 折中（默认 cubic）",
    )
    p.add_argument(
        "--mask-all-touched",
        action="store_true",
        default=True,
        help="掩膜时启用 all_touched，边界更平滑（默认开启）",
    )
    p.add_argument(
        "--no-mask-all-touched",
        dest="mask_all_touched",
        action="store_false",
        help="掩膜关闭 all_touched（边界更严格）",
    )
    args = p.parse_args()

    grib_dir = Path(args.grib_dir)
    out_dir = Path(args.out_dir)
    steps = [int(x) for x in str(args.steps).split(",") if x.strip()]

    boundary_shp = (args.boundary_shp or "").strip() or _load_boundary_from_config()

    for h in steps:
        g = grib_dir / f"{args.prefix}-{h}h-oper-fc.grib2"
        t = out_dir / f"ec_{args.prefix[:10]}_rain_total_{h}h.tif"
        _write_tif(
            g,
            t,
            boundary_shp=boundary_shp,
            crop_to_mask=not args.no_crop,
            target_cell_deg=args.target_cell_deg,
            mask_all_touched=args.mask_all_touched,
            resample_method=args.resample_method,
        )
        print(t)


if __name__ == "__main__":
    main()
