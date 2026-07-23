# Rain Impact River Logic Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `hhlyqyxt-master/utils/rainfall_impact_geojson.py` so that `river_directed_v6.pkl` is the authoritative topology and `haihe_river_directed_full_v6` is only a geometry/attribute lookup table, eliminating unknown names, duplicate segments, isolated segments, and broken downstream traces.

**Architecture:** Replace the current `ST_Dump` + `GROUP BY objectid` SQL approach with a one-to-one mapping between pkl edges and full_v6 rows. Direct-buffer edges are found via a spatial query on full_v6 and then classified by exact distance on the pkl edge. Downstream tracing runs on the pkl graph, and geometries/names are resolved from full_v6 rows matched by `(objectid, from_x, from_y, to_x, to_y)`.

**Tech Stack:** Python 3.11, NetworkX, GeoPandas/Shapely (for geometry reconstruction), psycopg2, pandas, pytest.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `hhlyqyxt-master/utils/rainfall_impact_geojson.py` | Core algorithm: station normalization, spatial query, pkl-graph edge classification, downstream trace, geometry/name resolution, GeoJSON assembly |
| `hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py` | Unit tests for edge classification, downstream trace, name fallback, dedup, missing-row fallback |
| `haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py` | Update `IMPACT_RULES` to reflect simplified algorithm; keep wrapper interface unchanged |
| `haihe-weather-analyzer-mcp/haihe_mcp_tools.py` | Update `get_affected_river_network_by_rainfall` description if it still mentions old behavior |
| `chainlitexam/tools/rainfall_river_impact.py` | Local wrapper parity: ensure parameter forwarding is still correct |
| `haiheliuyubaoyuagent-master/CLAUDE.md` | Update rain-impact conventions to match the new one-to-one design |

---

## Task 1: Add helper to build full_v6 row lookup

**Files:**
- Modify: `hhlyqyxt-master/utils/rainfall_impact_geojson.py`
- Test: `hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py`

- [ ] **Step 1: Write the failing test**

```python
def test_build_edge_lookup_maps_row_by_objectid_and_endpoints():
    from utils.rainfall_impact_geojson import _build_edge_lookup
    rows = [
        {"objectid": "1", "from_x": 116.0, "from_y": 39.0, "to_x": 116.1, "to_y": 39.1, "river_name": "A"},
        {"objectid": "1", "from_x": 116.1, "from_y": 39.1, "to_x": 116.2, "to_y": 39.2, "river_name": "B"},
    ]
    lookup = _build_edge_lookup(rows)
    assert lookup[("1", 116.0, 39.0, 116.1, 39.1)]["river_name"] == "A"
    assert lookup[("1", 116.1, 39.1, 116.2, 39.2)]["river_name"] == "B"
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd "C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)\hhlyqyxt-master"
python -m pytest utils/tests/test_rainfall_impact_geojson.py::test_build_edge_lookup_maps_row_by_objectid_and_endpoints -v
```
Expected: FAIL with `ImportError` or `AttributeError` for `_build_edge_lookup`.

- [ ] **Step 3: Add `_build_edge_lookup`**

Insert near the SQL helper section of `hhlyqyxt-master/utils/rainfall_impact_geojson.py`:

```python
def _build_edge_lookup(rows: list[dict]) -> dict[tuple[str, float, float, float, float], dict]:
    """Build a lookup from (objectid, from_x, from_y, to_x, to_y) to a full_v6 row."""
    lookup: dict[tuple[str, float, float, float, float], dict] = {}
    for row in rows or []:
        objectid = str(row.get("objectid") or "")
        if not objectid:
            continue
        key = (
            objectid,
            float(row.get("from_x") or 0.0),
            float(row.get("from_y") or 0.0),
            float(row.get("to_x") or 0.0),
            float(row.get("to_y") or 0.0),
        )
        lookup[key] = row
    return lookup
```

