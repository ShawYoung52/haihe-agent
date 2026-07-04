"""直接影响河流：30km 只用于命中判断，不裁剪几何。"""
from __future__ import annotations

from rainfall_edgeclip_common import pick_first, quote_ident, river_name_expr, validate_geom_column


def query_direct_edges_full(
    cur,
    *,
    schema: str,
    table: str,
    columns: set[str],
    geom_column: str,
    buffer_km: float,
) -> list[dict]:
    geom_col = validate_geom_column(columns, geom_column)
    objectid_col = pick_first(columns, ("objectid", "OBJECTID", "id", "gid"))
    id_col = pick_first(columns, ("id", "gid"))
    objectid_expr = f"r.{quote_ident(objectid_col)}::text" if objectid_col else "NULL::text"
    id_expr = f"r.{quote_ident(id_col)}::text" if id_col else objectid_expr
    river_expr = river_name_expr(columns, alias="r")
    q_schema = quote_ident(schema)
    q_table = quote_ident(table)
    q_geom = quote_ident(geom_col)
    buffer_m = float(buffer_km) * 1000.0

    cur.execute(f"""
        SELECT
            {id_expr} AS id,
            {objectid_expr} AS objectid,
            {river_expr} AS river_name,
            ST_AsGeoJSON(r.{q_geom}) AS geom_json,
            ST_Length(r.{q_geom}::geography) / 1000.0 AS length_km,
            0.0 AS remaining_after_buffer_km,
            MIN(ST_Distance(r.{q_geom}::geography, s.geom::geography) / 1000.0) AS min_station_distance_km,
            COUNT(DISTINCT s.station_id) AS trigger_station_count,
            jsonb_agg(DISTINCT jsonb_build_object(
                'station_id', s.station_id,
                'station_name', s.station_name,
                'lon', s.lon,
                'lat', s.lat,
                'rain_24h', s.rain_24h
            )) AS trigger_stations
        FROM {q_schema}.{q_table} r
        JOIN tmp_rain_impact_stations s
          ON ST_DWithin(r.{q_geom}::geography, s.geom::geography, %(buffer_m)s)
        WHERE r.{q_geom} IS NOT NULL
          AND NOT ST_IsEmpty(r.{q_geom})
        GROUP BY r.{q_geom}, {id_expr}, {objectid_expr}, {river_expr}
        ORDER BY min_station_distance_km, river_name, objectid
    """, {"buffer_m": buffer_m})
    return list(cur.fetchall())
