"""边级裁剪影响河流 PostGIS 查询。"""
from __future__ import annotations

import json
from typing import Any

from rainfall_edgeclip_common import (
    pick_first,
    quote_ident,
    river_name_expr,
    validate_geom_column,
)


def get_table_columns(cur, *, schema: str, table: str) -> set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
        (schema, table),
    )
    return {str(row["column_name"]) for row in cur.fetchall()}


def create_station_temp(cur, stations: list[dict], *, srid: int) -> None:
    cur.execute("DROP TABLE IF EXISTS tmp_rain_impact_stations")
    cur.execute(f"""
        CREATE TEMP TABLE tmp_rain_impact_stations(
            station_id text,
            station_name text,
            province text,
            city text,
            cnty text,
            town text,
            lon double precision,
            lat double precision,
            rain_24h double precision,
            geom geometry(Point,{int(srid)})
        ) ON COMMIT DROP
    """)
    rows = []
    for s in stations:
        lon = float(s["lon"])
        lat = float(s["lat"])
        rows.append((
            str(s.get("station_id") or ""),
            str(s.get("station_name") or s.get("name") or ""),
            str(s.get("province") or ""),
            str(s.get("city") or ""),
            str(s.get("cnty") or ""),
            str(s.get("town") or ""),
            lon,
            lat,
            float(s.get("rain_24h") or s.get("rainfall") or 0.0),
            lon,
            lat,
        ))
    cur.executemany(
        f"""
        INSERT INTO tmp_rain_impact_stations VALUES(
            %s,%s,%s,%s,%s,%s,%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),{int(srid)})
        )
        """,
        rows,
    )