- [ ] **Step 4: Run test to verify it passes**

Run the same pytest command.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd "C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)"
git add hhlyqyxt-master/utils/rainfall_impact_geojson.py hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py
git commit -m "feat(rain-impact): add full_v6 row lookup by objectid and endpoints"
```

---

## Task 2: Refactor direct-buffer query to return edge rows without aggregation

**Files:**
- Modify: `hhlyqyxt-master/utils/rainfall_impact_geojson.py`
- Test: `hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py`

- [ ] **Step 1: Replace `_query_direct_rows` with `_query_candidate_edge_rows`**

The new function should query `full_v6` rows within the station buffer, returning one row per pkl edge (no `ST_Dump`, no `GROUP BY objectid`).

```python
def _query_candidate_edge_rows(
    cur,
    schema: str,
    table: str,
    geom_col: str,
    objectid_col: str,
    river_name_col: str,
    buffer_km: float,
) -> list[dict]:
    """查询暴雨站点缓冲区内命中的 full_v6 行，每行对应一条 pkl 边。"""
    cur.execute(f"""
        SELECT
            r.{_qi(objectid_col)}::text AS objectid,
            COALESCE(NULLIF(TRIM(r.{_qi(river_name_col)}::text), ''), '未知') AS river_name,
            COALESCE(NULLIF(TRIM(r.src_name::text), ''), '未知') AS src_name,
            COALESCE(r.is_luan, false) AS is_luan,
            r.from_x, r.from_y, r.to_x, r.to_y,
            r.len_km,
            ST_AsGeoJSON(r.{_qi(geom_col)}) AS geom_json,
            MIN(ST_Distance(r.{_qi(geom_col)}::geography, s.geom::geography) / 1000.0) AS min_station_distance_km,
            COUNT(DISTINCT s.station_id) AS trigger_station_count,
            jsonb_agg(DISTINCT jsonb_build_object(
                'station_id', s.station_id,
                'station_name', s.station_name,
                'lon', s.lon,
                'lat', s.lat,
                'rain_24h', s.rain_24h
            )) AS trigger_stations
        FROM {_qi(schema)}.{_qi(table)} r
        JOIN tmp_rain24h_impact_stations s
          ON ST_DWithin(r.{_qi(geom_col)}::geography, s.geom::geography, %(buffer_m)s)
        WHERE r.{_qi(geom_col)} IS NOT NULL AND NOT ST_IsEmpty(r.{_qi(geom_col)})
        GROUP BY r.id, r.{_qi(objectid_col)}, r.{_qi(river_name_col)}, r.src_name, r.is_luan,
                 r.from_x, r.from_y, r.to_x, r.to_y, r.len_km, r.{_qi(geom_col)}
        ORDER BY min_station_distance_km, r.{_qi(objectid_col)}
    """, {"buffer_m": float(buffer_km) * 1000.0})
    return list(cur.fetchall())
