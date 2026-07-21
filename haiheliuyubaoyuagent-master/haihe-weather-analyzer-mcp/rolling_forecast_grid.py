"""滚动预报网格数据源（汛期替代 EC）。

数据湖路径模式：
    {ROOT}/{YYYYMM}/{YYYYMMDD}/{YYYYMMDDHH}/GRID_TJQX_LYPUB_TP1H_AEHH_000_DT_{YYYYMMDDHHMMSS}_000-240_*.nc

每天 08:00 / 20:00 两个起报时次各发布一次；文件为 NetCDF 格式，变量 TP1H（1 小时累计降水），
预报时效 000-240（0-240 小时，10 天）。

汛期（6 月 1 日 - 9 月 30 日）使用滚动预报网格，平时使用 EC（ECMWF AIFS）。
具体 NetCDF 变量提取需在内网用样本文件补全 `inspect_rolling_forecast_grid`。
"""
from __future__ import annotations

import logging
import math
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 数据湖根目录；外网开发环境无此路径，仅作为默认值，生产由 env 覆盖
DEFAULT_ROLLING_FORECAST_GRID_ROOT = os.getenv(
    "ROLLING_FORECAST_GRID_ROOT",
    "/CMADAAS/DATA/SEVP/BETJ/USR_QXT_YTH/M.3200.0006.M001/TP1H/000",
)

# 文件名前缀/后缀；NNNN 为批次号或集合成员号，多文件取最大
_ROLLING_GRID_FILE_RE = re.compile(
    r"^GRID_TJQX_LYPUB_TP1H_AEHH_000_DT_(?P<dt>\d{14})_000-240_(?P<seq>\d+)\.nc$"
)

# 汛期月份：6/7/8/9（6 月 1 日 - 9 月 30 日，含端点）
_FLOOD_SEASON_MONTHS = frozenset({6, 7, 8, 9})

# 起报时次（北京时间）：08:00 和 20:00
_FORECAST_CYCLES = (8, 20)

# 文件发现时回溯的历史时次数（每次 12 小时），2 天 = 4 个时次
_MAX_CYCLE_FALLBACK = 4


def is_flood_season(now: datetime | None = None) -> bool:
    """判断给定时间是否在海河流域汛期（6/1 00:00 - 9/30 23:59）。

    传入 now 方便单测；默认用当前系统时间。注意：服务器按北京时间运行，
    调用方应传入北京时间 datetime，或确保服务器时区为 Asia/Shanghai。
    """
    moment = now or datetime.now()
    return moment.month in _FLOOD_SEASON_MONTHS


def select_latest_forecast_cycle(now: datetime | None = None) -> datetime:
    """选择最近的可用起报时次（08:00 或 20:00）。

    规则：
    - now >= 20:00 → 当天 20:00
    - 08:00 <= now < 20:00 → 当天 08:00
    - now < 08:00 → 前一天 20:00
    """
    moment = now or datetime.now()
    today = moment.date()
    if moment.hour >= 20:
        return datetime(today.year, today.month, today.day, 20, 0, 0)
    if moment.hour >= 8:
        return datetime(today.year, today.month, today.day, 8, 0, 0)
    yesterday = today - timedelta(days=1)
    return datetime(yesterday.year, yesterday.month, yesterday.day, 20, 0, 0)


def _previous_cycle(cycle: datetime) -> datetime:
    """返回上一个起报时次（当前 20:00 → 当天 08:00；当前 08:00 → 前一天 20:00）。"""
    if cycle.hour == 20:
        return cycle.replace(hour=8)
    return (cycle - timedelta(days=1)).replace(hour=20)


def _cycle_directory(root: str | os.PathLike, cycle: datetime) -> Path:
    """构建起报时次对应的数据湖目录：{root}/{YYYYMM}/{YYYYMMDD}/{YYYYMMDDHH}/。"""
    return Path(root) / cycle.strftime("%Y%m") / cycle.strftime("%Y%m%d") / cycle.strftime("%Y%m%d%H")


def _pick_latest_file(directory: Path, cycle: datetime) -> str | None:
    """在目录中找到匹配 cycle 的滚动预报 .nc 文件；多文件取 NNNN 最大者。"""
    if not directory.is_dir():
        return None
    dt_str = cycle.strftime("%Y%m%d%H%M%S")
    best: tuple[int, Path] | None = None
    for entry in directory.iterdir():
        if not entry.is_file():
            continue
        match = _ROLLING_GRID_FILE_RE.match(entry.name)
        if not match or match.group("dt") != dt_str:
            continue
        seq = int(match.group("seq"))
        if best is None or seq > best[0]:
            best = (seq, entry)
    return str(best[1]) if best is not None else None


