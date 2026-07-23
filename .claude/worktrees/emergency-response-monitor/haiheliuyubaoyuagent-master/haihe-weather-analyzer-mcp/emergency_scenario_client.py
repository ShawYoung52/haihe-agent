"""
调用 emergency_http_server 的 /scenario/* 接口时的公共参数与 GET 请求。

默认在查询串附带 map_render=wms_sql，使服务端登记 hh_gis_wms_sql 并返回 map_sql_id，
避免 MCP/导出脚本拿到超大 map_geojson。

环境变量：
  EMERGENCY_HTTP_BASE   服务根 URL，默认 http://127.0.0.1:8080
  （GIS 父页面 WMS 与 python -m wms_vector_service 见 WMS_PORT 默认 8008）
  SCENARIO_MAP_RENDER   默认 wms_sql；设为空、0、false、geojson、full 则不传 map_render（传统整包 GeoJSON）
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def emergency_http_base_url() -> str:
    return (os.environ.get("EMERGENCY_HTTP_BASE") or os.environ.get("EMERGENCY_HTTP_BASE_URL") or "http://127.0.0.1:8080").strip().rstrip("/")


def scenario_map_render_kw(*, map_render: Optional[str] = None) -> Dict[str, str]:
    """
    返回需并入 GET 查询串的 map_render（或空字典）。
    map_render 显式传入时优先：空字符串表示不传（走服务端大块 GeoJSON）。
    """
    if map_render is not None:
        mr = str(map_render).strip()
        return {} if not mr else {"map_render": mr}
    env_val = (os.environ.get("SCENARIO_MAP_RENDER") or "wms_sql").strip().lower()
    if env_val in {"", "0", "false", "off", "geojson", "full"}:
        return {}
    return {"map_render": env_val}


def merge_scenario_query_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """若调用方未指定 map_render / map_mode，则按 SCENARIO_MAP_RENDER 追加。"""
    out = {k: v for k, v in params.items() if v is not None}
    if "map_render" in out or "map_mode" in out:
        return out
    out.update(scenario_map_render_kw())
    return out


def fetch_scenario_get(
    base_url: str,
    route: str,
    params: Dict[str, Any],
    *,
    timeout_sec: int = 120,
    map_render: Optional[str] = None,
) -> Dict[str, Any]:
    """
    GET 调用 /scenario/*（与 emergency_http_server 路由一致）。
    map_render：None 表示用环境默认；传 \"\" 表示不传 map_render。
    """
    merged = {k: v for k, v in params.items() if v is not None}
    if "map_render" not in merged and "map_mode" not in merged:
        merged.update(scenario_map_render_kw(map_render=map_render))
    query = urlencode(merged, doseq=False)
    url = f"{base_url.rstrip('/')}{route}?{query}"
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)