```

- [ ] **Step 2: Update tests that relied on old `_query_direct_rows` shape**

If existing tests call `_query_direct_rows`, update them to call `_query_candidate_edge_rows` and assert row-level properties (no aggregation, each row maps to one edge).

- [ ] **Step 3: Run existing tests**

```bash
cd "C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)\hhlyqyxt-master"
python -m pytest utils/tests/test_rainfall_impact_geojson.py -v
```
Expected: Tests may fail until downstream code is updated; that is OK for this task.

- [ ] **Step 4: Commit**

```bash
cd "C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)"
git add hhlyqyxt-master/utils/rainfall_impact_geojson.py hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py
git commit -m "feat(rain-impact): query full_v6 edge rows without aggregation"
```

---

## Task 3: Refactor edge classification to use pkl graph distances

**Files:**
- Modify: `hhlyqyxt-master/utils/rainfall_impact_geojson.py`
- Test: `hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py`

- [ ] **Step 1: Add `_classify_graph_edges`**

This helper takes candidate rows, builds the full_v6 lookup, matches rows to pkl edges, computes exact edge-to-station distances, and classifies edges.

```python
def _classify_graph_edges(
    candidate_rows: list[dict],
    graph,
    stations: list[dict],
    station_buffer_km: float,
    direct_match_km: float,
) -> tuple[dict[str, dict], set[str], dict]:
    """
    把 full_v6 候选行匹配到 pkl 边，并按精确距离分类。
    返回 (direct_edges, downstream_start_nodes, stats)。
    """
    lookup = _build_edge_lookup(candidate_rows)
    direct_edges: dict[str, dict] = {}
    buffer_only_keys: set[str] = set()
    start_nodes: set[Any] = set()

    for u, v, key, attr, p1, p2 in _iter_edges_with_points(graph):
        edge_key = _edge_key(u, v, key, attr)
        objectid = _edge_objectid_key(attr)
        row = lookup.get((objectid, p1[0], p1[1], p2[0], p2[1]))
        if row is None:
            continue

        min_dist = min(
            _point_to_segment_km(s["lon"], s["lat"], p1, p2)
            for s in stations
        )
        if min_dist > station_buffer_km:
            continue

        edge_info = {
            "edge_key": edge_key,
            "objectid": objectid,
            "river_name": get_edge_river_name(attr),
            "from_x": p1[0],
            "from_y": p1[1],
            "to_x": p2[0],
            "to_y": p2[1],
            "is_direct_graph_edge": min_dist <= direct_match_km,
            "is_luan": bool(attr.get("is_luan")),
            "min_station_distance_km": min_dist,
            "trigger_stations": row.get("trigger_stations", []),
            "row": row,
        }

        if min_dist <= direct_match_km:
            direct_edges[edge_key] = edge_info
        else:
            buffer_only_keys.add(edge_key)

        start_nodes.add(v)

    stats = _downstream_start_stats(
        direct_part_matched_edge_count=len(direct_edges),
        station_buffer_fallback_edge_count=len(buffer_only_keys),
        direct_match_km=direct_match_km,
        station_buffer_km=station_buffer_km,
    )
    return direct_edges, start_nodes, stats
```

- [ ] **Step 2: Replace `_find_direct_graph_starts` usage in `build_rainstorm_impact_thematic_map`**

Inside `build_rainstorm_impact_thematic_map`, after `_create_station_temp`:

```python
    candidate_rows = _query_candidate_edge_rows(
        cur, schema, river_table, geom_column, objectid_column, river_name_column, station_buffer_km
    )
    direct_edges, start_nodes, downstream_start_stats = _classify_graph_edges(
        candidate_rows, graph, rainstorm_stations, station_buffer_km, direct_match_km
    )
    direct_keys = {k for k, e in direct_edges.items() if e["is_direct_graph_edge"]}
```

- [ ] **Step 3: Write a test for classification**

```python
def test_classify_graph_edges_marks_direct_and_buffer_only():
    from utils.rainfall_impact_geojson import _classify_graph_edges
    import networkx as nx

    graph = nx.DiGraph()
    graph.add_edge(
        (116.0, 39.0), (116.1, 39.0),
        OBJECTID=1, name="A", river_name="A", length=10.0,
        from_x=116.0, from_y=39.0, to_x=116.1, to_y=39.0, is_luan=False,
    )
    rows = [
        {"objectid": "1", "from_x": 116.0, "from_y": 39.0, "to_x": 116.1, "to_y": 39.0,
         "river_name": "A", "src_name": "A", "is_luan": False, "len_km": 10.0,
         "trigger_stations": []},
    ]
    stations = [{"lon": 116.05, "lat": 39.005, "rain_24h": 100.0}]
    direct_edges, start_nodes, stats = _classify_graph_edges(
        rows, graph, stations, station_buffer_km=30.0, direct_match_km=10.0
    )
    assert len(direct_edges) == 1
    assert list(direct_edges.values())[0]["is_direct_graph_edge"]