def find_rolling_forecast_grid_file(
    root: str | os.PathLike | None = None,
    cycle: datetime | None = None,
    *,
    now: datetime | None = None,
    max_fallback: int = _MAX_CYCLE_FALLBACK,
) -> str | None:
    """查找最新的滚动预报网格 .nc 文件。

    优先用 cycle（若给定），否则按 now 选最新时次；文件不存在时按 12 小时步长回溯，
    最多回溯 max_fallback 个时次。max_fallback<=0 时直接返回 None（不做查找）。
    返回文件路径或 None。
    """
    base = root or DEFAULT_ROLLING_FORECAST_GRID_ROOT
    current = cycle or select_latest_forecast_cycle(now)
    if max_fallback <= 0:
        return None
    tried: list[str] = []
    for _ in range(max_fallback):
        directory = _cycle_directory(base, current)
        path = _pick_latest_file(directory, current)
        if path:
            logger.info("滚动预报文件命中: %s（搜索根=%s）", path, base)
            return path
        tried.append(f"{directory}（cycle={current.strftime('%Y%m%d%H')}）")
        current = _previous_cycle(current)
    logger.warning("滚动预报文件未找到，已搜索 %d 个时次: %s", len(tried), "; ".join(tried))
    return None


def inspect_rolling_forecast_grid(path: str | os.PathLike) -> dict[str, Any]:
    """打开 NetCDF 文件返回元信息（变量名、维度、坐标范围）。

    供内网环境用样本文件确认数据结构；外网开发环境无样本文件时调用会抛 ImportError/IOError。
    变量提取逻辑需在此函数补全后再接入下游产品。
    """
    import xarray as xr  # 懒导入：外网开发环境可能未装 xarray/netcdf4

    ds = xr.open_dataset(path, engine="netcdf4")
    try:
        info: dict[str, Any] = {
            "path": str(path),
            "data_vars": {name: str(ds[name].dims) for name in ds.data_vars},
            "coords": {name: str(ds[name].dims) for name in ds.coords},
            "attrs": dict(ds.attrs),
        }
        # 常见坐标范围，供确认覆盖区域
        for coord in ("lat", "latitude", "lon", "longitude", "time"):
            if coord in ds.coords:
                values = ds[coord].values
                if values.size:
                    info[f"{coord}_range"] = [float(values.min()), float(values.max())]
        return info
    finally:
        ds.close()


def read_rolling_forecast_precip(
    path: str | os.PathLike,
    *,
    start_hour: int = 0,
    end_hour: int = 240,
):
    """读取滚动预报 .nc 的 TP1H 变量，按时段切片返回已加载的 DataArray。

    NetCDF 结构（样本文件确认）：
    - dims: time(264) × lat(181) × lon(181)
    - TP1H: (time, lat, lon) float32，1 小时累计降水
    - time: -23..240（负值为分析期，0..240 为预报时效）
    - lat: 34.0..43.0（海河流域，~0.05° 分辨率）
    - lon: 111.0..120.0

    start_hour/end_hour 对 time 坐标做闭区间切片；默认 0..240（纯预报）。
    返回的 DataArray 已 .load() 到内存，文件关闭后仍可使用。
    """
    if start_hour > end_hour:
        raise ValueError(f"start_hour({start_hour}) 不能大于 end_hour({end_hour})")
    import xarray as xr  # 懒导入

    ds = xr.open_dataset(path, engine="netcdf4", decode_times=False)
    try:
        tp = ds["TP1H"].sel(time=slice(start_hour, end_hour)).load()
        return tp
    finally:
        ds.close()


def sample_rolling_forecast_at_stations(
    nc_path: str | os.PathLike,
    station_records: list[dict],
    hour: int,
    *,
    method: str = "nearest",
) -> dict[str, float]:
    """从滚动预报 .nc 提取指定时效的 TP1H，采样站点位置返回 {station_id: precip_mm}。

    与 `emergency_api._sample_forecast_at_stations` 对齐的接口：输入站点记录列表
    （含 Station_Id_C/Lat/Lon 或 station_id/lat/lon），返回 {站点ID: 降水量}。
    method: "nearest"（默认）或 "bilinear"（双线性插值）。
    """
    import numpy as np  # 懒导入
    import xarray as xr  # 懒导入

    ds = xr.open_dataset(nc_path, engine="netcdf4", decode_times=False)
    try:
        if hour not in ds["time"].values:
            return {}
        tp = ds["TP1H"].sel(time=hour)
        lat_min = float(ds["lat"].values.min())
        lat_max = float(ds["lat"].values.max())
        lon_min = float(ds["lon"].values.min())
        lon_max = float(ds["lon"].values.max())
        interp_kw = "linear" if method == "bilinear" else "nearest"
        result: dict[str, float] = {}
        for rec in station_records:
            sid = rec.get("Station_Id_C") or rec.get("station_id") or ""
            if not sid:
                continue
            lat = _safe_coord(rec, ("Lat", "lat"))
            lon = _safe_coord(rec, ("Lon", "lon"))
            if lat is None or lon is None:
                continue
            # 网格范围外跳过（与 EC GDAL 采样器的 px/xsize 边界检查对齐）
            if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
                continue
            try:
                if method == "bilinear":
                    val = float(tp.interp(lat=lat, lon=lon, method=interp_kw).values)
                else:
                    val = float(tp.sel(lat=lat, lon=lon, method="nearest").values)
            except (KeyError, ValueError):
                continue
            if not np.isfinite(val):
                continue
            result[str(sid)] = val
        return result
    finally:
        ds.close()


