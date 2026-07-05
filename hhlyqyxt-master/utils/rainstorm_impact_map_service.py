"""暴雨影响河流 JSON 地址提取服务。

同事入库只需要两个地址：影响河流 JSON 地址、暴雨站点 JSON 地址。
调用方传 rainstorm_impact_map.json 的 HTTP 地址，本方法负责请求、校验并返回这两个地址。
"""
from __future__ import annotations

from typing import Any

import requests


def get_rainstorm_impact_json_urls_from_url(url: str, *, timeout: int = 30) -> dict[str, Any]:
    """从 rainstorm_impact_map.json 地址中提取影响河流和暴雨站点两个 JSON 地址。"""
    map_package = _fetch_map_package(url, timeout=timeout)
    layers = _required_dict(map_package, "layers")
    rivers_layer = _required_dict(layers, "rivers")
    stations_layer = _required_dict(layers, "stations")

    return {
        "status": "ok",
        "map_json_url": url.strip(),
        "river_json_url": _required_text(rivers_layer, "url"),
        "station_json_url": _required_text(stations_layer, "url"),
        "summary": map_package.get("summary") or {},
        "rainfall_source": map_package.get("rainfall_source") or {},
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


__all__ = ["get_rainstorm_impact_json_urls_from_url"]