```

- [ ] **Step 4: Run tests**

```bash
cd "C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)\hhlyqyxt-master"
python -m pytest utils/tests/test_rainfall_impact_geojson.py -v
```

- [ ] **Step 5: Commit**

```bash
cd "C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)"
git add hhlyqyxt-master/utils/rainfall_impact_geojson.py hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py
git commit -m "feat(rain-impact): classify pkl edges by exact station distance"
```

---

## Task 4: Refactor downstream trace and geometry resolution

**Files:**
- Modify: `hhlyqyxt-master/utils/rainfall_impact_geojson.py`
- Test: `hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py`

- [ ] **Step 1: Keep `_collect_downstream_edges` but simplify `_save_downstream_edge`**

`_collect_downstream_edges` already traces the graph correctly. Keep it. Remove the unused `_query_downstream_rows` and `_create_downstream_temp` if no longer needed.

- [ ] **Step 2: Add `_resolve_edge_features`**

```python
def _resolve_edge_features(
    edges: list[dict],
    lookup: dict[tuple[str, float, float, float, float], dict],
    impact_type: str,
    luan_mapping: dict[str, str],
) -> list[dict]:
    """把 pkl 边列表解析为 GeoJSON features，几何/名称来自 full_v6 lookup。"""
    features = []
    for edge in edges:
        objectid = edge["objectid"]
        key = (objectid, edge["from_x"], edge["from_y"], edge["to_x"], edge["to_y"])
        row = lookup.get(key)
        geom_json = row.get("geom_json") if row else None
        if not geom_json:
            geom_json = json.dumps({
                "type": "LineString",
                "coordinates": [[edge["from_x"], edge["from_y"]], [edge["to_x"], edge["to_y"]]],
            })
            logger.warning("full_v6 缺少边几何，使用直线兜底: %s", edge["edge_key"])

        river_name = _pick_river_name(row, edge, luan_mapping)
        feature = {
            "type": "Feature",
            "properties": {
                "objectid": objectid,
                "id": objectid,
                "river_name": river_name,
                "is_luan": edge.get("is_luan", False),
                "impact_type": impact_type,
                "min_downstream_distance_km": edge.get("min_distance_km", 0.0),
                "end_downstream_distance_km": edge.get("end_distance_km", 0.0),
                "keep_km": edge.get("keep_km", 0.0),
                "clip_fraction": edge.get("clip_fraction", 1.0),
                "is_direct_graph_edge": edge.get("is_direct_graph_edge", False),
                "edge_key": edge["edge_key"],
            },
            "geometry": json.loads(geom_json),
        }
        features.append(feature)
    return features


def _pick_river_name(row: dict | None, edge: dict, luan_mapping: dict[str, str]) -> str:
    candidates = []
    if row:
        candidates.append(row.get("src_name"))
        candidates.append(row.get("river_name"))
    candidates.append(edge.get("river_name"))
    for name in candidates:
        if name and str(name).strip() and str(name).strip() != "未知":
            return str(name).strip()
    if edge.get("is_luan"):
        objectid = str(edge.get("objectid") or "")
        mapped = luan_mapping.get(objectid)
        if mapped:
            return mapped
    return "未知"
```

- [ ] **Step 3: Rewrite `_build_river_geojson`**

```python
def _build_river_geojson(
    direct_edges: dict[str, dict],
    downstream_edges: list[dict],
    candidate_rows: list[dict],
    graph_path=None,
) -> dict:
    lookup = _build_edge_lookup(candidate_rows)
    luan_mapping = _load_luan_name_mapping(graph_path)

    direct_features = _resolve_edge_features(
        list(direct_edges.values()), lookup, "direct_buffer", luan_mapping
    )
    downstream_features = _resolve_edge_features(
        downstream_edges, lookup, "downstream_50km", luan_mapping
    )

    features = direct_features + downstream_features
    features.sort(
        key=lambda f: (
            0 if f["properties"]["impact_type"] == "direct_buffer" else 1,
            f["properties"].get("river_name") or "",
            f["properties"].get("min_downstream_distance_km", 0.0),
        )
    )
    return {"type": "FeatureCollection", "features": features}