def _safe_coord(rec: dict, keys: tuple[str, ...]) -> float | None:
    for k in keys:
        v = rec.get(k)
        if v is None:
            continue
        try:
            num = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(num):
            return num
    return None


def materialize_rolling_forecast_to_files(
    nc_path: str | os.PathLike,
    hours: list[int],
    *,
    output_dir: str | os.PathLike | None = None,
) -> dict[str, str]:
    """把滚动预报 .nc 的指定时效切片写成独立 GeoTIFF（.tif），兼容 EC 消费方。

    返回 `{f"{h}h": temp_tif_path}`；每个 temp 文件是单波段 Float32 GeoTIFF，
    WGS84 投影 + 像元中心→边界 geotransform，与 `ec_forecast_precip_files_by_horizon`
    返回的 TIF 在 `_sample_station_forecast_rain_mm` / `gdal.Open` 中走完全相同的路径。
    用 GeoTIFF 而非 .nc 是因为 GDAL netCDF 驱动可能创建子数据集导致 `GetRasterBand(1)`
    取不到 TP1H；GeoTIFF 单波段无此歧义。时效不在 time 坐标内的条目跳过。
    """
    import tempfile
    import xarray as xr  # 懒导入
    from osgeo import gdal, osr  # 懒导入

    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="rolling_fc_")
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, str] = {}
    ds = xr.open_dataset(nc_path, engine="netcdf4", decode_times=False)
    try:
        # WGS84 投影（所有波段共用）
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        try:
            srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        except Exception:
            pass
        proj_wkt = srs.ExportToWkt()

        for h in hours:
            if h not in ds["time"].values:
                continue
            tp = ds["TP1H"].sel(time=h).load()
            lat = tp["lat"].values  # 升序
            lon = tp["lon"].values  # 升序
            arr = tp.values.astype("float32")  # (lat, lon) 升序
            arr = arr[::-1, :]  # 翻转为降序，GDAL row 0 = max lat
            lon_res = float(lon[1] - lon[0])
            lat_res = float(lat[1] - lat[0])
            gt = (
                float(lon[0]) - lon_res / 2,
                lon_res,
                0.0,
                float(lat[-1]) + lat_res / 2,
                0.0,
                -lat_res,
            )
            out_path = out_dir / f"tp1h_{h}h.tif"
            driver = gdal.GetDriverByName("GTiff")
            out_ds = driver.Create(str(out_path), int(arr.shape[1]), int(arr.shape[0]), 1, gdal.GDT_Float32)
            out_ds.SetGeoTransform(gt)
            out_ds.SetProjection(proj_wkt)
            band = out_ds.GetRasterBand(1)
            band.WriteArray(arr)
            band.FlushCache()
            out_ds = None
            result[f"{h}h"] = str(out_path)
    finally:
        ds.close()
    return result


def resolve_forecast_grid_source(
    *,
    now: datetime | None = None,
    ec_output_path: str | None = None,
    rolling_root: str | os.PathLike | None = None,
) -> dict[str, Any]:
    """按数据可用性切换预报网格数据源：有滚动预报数据→滚动预报；无→EC。

    数据湖中滚动预报文件存在时即用（实测汛期基本都有数据，非汛期通常无数据，
    天然对齐）；找不到时降级 EC。`is_flood_season` 仅作参考字段返回，不参与决策。

    返回:
        {"source": "rolling_forecast" | "ec", "reason": str,
         "file": str | None, "cycle": str | None,
         "ec_output_path": str | None, "is_flood_season": bool}
    """
    moment = now or datetime.now()
    cycle = select_latest_forecast_cycle(moment)
    path = find_rolling_forecast_grid_file(rolling_root, cycle, now=moment)
    flood = is_flood_season(moment)
    if path:
        logger.info("预报网格数据源=滚动预报 cycle=%s file=%s", cycle.strftime("%Y%m%d%H%M%S"), path)
        return {
            "source": "rolling_forecast",
            "reason": "已找到滚动预报文件" + ("（汛期）" if flood else "（非汛期，仍有数据）"),
            "file": path,
            "cycle": cycle.strftime("%Y%m%d%H%M%S"),
            "ec_output_path": ec_output_path,
            "is_flood_season": flood,
        }
    logger.warning("预报网格数据源=EC（滚动预报未找到）cycle=%s flood=%s rolling_root=%s", cycle.strftime("%Y%m%d%H%M%S"), flood, rolling_root or DEFAULT_ROLLING_FORECAST_GRID_ROOT)
    return {
        "source": "ec",
        "reason": "未找到滚动预报文件，使用 EC" + ("（汛期但数据缺失）" if flood else "（非汛期）"),
        "ec_output_path": ec_output_path,
        "file": None,
        "cycle": cycle.strftime("%Y%m%d%H%M%S"),
        "is_flood_season": flood,
    }
