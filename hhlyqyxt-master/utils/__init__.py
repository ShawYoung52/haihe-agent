"""hhlyqyxt 对外工具入口。"""
from __future__ import annotations

from .rainfall_impact_graph_start_rules import install_strict_direct_part_start_rule

install_strict_direct_part_start_rule()

from .rainstorm_impact_map_service import (
    create_rainstorm_impact_geojson_file,
    create_rainstorm_impact_map,
    get_rainstorm_impact_json_urls_from_url,
    get_rainstorm_impact_map_style,
)

__all__ = [
    "create_rainstorm_impact_geojson_file",
    "create_rainstorm_impact_map",
    "get_rainstorm_impact_json_urls_from_url",
    "get_rainstorm_impact_map_style",
]