def query_direct_edges_clipped(
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
        WITH buffer_union AS (
            SELECT ST_UnaryUnion(ST_Collect(ST_Buffer(geom::geography, %(buffer_m)s)::geometry)) AS geom
            FROM tmp_rain_impact_stations
        ),
        raw AS (
            SELECT
                {id_expr} AS id,
                {objectid_expr} AS objectid,
                {river_expr} AS river_name,
                r.{q_geom} AS original_geom,
                ST_LineMerge(r.{q_geom}) AS merged_geom,
                b.geom AS buffer_geom,
                ST_Multi(ST_CollectionExtract(ST_Intersection(r.{q_geom}, b.geom), 2)) AS clipped_geom
            FROM {q_schema}.{q_table} r
            CROSS JOIN buffer_union b
            WHERE r.{q_geom} IS NOT NULL
              AND NOT ST_IsEmpty(r.{q_geom})
              AND ST_Intersects(r.{q_geom}, b.geom)
        ),
        measured AS (
            SELECT
                *,
                CASE WHEN GeometryType(merged_geom) = 'LINESTRING' THEN merged_geom ELSE NULL END AS line_geom,
                ST_Length(original_geom::geography) / 1000.0 AS total_length_km
            FROM raw
            WHERE clipped_geom IS NOT NULL
              AND NOT ST_IsEmpty(clipped_geom)
        ),
        frac AS (
            SELECT
                m.*,
                CASE
                    WHEN line_geom IS NULL THEN 1.0
                    ELSE COALESCE((
                        SELECT MAX(GREATEST(
                            ST_LineLocatePoint(line_geom, ST_StartPoint(dp.geom)),
                            ST_LineLocatePoint(line_geom, ST_EndPoint(dp.geom))
                        ))
                        FROM ST_Dump(clipped_geom) AS dp
                    ), 1.0)
                END AS max_clip_fraction,
                CASE
                    WHEN line_geom IS NULL THEN false
                    ELSE ST_Intersects(ST_EndPoint(line_geom), buffer_geom)
                END AS end_inside_buffer
            FROM measured m
        )
        SELECT
            f.id,
            f.objectid,
            f.river_name,
            ST_AsGeoJSON(f.clipped_geom) AS geom_json,
            ST_Length(f.clipped_geom::geography) / 1000.0 AS length_km,
            CASE
                WHEN f.end_inside_buffer THEN 0.0
                ELSE GREATEST(0.0, f.total_length_km * (1.0 - f.max_clip_fraction))
            END AS remaining_after_buffer_km,
            MIN(ST_Distance(f.original_geom::geography, s.geom::geography) / 1000.0) AS min_station_distance_km,
            COUNT(DISTINCT s.station_id) AS trigger_station_count,
            jsonb_agg(DISTINCT jsonb_build_object(
                'station_id', s.station_id,
                'station_name', s.station_name,
                'lon', s.lon,
                'lat', s.lat,
                'rain_24h', s.rain_24h
            )) AS trigger_stations
        FROM frac f
        JOIN tmp_rain_impact_stations s
          ON ST_DWithin(f.original_geom::geography, s.geom::geography, %(buffer_m)s)
        WHERE f.clipped_geom IS NOT NULL
          AND NOT ST_IsEmpty(f.clipped_geom)
        GROUP BY f.id, f.objectid, f.river_name, f.clipped_geom, f.total_length_km, f.max_clip_fraction, f.end_inside_buffer
        ORDER BY min_station_distance_km, f.river_name, f.objectid
    """, {"buffer_m": buffer_m})
    return list(cur.fetchall())


def create_downstream_temp(cur, segments: list[dict]) -> None:
    cur.execute("DROP TABLE IF EXISTS tmp_downstream_segments")
    cur.execute("""
        CREATE TEMP TABLE tmp_downstream_segments(
            objectid text,
            river_name text,
            min_distance_km double precision,
            end_distance_km double precision,
            clip_fraction double precision,
            from_x double precision,
            from_y double precision
        ) ON COMMIT DROP
    """)
    cur.executemany(
        "INSERT INTO tmp_downstream_segments VALUES(%s,%s,%s,%s,%s,%s,%s)",
        [
            (
                str(s["objectid"]),
                str(s["river_name"]),
                float(s["min_distance_km"]),
                float(s["end_distance_km"]),
                float(s["clip_fraction"]),
                s.get("from_x"),
                s.get("from_y"),
            )
            for s in segments
        ],
    )


def query_downstream_edges_clipped(
    cur,
    *,
    schema: str,
    table: str,
    columns: set[str],
    geom_column: str,
    segments: list[dict],
) -> list[dict]:
    if not segments:
        return []
    create_downstream_temp(cur, segments)
    geom_col = validate_geom_column(columns, geom_column)
    objectid_col = pick_first(columns, ("objectid", "OBJECTID", "id", "gid"))
    id_col = pick_first(columns, ("id", "gid"))
    if not objectid_col:
        return []
    objectid_expr = f"r.{quote_ident(objectid_col)}::text"
    id_expr = f"r.{quote_ident(id_col)}::text" if id_col else objectid_expr
    river_expr = river_name_expr(columns, alias="r")
    q_schema = quote_ident(schema)
    q_table = quote_ident(table)
    q_geom = quote_ident(geom_col)
    q_objectid = quote_ident(objectid_col)

    cur.execute(f"""
        WITH joined AS (
            SELECT
                {id_expr} AS id,
                {objectid_expr} AS objectid,
                {river_expr} AS db_river_name,
                ds.river_name AS graph_river_name,
                ds.min_distance_km,
                ds.end_distance_km,
                ds.clip_fraction,
                ds.from_x,
                ds.from_y,
                r.{q_geom} AS original_geom,
                ST_LineMerge(r.{q_geom}) AS merged_geom
            FROM {q_schema}.{q_table} r
            JOIN tmp_downstream_segments ds
              ON r.{q_objectid}::text = ds.objectid
            WHERE r.{q_geom} IS NOT NULL
              AND NOT ST_IsEmpty(r.{q_geom})
        ),
        oriented AS (
            SELECT
                *,
                CASE
                    WHEN GeometryType(merged_geom) != 'LINESTRING' THEN NULL
                    WHEN from_x IS NULL OR from_y IS NULL THEN merged_geom
                    WHEN ST_Distance(ST_StartPoint(merged_geom), ST_SetSRID(ST_MakePoint(from_x, from_y), 4326))
                       <= ST_Distance(ST_EndPoint(merged_geom), ST_SetSRID(ST_MakePoint(from_x, from_y), 4326))
                    THEN merged_geom
                    ELSE ST_Reverse(merged_geom)
                END AS directed_line
            FROM joined
        ),
        clipped AS (
            SELECT
                id,
                objectid,
                COALESCE(NULLIF(TRIM(db_river_name), ''), graph_river_name) AS river_name,
                min_distance_km,
                end_distance_km,
                clip_fraction,
                CASE
                    WHEN directed_line IS NOT NULL AND clip_fraction < 0.999999 THEN ST_Multi(ST_LineSubstring(directed_line, 0, clip_fraction))
                    WHEN directed_line IS NOT NULL THEN directed_line
                    ELSE original_geom
                END AS clipped_geom
            FROM oriented
        )
        SELECT
            id,
            objectid,
            river_name,
            min_distance_km AS min_downstream_distance_km,
            end_distance_km AS end_downstream_distance_km,
            clip_fraction,
            ST_AsGeoJSON(clipped_geom) AS geom_json,
            ST_Length(clipped_geom::geography) / 1000.0 AS length_km
        FROM clipped
        WHERE clipped_geom IS NOT NULL
          AND NOT ST_IsEmpty(clipped_geom)
        ORDER BY min_distance_km, river_name, objectid
    """)
    return list(cur.fetchall())


def geojson_geometry(row: dict) -> dict | None:
    raw = row.get("geom_json")
    if not raw:
        return None
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None


def build_feature(row: dict, impact_type: str, downstream_map: dict[str, dict] | None = None) -> dict | None:
    geometry = geojson_geometry(row)
    if not geometry:
        return None
    river_name = str(row.get("river_name") or "未知")
    props: dict[str, Any] = {
        "impact_type": impact_type,
        "river_name": river_name,
        "id": row.get("id"),
        "objectid": row.get("objectid"),
        "length_km": round(float(row.get("length_km") or 0.0), 3),
    }
    if impact_type == "direct_buffer":
        props.update({
            "remaining_after_buffer_km": round(float(row.get("remaining_after_buffer_km") or 0.0), 3),
            "min_station_distance_km": round(float(row.get("min_station_distance_km") or 0.0), 3),
            "trigger_station_count": int(row.get("trigger_station_count") or 0),
            "trigger_stations": row.get("trigger_stations") or [],
            "geometry_source": "edge_table_clipped_30km_buffer",
        })
    else:
        info = (downstream_map or {}).get(river_name, {})
        props.update({
            "min_downstream_distance_km": row.get("min_downstream_distance_km") or info.get("min_distance_km"),
            "end_downstream_distance_km": row.get("end_downstream_distance_km"),
            "clip_fraction": row.get("clip_fraction"),
            "geometry_source": "edge_table_clipped_downstream_50km",
        })
    return {"type": "Feature", "geometry": geometry, "properties": props}
