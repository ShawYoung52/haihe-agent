from __future__ import annotations

import logging
import traceback
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request, Response

from .config import Settings, load_settings
from .db import fetch_geometries, fetch_sql_text
from .png import encode_rgba_png
from .render import parse_style, render_png_array, tile_bbox
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="SQL driven vector WMS renderer")
logger = logging.getLogger(__name__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],         # 允许所有源
    allow_credentials=True,
    allow_methods=["*"],         # 允许所有方法
    allow_headers=["*"],         # 允许所有请求头
)

def get_settings() -> Settings:
    return load_settings()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/wms")
def wms_getmap(
    request: Request,
    sql_id: Annotated[str, Query(description="ID in the SQL registry table")],
    bbox: Annotated[str | None, Query(description="minx,miny,maxx,maxy")] = None,
    crs: str | None = None,
    srs: str | None = None,
    width: int = 256,
    height: int = 256,
    format: str = "image/png",
) -> Response:
    params = _case_insensitive_params(request)
    request_sql_id = str(params.get("sql_id", sql_id))
    request_format = str(params.get("format", format)).lower()
    if request_format not in ("image/png", "png"):
        raise HTTPException(status_code=400, detail="Only image/png is supported")
    request_bbox = params.get("bbox", bbox)
    if not request_bbox:
        raise HTTPException(status_code=400, detail="bbox is required for /wms")

    settings = get_settings()
    request_crs = params.get("crs", crs) or params.get("srs", srs)
    srid = _parse_srid(request_crs, settings.default_srid)
    render_bbox = _parse_bbox(str(request_bbox))
    render_width = _parse_int(params.get("width", width), "width")
    render_height = _parse_int(params.get("height", height), "height")
    style_params = dict(params)
    style_params.pop("width", None)
    style_params.pop("height", None)
    return _render_response(request_sql_id, render_bbox, render_width, render_height, srid, style_params)


@app.get("/tiles/{z}/{x}/{y}.png")
def xyz_tile(
    request: Request,
    z: int,
    x: int,
    y: int,
    sql_id: Annotated[str, Query(description="ID in the SQL registry table")],
    size: int = 256,
    crs: str | None = None,
) -> Response:
    settings = get_settings()
    params = _case_insensitive_params(request)
    request_sql_id = str(params.get("sql_id", sql_id))
    srid = _parse_srid(params.get("crs", crs), settings.default_srid)
    tile_size = _parse_int(params.get("size", size), "size")
    try:
        bbox = tile_bbox(
            _parse_int(z, "z"),
            _parse_int(x, "x"),
            _parse_int(y, "y"),
            srid,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _render_response(request_sql_id, bbox, tile_size, tile_size, srid, params)


def _render_response(
    sql_id: str,
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
    srid: int,
    query_params: dict[str, str],
) -> Response:
    width = _parse_int(width, "width")
    height = _parse_int(height, "height")
    if width <= 0 or height <= 0 or width > 2048 or height > 2048:
        raise HTTPException(status_code=400, detail="width/height must be between 1 and 2048")

    settings = get_settings()
    try:
        sql_text = fetch_sql_text(settings, sql_id)
        geometries = fetch_geometries(settings, sql_text, bbox, srid)
        style = parse_style(query_params)
        image = render_png_array(geometries, bbox, width, height, style)
        return Response(
            content=encode_rgba_png(image),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=60"},
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("WMS render failed")
        raise HTTPException(
            status_code=500,
            detail={
                "message": str(exc),
                "type": type(exc).__name__,
                "traceback": traceback.format_exc().splitlines()[-6:],
            },
        ) from exc


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    parts = [float(part.strip()) for part in value.split(",")]
    if len(parts) != 4:
        raise HTTPException(status_code=400, detail="bbox must contain 4 numbers")
    minx, miny, maxx, maxy = parts
    if minx >= maxx or miny >= maxy:
        raise HTTPException(status_code=400, detail="bbox is invalid")
    return minx, miny, maxx, maxy


def _parse_srid(value: str | int | None, default: int = 4326) -> int:
    if not value:
        return int(default)
    upper = str(value).upper()
    if upper.startswith("EPSG:"):
        return int(upper.split(":", 1)[1])
    return int(value)


def _parse_int(value: str | int, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{name} must be an integer") from exc


def _case_insensitive_params(request: Request) -> dict[str, str]:
    return {key.lower(): value for key, value in request.query_params.items()}
