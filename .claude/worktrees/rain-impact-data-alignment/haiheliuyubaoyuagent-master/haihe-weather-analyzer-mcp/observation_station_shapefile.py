from __future__ import annotations

import importlib
import os
from typing import Any, Dict, List, Optional, Sequence

from haihe_mcp_tools import safe_float, station_id_of


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


def _cleanup_existing_shapefile_family(out_shp_path: str) -> None:
    base, _ = os.path.splitext(out_shp_path)
    for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        p = base + ext
        if os.path.exists(p):
            os.remove(p)


def _pick_station_name(record: Dict[str, Any]) -> str:
    for key in ("Station_Name", "StationName", "Station", "stname", "Name", "站名"):
        v = record.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _write_cpg_file(out_shp_path: str, encoding: str) -> None:
    base, _ = os.path.splitext(out_shp_path)
    cpg_path = base + ".cpg"
    with open(cpg_path, "w", encoding="ascii") as f:
        f.write(encoding)


def write_observation_station_shapefile(
    *,
    records: Sequence[Dict[str, Any]],
    accum_hours: int,
    out_shp_path: str,
    times: Optional[str] = None,
) -> Dict[str, Any]:
    field_name = _precip_field_for_accum_hours(accum_hours)
    os.makedirs(os.path.dirname(os.path.abspath(out_shp_path)), exist_ok=True)

    gdal = importlib.import_module("osgeo.gdal")
    ogr = importlib.import_module("osgeo.ogr")
    osr = importlib.import_module("osgeo.osr")
    gdal.UseExceptions()
    ogr.UseExceptions()

    _cleanup_existing_shapefile_family(out_shp_path)
    gdal.SetConfigOption("SHAPE_ENCODING", "UTF-8")
    drv = ogr.GetDriverByName("ESRI Shapefile")
    if drv is None:
        raise RuntimeError("GDAL/OGR 无 ESRI Shapefile 驱动")

    ds = drv.CreateDataSource(out_shp_path)
    if ds is None:
        raise RuntimeError(f"无法创建 shapefile: {out_shp_path}")

    srs = osr.SpatialReference()
    srs.SetWellKnownGeogCS("WGS84")
    layer = ds.CreateLayer(
        "obs_station_rain",
        srs=srs,
        geom_type=ogr.wkbPoint,
        options=["ENCODING=UTF-8"],
    )
    if layer is None:
        ds = None
        raise RuntimeError("无法创建点图层 obs_station_rain")

    fields = [
        ("station_id", ogr.OFTString, 32, None),
        ("station_nm", ogr.OFTString, 64, None),
        ("lon", ogr.OFTReal, None, 6),
        ("lat", ogr.OFTReal, None, 6),
        ("accum_h", ogr.OFTInteger, None, None),
        ("rain_mm", ogr.OFTReal, None, 3),
        ("rain_key", ogr.OFTString, 16, None),
        ("times", ogr.OFTString, 20, None),
    ]
    for name, ftype, width, precision in fields:
        fd = ogr.FieldDefn(name, ftype)
        if width:
            fd.SetWidth(int(width))
        if precision:
            fd.SetPrecision(int(precision))
        layer.CreateField(fd)

    layer_defn = layer.GetLayerDefn()
    written = 0
    for r in records:
        station_id = str(station_id_of(r) or "").strip()
        lon = safe_float(r.get("Lon"))
        lat = safe_float(r.get("Lat"))
        rain = _precip_value_for_accum_hours(r, int(accum_hours))
        if not station_id:
            continue
        if abs(lon) < 0.01 or abs(lat) < 0.01:
            continue
        if rain < 0:
            continue

        feat = ogr.Feature(layer_defn)
        feat.SetField("station_id", station_id)
        feat.SetField("station_nm", _pick_station_name(r))
        feat.SetField("lon", float(lon))
        feat.SetField("lat", float(lat))
        feat.SetField("accum_h", int(accum_hours))
        feat.SetField("rain_mm", float(rain))
        feat.SetField("rain_key", field_name)
        feat.SetField("times", str(times or ""))

        point = ogr.Geometry(ogr.wkbPoint)
        point.AddPoint(float(lon), float(lat))
        feat.SetGeometry(point)
        layer.CreateFeature(feat)
        feat = None
        point = None
        written += 1

    ds = None
    _write_cpg_file(out_shp_path, "UTF-8")
    return {
        "station_count": written,
        "accum_hours": int(accum_hours),
        "field": field_name,
        "shapefile_path": os.path.abspath(out_shp_path),
    }
