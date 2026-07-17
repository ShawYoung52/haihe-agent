"""问答智能体暴雨影响河流专题图工具。

这里只负责 MCP 参数解析、降雨站点提取和返回格式适配；
真正的河流筛选逻辑统一调用 hhlyqyxt-master/utils/rainfall_impact_geojson.py。
"""
from __future__ import annotations

import logging
import math
import sys
from pathlib import Path
from typing import Any, Callable

from constants import DIRECTED_GRAPH_FILENAME, RIVER_TABLE_VERSION

logger = logging.getLogger(__name__)
TOOL_NAME = "get_affected_river_network_by_rainfall"
DEFAULT_DIRECT_GRAPH_MATCH_KM = 10.0

# 影响河网计算规则说明，统一在空结果与有结果响应中复用
IMPACT_RULES = {
    "direct": f"ST_Dump(full_{RIVER_TABLE_VERSION}.geom) 后单线段 ST_DWithin 30km 命中，直接段不截断",
    "downstream": f"从暴雨站 30km 缓冲区内的 pkl 拓扑边向下游追踪 downstream_km，再回 full_{RIVER_TABLE_VERSION} 匹配真实河段并截断；其中同时命中真实直接河段的边标记为 is_direct_graph_edge",
    "direction": f"GeoJSON 坐标顺序使用 full_{RIVER_TABLE_VERSION} 数据库原始几何顺序；properties.flow_direction=database_geometry_order",
    "dedupe": "直接河段优先，按 objectid + 真实几何去重，避免多条 pkl 边重复映射同一真实河段",
    "name_fallback": f"当 full_{RIVER_TABLE_VERSION} 表 src_name 缺失（显示为“未知”）时，使用 pkl 图同名 objectid 的 src_name/river_name/name 回填河系名",
    "match_filter": "下游河段仅保留与 pkl 拓扑边匹配距离 ≤ 站点缓冲区（默认 30km）的真实河段，剔除因对齐偏差产生的远距离误匹配",
    "downstream_dedupe": "若下游河段几何被同 objectid 直接河段覆盖，则合并剔除，避免 30km 缓冲区内外重复渲染",
}


def _load_impact_builder() -> Callable:
    """加载外部牵引智能体的暴雨影响河流专题图 builder。"""
    repo_root = Path(__file__).resolve().parents[2]
    core_utils_dir = repo_root / "hhlyqyxt-master" / "utils"
    if not core_utils_dir.exists():
        raise RuntimeError(f"未找到统一暴雨影响河流工具目录：{core_utils_dir}")
    if str(core_utils_dir) not in sys.path:
        sys.path.insert(0, str(core_utils_dir))
    from rainfall_impact_geojson import build_rainstorm_impact_thematic_map

    return build_rainstorm_impact_thematic_map


def _resolve_graph_path(graph_path: str | None) -> str | None:
    """优先使用有向图 v6 版本，否则回退到原始 graph 路径。"""
    if not graph_path:
        return None
    path = Path(graph_path)
    if path.is_dir() or not path.name:
        v6_path = path / DIRECTED_GRAPH_FILENAME
    else:
        v6_path = path.with_name(DIRECTED_GRAPH_FILENAME)
    return str(v6_path) if v6_path.is_file() else graph_path


def _station_reaches_threshold(station: Any, threshold_mm: float) -> bool:
    if not isinstance(station, dict):
        return False
    try:
        return float(station.get("rainfall") or 0.0) >= float(threshold_mm)
    except (TypeError, ValueError):
        return False


def _normalize_station(station: dict, level: str) -> dict:
    name = station.get("name") or station.get("station_name")
    return {
        "station_id": station.get("station_id"),
        "station_name": name,
        "name": name,
        "lon": station.get("lon"),
        "lat": station.get("lat"),
        "rainfall": station.get("rainfall"),
        "rain_24h": station.get("rainfall"),
        "level": level,
    }


def _collect_non_empty_strings(items: list[Any] | None) -> set[str]:
    return {str(x).strip() for x in items or [] if x}


