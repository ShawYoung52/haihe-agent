from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .geometry import iter_geometries

WEB_MERCATOR_LIMIT = 20037508.342789244


@dataclass(frozen=True)
class RenderStyle:
    stroke: tuple[int, int, int, int] = (34, 95, 160, 255)
    fill: tuple[int, int, int, int] = (56, 135, 190, 90)
    line_width: float = 2.0
    point_radius: float = 5.0


def tile_bbox(z: int, x: int, y: int, srid: int = 4326) -> tuple[float, float, float, float]:
    z = int(z)
    x = int(x)
    y = int(y)
    srid = int(srid)
    if srid == 4326:
        return geodetic_tile_bbox(z, x, y)
    if srid != 3857:
        raise ValueError(f"Unsupported tile SRID: {srid}")

    tiles = 2**z
    span = WEB_MERCATOR_LIMIT * 2 / tiles
    minx = -WEB_MERCATOR_LIMIT + x * span
    maxx = minx + span
    maxy = WEB_MERCATOR_LIMIT - y * span
    miny = maxy - span
    return minx, miny, maxx, maxy


def geodetic_tile_bbox(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    tiles = 2**z
    lon_span = 360.0 / tiles
    lat_span = 180.0 / tiles
    minx = -180.0 + x * lon_span
    maxx = minx + lon_span
    maxy = 90.0 - y * lat_span
    miny = maxy - lat_span
    return minx, miny, maxx, maxy


def parse_style(params: dict[str, Any]) -> RenderStyle:
    style_json = params.get("style")
    style: dict[str, Any] = {}
    if style_json:
        import json

        style = json.loads(style_json) if isinstance(style_json, str) else dict(style_json)

    def pick(*names: str, default: Any = None) -> Any:
        for name in names:
            if name in params and params[name] not in (None, ""):
                return params[name]
            if name in style and style[name] not in (None, ""):
                return style[name]
        return default

    opacity = float(pick("opacity", default=1.0))
    fill_opacity = float(pick("fill_opacity", "fillOpacity", default=0.35))
    stroke_opacity = float(pick("stroke_opacity", "strokeOpacity", default=1.0))

    return RenderStyle(
        stroke=_parse_color(pick("stroke", "stroke_color", "strokeColor", default="#225fa0"), opacity * stroke_opacity),
        fill=_parse_color(pick("fill", "fill_color", "fillColor", default="#3887be"), opacity * fill_opacity),
        line_width=float(pick("width", "line_width", "lineWidth", default=2.0)),
        point_radius=float(pick("radius", "point_radius", "pointRadius", default=5.0)),
    )


def render_png_array(
    geometries: list[dict[str, Any]],
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
    style: RenderStyle,
) -> np.ndarray:
    try:
        return _render_png_array_geopandas(geometries, bbox, width, height, style)
    except ImportError as exc:
        raise RuntimeError(
            "GeoPandas rendering requires geopandas, shapely and matplotlib. "
            "Install project dependencies before starting the WMS service."
        ) from exc


def _render_png_array_geopandas(
    geometries: list[dict[str, Any]],
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
    style: RenderStyle,
) -> np.ndarray:
    import geopandas as gpd
    import matplotlib

    matplotlib.use("Agg", force=True)
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from shapely.geometry import shape

    width = int(width)
    height = int(height)
    if not geometries:
        return np.zeros((height, width, 4), dtype=np.uint8)

    shapely_geometries = [shape(geometry) for geometry in geometries if geometry]
    shapely_geometries = [geometry for geometry in shapely_geometries if not geometry.is_empty]
    if not shapely_geometries:
        return np.zeros((height, width, 4), dtype=np.uint8)

    dpi = 100
    figure = Figure(figsize=(width / dpi, height / dpi), dpi=dpi, frameon=False)
    canvas = FigureCanvasAgg(figure)
    axis = figure.add_axes((0, 0, 1, 1))
    axis.set_axis_off()
    axis.set_xlim(float(bbox[0]), float(bbox[2]))
    axis.set_ylim(float(bbox[1]), float(bbox[3]))
    axis.set_aspect("auto")
    axis.margins(0)
    figure.patch.set_alpha(0)
    axis.patch.set_alpha(0)

    series = gpd.GeoSeries(shapely_geometries)
    stroke = _mpl_color(style.stroke)
    fill = _mpl_color(style.fill)

    polygons = series[series.geom_type.isin(["Polygon", "MultiPolygon"])]
    lines = series[series.geom_type.isin(["LineString", "LinearRing", "MultiLineString"])]
    points = series[series.geom_type.isin(["Point", "MultiPoint"])]

    if not polygons.empty:
        polygons.plot(ax=axis, facecolor=fill, edgecolor=stroke, linewidth=style.line_width)
    if not lines.empty:
        lines.plot(ax=axis, color=stroke, linewidth=style.line_width)
    if not points.empty:
        points.plot(
            ax=axis,
            marker="o",
            markersize=max(1.0, style.point_radius * style.point_radius),
            facecolor=fill,
            edgecolor=stroke,
            linewidth=style.line_width,
        )

    axis.set_xlim(float(bbox[0]), float(bbox[2]))
    axis.set_ylim(float(bbox[1]), float(bbox[3]))
    axis.set_aspect("auto")
    canvas.draw()
    return np.asarray(canvas.buffer_rgba()).copy()


def _mpl_color(color: tuple[int, int, int, int]) -> tuple[float, float, float, float]:
    return color[0] / 255.0, color[1] / 255.0, color[2] / 255.0, color[3] / 255.0


def render_png_array_slow(
    geometries: list[dict[str, Any]],
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
    style: RenderStyle,
) -> np.ndarray:
    image = np.zeros((height, width, 4), dtype=np.uint8)
    for geometry in geometries:
        for gtype, coords in iter_geometries(geometry):
            if gtype == "Point":
                _draw_point(image, _project(coords, bbox, width, height), style)
            elif gtype == "MultiPoint":
                for point in coords:
                    _draw_point(image, _project(point, bbox, width, height), style)
            elif gtype == "LineString":
                _draw_line_string(image, coords, bbox, width, height, style)
            elif gtype == "MultiLineString":
                for line in coords:
                    _draw_line_string(image, line, bbox, width, height, style)
            elif gtype == "Polygon":
                _draw_polygon(image, coords, bbox, width, height, style)
            elif gtype == "MultiPolygon":
                for polygon in coords:
                    _draw_polygon(image, polygon, bbox, width, height, style)

    return image


def _parse_color(value: Any, opacity: float) -> tuple[int, int, int, int]:
    if isinstance(value, (list, tuple)):
        rgb = [int(v) for v in value[:3]]
        alpha = int(value[3]) if len(value) > 3 else 255
        return rgb[0], rgb[1], rgb[2], _clamp_alpha(alpha * opacity)

    text = str(value).strip()
    if text.startswith("#"):
        hex_value = text[1:]
        if len(hex_value) == 3:
            hex_value = "".join(ch * 2 for ch in hex_value)
        rgb = [int(hex_value[i : i + 2], 16) for i in range(0, 6, 2)]
        alpha = int(hex_value[6:8], 16) if len(hex_value) >= 8 else 255
        return rgb[0], rgb[1], rgb[2], _clamp_alpha(alpha * opacity)

    if text.startswith("rgba"):
        parts = text[text.find("(") + 1 : text.rfind(")")].split(",")
        r, g, b = [int(float(part.strip())) for part in parts[:3]]
        return r, g, b, _clamp_alpha(float(parts[3].strip()) * 255 * opacity)

    raise ValueError(f"Unsupported color value: {value!r}")


def _clamp_alpha(value: float) -> int:
    return max(0, min(255, int(round(value))))


def _project(point: list[float], bbox: tuple[float, float, float, float], width: int, height: int) -> tuple[float, float]:
    minx, miny, maxx, maxy = bbox
    x = (float(point[0]) - minx) / (maxx - minx) * width
    y = (maxy - float(point[1])) / (maxy - miny) * height
    return x, y


def _blend_pixel(image: np.ndarray, x: int, y: int, color: tuple[int, int, int, int]) -> None:
    if x < 0 or y < 0 or y >= image.shape[0] or x >= image.shape[1] or color[3] <= 0:
        return
    src_a = color[3] / 255.0
    dst_a = image[y, x, 3] / 255.0
    out_a = src_a + dst_a * (1.0 - src_a)
    if out_a <= 0:
        return
    for channel in range(3):
        src = color[channel] / 255.0
        dst = image[y, x, channel] / 255.0
        image[y, x, channel] = int(round((src * src_a + dst * dst_a * (1.0 - src_a)) / out_a * 255))
    image[y, x, 3] = int(round(out_a * 255))


def _blend_mask(image: np.ndarray, mask: np.ndarray, color: tuple[int, int, int, int]) -> None:
    if color[3] <= 0 or not mask.any():
        return
    ys, xs = np.where(mask)
    for y, x in zip(ys, xs):
        _blend_pixel(image, int(x), int(y), color)


def _draw_point(image: np.ndarray, point: tuple[float, float], style: RenderStyle) -> None:
    cx, cy = point
    radius = style.point_radius
    minx = math.floor(cx - radius)
    maxx = math.ceil(cx + radius)
    miny = math.floor(cy - radius)
    maxy = math.ceil(cy + radius)
    r2 = radius * radius
    for y in range(miny, maxy + 1):
        for x in range(minx, maxx + 1):
            if (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 <= r2:
                _blend_pixel(image, x, y, style.fill)
    _draw_circle_outline(image, cx, cy, radius, style.stroke, style.line_width)


def _draw_circle_outline(
    image: np.ndarray,
    cx: float,
    cy: float,
    radius: float,
    color: tuple[int, int, int, int],
    width: float,
) -> None:
    minx = math.floor(cx - radius - width)
    maxx = math.ceil(cx + radius + width)
    miny = math.floor(cy - radius - width)
    maxy = math.ceil(cy + radius + width)
    inner = max(0.0, radius - width / 2)
    outer = radius + width / 2
    for y in range(miny, maxy + 1):
        for x in range(minx, maxx + 1):
            dist = math.hypot(x + 0.5 - cx, y + 0.5 - cy)
            if inner <= dist <= outer:
                _blend_pixel(image, x, y, color)


def _draw_line_string(
    image: np.ndarray,
    coords: list[list[float]],
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
    style: RenderStyle,
) -> None:
    points = [_project(point, bbox, width, height) for point in coords]
    for start, end in zip(points, points[1:]):
        _draw_segment(image, start, end, style.stroke, style.line_width)


def _draw_segment(
    image: np.ndarray,
    start: tuple[float, float],
    end: tuple[float, float],
    color: tuple[int, int, int, int],
    width: float,
) -> None:
    x1, y1 = start
    x2, y2 = end
    pad = max(1.0, width / 2.0)
    minx = math.floor(min(x1, x2) - pad)
    maxx = math.ceil(max(x1, x2) + pad)
    miny = math.floor(min(y1, y2) - pad)
    maxy = math.ceil(max(y1, y2) + pad)
    length2 = (x2 - x1) ** 2 + (y2 - y1) ** 2
    radius = width / 2.0
    for y in range(miny, maxy + 1):
        for x in range(minx, maxx + 1):
            if length2 == 0:
                dist = math.hypot(x + 0.5 - x1, y + 0.5 - y1)
            else:
                t = ((x + 0.5 - x1) * (x2 - x1) + (y + 0.5 - y1) * (y2 - y1)) / length2
                t = max(0.0, min(1.0, t))
                px = x1 + t * (x2 - x1)
                py = y1 + t * (y2 - y1)
                dist = math.hypot(x + 0.5 - px, y + 0.5 - py)
            if dist <= radius:
                _blend_pixel(image, x, y, color)


def _draw_polygon(
    image: np.ndarray,
    rings: list[list[list[float]]],
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
    style: RenderStyle,
) -> None:
    if not rings:
        return
    projected_rings = [[_project(point, bbox, width, height) for point in ring] for ring in rings]
    mask = _ring_mask(projected_rings[0], width, height)
    for hole in projected_rings[1:]:
        mask &= ~_ring_mask(hole, width, height)
    _blend_mask(image, mask, style.fill)
    for ring in projected_rings:
        for start, end in zip(ring, ring[1:] + ring[:1]):
            _draw_segment(image, start, end, style.stroke, style.line_width)


def _ring_mask(ring: list[tuple[float, float]], width: int, height: int) -> np.ndarray:
    mask = np.zeros((height, width), dtype=bool)
    if len(ring) < 3:
        return mask
    for y in range(height):
        scan_y = y + 0.5
        intersections = []
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
            if (y1 > scan_y) != (y2 > scan_y):
                x = x1 + (scan_y - y1) * (x2 - x1) / (y2 - y1)
                intersections.append(x)
        intersections.sort()
        for left, right in zip(intersections[::2], intersections[1::2]):
            start = max(0, math.floor(left))
            end = min(width - 1, math.ceil(right))
            if end >= start:
                mask[y, start : end + 1] = True
    return mask