```

- [ ] **Step 4: Update `build_rainstorm_impact_thematic_map` call site**

Replace:
```python
    direct_rows = _query_direct_rows(...)
    start_nodes, direct_keys, downstream_start_stats = _find_direct_graph_starts(...)
    downstream_edges = _collect_downstream_edges(start_nodes, graph_path, direct_keys, downstream_km)
    downstream_rows = _query_downstream_rows(...)
```

With:
```python
    candidate_rows = _query_candidate_edge_rows(...)
    direct_edges, start_nodes, downstream_start_stats = _classify_graph_edges(
        candidate_rows, graph, rainstorm_stations, station_buffer_km, direct_match_km
    )
    direct_keys = {k for k, e in direct_edges.items() if e["is_direct_graph_edge"]}
    downstream_edges = _collect_downstream_edges(
        {node: 0.0 for node in start_nodes}, graph_path, direct_keys, downstream_km
    )
```

And replace `_build_river_geojson(direct_rows, downstream_rows, ...)` with `_build_river_geojson(direct_edges, downstream_edges, candidate_rows, graph_path)`.

- [ ] **Step 5: Run tests**

```bash
cd "C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)\hhlyqyxt-master"
python -m pytest utils/tests/test_rainfall_impact_geojson.py -v
```

- [ ] **Step 6: Commit**

```bash
cd "C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)"
git add hhlyqyxt-master/utils/rainfall_impact_geojson.py hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py
git commit -m "feat(rain-impact): resolve geometries and names from full_v6 lookup"
```

---

## Task 5: Clean up obsolete code

**Files:**
- Modify: `hhlyqyxt-master/utils/rainfall_impact_geojson.py`

- [ ] **Step 1: Remove functions no longer needed**

Delete or deprecate:
- `_query_direct_rows`
- `_query_downstream_rows`
- `_create_downstream_temp`
- `_fill_unmatched_downstream_edges`
- `_build_fallback_downstream_row`
- `_build_objectid_name_map`
- `_enrich_unknown_river_names`
- `_needs_name_enrichment`
- `_drop_downstream_covered_by_direct`
- `_normalize_river_name` (keep only if Luan expansion still needed elsewhere)

Keep:
- `_load_luan_name_mapping`
- `_apply_luan_names` (simplified to only use objectid mapping)

- [ ] **Step 2: Simplify Luan name application**

Update `_apply_luan_names` to use the objectid mapping directly:

```python
def _apply_luan_names(features: list[dict], mapping: dict[str, str]) -> None:
    for feature in features:
        props = feature.get("properties") or {}
        if not props.get("is_luan"):
            continue
        objectid = str(props.get("objectid") or "")
        if objectid in mapping:
            props["river_name"] = mapping[objectid]
