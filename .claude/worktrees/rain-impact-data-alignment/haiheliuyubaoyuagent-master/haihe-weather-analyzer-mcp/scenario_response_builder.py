from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class TableColumn:
    key: str
    title: str

    def to_dict(self) -> Dict[str, str]:
        return {"key": self.key, "title": self.title}


@dataclass
class ScenarioTable:
    table_key: str
    table_name: str
    columns: List[TableColumn]
    rows: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "table_key": self.table_key,
            "table_name": self.table_name,
            "columns": [c.to_dict() for c in self.columns],
            "rows": self.rows,
        }


def build_feature(
    feature_id: str,
    geometry: Dict[str, Any],
    style_type: str,
    props: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    base_props = {"feature_id": feature_id, "style_type": style_type}
    if props:
        base_props.update(props)
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": base_props,
    }


def feature_collection(features: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": features,
    }


def build_response_payload(
    scenario: str,
    geojson_obj: Dict[str, Any],
    tables: List[ScenarioTable],
    *,
    query_time: Optional[str] = None,
    crs: str = "EPSG:4326",
    map_sql_id: Optional[int] = None,
    map_sql_ids: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    meta: Dict[str, Any] = {
        "crs": crs,
        "query_time": query_time,
    }
    if map_sql_id is not None:
        meta["map_sql_id"] = int(map_sql_id)
        meta["map_render"] = "wms_sql"
    if map_sql_ids:
        meta["map_sql_ids"] = {str(k): int(v) for k, v in map_sql_ids.items()}
        meta["map_render"] = "wms_sql"
    if map_sql_id is not None or map_sql_ids:
        # 与 WMS/PostGIS 占位符一致，供地图视口与坐标系注入
        meta["wms_sql_params"] = ["srid", "minx", "miny", "maxx", "maxy"]
    return {
        "success": True,
        "message": "ok",
        "data": {
            "scenario": scenario,
            "map_geojson": json.dumps(geojson_obj, ensure_ascii=False),
            "tables": [t.to_dict() for t in tables],
            "meta": meta,
        },
    }


def build_downstream_scene_demo() -> Dict[str, Any]:
    """示例：某条河流的下流河系（蓝=当前河，绿=下游河）"""
    features = [
        build_feature(
            feature_id="river_main_001",
            style_type="current_river",
            geometry={
                "type": "LineString",
                "coordinates": [[117.0, 39.1], [117.1, 39.0], [117.2, 38.9]],
            },
            props={"river_name": "示例当前河", "river_length_km": 123.4},
        ),
        build_feature(
            feature_id="river_down_101",
            style_type="downstream_river",
            geometry={
                "type": "LineString",
                "coordinates": [[117.2, 38.9], [117.3, 38.8]],
            },
            props={"river_name": "示例下游河A", "river_length_km": 56.7, "distance_km": 12.3},
        ),
    ]

    table = ScenarioTable(
        table_key="downstream_rivers",
        table_name="下流河系列表",
        columns=[
            TableColumn("river_name", "河名称"),
            TableColumn("river_length_km", "河长(km)"),
            TableColumn("distance_km", "距离(km)"),
            TableColumn("feature_id", "定位ID"),
        ],
        rows=[
            {
                "river_name": "示例下游河A",
                "river_length_km": 56.7,
                "distance_km": 12.3,
                "feature_id": "river_down_101",
            }
        ],
    )
    return build_response_payload(
        scenario="river_downstream",
        geojson_obj=feature_collection(features),
        tables=[table],
    )

