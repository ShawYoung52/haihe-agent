"""暴雨影响河流专题图 URL 解析服务。

对外只保留一个入口：build_rainstorm_impact_map_from_url(url)。
调用方传 rainstorm_impact_map.json 的 HTTP 地址，本方法负责请求、校验并整理成前端/GIS 可直接渲染的结构。
"""
from __future__ import annotations

from typing import Any

import requests

DEFAULT_TITLE = "暴雨影响河流专题图"
DEFAULT_LEGEND = [
    {"label": "直接影响河段", "type": "line", "color": "#E53935", "width": 4},
    {"label": "下游影响河段", "type": "line", "color": "#FB8C00", "width": 3},
    {"label": "暴雨触发站", "type": "circle", "color": "#1E88E5", "radius": 6},
]
DEFAULT_RIVER_STYLE = {
    "downstream_50km": {
        "name": "下游影响河段",
        "color": "#FB8C00",
        "width": 3,
        "opacity": 0.9,
        "filter": {"property": "impact_type", "equals": "downstream_50km"},
    },
    "direct_buffer": {
        "name": "直接影响河段",
        "color": "#E53935",
        "width": 4,
        "opacity": 0.95,
        "filter": {"property": "impact_type", "equals": "direct_buffer"},
    },
}
DEFAULT_STATION_STYLE = {
    "name": "暴雨触发站",
    "color": "#1E88E5",
    "strokeColor": "#FFFFFF",
    "radius": 6,
    "opacity": 0.95,
}


def build_rainstorm_impact_map_from_url(url: str, *, timeout: int = 30) -> dict[str, Any]:
    """请求 rainstorm_impact_map.json URL，并返回前端/GIS 可渲染的专题图结构。"""
    map_package = _fetch_map_package(url, timeout=timeout)
    rivers_layer = _required_dict(_required_dict(map_package, "layers"), "rivers")
    stations_layer = _required_dict(map_package["layers"], "stations")

    return {
        "status": "ok",
        "product_type": map_package.get("product_type") or "thematic_map",
        "title": map_package.get("title") or DEFAULT_TITLE,
        "summary": map_package.get("summary") or {},
        "rainfall_source": map_package.get("rainfall_source") or {},
        "map_layers": {
            "rivers": {
                "url": _required_text(rivers_layer, "url"),
                "style": rivers_layer.get("style") or DEFAULT_RIVER_STYLE,
            },
            "stations": {
                "url": _required_text(stations_layer, "url"),
                "style": stations_layer.get("style") or DEFAULT_STATION_STYLE,
            },
        },
        "legend": map_package.get("legend") or DEFAULT_LEGEND,
        "files": map_package.get("files") or {},
    }


def _fetch_map_package(url: str, *, timeout: int) -> dict[str, Any]:
    if not isinstance(url, str) or not url.strip().startswith(("http://", "https://")):
        raise ValueError("url 必须是 rainstorm_impact_map.json 的 HTTP 地址")
    try:
        response = requests.get(url.strip(), timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"请求 rainstorm_impact_map.json 失败：{url}") from exc
    except ValueError as exc:
        raise RuntimeError("rainstorm_impact_map.json 返回内容不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("rainstorm_impact_map.json 顶层必须是 JSON 对象")
    return payload


def _required_dict(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"rainstorm_impact_map.json 缺少对象字段：{key}")
    return value


def _required_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"rainstorm_impact_map.json 缺少文本字段：{key}")
    return value.strip()


__all__ = ["build_rainstorm_impact_map_from_url"]
