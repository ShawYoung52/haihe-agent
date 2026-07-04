"""暴雨影响专题图数据封装。

给业务系统或同事代码直接调用的轻量入口，内部复用
utils.rainfall_impact_geojson.build_rain24h_impact_river_geojson。

典型调用：

    from utils.rainstorm_impact_map_service import create_rainstorm_impact_thematic_map

    result = create_rainstorm_impact_thematic_map(
        csv_path="/data/24hourmindata.csv",
        output_dir="/tmp/rainstorm_impact",
    )

返回 result["map_layers"] 可直接给前端/GIS 渲染；如果传 output_dir，会同时落盘
river_impact.geojson、impact_stations.geojson、summary.json。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from utils.rainfall_impact_geojson import build_rain24h_impact_river_geojson
except Exception:  # pragma: no cover - 兼容包内相对导入
    from .rainfall_impact_geojson import build_rain24h_impact_river_geojson


DEFAULT_RIVER_STYLE = {
    "direct_buffer": {
        "name": "直接影响河段",
        "color": "#E53935",
        "width": 4,
        "description": "暴雨站点30km范围内命中的真实河段，不截断。",
    },
    "downstream_50km": {
        "name": "下游影响河段",
        "color": "#FB8C00",
        "width": 3,
        "description": "按河网拓扑向下游追踪50km，并按范围截断。",
    },
}

DEFAULT_STATION_STYLE = {
    "name": "暴雨触发站",
    "color": "#1E88E5",
    "radius": 6,
    "description": "24小时累计降水达到暴雨阈值的自动站。",
}


def _write_json(path: Path, data: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _river_names_from_geojson(river_geojson: dict) -> list[str]:
    names: set[str] = set()
    for feature in river_geojson.get("features") or []:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
        name = str(props.get("river_name") or props.get("rivername") or "").strip()
        if name:
            names.add(name)
    return sorted(names)


def _build_summary(core_result: dict) -> dict:
    station_summary = core_result.get("station_summary") if isinstance(core_result.get("station_summary"), dict) else {}
    river_geojson = core_result.get("river_geojson") if isinstance(core_result.get("river_geojson"), dict) else {}
    station_geojson = core_result.get("station_geojson") if isinstance(core_result.get("station_geojson"), dict) else {}
    direct_rivers = core_result.get("direct_rivers") or []
    downstream_rivers = core_result.get("downstream_rivers") or []
    affected_rivers = _river_names_from_geojson(river_geojson)

    return {
        "status": core_result.get("status", "ok"),
        "message": core_result.get("message", ""),
        "time_range": core_result.get("time_range") or {},
        "rain_threshold_mm": (core_result.get("params") or {}).get("rain_threshold_mm"),
        "station_buffer_km": (core_result.get("params") or {}).get("station_buffer_km"),
        "downstream_km": (core_result.get("params") or {}).get("downstream_km"),
        "total_station_count": station_summary.get("total_station_count", 0),
        "impact_station_count": station_summary.get("impact_station_count", 0),
        "max_rain_24h": station_summary.get("max_rain_24h", 0),
        "affected_river_count": len(affected_rivers),
        "direct_river_count": len(direct_rivers),
        "downstream_river_count": len(downstream_rivers),
        "river_feature_count": len(river_geojson.get("features") or []),
        "station_feature_count": len(station_geojson.get("features") or []),
        "affected_rivers": affected_rivers,
        "direct_rivers": sorted(str(x) for x in direct_rivers),
        "downstream_rivers": sorted(str(x) for x in downstream_rivers),
    }


def build_rainstorm_impact_thematic_map_data(
    csv_path: str | Path,
    *,
    rain_threshold_mm: float = 50.0,
    station_buffer_km: float = 30.0,
    downstream_km: float = 50.0,
    river_table: str = "haihe_river_directed_full_v5",
    schema: str = "public",
    graph_path: str | Path | None = None,
    top_station_limit: int = 100,
    direct_match_km: float = 3.0,
    output_dir: str | Path | None = None,
) -> dict:
    """构建暴雨影响专题图数据。

    Args:
        csv_path: 5分钟站点降水CSV，需包含 Station_Id_C、Datetime、PRE、Lon、Lat。
        rain_threshold_mm: 暴雨触发阈值，默认50mm。
        station_buffer_km: 直接影响河段缓冲半径，默认30km。
        downstream_km: 下游追踪距离，默认50km。
        river_table: PostGIS 河流表，默认 haihe_river_directed_full_v5。
        schema: PostGIS schema，默认 public。
        graph_path: river_directed_v5.pkl 路径；不传则使用牵引智能体默认查找逻辑。
        top_station_limit: 返回雨量排名站点数量。
        direct_match_km: 直接河段与pkl边匹配阈值，默认3km，对齐牵引智能体。
        output_dir: 可选，传入后将专题图GeoJSON和摘要落盘。

    Returns:
        dict，核心字段：
        - summary: 业务摘要
        - map_layers.rivers: 影响河段 GeoJSON
        - map_layers.stations: 暴雨触发站 GeoJSON
        - raw: 原核心函数完整返回
        - output_files: output_dir 不为空时包含落盘文件路径
    """
    core_result = build_rain24h_impact_river_geojson(
        csv_path=str(csv_path),
        rain_threshold_mm=rain_threshold_mm,
        station_buffer_km=station_buffer_km,
        downstream_km=downstream_km,
        river_table=river_table,
        schema=schema,
        graph_path=graph_path,
        top_station_limit=top_station_limit,
        direct_match_km=direct_match_km,
    )

    river_geojson = core_result.get("river_geojson") or {"type": "FeatureCollection", "features": []}
    station_geojson = core_result.get("station_geojson") or {"type": "FeatureCollection", "features": []}
    summary = _build_summary(core_result)

    result = {
        "status": core_result.get("status", "ok"),
        "summary": summary,
        "map_layers": {
            "rivers": river_geojson,
            "stations": station_geojson,
            "styles": {
                "rivers": DEFAULT_RIVER_STYLE,
                "stations": DEFAULT_STATION_STYLE,
            },
        },
        "impact_stations": core_result.get("impact_stations") or [],
        "rainfall_24h_top_stations": core_result.get("rainfall_24h_top_stations") or [],
        "output_files": {},
        "raw": core_result,
    }

    if output_dir:
        out = Path(output_dir)
        result["output_files"] = {
            "river_impact_geojson": _write_json(out / "river_impact.geojson", river_geojson),
            "impact_stations_geojson": _write_json(out / "impact_stations.geojson", station_geojson),
            "summary_json": _write_json(out / "summary.json", summary),
        }

    return result


def create_rainstorm_impact_thematic_map(
    csv_path: str | Path,
    output_dir: str | Path | None = None,
    **kwargs: Any,
) -> dict:
    """同事调用友好入口：制作暴雨影响专题图数据。

    这是 build_rainstorm_impact_thematic_map_data 的简短别名，便于业务代码直接调用。
    """
    return build_rainstorm_impact_thematic_map_data(
        csv_path=csv_path,
        output_dir=output_dir,
        **kwargs,
    )


__all__ = [
    "build_rainstorm_impact_thematic_map_data",
    "create_rainstorm_impact_thematic_map",
]
