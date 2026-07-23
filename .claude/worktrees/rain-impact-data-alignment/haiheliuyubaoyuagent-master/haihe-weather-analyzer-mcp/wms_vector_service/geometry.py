from __future__ import annotations

import binascii
import json
import struct
from typing import Any

Geometry = dict[str, Any]

_GEOJSON_KEYS = (
    "geom_geojson",
    "geometry_geojson",
    "geojson",
    "st_asgeojson",
)
_WKB_KEYS = ("geom_wkb", "geometry_wkb", "wkb", "geom", "geometry")


def geometry_from_row(row: Any) -> Geometry | None:
    """从查询结果的一行中解析几何（支持 dict / SQLAlchemy Row / 单列 GeoJSON 字符串）。"""
    if row is None:
        return None
    if hasattr(row, "_mapping"):
        return geometry_from_row(dict(row._mapping))
    if isinstance(row, dict):
        for key in _GEOJSON_KEYS:
            value = _get_case_insensitive(row, key)
            if value:
                return _load_geojson(value)
        for key in _WKB_KEYS:
            value = _get_case_insensitive(row, key)
            if value:
                if isinstance(value, str) and value.lstrip().startswith("{"):
                    return _load_geojson(value)
                return _load_wkb(value)
        for _k, value in row.items():
            if isinstance(value, str) and value.strip().startswith("{"):
                try:
                    return _load_geojson(value)
                except Exception:
                    continue
        return None
    if isinstance(row, (str, bytes, bytearray, memoryview)):
        s = row.decode("utf-8") if isinstance(row, (bytes, bytearray, memoryview)) else str(row)
        s = s.strip()
        if s.startswith("{"):
            return _load_geojson(s)
        return None
    return None


def iter_geometries(geometry: Geometry):
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "GeometryCollection":
        for child in geometry.get("geometries", []):
            yield from iter_geometries(child)
        return
    if gtype and coords is not None:
        yield gtype, coords


def _get_case_insensitive(row: dict[str, Any], key: str) -> Any:
    if key in row:
        return row[key]
    lowered = key.lower()
    for row_key, value in row.items():
        if row_key.lower() == lowered:
            return value
    return None


def _load_geojson(value: Any) -> Geometry:
    if isinstance(value, dict):
        parsed = value
    elif isinstance(value, (bytes, bytearray, memoryview)):
        parsed = json.loads(bytes(value).decode("utf-8"))
    else:
        parsed = json.loads(str(value))

    if parsed.get("type") == "Feature":
        return parsed["geometry"]
    return parsed


def _load_wkb(value: Any) -> Geometry:
    if isinstance(value, memoryview):
        data = value.tobytes()
    elif isinstance(value, bytearray):
        data = bytes(value)
    elif isinstance(value, bytes):
        data = value
    elif isinstance(value, str):
        data = binascii.unhexlify(value[2:] if value.startswith("\\x") else value)
    else:
        raise TypeError(f"Unsupported geometry value type: {type(value)!r}")

    geometry, offset = _parse_wkb(data, 0)
    if offset > len(data):
        raise ValueError("Invalid WKB geometry")
    return geometry


def _parse_wkb(data: bytes, offset: int) -> tuple[Geometry, int]:
    byte_order = data[offset]
    endian = "<" if byte_order == 1 else ">"
    offset += 1
    raw_type = struct.unpack_from(f"{endian}I", data, offset)[0]
    offset += 4
    has_z = bool(raw_type & 0x80000000) or raw_type // 1000 in (1, 3)
    has_m = bool(raw_type & 0x40000000) or raw_type // 1000 in (2, 3)
    has_srid = bool(raw_type & 0x20000000)
    gtype = raw_type & 0xFF if raw_type & 0xE0000000 else raw_type % 1000
    dimensions = 2 + int(has_z) + int(has_m)
    if has_srid:
        offset += 4

    if gtype == 1:
        point, offset = _read_point(data, offset, endian, dimensions)
        return {"type": "Point", "coordinates": point}, offset
    if gtype == 2:
        coords, offset = _read_point_array(data, offset, endian, dimensions)
        return {"type": "LineString", "coordinates": coords}, offset
    if gtype == 3:
        rings, offset = _read_rings(data, offset, endian, dimensions)
        return {"type": "Polygon", "coordinates": rings}, offset
    if gtype == 4:
        geoms, offset = _read_geometry_array(data, offset, endian)
        return {"type": "MultiPoint", "coordinates": [g["coordinates"] for g in geoms]}, offset
    if gtype == 5:
        geoms, offset = _read_geometry_array(data, offset, endian)
        return {"type": "MultiLineString", "coordinates": [g["coordinates"] for g in geoms]}, offset
    if gtype == 6:
        geoms, offset = _read_geometry_array(data, offset, endian)
        return {"type": "MultiPolygon", "coordinates": [g["coordinates"] for g in geoms]}, offset
    if gtype == 7:
        geoms, offset = _read_geometry_array(data, offset, endian)
        return {"type": "GeometryCollection", "geometries": geoms}, offset
    raise ValueError(f"Unsupported WKB geometry type: {raw_type}")


def _read_point(data: bytes, offset: int, endian: str, dimensions: int) -> tuple[list[float], int]:
    values = struct.unpack_from(f"{endian}{'d' * dimensions}", data, offset)
    return [values[0], values[1]], offset + 8 * dimensions


def _read_point_array(
    data: bytes,
    offset: int,
    endian: str,
    dimensions: int,
) -> tuple[list[list[float]], int]:
    count = struct.unpack_from(f"{endian}I", data, offset)[0]
    offset += 4
    coords = []
    for _ in range(count):
        point, offset = _read_point(data, offset, endian, dimensions)
        coords.append(point)
    return coords, offset


def _read_rings(
    data: bytes,
    offset: int,
    endian: str,
    dimensions: int,
) -> tuple[list[list[list[float]]], int]:
    ring_count = struct.unpack_from(f"{endian}I", data, offset)[0]
    offset += 4
    rings = []
    for _ in range(ring_count):
        ring, offset = _read_point_array(data, offset, endian, dimensions)
        rings.append(ring)
    return rings, offset


def _read_geometry_array(data: bytes, offset: int, endian: str) -> tuple[list[Geometry], int]:
    count = struct.unpack_from(f"{endian}I", data, offset)[0]
    offset += 4
    geoms = []
    for _ in range(count):
        geom, offset = _parse_wkb(data, offset)
        geoms.append(geom)
    return geoms, offset