def _extract_rainstorm_stations(
    rainfall_result: dict,
    threshold_mm: float,
    rain_levels: list[tuple[str, float, float]],
) -> tuple[list[dict], set[str], set[str]]:
    """从降雨分析结果中提取达到阈值的站点及其所属区域。"""
    level_to_threshold = {name: low for name, low, _high in rain_levels}
    stations: list[dict] = []
    zone_77_regions: set[str] = set()
    admin_divisions: set[str] = set()

    for level_item in rainfall_result.get("level_analysis") or []:
        level = level_item.get("level", "")
        if level_to_threshold.get(level, math.inf) < threshold_mm:
            continue
        zone_77_regions.update(_collect_non_empty_strings(level_item.get("zone_77_regions")))
        admin_divisions.update(_collect_non_empty_strings(level_item.get("admin_divisions")))
        stations.extend(
            _normalize_station(station, level)
            for station in level_item.get("stations", []) or []
            if _station_reaches_threshold(station, threshold_mm)
        )

    return stations, zone_77_regions, admin_divisions


def _direction_info(segments: list[dict], river_geojson: dict | None) -> dict:
    features = river_geojson.get("features", []) if isinstance(river_geojson, dict) else []
    directed_features = [
        f
        for f in features
        if isinstance(f, dict)
        and (f.get("properties") or {}).get("flow_direction") == "database_geometry_order"
    ]
    return {
        "enabled": bool(features or segments),
        "field": "flow_direction",
        "value": "database_geometry_order",
        "direction_source": f"full_{RIVER_TABLE_VERSION}_original_geometry",
        "geojson_feature_count": len(features),
        "directed_geojson_feature_count": len(directed_features),
        "coordinate_order": f"使用 full_{RIVER_TABLE_VERSION} 数据库原始几何点序，不在问答工具里反转坐标。",
        "how_to_use": "前端或入库需要方向时，直接按 geometry.coordinates 的点序使用；properties.flow_direction 表示该点序来自数据库原始几何。",
    }


def _base_response_fields(
    rainfall_result: dict,
    threshold_mm: float,
    zones: set[str],
    admins: set[str],
    stations: list[dict],
    segments: list[dict],
    river_geojson: dict | None,
    start_stats: dict,
    summary: str,
    affected_rivers: list[str] | None = None,
    rules: dict | None = None,
) -> dict:
    """构建问答返回结构中的公共字段。"""
    response = {
        "time_range_readable": rainfall_result.get("time_range_readable", ""),
        "rainfall_threshold_mm": threshold_mm,
        "affected_zone_77_regions": sorted(zones),
        "affected_admin_divisions": sorted(admins),
        "stations": stations,
        "total_segments": len(segments),
        "affected_segments": len(segments),
        "segments": segments,
        "direction": _direction_info(segments, river_geojson),
        "start_stats": start_stats,
        "summary": summary,
        "affected_rivers": affected_rivers or [],
        "river_geojson": river_geojson,
    }
    if rules is not None:
        response["rules"] = rules
    return response


def _start_stats(
    downstream_edge_count: int,
    downstream_start_stats: dict,
    direct_part_match_km: float | None = None,
) -> dict:
    if direct_part_match_km is None:
        direct_part_match_km = downstream_start_stats.get("direct_match_km", DEFAULT_DIRECT_GRAPH_MATCH_KM)
    return {
        "downstream_edge_count": downstream_edge_count,
        "direct_part_match_km": direct_part_match_km,
        "downstream_start_stats": downstream_start_stats,
    }


def _empty_response(
    rainfall_result: dict,
    threshold_mm: float,
    zones: set[str],
    admins: set[str],
    direct_graph_match_km: float = DEFAULT_DIRECT_GRAPH_MATCH_KM,
) -> dict:
    return _base_response_fields(
        rainfall_result,
        threshold_mm,
        zones,
        admins,
        stations=[],
        segments=[],
        river_geojson=None,
        start_stats=_start_stats(
            downstream_edge_count=0,
            downstream_start_stats={
                "direct_match_km": direct_graph_match_km,
                "station_buffer_km": 30.0,
                "station_buffer_fallback_used": False,
                "station_buffer_fallback_edge_count": 0,
                "direct_part_matched_edge_count": 0,
            },
        ),
        summary=(
            f"统计时段 {rainfall_result.get('time_range_readable', '')} 内，"
            f"未达到 {threshold_mm}mm 降雨阈值的河系数据。"
        ),
        rules=IMPACT_RULES,
    )


