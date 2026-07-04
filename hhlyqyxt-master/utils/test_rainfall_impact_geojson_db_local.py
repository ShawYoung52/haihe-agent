r"""本地 DB 版测试入口：5 分钟 CSV -> 24 小时降水 -> 影响河流 GeoJSON。

只保留这一个入口文件。数据库连接使用本文件参数/环境变量。

当前输出口径：
- 30km 缓冲区仍然用于判断直接影响河流；直接河流不截断；
- 不在 30km 内、但由下游追踪得到的河流，按 downstream_km 做几何截断；
- 截断在本地 GeoJSON 层完成，不改变数据库，也不新增其他 py 文件。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import pandas as pd
from sqlalchemy import create_engine

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for p in (PROJECT_ROOT, CURRENT_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import rainfall_impact_geojson as rig  # noqa: E402

DEFAULT_CSV_PATH = r"C:\Users\gaozr\Downloads\24hourmindata.csv"
DEFAULT_GRAPH_PATH = r"E:\tj\line\result\river_directed_v5.pkl"
DEFAULT_OUTPUT_DIR = r"C:\Users\gaozr\Downloads\24hourmindata_db_impact_output"
DEFAULT_DB_HOST = "211.157.132.19"
DEFAULT_DB_PORT = 48091
DEFAULT_DB_NAME = "hhly"
DEFAULT_DB_USER = "postgres"
DEFAULT_DB_SCHEMA = "public"
DEFAULT_RIVER_TABLE = "haihe_river_directed_full_v5"
KM_PER_DEG = 111.32


def json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)


def make_engine(args: argparse.Namespace):
    if not args.db_password:
        raise ValueError("缺少数据库密码：请设置 HHLY_DB_PASSWORD 或传入 --db-password")
    password = quote_plus(str(args.db_password))
    url = f"postgresql+psycopg2://{args.db_user}:{password}@{args.db_host}:{args.db_port}/{args.db_name}"
    return create_engine(url, connect_args={"sslmode": args.db_sslmode, "connect_timeout": args.db_connect_timeout})


def _segment_km(a: list[float], b: list[float]) -> float:
    lon1, lat1 = float(a[0]), float(a[1])
    lon2, lat2 = float(b[0]), float(b[1])
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    dx = (lon2 - lon1) * math.cos(mean_lat) * KM_PER_DEG
    dy = (lat2 - lat1) * KM_PER_DEG
    return math.hypot(dx, dy)


def _point_to_segment_km(lon: float, lat: float, a: list[float], b: list[float]) -> float:
    lon1, lat1 = float(a[0]), float(a[1])
    lon2, lat2 = float(b[0]), float(b[1])
    mean_lat = math.radians((lat + lat1 + lat2) / 3.0)
    x = lon * math.cos(mean_lat) * KM_PER_DEG
    y = lat * KM_PER_DEG
    x1 = lon1 * math.cos(mean_lat) * KM_PER_DEG
    y1 = lat1 * KM_PER_DEG
    x2 = lon2 * math.cos(mean_lat) * KM_PER_DEG
    y2 = lat2 * KM_PER_DEG
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x - x1, y - y1)
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))


def _iter_lines(geometry: dict) -> list[list[list[float]]]:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "LineString":
        return [coords]
    if gtype == "MultiLineString":
        return coords
    return []


def _min_distance_to_stations_km(geometry: dict, stations: list[dict]) -> float | None:
    lines = _iter_lines(geometry)
    if not lines or not stations:
        return None
    best = math.inf
    for station in stations:
        try:
            lon = float(station["lon"])
            lat = float(station["lat"])
        except Exception:
            continue
        for line in lines:
            for i in range(len(line) - 1):
                best = min(best, _point_to_segment_km(lon, lat, line[i], line[i + 1]))
    return None if best == math.inf else best


def _clip_line_from_start(line: list[list[float]], keep_km: float) -> list[list[float]]:
    if len(line) <= 1 or keep_km <= 0:
        return []
    kept = [line[0]]
    remaining = float(keep_km)
    for i in range(len(line) - 1):
        a = line[i]
        b = line[i + 1]
        seg_km = _segment_km(a, b)
        if seg_km <= 0:
            continue
        if remaining >= seg_km:
            kept.append(b)
            remaining -= seg_km
            continue
        ratio = max(0.0, min(1.0, remaining / seg_km))
        cut = [float(a[0]) + (float(b[0]) - float(a[0])) * ratio, float(a[1]) + (float(b[1]) - float(a[1])) * ratio]
        kept.append(cut)
        break
    return kept if len(kept) >= 2 else []


def _clip_geometry_from_start(geometry: dict, keep_km: float) -> dict | None:
    lines = _iter_lines(geometry)
    if not lines or keep_km <= 0:
        return None
    clipped_lines = []
    remaining = float(keep_km)
    for line in lines:
        if remaining <= 0:
            break
        line_len = sum(_segment_km(line[i], line[i + 1]) for i in range(len(line) - 1))
        clipped = _clip_line_from_start(line, remaining)
        if clipped:
            clipped_lines.append(clipped)
        remaining -= line_len
    if not clipped_lines:
        return None
    if len(clipped_lines) == 1:
        return {"type": "LineString", "coordinates": clipped_lines[0]}
    return {"type": "MultiLineString", "coordinates": clipped_lines}


def _postprocess_river_geojson(result: dict, station_buffer_km: float, downstream_km: float) -> dict:
    """恢复本地测试口径：直接河流不截断，下游河流按 50km 截断。

    build_rain24h_impact_river_geojson 会负责 30km 命中和下游拓扑追踪；这里再用
    真实 geometry 与触发站距离校正 direct/downstream，并只截断 downstream_50km。
    """
    river_geojson = result.get("river_geojson") or {"type": "FeatureCollection", "features": []}
    stations = result.get("impact_stations") or []
    features = []
    for feature in river_geojson.get("features", []):
        geometry = feature.get("geometry")
        if not geometry:
            continue
        props = dict(feature.get("properties") or {})
        min_station_km = _min_distance_to_stations_km(geometry, stations)
        props["min_station_distance_km"] = round(min_station_km, 3) if min_station_km is not None else None

        if min_station_km is not None and min_station_km <= float(station_buffer_km):
            # 直接影响：30km 内命中，完整真实河流输出，不截断。
            props["impact_type"] = "direct_buffer"
            props["geometry_source"] = "full_geometry_30km_direct_uncut"
            features.append({"type": "Feature", "geometry": geometry, "properties": props})
            continue

        # 非 30km 内命中的影响河流，按下游 50km 口径截断。
        start_dist = props.get("min_downstream_distance_km")
        try:
            start_dist = float(start_dist) if start_dist is not None else 0.0
        except Exception:
            start_dist = 0.0
        keep_km = max(float(downstream_km) - max(start_dist, 0.0), 0.0)
        clipped = _clip_geometry_from_start(geometry, keep_km)
        if not clipped:
            continue
        props["impact_type"] = "downstream_50km"
        props["keep_km"] = round(keep_km, 3)
        props["geometry_source"] = "local_geojson_clipped_downstream_50km"
        features.append({"type": "Feature", "geometry": clipped, "properties": props})

    features.sort(key=lambda f: (
        0 if f["properties"].get("impact_type") == "direct_buffer" else 1,
        f["properties"].get("river_name") or "",
        f["properties"].get("objectid") or "",
    ))
    return {"type": "FeatureCollection", "features": features}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="真实河流 24 小时降水影响 GeoJSON")
    parser.add_argument("--csv", default=DEFAULT_CSV_PATH)
    parser.add_argument("--graph", default=DEFAULT_GRAPH_PATH)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--rain-threshold-mm", type=float, default=50.0)
    parser.add_argument("--station-buffer-km", type=float, default=30.0)
    parser.add_argument("--downstream-km", type=float, default=50.0)
    parser.add_argument("--db-host", default=os.getenv("HHLY_DB_HOST", DEFAULT_DB_HOST))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("HHLY_DB_PORT", DEFAULT_DB_PORT)))
    parser.add_argument("--db-name", default=os.getenv("HHLY_DB_NAME", DEFAULT_DB_NAME))
    parser.add_argument("--db-user", default=os.getenv("HHLY_DB_USER", DEFAULT_DB_USER))
    parser.add_argument("--db-password", default=os.getenv("HHLY_DB_PASSWORD", ""))
    parser.add_argument("--db-schema", default=os.getenv("HHLY_DB_SCHEMA", DEFAULT_DB_SCHEMA))
    parser.add_argument("--db-sslmode", default=os.getenv("HHLY_DB_SSLMODE", "disable"))
    parser.add_argument("--db-connect-timeout", type=int, default=int(os.getenv("HHLY_DB_CONNECT_TIMEOUT", "5")))
    parser.add_argument("--river-table", default=os.getenv("HHLY_RIVER_TABLE_FULL", DEFAULT_RIVER_TABLE))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    engine = make_engine(args)
    rig._get_engine = lambda: engine

    result = rig.build_rain24h_impact_river_geojson(
        args.csv,
        rain_threshold_mm=args.rain_threshold_mm,
        station_buffer_km=args.station_buffer_km,
        downstream_km=args.downstream_km,
        river_table=args.river_table,
        schema=args.db_schema,
        graph_path=args.graph,
    )

    river_geojson = _postprocess_river_geojson(result, args.station_buffer_km, args.downstream_km)
    river_path = output / "impact_rivers_postgis.geojson"
    station_path = output / "impact_stations.geojson"
    summary_path = output / "summary.json"

    river_path.write_text(json.dumps(river_geojson, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    station_path.write_text(json.dumps(result.get("station_geojson", {"type": "FeatureCollection", "features": []}), ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    summary = {k: v for k, v in result.items() if k not in {"river_geojson", "station_geojson"}}
    summary["outputs"] = {"river_geojson": str(river_path), "station_geojson": str(station_path), "summary_json": str(summary_path)}
    summary["postprocess"] = {
        "direct_rule": "30km direct uses full geometry without clipping",
        "downstream_rule": "downstream_50km geometry is locally clipped by downstream_km",
        "geojson_feature_count": len(river_geojson.get("features", [])),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")

    print("DB 版本地测试完成")
    print(f"渲染河流表：{args.river_table}")
    print(f"数据库：{args.db_host}:{args.db_port}/{args.db_name}")
    print(f"GeoJSON要素数：{len(river_geojson.get('features', []))}")
    print(f"输出文件：{river_path}")


if __name__ == "__main__":
    main()
