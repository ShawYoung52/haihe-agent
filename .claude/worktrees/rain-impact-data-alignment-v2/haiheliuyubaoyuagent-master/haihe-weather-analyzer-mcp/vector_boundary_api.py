"""
独立矢量边界分析 HTTP 服务
供同事直接调用：POST /api/query_rivers_by_boundary
支持 GeoJSON 和 Shapefile（Base64）两种输入
"""
from __future__ import annotations

import base64
import configparser
import io
import json
import logging
import os
import struct

import psycopg2
from fastapi import FastAPI, HTTPException
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel

from constants import RIVER_TABLE_FULL

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="矢量边界河流分析服务", version="1.0.0")

config = configparser.ConfigParser()
config_path = os.path.join(os.path.dirname(__file__), "config.ini")
config.read(config_path, encoding="utf-8-sig")


class BoundaryRequest(BaseModel):
    boundary: str
    include_downstream: bool = True


def _parse_boundary(boundary: str) -> dict:
    """解析矢量边界，支持 GeoJSON 和 Shapefile Base64"""
    stripped = boundary.strip()

    # 尝试 GeoJSON
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            geom = json.loads(stripped)
            t = geom.get("type", "")
            if t in ("Polygon", "MultiPolygon"):
                return geom
            if t in ("Feature", "FeatureCollection"):
                if t == "Feature":
                    return geom["geometry"]
                if geom.get("features"):
                    return geom["features"][0]["geometry"]
        except Exception:
            pass

    # 尝试 Shapefile Base64
    try:
        raw = base64.b64decode(stripped)
        if len(raw) >= 100:
            shape_type = struct.unpack("<i", raw[32:36])[0]
            if shape_type in (5, 15, 25):
                try:
                    import shapefile as sf
                    reader = sf.Reader(shp=io.BytesIO(raw), shx=None, dbf=None)
                    shp = reader.shape(0)
                    coords = shp.points
                    parts = list(shp.parts) + [len(coords)]
                    rings = []
                    for i in range(len(parts) - 1):
                        ring = [list(coords[j]) for j in range(parts[i], parts[i + 1])]
                        if len(ring) >= 4:
                            rings.append(ring)
                    if len(rings) == 1:
                        return {"type": "Polygon", "coordinates": rings}
                    elif len(rings) > 1:
                        return {"type": "MultiPolygon", "coordinates": [rings]}
                except ImportError:
                    try:
                        import geopandas as gpd
                        gdf = gpd.read_file(io.BytesIO(raw))
                        if not gdf.empty:
                            fc = json.loads(gdf.geometry.to_json())
                            if fc.get("features"):
                                return fc["features"][0]["geometry"]
                    except ImportError:
                        raise HTTPException(400, "解析 Shapefile 需安装 pyshp：pip install pyshp")
    except HTTPException:
        raise
    except Exception:
        pass

    raise HTTPException(400, "无法解析边界数据，仅支持 GeoJSON（Polygon/MultiPolygon）或 Shapefile(.shp) Base64")


def _load_river_graph():
    """加载河网有向图"""
    import pickle as _pickle
    import os as _os
    import networkx as _nx

    pg_conf = config["postgres"] if "postgres" in config else {}
    graph_dir = pg_conf.get("graph_dir", "/home/ev/data/graph")
    graph_file = pg_conf.get("graph_file", "haihe_graph.pkl")
    graph_path = _os.path.join(graph_dir, graph_file) if graph_dir else ""
    if graph_path and _os.path.isfile(graph_path):
        with open(graph_path, "rb") as f:
            return _pickle.load(f)
    return None


