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
DEFAULT_FLOW_VELOCITY_MPS = 2.0

# 影响河网计算规则说明，统一在空结果与有结果响应中复用
IMPACT_RULES = {
    "direct": f"full_{RIVER_TABLE_VERSION} 中位于暴雨站点 station_buffer_km（默认 30km）缓冲区内的候选行全部作为 direct_buffer 输出；其中距站点 ≤ direct_match_km（默认 10km）的标记 is_direct_graph_edge=true。距离分类用 SQL 真实几何最近距离，非 pkl 端点弦距。",
    "downstream": f"从缓冲区内 pkl 边的下游节点起 Dijkstra 追踪 downstream_km；下游边不在 direct_buffer 中的才记录。下游几何通过 objectid 二次查询 full_{RIVER_TABLE_VERSION} 补全，精确端点键失配时按同 objectid 几何空间邻近兜底匹配。",
    "direction": f"GeoJSON 坐标顺序使用 full_{RIVER_TABLE_VERSION} 数据库原始几何顺序；properties.flow_direction=database_geometry_order。下游裁剪按 pkl from 节点判定方向后从上游端保留 keep_km，纯 Python 无 Shapely 依赖。",
    "dedupe": "每条 pkl 边至多一个 feature：direct_buffer 中的边在下游追踪时跳过记录（遍历继续穿过），消除跨组重复。",
    "name_fallback": f"名称优先级：full_{RIVER_TABLE_VERSION}.src_name → river_name → pkl 名称 → 滦河 objectid 映射（仅单字缩写或全部失败时启用，不覆盖合法全名）→ 未知。",
    "match_filter": "已移除 match_distance_km 过滤；改为三级匹配：精确端点键（objectid+端点 6 位小数取整）→ 反向端点键 → 同 objectid 几何空间邻近（两端点 100m 内）。",
    "downstream_dedupe": "已移除 Shapely 几何覆盖去重；重复由结构保证（direct_keys 跳过 + pkl 边天然唯一）。",
    "propagation": "传播时间按统一经验流速 flow_velocity_mps（默认 2.0 m/s ≈ 7.2 km/h）估算：河流级传播距离 ÷ 流速；下游河流取 Dijkstra 累计 end_distance_km 最大值，仅直接受影响的河流取站点缓冲区内最长直接河段长度。河名口径与 affected_rivers 一致。",
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


def _resolve_flow_velocity(flow_velocity_mps: float) -> float:
    """0/None = 使用默认经验流速；负数或非有限数值报错；正数原样返回。"""
    value = float(flow_velocity_mps or 0.0)
    if not math.isfinite(value) or value < 0:
        raise ValueError("flow_velocity_mps 必须为非负有限数值")
    return value if value > 0 else DEFAULT_FLOW_VELOCITY_MPS


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
    river_propagation: dict | None = None,
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
        "river_propagation": river_propagation
        or {"flow_velocity_mps": DEFAULT_FLOW_VELOCITY_MPS, "rivers": []},
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
    flow_velocity_mps: float = DEFAULT_FLOW_VELOCITY_MPS,
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
        river_propagation={"flow_velocity_mps": float(flow_velocity_mps), "rivers": []},
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
        river_propagation=result.get("river_propagation"),
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
    flow_velocity_mps: float = 0.0,
) -> dict:
    """不依赖 MCP 的核心实现，可被 chainlitexam 本地工具直接调用。"""
    velocity = _resolve_flow_velocity(flow_velocity_mps)
    custom_timerange = f"[{start_time},{end_time}]" if start_time and end_time else ""
    rainfall_result = analyze_rainfall_core(time_str, pg_conf, custom_timerange)
    stations, zone_77_regions, admin_divisions = _extract_rainstorm_stations(
        rainfall_result, rainfall_threshold_mm, rain_levels
    )
    if not stations:
        return _empty_response(
            rainfall_result, rainfall_threshold_mm, zone_77_regions, admin_divisions, direct_graph_match_km,
            flow_velocity_mps=velocity,
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
        flow_velocity_mps=velocity,
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
        flow_velocity_mps: float = 0.0,
    ) -> dict:
        """制作暴雨影响河流专题图数据。

        - flow_velocity_mps: 经验流速 m/s，0 表示默认 2.0。
        """
        return build_affected_river_network_result(
            time_str=time_str,
            start_time=start_time,
            end_time=end_time,
            rainfall_threshold_mm=rainfall_threshold_mm,
            max_edges=max_edges,
            include_background=include_background,
            downstream_km=downstream_km,
            direct_graph_match_km=direct_graph_match_km,
            flow_velocity_mps=flow_velocity_mps,
            pg_conf=base_tools.config["postgres"],
            analyze_rainfall_core=base_tools._analyze_rainfall_core,
            rain_levels=base_tools.RAIN_LEVELS,
            graph_path=_resolve_graph_path(base_tools.config.get("paths", "graph", fallback="")),
        )

    logger.info("已注册 %s 工具", TOOL_NAME)