def _format_mcp_response(
    result: dict, rainfall_result: dict, threshold_mm: float, zones: set[str], admins: set[str]
) -> dict:
    segments = result.get("segments", [])
    river_geojson = result.get("river_geojson")
    downstream_start_stats = result.get("downstream_start_stats", {})
    affected_rivers = result.get("affected_rivers")
    if affected_rivers is None:
        affected_rivers = sorted(
            {str(s.get("rivername") or "").strip() for s in segments if s.get("rivername")}
        )
    return _base_response_fields(
        rainfall_result,
        threshold_mm,
        zones,
        admins,
        stations=result.get("impact_stations", []),
        segments=segments,
        river_geojson=river_geojson,
        start_stats=_start_stats(
            downstream_edge_count=(result.get("river_summary") or {}).get("downstream_edge_count", 0),
            downstream_start_stats=downstream_start_stats,
        ),
        summary=(
            f"统计时段 {rainfall_result.get('time_range_readable', '')} 内，"
            f"降雨量≥{threshold_mm}mm 的实况站点共影响 {len(affected_rivers)} 条河流。"
        ),
        affected_rivers=affected_rivers,
        rules=IMPACT_RULES,
    )


def build_affected_river_network_result(
    time_str: str,
    start_time: str,
    end_time: str,
    rainfall_threshold_mm: float,
    max_edges: int,
    include_background: bool,
    downstream_km: float,
    direct_graph_match_km: float,
    pg_conf: dict,
    analyze_rainfall_core: Callable,
    rain_levels: list[tuple[str, float, float]],
    graph_path: str | None,
) -> dict:
    """不依赖 MCP 的核心实现，可被 chainlitexam 本地工具直接调用。"""
    custom_timerange = f"[{start_time},{end_time}]" if start_time and end_time else ""
    rainfall_result = analyze_rainfall_core(time_str, pg_conf, custom_timerange)
    stations, zone_77_regions, admin_divisions = _extract_rainstorm_stations(
        rainfall_result, rainfall_threshold_mm, rain_levels
    )
    if not stations:
        return _empty_response(
            rainfall_result, rainfall_threshold_mm, zone_77_regions, admin_divisions, direct_graph_match_km
        )

    builder = _load_impact_builder()
    result = builder(
        stations,
        pg_conf=pg_conf,
        graph_path=_resolve_graph_path(graph_path),
        rainfall_threshold_mm=rainfall_threshold_mm,
        downstream_km=downstream_km,
        direct_match_km=direct_graph_match_km,
        max_segments=max_edges,
        extra_summary={"time_range_readable": rainfall_result.get("time_range_readable", "")},
    )
    return _format_mcp_response(result, rainfall_result, rainfall_threshold_mm, zone_77_regions, admin_divisions)


def _unregister_existing_tool(mcp, name: str) -> None:
    """尝试从 MCP 工具管理器中移除同名旧工具，避免重复注册。"""
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


def register_fixed_rainfall_impact_tool(mcp) -> None:
    """注册暴雨影响河流专题图工具（MCP 入口）。"""
    _unregister_existing_tool(mcp, TOOL_NAME)
    import tools as base_tools

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
        return build_affected_river_network_result(
            time_str=time_str,
            start_time=start_time,
            end_time=end_time,
            rainfall_threshold_mm=rainfall_threshold_mm,
            max_edges=max_edges,
            include_background=include_background,
            downstream_km=downstream_km,
            direct_graph_match_km=direct_graph_match_km,
            pg_conf=base_tools.config["postgres"],
            analyze_rainfall_core=base_tools._analyze_rainfall_core,
            rain_levels=base_tools.RAIN_LEVELS,
            graph_path=_resolve_graph_path(base_tools.config.get("paths", "graph", fallback="")),
        )

    logger.info("已注册 %s 工具", TOOL_NAME)