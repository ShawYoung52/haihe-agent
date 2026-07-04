"""问答智能体暴雨影响河流专题图工具。

这里只负责 MCP 参数解析、降雨站点提取和返回格式适配；
真正的河流筛选逻辑统一调用 hhlyqyxt-master/utils/rainfall_impact_geojson.py。
"""
from __future__ import annotations

import logging
import math
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
TOOL_NAME = "get_affected_river_network_by_rainfall"
DEFAULT_DIRECT_GRAPH_MATCH_KM = 3.0


def _load_impact_builder():
    repo_root = Path(__file__).resolve().parents[2]
    core_utils_dir = repo_root / "hhlyqyxt-master" / "utils"
    if not core_utils_dir.exists():
        raise RuntimeError(f"未找到统一暴雨影响河流工具目录：{core_utils_dir}")
    if str(core_utils_dir) not in sys.path:
        sys.path.insert(0, str(core_utils_dir))
    from rainfall_impact_geojson import build_rainstorm_impact_thematic_map

    return build_rainstorm_impact_thematic_map


def _resolve_graph_path(base_tools) -> str | None:
    graph_path = base_tools.config.get("paths", "graph", fallback="")
    if not graph_path:
        return None
    v5_path = os.path.join(os.path.dirname(graph_path), "river_directed_v5.pkl")
    return v5_path if os.path.isfile(v5_path) else graph_path


def _extract_rainstorm_stations(rainfall_result: dict, threshold_mm: float, base_tools) -> tuple[list[dict], set[str], set[str]]:
    level_to_threshold = {name: low for name, low, _high in base_tools.RAIN_LEVELS}
    stations: list[dict] = []
    zone_77_regions: set[str] = set()
    admin_divisions: set[str] = set()

    for level_item in rainfall_result.get("level_analysis", []) or []:
        level = level_item.get("level", "")
        if level_to_threshold.get(level, math.inf) < threshold_mm:
            continue
        zone_77_regions.update(str(x).strip() for x in level_item.get("zone_77_regions", []) or [] if x)
        admin_divisions.update(str(x).strip() for x in level_item.get("admin_divisions", []) or [] if x)
        for station in level_item.get("stations", []) or []:
            if _station_reaches_threshold(station, threshold_mm):
                stations.append(_normalize_station(station, level))

    return stations, zone_77_regions, admin_divisions


def _station_reaches_threshold(station: Any, threshold_mm: float) -> bool:
    if not isinstance(station, dict):
        return False
    try:
        return float(station.get("rainfall") or 0.0) >= float(threshold_mm)
    except (TypeError, ValueError):
        return False


def _normalize_station(station: dict, level: str) -> dict:
    return {
        "station_id": station.get("station_id"),
        "station_name": station.get("name") or station.get("station_name"),
        "name": station.get("name") or station.get("station_name"),
        "lon": station.get("lon"),
        "lat": station.get("lat"),
        "rainfall": station.get("rainfall"),
        "rain_24h": station.get("rainfall"),
        "level": level,
    }


def _unregister_existing_tool(mcp, name: str) -> None:
    for manager in (mcp, getattr(mcp, "_tool_manager", None), getattr(mcp, "tool_manager", None)):
        if manager is None:
            continue
        remover = getattr(manager, "remove_tool", None)
        if callable(remover):
            remover(name)
            return
        for attr in ("_tools", "tools"):
            registry = getattr(manager, attr, None)
            if isinstance(registry, dict) and name in registry:
                registry.pop(name)
                return


def _empty_response(rainfall_result: dict, threshold_mm: float, zones: set[str], admins: set[str]) -> dict:
    time_range = rainfall_result.get("time_range_readable", "")
    return {
        "time_range_readable": time_range,
        "rainfall_threshold_mm": threshold_mm,
        "affected_rivers": [],
        "affected_zone_77_regions": sorted(zones),
        "affected_admin_divisions": sorted(admins),
        "stations": [],
        "total_segments": 0,
        "affected_segments": 0,
        "segments": [],
        "summary": f"统计时段 {time_range} 内，未达到 {threshold_mm}mm 降雨阈值的河系数据。",
    }


def _format_mcp_response(result: dict, rainfall_result: dict, threshold_mm: float, zones: set[str], admins: set[str]) -> dict:
    segments = result.get("segments", [])
    affected_rivers = result.get("affected_rivers") or sorted({str(s.get("rivername") or "").strip() for s in segments if s.get("rivername")})
    time_range = rainfall_result.get("time_range_readable", "")
    return {
        "time_range_readable": time_range,
        "rainfall_threshold_mm": threshold_mm,
        "affected_rivers": affected_rivers,
        "affected_zone_77_regions": sorted(zones),
        "affected_admin_divisions": sorted(admins),
        "stations": result.get("impact_stations", []),
        "total_segments": len(segments),
        "affected_segments": len(segments),
        "segments": segments,
        "start_stats": {
            "downstream_edge_count": result.get("river_summary", {}).get("downstream_edge_count", 0),
            "direct_part_match_km": result.get("params", {}).get("direct_match_km", DEFAULT_DIRECT_GRAPH_MATCH_KM),
        },
        "river_geojson": result.get("river_geojson"),
        "summary": f"统计时段 {time_range} 内，降雨量≥{threshold_mm}mm 的站点共影响 {len(affected_rivers)} 条河流。",
        "rules": {
            "direct": "ST_Dump(full_v5.geom) 后单线段 ST_DWithin 30km 命中，直接段不截断",
            "downstream": "从直接命中拓扑边沿 pkl 河网追踪 downstream_km，回 full_v5 匹配最近真实河段并截断",
            "dedupe": "按拓扑 edge_key 区分，不按 river_name/objectid 提前误删",
        },
    }


def register_fixed_rainfall_impact_tool(mcp) -> None:
    """注册暴雨影响河流专题图工具。"""
    _unregister_existing_tool(mcp, TOOL_NAME)

    @mcp.tool()
    def get_affected_river_network_by_rainfall(
        time_str: str,
        start_time: str = "",
        end_time: str = "",
        rainfall_threshold_mm: float = 50.0,
        max_edges: int = 5000,
        include_background: bool = True,
        downstream_km: float = 50.0,
        direct_graph_match_km: float = DEFAULT_DIRECT_GRAPH_MATCH_KM,
    ) -> dict:
        """制作暴雨影响河流专题图数据。"""
        import tools as base_tools

        pg_conf = base_tools.config["postgres"]
        custom_timerange = f"[{start_time},{end_time}]" if start_time and end_time else ""
        rainfall_result = base_tools._analyze_rainfall_core(time_str, pg_conf, custom_timerange)
        stations, zone_77_regions, admin_divisions = _extract_rainstorm_stations(rainfall_result, rainfall_threshold_mm, base_tools)
        if not stations:
            return _empty_response(rainfall_result, rainfall_threshold_mm, zone_77_regions, admin_divisions)

        builder = _load_impact_builder()
        result = builder(
            stations,
            pg_conf=pg_conf,
            graph_path=_resolve_graph_path(base_tools),
            rainfall_threshold_mm=rainfall_threshold_mm,
            downstream_km=downstream_km,
            direct_match_km=direct_graph_match_km,
            max_segments=max_edges,
            extra_summary={"time_range_readable": rainfall_result.get("time_range_readable", "")},
        )
        return _format_mcp_response(result, rainfall_result, rainfall_threshold_mm, zone_77_regions, admin_divisions)

    logger.info("已注册 %s 工具", TOOL_NAME)