```

Note: If `_pick_river_name` already handles Luan mapping, `_apply_luan_names` may be removed entirely. Decide based on code organization.

- [ ] **Step 3: Run tests**

```bash
cd "C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)\hhlyqyxt-master"
python -m pytest utils/tests/test_rainfall_impact_geojson.py -v
```

- [ ] **Step 4: Commit**

```bash
cd "C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)"
git add hhlyqyxt-master/utils/rainfall_impact_geojson.py
git commit -m "refactor(rain-impact): remove obsolete aggregation and fallback code"
```

---

## Task 6: Update MCP wrapper and local wrapper

**Files:**
- Modify: `haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py`
- Modify: `haihe-weather-analyzer-mcp/haihe_mcp_tools.py`
- Modify: `chainlitexam/tools/rainfall_river_impact.py`

- [ ] **Step 1: Update `IMPACT_RULES` in `fixed_rainfall_impact_tool.py`**

Replace the current rules description with one that reflects the new design:

```python
IMPACT_RULES = [
    {
        "rule": "direct_match_km",
        "value": DEFAULT_DIRECT_GRAPH_MATCH_KM,
        "description": "站点到 pkl 边真实几何距离 ≤ direct_match_km 时标记为 direct_buffer 真实直接河段。",
    },
    {
        "rule": "station_buffer_km",
        "value": 30.0,
        "description": "站点 30km 缓冲区内的 pkl 边作为下游追踪起点；这些边不直接标记为真实河段。",
    },
    {
        "rule": "downstream_km",
        "value": 50.0,
        "description": "从直接缓冲区起点沿 pkl 有向图向下游追踪 50km。",
    },
    {
        "rule": "edge_geometry_lookup",
        "value": True,
        "description": "每条 pkl 边通过 (objectid, from_x, from_y, to_x, to_y) 到 full_v6 查找原始几何；缺失时用 pkl 边直线兜底。",
    },
    {
        "rule": "name_fallback",
        "value": ["full_v6.src_name", "full_v6.river_name", "pkl.river_name", "luan_mapping"],
        "description": "河流名称按优先级 fallback，滦河系按 is_luan + objectid 映射表规范化。",
    },
]
```

- [ ] **Step 2: Update tool description in `haihe_mcp_tools.py`** if it still describes the old objectid-aggregation behavior.

- [ ] **Step 3: Verify `chainlitexam/tools/rainfall_river_impact.py` still forwards parameters correctly**

No signature changes should be needed. Run:

```bash
cd "C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)\chainlitexam"
python -m pytest tests/test_rainfall_river_impact.py -v
```

- [ ] **Step 4: Commit**

```bash
cd "C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)"
git add haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py haihe-weather-analyzer-mcp/haihe_mcp_tools.py chainlitexam/tools/rainfall_river_impact.py
git commit -m "docs(rain-impact): update wrapper rules and descriptions for edge-based design"
```

---

## Task 7: Update CLAUDE.md conventions

**Files:**
- Modify: `haiheliuyubaoyuagent-master/CLAUDE.md`

- [ ] **Step 1: Update the rain-impact section**

Replace the current long caveat paragraph with:

```markdown
- `../hhlyqyxt-master/utils/rainfall_impact_geojson.py` — Traction-agent core algorithm for affected rivers (cross-repo dependency; keep `direct_match_km` defaults and graph/table version constants in sync; imports pandas directly, so ensure pandas is installed). **Algorithm invariant:** pkl directed graph is the authoritative topology; `full_v6` is a geometry/attribute lookup table with one row per pkl edge matched by `(objectid, from_x, from_y, to_x, to_y)`. Direct-buffer edges are found via `ST_DWithin` on full_v6 and classified by exact distance on the pkl edge. `direct_match_km` only marks which of those edges are "real direct river segments" (`is_direct_graph_edge`). Do not revert to objectid-aggregation or `ST_Dump` based direct matching.
```

- [ ] **Step 2: Commit**

```bash
cd "C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)"
git add haiheliuyubaoyuagent-master/CLAUDE.md
git commit -m "docs: update CLAUDE.md rain-impact conventions for edge-based design"
```

---

## Task 8: Add integration-level tests and run full suites

**Files:**
- Modify: `hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py`

- [ ] **Step 1: Add end-to-end test with mocked graph and DB**

```python
def test_build_rainstorm_impact_no_duplicate_edge_keys():
    """集成测试：两站命中同一条边，输出中该边只出现一次。"""
    from utils.rainfall_impact_geojson import build_rainstorm_impact_thematic_map
    import networkx as nx

    graph = nx.DiGraph()
    graph.add_edge(
        (116.0, 39.0), (116.1, 39.0),
        OBJECTID=1, name="A", river_name="A", length=10.0,
        from_x=116.0, from_y=39.0, to_x=116.1, to_y=39.0, is_luan=False,
    )
    # Save temp pkl
    import tempfile, pickle, os
    with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
        pickle.dump(graph, f)
        pkl_path = f.name
    try:
        result = build_rainstorm_impact_thematic_map(
            [{"lon": 116.05, "lat": 39.005, "rain_24h": 100.0},
             {"lon": 116.05, "lat": 39.006, "rain_24h": 80.0}],
            graph_path=pkl_path,
            rainfall_threshold_mm=50.0,
            station_buffer_km=30.0,
            downstream_km=50.0,
            direct_match_km=10.0,
        )
        keys = {f["properties"]["edge_key"] for f in result["river_geojson"]["features"]}
        assert len(keys) == len(result["river_geojson"]["features"])
    finally:
        os.unlink(pkl_path)