def _analysis_river_by_name(pg_conf: dict, rivername: str) -> set[str]:
    """查询河流的下游河流（图拓扑 + DB 双重保障）"""
    schema = pg_conf.get("schema", "public")
    table = pg_conf.get("river_table_full", RIVER_TABLE_FULL) or RIVER_TABLE_FULL
    downstream: set[str] = set()

    # 1. 尝试直接从数据库 direct_downstream_of_rivers 列读取
    try:
        with psycopg2.connect(
            host=pg_conf["host"], port=pg_conf["port"],
            dbname=pg_conf["dbname"], user=pg_conf["user"],
            password=pg_conf["password"], sslmode=pg_conf.get("sslmode", "prefer"),
            connect_timeout=int(pg_conf.get("connect_timeout", "5")),
        ) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                col_row = cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                      AND column_name = 'direct_downstream_of_rivers'
                """, (schema, table))
                if cur.fetchone():
                    cur.execute(f"""
                        SELECT DISTINCT NULLIF(TRIM(direct_downstream_of_rivers), '') AS downstream_names
                        FROM {schema}.{table}
                        WHERE COALESCE(NULLIF(TRIM(river_name), ''), NULLIF(TRIM(src_name), '')) = %s
                          AND NULLIF(TRIM(direct_downstream_of_rivers), '') IS NOT NULL
                    """, (rivername,))
                    for row in cur.fetchall():
                        s = str(row.get("downstream_names") or "").strip()
                        for part in s.replace("[", "").replace("]", "").split(","):
                            dn = part.strip().strip("'\"")
                            if dn and dn != rivername:
                                downstream.add(dn)

                # 2. 端点拓扑匹配：当前河流终点 = 其他河流起点
                if not downstream:
                    cur.execute(f"""
                        WITH curr AS (
                            SELECT to_x, to_y FROM {schema}.{table}
                            WHERE COALESCE(NULLIF(TRIM(river_name), ''), NULLIF(TRIM(src_name), '')) = %s
                            LIMIT 1
                        )
                        SELECT DISTINCT COALESCE(NULLIF(TRIM(t.river_name), ''), NULLIF(TRIM(t.src_name), '')) AS dn
                        FROM {schema}.{table} t, curr c
                        WHERE ABS(t.from_x - c.to_x) < 0.01 AND ABS(t.from_y - c.to_y) < 0.01
                          AND COALESCE(NULLIF(TRIM(t.river_name), ''), NULLIF(TRIM(t.src_name), '')) IS NOT NULL
                          AND COALESCE(NULLIF(TRIM(t.river_name), ''), NULLIF(TRIM(t.src_name), '')) <> %s
                    """, (rivername, rivername))
                    for row in cur.fetchall():
                        dn = str(row.get("dn") or "").strip()
                        if dn:
                            downstream.add(dn)
    except Exception as e:
        logger.warning(f"数据库查询下游失败: {e}")

    # 3. 图拓扑兜底（如果有 pickle 文件）
    if not downstream:
        try:
            G = _load_river_graph()
            if G is not None:
                river_edges = [
                    end for start, end, attr in G.edges(data=True)
                    if attr.get("rivername") == rivername
                ]
                visited = set()
                for end_node in river_edges:
                    for _u, _v, attr in G.out_edges(end_node, data=True):
                        name = attr.get("rivername", "")
                        if name and name != rivername and name not in visited:
                            visited.add(name)
                            downstream.add(name)
        except Exception as e:
            logger.warning(f"图拓扑查询下游失败: {e}")

    return downstream


@app.post("/api/query_rivers_by_boundary")
def query_rivers_by_boundary(req: BoundaryRequest):
    """
    查询与矢量边界相交的河流、行政区划、77分区河系及下游河流
    """
    pg_conf = config["postgres"] if "postgres" in config else {}
    if not pg_conf:
        raise HTTPException(500, "缺少 [postgres] 数据库配置")

    river_table = pg_conf.get("river_table_full", RIVER_TABLE_FULL) or RIVER_TABLE_FULL

    # 1. 解析边界
    geom = _parse_boundary(req.boundary)
    geojson_str = json.dumps(geom, ensure_ascii=False)
    input_type = geom.get("type", "Unknown")

    try:
        with psycopg2.connect(
            host=pg_conf["host"], port=pg_conf["port"],
            dbname=pg_conf["dbname"], user=pg_conf["user"],
            password=pg_conf["password"], sslmode=pg_conf.get("sslmode", "prefer"),
            connect_timeout=int(pg_conf.get("connect_timeout", "5")),
        ) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # 2. 相交河流
                cur.execute(f"""
                    SELECT r.river_name, ST_AsGeoJSON(ST_Collect(r.geom)) AS river_geom, COUNT(*) AS segment_count
                    FROM {river_table} r
                    WHERE ST_Intersects(r.geom, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))
                      AND r.river_name IS NOT NULL AND r.river_name != ''
                    GROUP BY r.river_name ORDER BY r.river_name
                """, (geojson_str,))
                river_rows = cur.fetchall()

                # 3. 相交行政区划
                cur.execute("""
                    SELECT DISTINCT a.province_name, a.city_name, a.county_name, a.full_name
                    FROM haihe_admin_division a
                    WHERE ST_Intersects(a.geom, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))
                    ORDER BY a.province_name, a.city_name, a.county_name
                """, (geojson_str,))
                admin_rows = cur.fetchall()

                # 4. 相交77分区
                cur.execute("""
                    SELECT DISTINCT z.zone_name, z.zone_code
                    FROM haihe_zone_77 z
                    WHERE ST_Intersects(z.geom, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))
                    ORDER BY z.zone_name
                """, (geojson_str,))
                zone_rows = cur.fetchall()

    except psycopg2.Error as e:
        raise HTTPException(500, f"数据库查询失败: {e}")
    except Exception as e:
        raise HTTPException(500, f"空间分析失败: {e}")

    # 组装结果
    all_river_names = [r["river_name"] for r in river_rows]

    result = {
        "boundary_type": input_type,
        "river_count": len(river_rows),
        "intersecting_rivers": [
            {
                "river_name": r["river_name"],
                "segment_count": r["segment_count"],
                "river_geojson": r["river_geom"],
            }
            for r in river_rows
        ],
        "admin_unit_count": len(admin_rows),
        "boundary_admin_units": [
            {"province": a["province_name"], "city": a["city_name"], "county": a["county_name"], "full_name": a["full_name"]}
            for a in admin_rows
        ],
        "partition_count": len(zone_rows),
        "boundary_partitions_77": [
            {"zone_name": z["zone_name"], "zone_code": z["zone_code"]}
            for z in zone_rows
        ],
    }

    # 5. 下游河流
    if req.include_downstream and all_river_names:
        downstream_map = {}
        for rname in all_river_names:
            downstream_map[rname] = [
                {"river_name": dn}
                for dn in sorted(_analysis_river_by_name(pg_conf, rname))
            ]
        result["downstream_rivers"] = downstream_map
    else:
        result["downstream_rivers"] = {}

    return result


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "矢量边界河流分析服务"}


def main():
    import uvicorn
    port = int(os.getenv("BOUNDARY_API_PORT", "8091"))
    host = os.getenv("BOUNDARY_API_HOST", "0.0.0.0")
    print(f"🔄 矢量边界分析服务启动: http://{host}:{port}")
    print(f"📡 POST /api/query_rivers_by_boundary")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()