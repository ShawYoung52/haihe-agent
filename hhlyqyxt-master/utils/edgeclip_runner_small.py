"""边级裁剪运行函数。"""
from __future__ import annotations

import json
from pathlib import Path
from psycopg2.extras import RealDictCursor
from rainfall_impact_geojson import _station_record, aggregate_5min_station_pre_to_24h
from rainfall_edgeclip_common import collect_downstream_segments, direct_seed_nodes
from rainfall_edgeclip_db import build_feature, create_station_temp, get_table_columns, query_direct_edges_clipped, query_downstream_edges_clipped


def run_edgeclip(*, conn, csv_path: str, graph_path: str, output_dir: str, db_schema: str = "public", db_srid: int = 4326, river_edge_table: str = "haihe_river_directed_simple_v5", river_geom_column: str = "geom", rain_threshold_mm: float = 50.0, station_buffer_km: float = 30.0, downstream_km: float = 50.0) -> dict:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    station_df = aggregate_5min_station_pre_to_24h(csv_path)
    impact_df = station_df[(station_df["rain_24h"] >= rain_threshold_mm) & station_df["lon"].notna() & station_df["lat"].notna()].copy()
    stations = [_station_record(row) for _, row in impact_df.iterrows()]
    direct_rows = []
    downstream_rows = []
    downstream_map = {}
    downstream_segments = []
    direct_objectids = set()
    direct_rivers = set()
    if stations:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            columns = get_table_columns(cur, schema=db_schema, table=river_edge_table)
            if not columns:
                raise ValueError(f"未找到河流边表：{db_schema}.{river_edge_table}")
            create_station_temp(cur, stations, srid=db_srid)
            direct_rows = query_direct_edges_clipped(cur, schema=db_schema, table=river_edge_table, columns=columns, geom_column=river_geom_column, buffer_km=station_buffer_km)
            start_dist, direct_objectids, direct_rivers = direct_seed_nodes(direct_rows, graph_path)
            downstream_map, downstream_segments = collect_downstream_segments(start_dist, graph_path=graph_path, direct_objectids=direct_objectids, direct_rivers=direct_rivers, downstream_km=downstream_km)
            downstream_segments = [x for x in downstream_segments if str(x.get("objectid") or "") not in direct_objectids and str(x.get("river_name") or "") not in direct_rivers]
            downstream_map = {k: v for k, v in downstream_map.items() if str(k) not in direct_rivers}
            downstream_rows = query_downstream_edges_clipped(cur, schema=db_schema, table=river_edge_table, columns=columns, geom_column=river_geom_column, segments=downstream_segments)
    features = []
    seen = set()
    for row, kind in [(x, "direct_buffer") for x in direct_rows] + [(x, "downstream_50km") for x in downstream_rows]:
        if kind == "downstream_50km":
            oid = str(row.get("objectid") or "")
            rn = str(row.get("river_name") or "")
            if oid in direct_objectids or rn in direct_rivers:
                continue
        feature = build_feature(row, kind, downstream_map if kind == "downstream_50km" else None)
        if not feature:
            continue
        key = (feature["properties"].get("objectid"), feature["properties"].get("river_name"), kind)
        if key not in seen:
            seen.add(key)
            features.append(feature)
    features.sort(key=lambda f: (0 if f["properties"].get("impact_type") == "direct_buffer" else 1, f["properties"].get("river_name") or "", f["properties"].get("objectid") or ""))
    river_path = out / "impact_rivers_postgis.geojson"
    summary_path = out / "summary.json"
    river_path.write_text(json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {"status": "ok", "station_summary": {"total_station_count": int(len(station_df)), "impact_station_count": int(len(stations)), "max_rain_24h": float(station_df["rain_24h"].max() or 0.0)}, "river_summary": {"direct_segment_count": len(direct_rows), "direct_river_count": len(direct_rivers), "downstream_graph_segment_count": len(downstream_segments), "downstream_db_segment_count": len(downstream_rows), "downstream_river_count": len(downstream_map), "geojson_feature_count": len(features)}, "direct_rivers": sorted(direct_rivers), "downstream_segments": downstream_segments, "outputs": {"river_geojson": str(river_path), "summary_json": str(summary_path)}}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