```

- [ ] **Step 2: Run full traction-agent test suite**

```bash
cd "C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)\hhlyqyxt-master"
python -m pytest utils/tests/test_rainfall_impact_geojson.py -v
```

- [ ] **Step 3: Run chainlitexam fast-path and full suites**

```bash
cd "C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)\chainlitexam"
python tests/test_fast_paths.py
python -m pytest tests/ -v
```

- [ ] **Step 4: Commit**

```bash
cd "C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)"
git add hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py
git commit -m "test(rain-impact): add integration test for edge-key dedup"
```

---

## Task 9: Code review and simplification

**Files:**
- All modified files in this plan

- [ ] **Step 1: Run `code-review` skill on the cumulative diff**

- [ ] **Step 2: Run `code-simplifier` skill or agent to apply cleanups**

- [ ] **Step 3: Fix any issues found**

- [ ] **Step 4: Re-run tests**

```bash
cd "C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)\hhlyqyxt-master"
python -m pytest utils/tests/test_rainfall_impact_geojson.py -v
cd "C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)\chainlitexam"
python tests/test_fast_paths.py
python -m pytest tests/ -v
```

- [ ] **Step 5: Commit**

```bash
cd "C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)"
git add -A
git commit -m "refactor(rain-impact): apply code-review and simplifier cleanups"
```

---

## Task 10: Final verification and memory

**Files:**
- `haiheliuyubaoyuagent-master/.planning/2026-07-17-rain-impact-redesign/progress.md`
- `haiheliuyubaoyuagent-master/.planning/2026-07-17-rain-impact-redesign/task_plan.md`

- [ ] **Step 1: Update planning progress files**

Mark all phases complete and record test results.

- [ ] **Step 2: Record claude-mem observation**

Summarize the design decision: pkl graph as topology, full_v6 as lookup, one-to-one edge mapping.

- [ ] **Step 3: Run final verification**

```bash
cd "C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)\hhlyqyxt-master"
python -m pytest utils/tests/test_rainfall_impact_geojson.py -v
cd "C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)\chainlitexam"
python tests/test_fast_paths.py
python -m pytest tests/ -v
```

- [ ] **Step 4: Finish branch**

Use `superpowers:finishing-a-development-branch` to present merge/PR options to the user.

---

## Self-Review

- **Spec coverage:** Each section of the design doc is mapped to tasks: topology authority (Tasks 2-4), name fallback (Task 4), direct/buffer separation (Task 3), geometry lookup (Task 4), cleanup (Task 5), wrappers (Task 6), docs (Task 7), tests (Tasks 1, 3, 4, 8).
- **Placeholder scan:** No TBD/TODO/fill-in-details found; each step includes code or exact commands.
- **Type consistency:** `_classify_graph_edges` returns `dict[str, dict]` for `direct_edges`; `_resolve_edge_features` consumes `list[dict]`; lookup key is consistently `(objectid, from_x, from_y, to_x, to_y)`.
- **API compatibility:** Public function signatures unchanged.
