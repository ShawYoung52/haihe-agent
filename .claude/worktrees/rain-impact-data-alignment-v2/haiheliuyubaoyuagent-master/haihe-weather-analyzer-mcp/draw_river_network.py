#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
海河流域河网图绘制脚本

功能：
1. 从河网图数据中获取河流线段
2. 叠加八级河系矢量数据（water_line_haihe.shp）
3. 显示河流影响时间信息
4. 输出PNG格式的河网图
"""

from __future__ import annotations

import argparse
import configparser
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

import numpy as np

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap
except Exception as exc:
    raise RuntimeError("缺少 matplotlib 依赖，无法绘图。") from exc

try:
    from matplotlib import font_manager
except Exception:
    font_manager = None

try:
    # GDAL用于读取shapefile
    import importlib
    gdal = importlib.import_module("osgeo.gdal")
    ogr = importlib.import_module("osgeo.ogr")
    osr = importlib.import_module("osgeo.osr")
except Exception as exc:
    raise RuntimeError("缺少 GDAL（osgeo）依赖，无法读取矢量数据。") from exc

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:
    psycopg2 = None
    RealDictCursor = None

# 导入项目中的工具函数
sys.path.insert(0, os.path.dirname(__file__))
from tools import get_graph, iter_graph_edges, RIVER_FLOW_SPEEDS_KMH, _impact_time_descriptions, _get_end_nodes_by_river_map


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="绘制海河流域河网图（含八级河系和河流影响时间）")
    parser.add_argument(
        "--output", 
        default="haihe_river_network.png", 
        help="输出 PNG 文件路径"
    )
    parser.add_argument(
        "--config", 
        default="config.ini", 
        help="配置文件路径（含数据库配置）"
    )
    parser.add_argument(
        "--no-admin-overlay",
        action="store_true",
        help="不叠加行政区划底图"
    )
    parser.add_argument(
        "--admin-level",
        default="city_adcode",
        choices=["all", "city", "district", "city_district", "hybrid", "city_adcode"],
        help="行政区划层级：city_adcode=地级市；city=市级；district=区县级"
    )
    parser.add_argument(
        "--admin-max-features",
        type=int,
        default=8000,
        help="最多加载的行政区划数量"
    )
    parser.add_argument(
        "--admin-simplify-deg",
        type=float,
        default=0.005,
        help="行政区划几何简化容差（度），越大越简略"
    )
    parser.add_argument(
        "--font-path", 
        default="", 
        help="中文字体文件路径（ttf/ttc）"
    )
    parser.add_argument(
        "--eight-level-river-shp", 
        default=r"D:\tj\水系\water_line_haihe\water_line_haihe.shp",
        help="八级河系矢量文件路径（shapefile）"
    )
    parser.add_argument(
        "--start-river",
        default="",
        help="起始河流名称（可选），如果指定则只显示该河流及其下游"
    )
    parser.add_argument(
        "--max-edges",
        type=int,
        default=5000,
        help="最大显示的河段数量"
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=300,
        help="输出分辨率 DPI"
    )
    parser.add_argument(
        "--fig-width",
        type=float,
        default=10.0,
        help="图形宽度（英寸）"
    )
    parser.add_argument(
        "--fig-height",
        type=float,
        default=16.0,
        help="图形高度（英寸）"
    )
    parser.add_argument(
        "--show-impact-time",
        action="store_true",
        default=True,
        help="是否显示河流影响时间信息"
    )
    parser.add_argument(
        "--impact-source-river",
        default="",
        help="计算影响时间的源河流名称"
    )
    parser.add_argument(
        "--theme",
        default="both",
        choices=["light", "dark", "both"],
        help="主题风格：light=浅色，dark=深色，both=同时导出深浅两套"
    )
    parser.add_argument(
        "--transparent-bg",
        action="store_true",
        help="导出透明背景PNG（叠加到PPT/前端底图时建议开启）"
    )
    parser.add_argument(
        "--precip-raster",
        default="",
        help="降水栅格路径（GeoTIFF）。传入后会叠加到河网图上"
    )
    parser.add_argument(
        "--precip-alpha",
        type=float,
        default=0.55,
        help="降水栅格透明度（0~1）"
    )
    parser.add_argument(
        "--precip-min-mm",
        type=float,
        default=0.1,
        help="降水显示阈值（mm），低于该值不着色"
    )
    return parser.parse_args()


def _set_font(font_path: str = "") -> bool:
    """设置字体，优先使用指定的中文字体"""
    if font_manager is not None and font_path and os.path.exists(font_path):
        try:
            font_manager.fontManager.addfont(font_path)
            name = font_manager.FontProperties(fname=font_path).get_name()
            matplotlib.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            matplotlib.rcParams["axes.unicode_minus"] = False
            print(f"[font] 使用中文字体: {name}")
            return True
        except Exception as e:
            print(f"[font] 加载字体失败: {e}，使用默认字体")
    
    # 尝试系统中常见的中文字体
    common_chinese_fonts = [
        "SimHei", "Microsoft YaHei", "WenQuanYi Micro Hei", 
        "Droid Sans Fallback", "DejaVu Sans"
    ]
    
    for font_name in common_chinese_fonts:
        try:
            matplotlib.rcParams["font.sans-serif"] = [font_name, "DejaVu Sans"]
            matplotlib.rcParams["axes.unicode_minus"] = False
            print(f"[font] 使用系统字体: {font_name}")
            return True
        except Exception:
            continue
    
    # 最后回退到默认
    matplotlib.rcParams["font.sans-serif"] = ["DejaVu Sans", "Arial", "Helvetica", "sans-serif"]
    matplotlib.rcParams["axes.unicode_minus"] = False
    print("[font] 使用默认英文字体（中文可能无法正常显示）")
    return False


def load_river_segments(start_river: str = "", max_edges: int = 5000) -> List[Dict[str, Any]]:
    """
    从河网图中加载河流线段数据
    
    Args:
        start_river: 起始河流名称，如果指定则只返回该河流及下游
        max_edges: 最大边数限制
        
    Returns:
        河流线段列表，每个元素包含 from_x, from_y, to_x, to_y, rivername, length_km 等信息
    """
    print(f"[river] 加载河网数据 (start_river={start_river or '全流域'}, max_edges={max_edges})")
    
    try:
        G = get_graph()
        segments = []
        
        # 收集需要绘制的边
        edges_to_draw = []
        
        if start_river:
            # 找到起始河流的所有边及其下游节点
            from collections import deque
            
            target_edge_ids = set()
            river_end_nodes = set()
            
            for u, v, key, attr in iter_graph_edges(G):
                if attr.get("rivername") == start_river:
                    river_end_nodes.add(v)
                    edge_id = (u, v, key) if key is not None else (u, v)
                    target_edge_ids.add(edge_id)
            
            if not river_end_nodes:
                print(f"[river] 警告: 未找到河流 '{start_river}'")
                return []
            
            # BFS遍历所有下游边
            queue = deque(river_end_nodes)
            visited_nodes = set(river_end_nodes)
            
            while queue:
                curr_node = queue.popleft()
                
                if G.is_multigraph():
                    for _u, next_node, key, attr in G.out_edges(curr_node, keys=True, data=True):
                        edge_id = (_u, next_node, key)
                        target_edge_ids.add(edge_id)
                        
                        if next_node not in visited_nodes:
                            visited_nodes.add(next_node)
                            queue.append(next_node)
                else:
                    for _u, next_node, attr in G.out_edges(curr_node, data=True):
                        edge_id = (_u, next_node)
                        target_edge_ids.add(edge_id)
                        
                        if next_node not in visited_nodes:
                            visited_nodes.add(next_node)
                            queue.append(next_node)
            
            # 筛选出目标边
            for u, v, key, attr in iter_graph_edges(G):
                edge_id = (u, v, key) if key is not None else (u, v)
                if edge_id in target_edge_ids:
                    edges_to_draw.append((u, v, key, attr))
        else:
            # 全图模式
            edges_to_draw = list(iter_graph_edges(G))
        
        # 转换为线段格式
        skipped = 0
        for i, (u, v, key, attr) in enumerate(edges_to_draw):
            if i >= max_edges:
                break
            
            try:
                # 解析坐标
                if isinstance(u, str) and "," in u:
                    from_x, from_y = map(float, u.split(","))
                else:
                    continue
                    
                if isinstance(v, str) and "," in v:
                    to_x, to_y = map(float, v.split(","))
                else:
                    continue
                
                length_km = float(attr.get("length_km", 0.0))
                rivername = attr.get("rivername", "未知")
                strahler_order = attr.get("strahler_order", 1)
                
                segments.append({
                    "from_x": from_x,
                    "from_y": from_y,
                    "to_x": to_x,
                    "to_y": to_y,
                    "rivername": rivername,
                    "length_km": length_km,
                    "strahler_order": strahler_order,
                })
                
            except Exception as e:
                skipped += 1
                continue
        
        if skipped > 0:
            print(f"[river] 跳过 {skipped} 条无效线段")
        
        print(f"[river] 成功加载 {len(segments)} 条河段")
        return segments
        
    except Exception as e:
        print(f"[river] 加载河网数据失败: {e}")
        import traceback
        traceback.print_exc()
        return []


def load_eight_level_rivers(shp_path: str) -> List[Dict[str, Any]]:
    """
    加载八级河系矢量数据
    
    Args:
        shp_path: shapefile路径
        
    Returns:
        河流要素列表
    """
    print(f"[shp] 加载八级河系: {shp_path}")
    
    if not os.path.exists(shp_path):
        print(f"[shp] 警告: 文件不存在 {shp_path}")
        return []
    
    try:
        ds = ogr.Open(shp_path)
        if ds is None:
            print(f"[shp] 无法打开shapefile: {shp_path}")
            return []
        
        layer = ds.GetLayer(0)
        if layer is None:
            print(f"[shp] 图层为空")
            return []
        
        rivers = []
        feature_count = layer.GetFeatureCount()
        print(f"[shp] 共有 {feature_count} 个要素")
        
        # 检查字段
        layer_defn = layer.GetLayerDefn()
        field_names = [layer_defn.GetFieldDefn(i).GetName() for i in range(layer_defn.GetFieldCount())]
        print(f"[shp] 字段: {field_names}")
        
        for feat in layer:
            geom = feat.GetGeometryRef()
            if geom is None:
                continue
            
            # 提取属性
            attrs = {}
            for i in range(layer_defn.GetFieldCount()):
                field_name = layer_defn.GetFieldDefn(i).GetName()
                attrs[field_name] = feat.GetField(i)
            
            # 提取几何坐标
            coords = []
            geom_type = geom.GetGeometryType()
            
            if geom_type == ogr.wkbLineString or geom_type == ogr.wkbLineString25D:
                points = geom.GetPoints()
                if points:
                    coords = [(p[0], p[1]) for p in points]
            elif geom_type == ogr.wkbMultiLineString or geom_type == ogr.wkbMultiLineString25D:
                for i in range(geom.GetGeometryCount()):
                    sub_geom = geom.GetGeometryRef(i)
                    if sub_geom:
                        points = sub_geom.GetPoints()
                        if points:
                            coords.append([(p[0], p[1]) for p in points])
            
            if coords:
                rivers.append({
                    "geometry": coords,
                    "attributes": attrs,
                })
        
        ds = None
        print(f"[shp] 成功加载 {len(rivers)} 条八级河系")
        return rivers
        
    except Exception as e:
        print(f"[shp] 加载失败: {e}")
        import traceback
        traceback.print_exc()
        return []


def _get_downstream_impacts_structured(
    river: str,
    attr_name: str = "length_km",
) -> list:
    """
    返回结构化的下游影响结果，不是自然语言。
    （从tools.py复制，因为该函数在tools.py中不在模块级别）
    """
    import heapq

    G = get_graph()

    river_end_nodes = set(_get_end_nodes_by_river_map().get(river, ()))

    if not river_end_nodes:
        return []

    best_dist = {node: 0.0 for node in river_end_nodes}
    heap = [(0.0, node) for node in river_end_nodes]
    heapq.heapify(heap)

    impact_distances = {}

    while heap:
        current_dist, curr_node = heapq.heappop(heap)

        if current_dist > best_dist.get(curr_node, float("inf")):
            continue

        for _u, next_node, attr in G.out_edges(curr_node, data=True):
            r_name = attr.get("rivername")

            edge_len = attr.get(attr_name, 0.0)
            if not isinstance(edge_len, (int, float)):
                edge_len = 0.0
            edge_len = float(edge_len)
            if edge_len < 0:
                edge_len = 0.0

            if r_name and r_name != river:
                old = impact_distances.get(r_name, float("inf"))
                if current_dist < old:
                    impact_distances[r_name] = current_dist

            next_dist = current_dist if r_name == river else current_dist + edge_len

            if next_dist < best_dist.get(next_node, float("inf")):
                best_dist[next_node] = next_dist
                heapq.heappush(heap, (next_dist, next_node))

    result = []
    for to_river in sorted(impact_distances.keys()):
        result.append({
            "river_name": to_river,
            "impact_distance_km": round(float(impact_distances[to_river]), 3),
        })
    return result


def calculate_impact_times(source_river: str) -> Dict[str, Any]:
    """
    计算源河流到下游各河流的影响时间（仅针对数据库中的河流）
    
    Args:
        source_river: 源河流名称
        
    Returns:
        影响时间信息字典
    """
    if not source_river:
        return {}
    
    print(f"[impact] 计算数据库中河流 '{source_river}' 的影响时间")
    
    try:
        # 直接使用内部函数获取下游影响
        impacts = _get_downstream_impacts_structured(source_river)
        
        if not impacts:
            print(f"[impact] 警告: 未找到河流 '{source_river}' 的下游影响信息")
            return {
                "source_river": source_river,
                "downstream_count": 0,
                "downstream": [],
            }
        
        # 按距离排序
        impacts_sorted = sorted(impacts, key=lambda x: float(x.get("impact_distance_km", 0.0) or 0.0))
        
        # 构建返回结果
        downstream_results = []
        for item in impacts_sorted[:20]:  # 最多20条
            to_river = item.get("river_name")
            dist_km = float(item.get("impact_distance_km", 0.0) or 0.0)
            time_pack = _impact_time_descriptions(dist_km)
            
            downstream_results.append({
                "downstream_river": to_river,
                "impact_distance_km": round(max(0.0, dist_km), 3),
                "time_estimates": time_pack["scenarios"],
                "descriptions": time_pack["descriptions"],
            })
        
        result = {
            "source_river": source_river,
            "flow_speeds_kmh": dict(RIVER_FLOW_SPEEDS_KMH),
            "downstream_count": len(downstream_results),
            "downstream": downstream_results,
        }
        
        print(f"[impact] 找到 {len(downstream_results)} 条下游河流")
        return result
        
    except Exception as e:
        print(f"[impact] 计算失败: {e}")
        import traceback
        traceback.print_exc()
        return {}


def load_admin_divisions_with_centers(
    config_path: str,
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    admin_level: str = "city_adcode",
    max_features: int = 8000,
    simplify_deg: float = 0.005,
) -> dict:
    """
    从PostGIS数据库加载行政区划边界和中心点
    
    Returns:
        {"polygons": [...], "centers": [(x, y, name), ...]}
    """
    result = {
        "polygons": [],
        "centers": []
    }
    
    if psycopg2 is None or RealDictCursor is None:
        print("[admin] 警告: psycopg2不可用，跳过行政区划")
        print("[admin] 请安装: pip install psycopg2-binary")
        return result
    
    if not os.path.exists(config_path):
        print(f"[admin] 配置文件不存在: {config_path}")
        return result
    
    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding="utf-8")
    
    if "postgres" not in cfg:
        print("[admin] config.ini缺少[postgres]配置段")
        return result
    
    pg = cfg["postgres"]
    schema = pg.get("schema", "public")
    srid = pg.getint("srid", 4326)
    
    level_mode = str(admin_level or "").strip().lower() or "city_adcode"

    # 构建SQL查询 - 增加中心点计算
    geom_to_4326 = (
        "ST_Transform(CASE WHEN ST_SRID(a.geom) = 0 "
        f"THEN ST_SetSRID(a.geom, {srid}) ELSE a.geom END, 4326)"
    )
    
    if level_mode in {"city", "city_adcode"}:
        # 市级模式：将细粒度行政区先按地市聚合为市级边界，避免“只有乡镇数据导致无法叠加”
        sql = f"""
            WITH base AS (
                SELECT
                    a.province_name,
                    a.city_name,
                    a.name,
                    {geom_to_4326} AS g
                FROM {schema}.haihe_admin_division a
                WHERE {geom_to_4326} && ST_MakeEnvelope(%(min_x)s, %(min_y)s, %(max_x)s, %(max_y)s, 4326)
                  AND ST_Intersects({geom_to_4326}, ST_MakeEnvelope(%(min_x)s, %(min_y)s, %(max_x)s, %(max_y)s, 4326))
            ),
            grouped AS (
                SELECT
                    province_name,
                    NULLIF(city_name, '') AS city_name_clean,
                    COALESCE(NULLIF(city_name, ''), name) AS city_label,
                    ST_UnaryUnion(ST_Collect(g)) AS g_city
                FROM base
                GROUP BY province_name, NULLIF(city_name, ''), COALESCE(NULLIF(city_name, ''), name)
            )
            SELECT
                ROW_NUMBER() OVER (ORDER BY city_label) AS id,
                NULL::text AS adcode,
                city_label AS name,
                'city_agg' AS level,
                province_name,
                city_name_clean AS city_name,
                ''::text AS county_name,
                ''::text AS town_name,
                ST_GeometryType(g_city) AS geom_type,
                ST_AsText(
                    CASE WHEN %(simp)s::double precision > 0
                        THEN ST_SimplifyPreserveTopology(g_city, %(simp)s::double precision)
                        ELSE g_city
                    END
                ) AS geom_wkt,
                ST_AsGeoJSON(
                    CASE WHEN %(simp)s::double precision > 0
                        THEN ST_SimplifyPreserveTopology(g_city, %(simp)s::double precision)
                        ELSE g_city
                    END
                ) AS geom_json,
                ST_X(ST_Centroid(g_city)) AS center_x,
                ST_Y(ST_Centroid(g_city)) AS center_y
            FROM grouped
            ORDER BY city_label
            LIMIT %(max_features)s
        """
    else:
        geom_json_sql = f"""
            ST_AsText(
                CASE WHEN %(simp)s::double precision > 0
                    THEN ST_SimplifyPreserveTopology({geom_to_4326}, %(simp)s::double precision)
                    ELSE {geom_to_4326}
                END
            ) AS geom_wkt,
            ST_AsGeoJSON(
                CASE WHEN %(simp)s::double precision > 0
                    THEN ST_SimplifyPreserveTopology({geom_to_4326}, %(simp)s::double precision)
                    ELSE {geom_to_4326}
                END
            ) AS geom_json,
            ST_X(ST_Centroid({geom_to_4326})) AS center_x,
            ST_Y(ST_Centroid({geom_to_4326})) AS center_y
        """
        
        sql = f"""
            SELECT
                a.id,
                a.adcode,
                a.name,
                a.level,
                a.province_name,
                a.city_name,
                a.county_name,
                a.town_name,
                ST_GeometryType({geom_to_4326}) AS geom_type,
                {geom_json_sql}
            FROM {schema}.haihe_admin_division a
            WHERE {geom_to_4326} && ST_MakeEnvelope(%(min_x)s, %(min_y)s, %(max_x)s, %(max_y)s, 4326)
              AND ST_Intersects({geom_to_4326}, ST_MakeEnvelope(%(min_x)s, %(min_y)s, %(max_x)s, %(max_y)s, 4326))
            ORDER BY a.id
            LIMIT %(max_features)s
        """
    
    polygons = []
    centers = []
    conn = None
    
    try:
        conn = psycopg2.connect(
            host=pg.get("host", "127.0.0.1"),
            port=pg.getint("port", 5432),
            dbname=pg.get("dbname"),
            user=pg.get("user"),
            password=pg.get("password"),
            sslmode=pg.get("sslmode", "disable"),
        )
        
        print(f"[admin] 数据库连接成功: {pg.get('host')}:{pg.getint('port', 5432)}/{pg.get('dbname')}")
        
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            params = {
                "min_x": float(min_x),
                "min_y": float(min_y),
                "max_x": float(max_x),
                "max_y": float(max_y),
                "max_features": int(max_features),
                "simp": float(simplify_deg),
            }
            
            cur.execute(sql, params)
            rows = cur.fetchall()
            
            print(f"[admin] 查询返回 {len(rows)} 条行政区划记录")
            if rows and len(rows) >= int(max_features):
                print(f"[admin] 警告: 命中 max_features={max_features} 上限，行政区可能被截断；建议调大 --admin-max-features")

            def _s(v: Any) -> str:
                return str(v or "").strip()

            def _is_town_like(r: Dict[str, Any]) -> bool:
                name = _s(r.get("name"))
                level = _s(r.get("level")).lower()
                town_name = _s(r.get("town_name"))
                if town_name:
                    return True
                if level in {"town", "street", "village"}:
                    return True
                return any(k in name for k in ["镇", "乡", "街道", "村", "农场", "园区"])

            def _is_city_like(r: Dict[str, Any]) -> bool:
                if _is_town_like(r):
                    return False
                level = _s(r.get("level")).lower()
                county_name = _s(r.get("county_name"))
                adcode = _s(r.get("adcode"))
                if level in {"city", "prefecture", "city_adcode"}:
                    return True
                if adcode.isdigit() and len(adcode) == 6 and adcode.endswith("00"):
                    return True
                return county_name == ""

            def _is_county_like(r: Dict[str, Any]) -> bool:
                if _is_town_like(r):
                    return False
                level = _s(r.get("level")).lower()
                county_name = _s(r.get("county_name"))
                return level in {"district", "county"} or county_name != ""

            # 先按请求层级筛选；市级模式不再自动降级到更细层级，避免边界过密
            level_mode = _s(admin_level).lower() or "city_adcode"
            if level_mode in {"city", "city_adcode"}:
                selected_rows = [r for r in rows if _is_city_like(r)]
                if not selected_rows:
                    print(f"[admin] 提示: 按{admin_level}未筛到要素，保持市级模式（不降级到区县/乡镇）")
            elif level_mode in {"district", "city_district", "hybrid"}:
                selected_rows = [r for r in rows if _is_county_like(r)]
            else:
                selected_rows = [r for r in rows if not _is_town_like(r)]

            if level_mode not in {"city", "city_adcode"}:
                if not selected_rows:
                    selected_rows = [r for r in rows if _is_county_like(r)]
                    if selected_rows:
                        print(f"[admin] 提示: 按{admin_level}未筛到要素，已回退到区县级，共 {len(selected_rows)} 条")
                if not selected_rows:
                    selected_rows = [r for r in rows if not _is_town_like(r)]
                    if selected_rows:
                        print(f"[admin] 提示: 区县级仍为空，已回退到非乡镇要素，共 {len(selected_rows)} 条")
                if not selected_rows:
                    selected_rows = rows
                    if selected_rows:
                        print(f"[admin] 警告: 仅检索到乡镇级要素，已全部使用，共 {len(selected_rows)} 条")

            # 标注层级策略：优先“最细但可读”的区县级，其次地市级；乡镇级不做名称标注
            if any(_is_county_like(r) for r in selected_rows):
                label_rows = [r for r in selected_rows if _is_county_like(r)]
                print(f"[admin] 标注层级: 区县级（{len(label_rows)} 条）")
            elif any(_is_city_like(r) for r in selected_rows):
                label_rows = [r for r in selected_rows if _is_city_like(r)]
                print(f"[admin] 标注层级: 地市级（{len(label_rows)} 条）")
            else:
                label_rows = []
                print("[admin] 标注层级: 无（仅乡镇级要素，跳过文字标注）")
            label_name_set = {_s(r.get("name")) for r in label_rows if _s(r.get("name"))}

            debug_count = 0
            for row in selected_rows:
                # 调试：仅打印前5条，避免刷屏
                if debug_count < 5:
                    geom_type = row.get("geom_type", "unknown")
                    level = row.get("level", "")
                    province_name = row.get("province_name", "")
                    city_name = row.get("city_name", "")
                    county_name = row.get("county_name", "")
                    name = row.get("name", "N/A")
                    print(f"[admin] 记录 {debug_count + 1}: {name} - level={level}, province={province_name}, city={city_name}, county={county_name}")
                    debug_count += 1
                
                # 收集中心点信息
                name = row.get("name", "")
                center_x = row.get("center_x")
                center_y = row.get("center_y")
                if center_x is not None and center_y is not None and name and name in label_name_set:
                    centers.append((float(center_x), float(center_y), name))
                
                # 解析几何数据
                geom_wkt = row.get("geom_wkt")
                geom_json = row.get("geom_json")
                
                if not geom_wkt and not geom_json:
                    continue
                
                try:
                    import json
                    from shapely import wkt, geometry as shapely_geom
                    
                    # 尝试用WKT解析
                    if geom_wkt:
                        geom_obj = wkt.loads(geom_wkt)
                        rings = _extract_polygon_rings_shapely(geom_obj)
                        if rings:
                            polygons.extend(rings)
                        continue
                    
                    # 尝试用GeoJSON解析
                    if geom_json:
                        geom_dict = json.loads(geom_json)
                        rings = _extract_polygon_rings(geom_dict)
                        if rings:
                            polygons.extend(rings)
                        continue
                        
                except ImportError:
                    # shapely不可用，使用GeoJSON解析
                    if geom_json:
                        try:
                            import json
                            geom_obj = json.loads(geom_json)
                            rings = _extract_polygon_rings(geom_obj)
                            if rings:
                                polygons.extend(rings)
                        except Exception as e:
                            if len(polygons) < 3:
                                print(f"[admin] 解析GeoJSON失败: {e}")
                except Exception as e:
                    if len(polygons) < 3:
                        print(f"[admin] 解析几何失败: {e}")
                    continue
        
        print(f"[admin] 成功加载 {len(polygons)} 个行政区多边形, {len(centers)} 个中心点")
        result["polygons"] = polygons
        result["centers"] = centers
        return result
        
    except Exception as e:
        print(f"[admin] 查询失败: {e}")
        print(f"[admin] 数据库配置: host={pg.get('host')}, port={pg.getint('port', 5432)}, dbname={pg.get('dbname')}")
        import traceback
        traceback.print_exc()
        return result
    finally:
        if conn is not None:
            conn.close()


def load_admin_divisions_from_db(
    config_path: str,
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    admin_level: str = "city_adcode",
    max_features: int = 500,
    simplify_deg: float = 0.005,
) -> List[List[List[float]]]:
    """
    从PostGIS数据库加载行政区划边界
    
    Args:
        config_path: 配置文件路径
        min_x, min_y, max_x, max_y: 查询范围
        admin_level: 行政级别
        max_features: 最大要素数
        simplify_deg: 简化容差
        
    Returns:
        多边形环列表 [[lon, lat], ...]
    """
    if psycopg2 is None or RealDictCursor is None:
        print("[admin] 警告: psycopg2不可用，跳过行政区划")
        print("[admin] 请安装: pip install psycopg2-binary")
        return []
    
    if not os.path.exists(config_path):
        print(f"[admin] 配置文件不存在: {config_path}")
        return []
    
    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding="utf-8")
    
    if "postgres" not in cfg:
        print("[admin] config.ini缺少[postgres]配置段")
        return []
    
    pg = cfg["postgres"]
    schema = pg.get("schema", "public")
    srid = pg.getint("srid", 4326)
    
    # 构建SQL查询
    geom_to_4326 = (
        "ST_Transform(CASE WHEN ST_SRID(a.geom) = 0 "
        f"THEN ST_SetSRID(a.geom, {srid}) ELSE a.geom END, 4326)"
    )
    
    geom_json_sql = f"""
        ST_AsText(
            CASE WHEN %(simp)s::double precision > 0
                THEN ST_SimplifyPreserveTopology({geom_to_4326}, %(simp)s::double precision)
                ELSE {geom_to_4326}
            END
        ) AS geom_wkt,
        ST_AsGeoJSON(
            CASE WHEN %(simp)s::double precision > 0
                THEN ST_SimplifyPreserveTopology({geom_to_4326}, %(simp)s::double precision)
                ELSE {geom_to_4326}
            END
        ) AS geom_json
    """
    
    sql = f"""
        SELECT
            a.adcode,
            a.city_name,
            a.county_name,
            a.name,
            ST_GeometryType({geom_to_4326}) AS geom_type,
            {geom_json_sql}
        FROM {schema}.haihe_admin_division a
        WHERE {geom_to_4326} && ST_MakeEnvelope(%(min_x)s, %(min_y)s, %(max_x)s, %(max_y)s, 4326)
          AND ST_Intersects({geom_to_4326}, ST_MakeEnvelope(%(min_x)s, %(min_y)s, %(max_x)s, %(max_y)s, 4326))
        ORDER BY a.id
        LIMIT %(max_features)s
    """
    
    polygons = []
    conn = None
    
    try:
        conn = psycopg2.connect(
            host=pg.get("host", "127.0.0.1"),
            port=pg.getint("port", 5432),
            dbname=pg.get("dbname"),
            user=pg.get("user"),
            password=pg.get("password"),
            sslmode=pg.get("sslmode", "disable"),
        )
        
        print(f"[admin] 数据库连接成功: {pg.get('host')}:{pg.getint('port', 5432)}/{pg.get('dbname')}")
        
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            params = {
                "min_x": float(min_x),
                "min_y": float(min_y),
                "max_x": float(max_x),
                "max_y": float(max_y),
                "max_features": int(max_features),
                "simp": float(simplify_deg),
            }
            
            cur.execute(sql, params)
            rows = cur.fetchall()
            
            print(f"[admin] 查询返回 {len(rows)} 条行政区划记录")
            
            for row in rows:
                # 调试：打印前3条记录的几何类型
                if len(polygons) == 0 and len(rows) <= 3:
                    geom_type = row.get("geom_type", "unknown")
                    name = row.get("name", row.get("city_name", "N/A"))
                    print(f"[admin] 记录 {len(polygons)+1}: {name} - 几何类型={geom_type}")
                
    # 根据层级过滤 - 只保留地级市（adcode以00结尾的6位编码）
                adcode = row.get("adcode", "")
                name = row.get("name", "")
                
                if adcode:
                    adcode_str = str(adcode)
                    # 只保留地级市：6位编码且以00结尾
                    if len(adcode_str) == 6 and adcode_str.endswith('00'):
                        pass  # 保留市级
                    else:
                        continue  # 跳过区级、县级、乡镇级
                else:
                    continue
                
                # 解析几何数据
                geom_wkt = row.get("geom_wkt")
                geom_json = row.get("geom_json")
                
                if not geom_wkt and not geom_json:
                    continue
                
                try:
                    import json
                    from shapely import wkt, geometry as shapely_geom
                    
                    # 尝试用WKT解析
                    if geom_wkt:
                        geom_obj = wkt.loads(geom_wkt)
                        rings = _extract_polygon_rings_shapely(geom_obj)
                        if rings:
                            polygons.extend(rings)
                        continue
                    
                    # 尝试用GeoJSON解析
                    if geom_json:
                        geom_dict = json.loads(geom_json)
                        rings = _extract_polygon_rings(geom_dict)
                        if rings:
                            polygons.extend(rings)
                        continue
                        
                except ImportError:
                    # shapely不可用，使用GeoJSON解析
                    if geom_json:
                        try:
                            import json
                            geom_obj = json.loads(geom_json)
                            rings = _extract_polygon_rings(geom_obj)
                            if rings:
                                polygons.extend(rings)
                        except Exception as e:
                            if len(polygons) < 3:
                                name = row.get("name", row.get("city_name", "N/A"))
                                print(f"[admin] 解析GeoJSON失败 ({name}): {e}")
                except Exception as e:
                    if len(polygons) < 3:
                        name = row.get("name", row.get("city_name", "N/A"))
                        print(f"[admin] 解析几何失败 ({name}): {e}")
                    continue
        
        print(f"[admin] 成功加载 {len(polygons)} 个行政区多边形")
        return polygons
        
    except Exception as e:
        print(f"[admin] 查询失败: {e}")
        print(f"[admin] 数据库配置: host={pg.get('host')}, port={pg.getint('port', 5432)}, dbname={pg.get('dbname')}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        if conn is not None:
            conn.close()


def _extract_polygon_rings_shapely(geom_obj) -> List[List[List[float]]]:
    """从shapely几何对象中提取多边形外环坐标"""
    if geom_obj is None:
        return []
    
    rings = []
    geom_type = geom_obj.geom_type
    
    if geom_type == 'Polygon':
        # 提取外环坐标
        if geom_obj.exterior:
            coords = list(geom_obj.exterior.coords)
            rings.append(coords)
    elif geom_type == 'MultiPolygon':
        # 提取每个多边形的外环
        for poly in geom_obj.geoms:
            if poly.exterior:
                coords = list(poly.exterior.coords)
                rings.append(coords)
    
    return rings


def _extract_polygon_rings(geom_obj: dict) -> List[List[List[float]]]:
    """从GeoJSON中提取多边形外环坐标"""
    if not geom_obj or not isinstance(geom_obj, dict):
        return []
    
    rings = []
    geom_type = geom_obj.get("type")
    coords = geom_obj.get("coordinates")
    
    if not coords:
        return []
    
    if geom_type == "Polygon":
        # Polygon: coordinates是[[ring1], [ring2], ...]
        if coords and len(coords) > 0:
            rings.append(coords[0])  # 外环
    elif geom_type == "MultiPolygon":
        # MultiPolygon: coordinates是[[[poly1_ring1], [poly1_ring2]], [[poly2_ring1]], ...]
        for poly in coords:
            if poly and len(poly) > 0:
                rings.append(poly[0])  # 每个多边形的外环
    elif geom_type == "GeometryCollection":
        # GeometryCollection可能包含多个几何体
        geometries = geom_obj.get("geometries", [])
        for sub_geom in geometries:
            sub_rings = _extract_polygon_rings(sub_geom)
            rings.extend(sub_rings)
    
    return rings


def draw_admin_boundaries(
    ax,
    polygons: List[List[List[float]]],
    line_color: str = "#b8860b",
    line_alpha: float = 0.5,
):
    """绘制行政区划边界"""
    for ring in polygons:
        if not ring or len(ring) < 3:
            continue
        xs = [pt[0] for pt in ring]
        ys = [pt[1] for pt in ring]
        ax.plot(xs, ys, color=line_color, linewidth=0.6, zorder=1, alpha=line_alpha)


def _theme_output_path(output_path: str, theme: str) -> str:
    if theme == "light":
        return output_path
    base, ext = os.path.splitext(output_path)
    ext = ext or ".png"
    return f"{base}_{theme}{ext}"


def _load_precip_raster_overlay(raster_path: str) -> Optional[Dict[str, Any]]:
    """读取降水栅格并转换为 matplotlib 可直接叠加的结构。"""
    p = (raster_path or "").strip()
    if not p:
        return None
    if not os.path.exists(p):
        print(f"[precip] 警告: 栅格不存在，跳过叠加: {p}")
        return None
    try:
        ds = gdal.Open(p)
        if ds is None:
            print(f"[precip] 警告: 无法打开栅格，跳过叠加: {p}")
            return None
        band = ds.GetRasterBand(1)
        data = band.ReadAsArray().astype(np.float32)
        nodata = band.GetNoDataValue()
        gt = ds.GetGeoTransform()
        xsize = ds.RasterXSize
        ysize = ds.RasterYSize
        ds = None

        if nodata is not None:
            data[np.isclose(data, float(nodata))] = np.nan
        data[data < 0] = np.nan

        finite = np.isfinite(data)
        if not finite.any():
            print(f"[precip] 警告: 栅格无有效像元，跳过叠加: {p}")
            return None

        dmax = float(np.nanmax(data))
        # 与实况产品保持一致：若值域疑似“米”，自动转“毫米”
        if 0 < dmax < 0.02:
            data *= 1000.0
            dmax = float(np.nanmax(data))
            print(f"[precip] 自动执行米->毫米换算，最大值约 {dmax:.3f} mm")

        xmin = gt[0]
        xmax = gt[0] + xsize * gt[1]
        ymax = gt[3]
        ymin = gt[3] + ysize * gt[5]
        extent = (xmin, xmax, ymin, ymax)
        print(f"[precip] 已加载栅格: {p}")
        return {"data": data, "extent": extent, "max_mm": dmax}
    except Exception as e:
        print(f"[precip] 警告: 读取栅格失败，跳过叠加: {e}")
        return None


def draw_river_network(
    segments: List[Dict[str, Any]],
    eight_level_rivers: List[Dict[str, Any]],
    impact_data: Dict[str, Any],
    output_path: str,
    start_river: str = "",
    dpi: int = 300,
    fig_width: float = 10.0,
    fig_height: float = 16.0,
    font_path: str = "",
    config_path: str = "config.ini",
    no_admin_overlay: bool = False,
    admin_level: str = "city_adcode",
    admin_max_features: int = 8000,
    admin_simplify_deg: float = 0.005,
    theme: str = "light",
    transparent_bg: bool = False,
    precip_raster_path: str = "",
    precip_alpha: float = 0.55,
    precip_min_mm: float = 0.1,
):
    """
    绘制河网图
    
    Args:
        segments: 河网线段数据
        eight_level_rivers: 八级河系数据
        impact_data: 影响时间数据
        output_path: 输出文件路径
        start_river: 起始河流名称
        dpi: 输出分辨率
        fig_width: 图形宽度
        fig_height: 图形高度
        font_path: 字体路径
        config_path: 配置文件路径
        no_admin_overlay: 是否不叠加行政区划
        admin_level: 行政区划层级
        admin_max_features: 最大行政区数量
        admin_simplify_deg: 简化容差
    """
    print(f"[draw] 开始绘制河网图")
    
    # 设置字体
    _set_font(font_path)
    
    # 主题配色
    is_dark = str(theme).strip().lower() == "dark"
    if is_dark:
        map_bg = "#0f1720"
        info_bg = "#111827"
        figure_bg = "#0b1118"
        admin_line_color = "#b59f7a"
        admin_text_color = "#d1d5db"
        admin_text_bbox = "#1f2937"
        river_color = "#7dc3ff"
        arrow_color = "#b5dcff"
        node_color = "#ff6b7d"
        node_edge_color = "#ff91a0"
        river_label_color = "#ffe6ea"
        river_label_bbox = "#263445"
        eight_level_color = "#8b93a3"
        impact_box_face = "#1f2a37"
        impact_box_edge = "#334155"
        impact_text_color = "#e5e7eb"
        axis_text_color = "#d1d5db"
        title_color = "#f3f4f6"
        legend_face = "#1f2937"
        precip_dry_color = "#0f1720"
    else:
        map_bg = "#f2f6fb"
        info_bg = "#f8fbff"
        figure_bg = "white"
        admin_line_color = "#b8860b"
        admin_text_color = "#5f6368"
        admin_text_bbox = "white"
        river_color = "#5b8ec6"
        arrow_color = "#2a5080"
        node_color = "#d7263d"
        node_edge_color = "#aa1d2f"
        river_label_color = "#8b0000"
        river_label_bbox = "white"
        eight_level_color = "#999999"
        impact_box_face = "#fff8e6"
        impact_box_edge = "#f2dba7"
        impact_text_color = "#222222"
        axis_text_color = "#2f2f2f"
        title_color = "#1f2937"
        legend_face = "white"
        precip_dry_color = "#f2f6fb"

    # 单图框布局：默认竖图，更适合网页纵向居中布局
    fig, ax = plt.subplots(figsize=(float(fig_width), float(fig_height)))
    is_portrait = float(fig_height) >= float(fig_width)
    if is_portrait:
        fig.subplots_adjust(left=0.075, right=0.975, top=0.94, bottom=0.07)
    else:
        fig.subplots_adjust(left=0.055, right=0.985, top=0.92, bottom=0.08)
    if not transparent_bg:
        fig.patch.set_facecolor(figure_bg)
        ax.set_facecolor(map_bg)
    else:
        fig.patch.set_alpha(0.0)
        ax.set_facecolor((0, 0, 0, 0))
    
    # 计算视图范围（用于加载行政区划）
    if segments:
        all_x = [seg["from_x"] for seg in segments] + [seg["to_x"] for seg in segments]
        all_y = [seg["from_y"] for seg in segments] + [seg["to_y"] for seg in segments]
        margin_ratio = 0.15
        x_margin = (max(all_x) - min(all_x)) * margin_ratio if all_x else 0.5
        y_margin = (max(all_y) - min(all_y)) * margin_ratio if all_y else 0.5
        view_min_x = min(all_x) - x_margin if all_x else 115.0
        view_max_x = max(all_x) + x_margin if all_x else 120.0
        view_min_y = min(all_y) - y_margin if all_y else 38.0
        view_max_y = max(all_y) + y_margin if all_y else 42.0
        plot_x_margin = (max(all_x) - min(all_x)) * 0.06 if all_x else 0.3
        plot_y_margin = (max(all_y) - min(all_y)) * 0.08 if all_y else 0.3
        plot_min_x = min(all_x) - plot_x_margin if all_x else view_min_x
        plot_max_x = max(all_x) + plot_x_margin if all_x else view_max_x
        plot_min_y = min(all_y) - plot_y_margin if all_y else view_min_y
        plot_max_y = max(all_y) + plot_y_margin if all_y else view_max_y
    else:
        view_min_x, view_max_x = 115.0, 120.0
        view_min_y, view_max_y = 38.0, 42.0
        plot_min_x, plot_max_x = view_min_x, view_max_x
        plot_min_y, plot_max_y = view_min_y, view_max_y

    precip_overlay = _load_precip_raster_overlay(precip_raster_path)
    if precip_overlay is not None:
        precip_data = precip_overlay["data"].copy()
        show_floor = max(0.0, float(precip_min_mm))
        if show_floor > 0:
            precip_data[np.isfinite(precip_data) & (precip_data < show_floor)] = np.nan
        if np.isfinite(precip_data).any():
            levels = [0.0, 0.1, 10.0, 25.0, 50.0, 100.0, 250.0, 1000.0]
            colors = [
                precip_dry_color,
                "#c9f7c5",
                "#7ed957",
                "#2db84d",
                "#00a2ff",
                "#1737ff",
                "#d50000",
            ]
            precip_cmap = ListedColormap(colors)
            precip_cmap.set_bad((0, 0, 0, 0))
            precip_norm = BoundaryNorm(levels, precip_cmap.N, clip=True)
            ax.imshow(
                precip_data,
                extent=precip_overlay["extent"],
                origin="upper",
                cmap=precip_cmap,
                norm=precip_norm,
                interpolation="nearest",
                alpha=min(1.0, max(0.0, float(precip_alpha))),
                zorder=1.4,
            )
            print(f"[draw] 已叠加降水栅格（阈值 {show_floor:.2f} mm）")
        else:
            print("[draw] 提示: 降水栅格应用阈值后无有效像元，已跳过叠加")
    
    # 加载并绘制行政区划底图
    admin_polygons = []
    admin_centers = []  # 用于存储行政区划中心点和名称
    
    if not no_admin_overlay:
        print(f"[draw] 加载行政区划底图 (范围: lon[{view_min_x:.2f},{view_max_x:.2f}] lat[{view_min_y:.2f},{view_max_y:.2f}])")
        # 行政区加载范围略放大，避免边缘被裁掉
        pad_x = max(0.05, (view_max_x - view_min_x) * 0.12)
        pad_y = max(0.05, (view_max_y - view_min_y) * 0.12)
        admin_result = load_admin_divisions_with_centers(
            config_path=config_path,
            min_x=view_min_x - pad_x,
            min_y=view_min_y - pad_y,
            max_x=view_max_x + pad_x,
            max_y=view_max_y + pad_y,
            admin_level=admin_level,
            max_features=admin_max_features,
            simplify_deg=admin_simplify_deg,
        )
        if admin_result:
            admin_polygons = admin_result.get("polygons", [])
            admin_centers = admin_result.get("centers", [])
            draw_admin_boundaries(ax, admin_polygons, line_color=admin_line_color, line_alpha=0.5 if not is_dark else 0.55)
            print(f"[draw] 已绘制 {len(admin_polygons)} 个行政区边界")
            
            # 绘制行政区划名称标注
            if admin_centers:
                # 去重并限制数量，避免网页端出现“满图文字”
                uniq_centers = []
                seen_names = set()
                for center_x, center_y, admin_name in admin_centers:
                    k = str(admin_name or "").strip()
                    if not k or k in seen_names:
                        continue
                    seen_names.add(k)
                    uniq_centers.append((center_x, center_y, admin_name))
                max_admin_labels = 120
                if len(uniq_centers) > max_admin_labels:
                    step = max(1, len(uniq_centers) // max_admin_labels)
                    uniq_centers = uniq_centers[::step][:max_admin_labels]

                print(f"[draw] 添加 {len(uniq_centers)} 个行政区划名称标注")
                for center_x, center_y, admin_name in uniq_centers:
                    if not (plot_min_x <= center_x <= plot_max_x and plot_min_y <= center_y <= plot_max_y):
                        continue
                    ax.text(
                        center_x, center_y,
                        admin_name,
                        fontsize=7,
                        color=admin_text_color,
                        ha='center',
                        va='center',
                        fontweight='normal',
                        zorder=4,
                        alpha=0.85,
                        bbox=dict(boxstyle='round,pad=0.25', facecolor=admin_text_bbox, alpha=0.55 if is_dark else 0.6, edgecolor='none'),
                        clip_on=True,
                    )
        else:
            print("[draw] 警告: 未能加载行政区划数据")
    
    # 颜色映射：根据Strahler阶数着色
    order_colors = {
        1: "#a8d5ba",  # 浅绿 - 一级支流
        2: "#7bc8a4",  # 绿色 - 二级支流
        3: "#4db88c",  # 深绿 - 三级支流
        4: "#2ea878",  # 更深绿 - 四级支流
        5: "#1e9966",  # 五级
        6: "#0e8a54",  # 六级
        7: "#007b42",  # 七级
        8: "#006c30",  # 八级 - 主干流
    }
    
    # 绘制河网线段 - 使用蓝色粗线条和图片样式
    print(f"[draw] 绘制 {len(segments)} 条河段")
    river_name_labels = {}  # 用于收集河流名称，避免重复标注
    
    # 使用单一色，根据阶数调整线宽
    
    # 先收集所有节点
    all_nodes = {}
    
    for seg_idx, seg in enumerate(segments):
        order = seg.get("strahler_order", 1)
        rivername = seg.get("rivername", "")
        
        # 根据阶数设置线宽 - 高阶河流更粗
        linewidth = 1.0 + order * 1.2  # 1阶=2.2, 8阶=10.6
        
        from_x, from_y = seg["from_x"], seg["from_y"]
        to_x, to_y = seg["to_x"], seg["to_y"]
        
        # 收集起点和终点节点
        from_node = (from_x, from_y)
        to_node = (to_x, to_y)
        
        if from_node not in all_nodes:
            all_nodes[from_node] = []
        all_nodes[from_node].append(order)
        
        if to_node not in all_nodes:
            all_nodes[to_node] = []
        all_nodes[to_node].append(order)
        
        ax.plot(
            [from_x, to_x],
            [from_y, to_y],
            color=river_color,
            linewidth=linewidth,
            alpha=0.7,
            solid_capstyle='round',
            zorder=2
        )
        
        # 收集河流名称（只在3阶以上且未标注过的河流显示名称）
        if rivername and order >= 3 and rivername not in river_name_labels:
            mid_x = (from_x + to_x) / 2
            mid_y = (from_y + to_y) / 2
            river_name_labels[rivername] = (mid_x, mid_y)
        
        # 在河段中间添加小箭头表示流向
        if seg_idx % 15 == 0 and order >= 2:  # 每15段加一个箭头
            mid_x = (from_x + to_x) / 2
            mid_y = (from_y + to_y) / 2
            dx = to_x - from_x
            dy = to_y - from_y
            
            length = np.sqrt(dx**2 + dy**2)
            if length > 0:
                # 归一化并添加箭头
                dx_norm = dx / length * 0.02
                dy_norm = dy / length * 0.02
                
                # 确保箭头在河流上方
                ax.annotate('', 
                           xy=(mid_x + dx_norm/2, mid_y + dy_norm/2),
                           xytext=(mid_x - dx_norm/2, mid_y - dy_norm/2),
                           arrowprops=dict(arrowstyle='->', color=arrow_color, lw=1.5, alpha=0.9),
                           zorder=7)  # zorder=7确保在河流上方但低于节点
    
    # 绘制节点 - 红色圆点（更小）
    print(f"[draw] 绘制 {len(all_nodes)} 个节点")
    node_marker_size = 3.5  # 节点大小（减小，避免抢占视觉焦点）
    
    for node_coord, orders in all_nodes.items():
        max_order = max(orders) if orders else 1
        # 所有节点都标红，大小进一步减小
        ax.plot(
            node_coord[0], node_coord[1],
            marker='o',
            markersize=node_marker_size * (0.8 + max_order * 0.1),  # 进一步减小节点大小
            color=node_color,
            markeredgecolor=node_edge_color,
            markeredgewidth=0.4,
            zorder=6,
            alpha=0.8
        )
    
    # 绘制河流名称标注 - 使用图片样式（白底红字）
    if river_name_labels:
        print(f"[draw] 添加 {len(river_name_labels)} 个河流名称标注")
        for rivername, (x, y) in river_name_labels.items():
            if not (plot_min_x <= x <= plot_max_x and plot_min_y <= y <= plot_max_y):
                continue
            ax.text(
                x, y,
                rivername,
                fontsize=7.5,
                color=river_label_color,
                ha='center',
                va='center',
                fontweight='bold',
                bbox=dict(boxstyle='round,pad=0.22', facecolor=river_label_bbox, alpha=0.72, edgecolor='none'),
                zorder=5,
                clip_on=True,
            )
    
    # 绘制八级河系 - 使用淡色、半透明、细线
    if eight_level_rivers:
        print(f"[draw] 绘制 {len(eight_level_rivers)} 条八级河系")
        for river in eight_level_rivers:
            coords = river["geometry"]
            attrs = river["attributes"]
            
            # 判断是单一线还是多段线
            if isinstance(coords[0], tuple):
                # 单一线
                xs = [p[0] for p in coords]
                ys = [p[1] for p in coords]
                ax.plot(xs, ys, color=eight_level_color, linewidth=0.5, alpha=0.4, zorder=3, label="八级河系" if river == eight_level_rivers[0] else "")
            else:
                # 多段线
                for line_coords in coords:
                    xs = [p[0] for p in line_coords]
                    ys = [p[1] for p in line_coords]
                    ax.plot(xs, ys, color=eight_level_color, linewidth=0.5, alpha=0.4, zorder=3, label="八级河系" if river == eight_level_rivers[0] else "")
    
    # 右侧信息面板：影响时间信息
    if impact_data:
        downstream_list = impact_data.get("downstream", [])
        source_river = impact_data.get("source_river", "N/A")
        
        if downstream_list:
            print(f"[draw] 添加影响时间标注（{len(downstream_list)} 条下游河流）")
            
            # 在右侧信息面板显示影响时间，避免遮挡主图
            text_lines = [f"源河流: {source_river}"]
            text_lines.append("")
            text_lines.append("下游河流影响时间（平均流速 5.84 km/h）:")
            
            # 为保证长图可读性，信息栏中展示前12条
            show_limit = 12
            for item in downstream_list[:show_limit]:
                river_name = item.get("downstream_river", "未知")
                dist = item.get("impact_distance_km", 0)
                time_avg = item.get("time_estimates", {}).get("avg", {})
                duration_human = time_avg.get("duration", {}).get("human", "N/A")
                
                text_lines.append(f"  • {river_name}: {dist:.1f}km → {duration_human}")
            if len(downstream_list) > show_limit:
                text_lines.append(f"  …其余 {len(downstream_list) - show_limit} 条略")
            
            # 添加文本框到主图
            props = dict(boxstyle='round,pad=0.45', facecolor=impact_box_face, alpha=0.95, edgecolor=impact_box_edge)
            ax.text(
                0.02, 0.98,
                '\n'.join(text_lines),
                transform=ax.transAxes,
                fontsize=8.5,
                verticalalignment='top',
                color=impact_text_color,
                bbox=props,
                zorder=10
            )
        else:
            print(f"[draw] 警告: 影响时间数据中没有下游河流信息")
    else:
        print(f"[draw] 提示: 未提供影响时间数据（可通过 --impact-source-river 参数指定）")
    
    # 设置标题
    title_text = f"海河流域水系拓扑图"
    if start_river:
        title_text += f" - {start_river}"
    
    ax.set_title(title_text, fontsize=15, fontweight='bold', pad=10, color=title_color)
    
    # 设置标签
    ax.set_xlabel("经度", fontsize=10, color=axis_text_color)
    ax.set_ylabel("纬度", fontsize=10, color=axis_text_color)
    ax.tick_params(axis='both', colors=axis_text_color)
    for spine in ax.spines.values():
        spine.set_color("#6b7280" if is_dark else "#9ca3af")
    
    # 不添加网格（根据图片样式）
    ax.grid(False)
    
    # 图例放在右侧信息面板，避免遮挡河网主体
    legend_elements = [
        # 河网
        plt.Line2D([0], [0], color=river_color, linewidth=2.0, label="河网（按阶数分级）"),
        plt.Line2D([0], [0], color=river_color, linewidth=9.0, label="  - 高阶主干流"),
        plt.Line2D([0], [0], color=river_color, linewidth=5.0, label="  - 中阶支流"),
        plt.Line2D([0], [0], color=river_color, linewidth=2.0, label="  - 低阶小溪流"),
        # 节点
        plt.Line2D([0], [0], marker='o', color='w', markerfacecolor=node_color,
                   markersize=6, label="河流节点", linestyle='None'),
        # 八级河系
        plt.Line2D([0], [0], color=eight_level_color, linewidth=0.5, alpha=0.4, label="八级河系（参考）"),
        # 行政区划
        plt.Line2D([0], [0], color=admin_line_color, linewidth=0.6, alpha=0.5, label="行政区划边界"),
    ]
    if precip_overlay is not None:
        legend_elements.append(
            plt.Line2D([0], [0], color="#2db84d", linewidth=3.0, alpha=0.8, label="降水栅格叠加")
        )
    leg = ax.legend(
        handles=legend_elements,
        loc='lower right',
        fontsize=8,
        framealpha=0.95,
        title="图例说明",
        title_fontsize=9,
    )
    leg.get_frame().set_facecolor(legend_face)
    leg.get_frame().set_edgecolor("#4b5563" if is_dark else "#d1d5db")
    for txt in leg.get_texts():
        txt.set_color(impact_text_color if is_dark else "#222222")
    leg.get_title().set_color(impact_text_color if is_dark else "#222222")
    
    # 统一视图范围
    ax.set_xlim(plot_min_x, plot_max_x)
    ax.set_ylim(plot_min_y, plot_max_y)

    # 竖图时采用填充布局，避免“竖画布里仍是横条”
    if is_portrait:
        ax.set_aspect('auto')
    else:
        ax.set_aspect('equal', adjustable='box')
    
    # 保存图像
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) if os.path.dirname(output_path) else '.', exist_ok=True)
    save_face = "none" if transparent_bg else figure_bg
    fig.savefig(output_path, dpi=dpi, bbox_inches='tight', facecolor=save_face, transparent=bool(transparent_bg))
    plt.close(fig)
    
    print(f"[draw] 河网图已保存到: {output_path} (theme={theme}, transparent={transparent_bg})")


def main():
    args = _parse_args()
    
    print("=" * 60)
    print("海河流域河网图绘制工具")
    print("=" * 60)
    
    # 1. 加载河网数据
    segments = load_river_segments(
        start_river=args.start_river,
        max_edges=args.max_edges
    )
    
    if not segments:
        print("[error] 未能加载河网数据，退出")
        sys.exit(1)
    
    # 2. 加载八级河系
    eight_level_rivers = load_eight_level_rivers(args.eight_level_river_shp)
    
    # 3. 计算影响时间
    impact_data = {}
    if args.show_impact_time and args.impact_source_river:
        impact_data = calculate_impact_times(args.impact_source_river)
    elif args.show_impact_time and args.start_river:
        impact_data = calculate_impact_times(args.start_river)
    
    # 4. 绘制河网图（支持浅/深双主题）
    themes = ["light", "dark"] if args.theme == "both" else [args.theme]
    output_paths = []
    for one_theme in themes:
        out_path = _theme_output_path(args.output, one_theme)
        output_paths.append(out_path)
        draw_river_network(
            segments=segments,
            eight_level_rivers=eight_level_rivers,
            impact_data=impact_data,
            output_path=out_path,
            start_river=args.start_river,
            dpi=args.dpi,
            fig_width=args.fig_width,
            fig_height=args.fig_height,
            font_path=args.font_path,
            config_path=args.config,
            no_admin_overlay=args.no_admin_overlay,
            admin_level=args.admin_level,
            admin_max_features=args.admin_max_features,
            admin_simplify_deg=args.admin_simplify_deg,
            theme=one_theme,
            transparent_bg=bool(args.transparent_bg),
            precip_raster_path=args.precip_raster,
            precip_alpha=args.precip_alpha,
            precip_min_mm=args.precip_min_mm,
        )
    
    print("=" * 60)
    print("绘制完成！")
    print("输出文件:")
    for p in output_paths:
        print(f"  - {p}")
    print("=" * 60)


if __name__ == "__main__":
    main()
