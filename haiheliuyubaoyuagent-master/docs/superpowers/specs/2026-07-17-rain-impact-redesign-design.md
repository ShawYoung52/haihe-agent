# Design: Rain Impact River Logic Redesign (v6)

**Date:** 2026-07-17  
**Scope:** `hhlyqyxt-master/utils/rainfall_impact_geojson.py` and `haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py`  
**Data sources:** `public.haihe_river_directed_full_v6` (PostgreSQL) + `river_directed_v6.pkl` (NetworkX DiGraph)

## Problem Statement

The current rain-impact river algorithm produces:

- `river_name="未知"` when `full_v6.src_name` is empty.
- Duplicate / overlapping segments for the same `objectid`.
- Isolated segments that are geographically disconnected from the main river network.
- Broken downstream traces (jumps / gaps).
- Inconsistent downstream distance accounting.

## Root Cause

`build_v6_graph_and_import.py` (in `E:\tj\line`) builds the v6 data as follows:

1. Read `luanhe_merged_v6_split.shp` (258 LineString rows).
2. Create one NetworkX DiGraph edge per shapefile row → 258 edges, 252 nodes.
3. Import each edge into PostgreSQL as one row of `haihe_river_directed_full_v6` (geom stored as `MultiLineString` type but containing a single LineString) and one row of `haihe_river_directed_simple_v6`.

Therefore, **`full_v6` contains exactly one row per pkl edge**. The current code incorrectly treats `full_v6` as a collection of `objectid`-grouped geometries, using `ST_Dump(geom)` and `GROUP BY objectid`. This:

- Collapses multiple legitimate pkl segments that share an `objectid` into one `MultiLineString`.
- Loses the one-to-one mapping between pkl edges and full-v6 rows.
- Forces downstream matching to guess which part of the aggregated geometry corresponds to a pkl edge, causing misalignment, isolated segments, and duplicate-looking output.

## Design Principle

**Use the pkl directed graph as the authoritative topology. Each pkl edge is the atomic unit. `full_v6` is a geometry/attribute lookup table: one row maps to exactly one pkl edge by `(objectid, from_x, from_y, to_x, to_y)`.**

This eliminates the need for `ST_Dump`, `GROUP BY objectid`, spurious-match filtering, Shapely deduplication, and complex Luan-name fallbacks.

## Algorithm

### Step 1 — Normalize stations

Same as today: filter to `rain_24h >= rainfall_threshold_mm`, drop invalid lon/lat, sort by rainfall descending.

### Step 2 — Find affected pkl edges (direct buffer)

1. Query `full_v6` with a spatial filter: select rows whose geometry is within `station_buffer_km` of any暴雨 station. Use a simple `ST_DWithin` on the row geometry (no `ST_Dump`, no `GROUP BY`). Because `full_v6` has only 258 rows and each row is one pkl edge, this returns the candidate edges efficiently.
2. Match each returned row to its pkl edge using `(objectid, from_x, from_y, to_x, to_y)`.
3. For each matched pkl edge, compute the exact minimum distance to all stations:
   - If distance ≤ `direct_match_km` (default 10 km), mark as `is_direct_graph_edge=true`.
   - If distance ≤ `station_buffer_km` (default 30 km), add its downstream node as a downstream trace start.

This keeps the benefit of PostGIS spatial indexing while avoiding `ST_Dump` / `GROUP BY objectid`. The final distance classification happens on the pkl edge geometry, so it is consistent with downstream tracing.

### Step 3 — Downstream trace

Use Dijkstra from all candidate start nodes (the downstream node `v` of each direct-buffer edge):

```text
best[node] = 0 for all start nodes
priority queue initialized with start nodes
distances accumulated by edge.length
stop when distance >= downstream_km
```

For each visited edge, record:

- `edge_key`
- `objectid`
- `min_distance_km`: distance from start to edge’s upstream node `u`
- `keep_km`: min(edge.length, downstream_km - min_distance_km)
- `end_distance_km`: min_distance_km + keep_km
- `is_direct_graph_edge`: whether this edge was within `direct_match_km`
- `is_luan`: from pkl edge attribute

Edges are naturally de-duplicated by `edge_key` during traversal.

### Step 4 — Resolve geometry and names from full_v6

Build an in-memory lookup from pkl edge → full_v6 row using `(objectid, from_x, from_y, to_x, to_y)`.

For each direct / downstream edge:

1. Look up the matching full_v6 row.
2. If found, use `full_v6.geom` (the original LineString) as the feature geometry.
3. If not found, fall back to the pkl edge straight line and log a warning.
4. Determine `river_name`:
   - `full_v6.src_name` if non-empty and not `"未知"`
   - else `full_v6.river_name` if non-empty and not `"未知"`
   - else pkl edge `river_name` if non-empty and not `"未知"`
   - else if `is_luan=true`, apply Luan objectid mapping
   - else `"未知"`

Because each pkl edge has at most one full_v6 row, there is no duplication or fragmentation.

### Step 5 — Build GeoJSON

Produce one feature per selected pkl edge:

- `impact_type=direct_buffer` for edges within `direct_match_km`.
- `impact_type=downstream_50km` for traced downstream edges.

Features are already unique by `edge_key`. No additional geometry deduplication is required.

Sort output: direct_buffer first, then downstream_50km; within each group by `river_name` and `min_distance_km`.

## Data-Flow Diagram

```
暴雨站点列表
    │
    ├──► full_v6 空间过滤 (ST_DWithin, 30km) ──► 候选边行
    │                                              │
    │                                              ▼
    │                    pkl 图边匹配 (objectid + 端点)
    │                                              │
    ▼                                              ▼
pkl 有向图 ◄────────── 精确距离分类 ──► 直接边集合 + 下游起点集合
    │
    ▼
Dijkstra 下游追踪 ──► 下游边集合
    │
    ▼
full_v6 行 lookup (objectid + 端点) ──► 几何 + 名称
    │
    ▼
GeoJSON FeatureCollection
```

## Interfaces

Public functions keep their current signatures:

- `build_rainstorm_impact_thematic_map(...)`
- `build_rain24h_impact_river_geojson(...)`

Internal helpers to be added/refactored:

- `_edge_station_distance(edge, station) -> float`
- `_find_direct_edges(stations, graph, station_buffer_km, direct_match_km) -> tuple[dict[edge_key, edge], set[edge_key]]`
- `_trace_downstream_edges(start_nodes, graph, downstream_km, direct_keys) -> dict[edge_key, edge]`
- `_build_full_v6_lookup(cur, schema, table) -> dict[(objectid, from_x, from_y, to_x, to_y), row]`
- `_resolve_edge_geometry(edge, lookup) -> dict`
- `_build_river_features(direct_edges, downstream_edges, lookup, luan_mapping) -> list[dict]`

## Error Handling

| Scenario | Behavior |
|----------|----------|
| pkl graph cannot be loaded | Raise exception; return error result to caller |
| `full_v6` row count ≠ pkl edge count | Log warning; continue, using straight-line fallback for missing edges |
| Individual pkl edge missing from `full_v6` | Use straight-line fallback; log warning |
| No暴雨 stations meet threshold | Return empty result with informative message |
| No edges within station buffer | Return empty result |

## Testing Plan

### Unit tests (in `utils/tests/test_rainfall_impact_geojson.py`)

1. **Single station, single direct edge, no downstream**  
   Verify one `direct_buffer` feature and correct `river_name`.

2. **Two stations hit same edge**  
   Verify the edge appears exactly once and trigger stations include both.

3. **Downstream trace length accounting**  
   Mock a chain of edges with known lengths; verify `min_distance_km` and `end_distance_km`.

4. **Name fallback priority**  
   Test full_v6 src_name → river_name → pkl name → Luan mapping → "未知".

5. **Missing full_v6 row fallback**  
   Remove one row from lookup; verify output uses straight-line geometry.

### Integration tests

1. Run end-to-end with the internal rain sample (`E:\fsdownload\rain_impact_result.json`).
2. Assert:
   - `river_name="未知"` count is 0 (or only for edges genuinely without any name source).
   - No duplicate `edge_key` features.
   - No downstream feature with `match_distance_km > station_buffer_km`.
   - `downstream_start_stats` reflects direct-match vs buffer-only counts.

## Migration / Rollback

- The change is localized to `rainfall_impact_geojson.py`.
- `fixed_rainfall_impact_tool.py` only needs updates to `IMPACT_RULES` and any parameter forwarding; no algorithm change.
- The original `full_v6` table and `river_directed_v6.pkl` remain untouched.
- Rollback: revert `rainfall_impact_geojson.py` to the previous commit.

## Decisions

| Decision | Rationale |
|----------|-----------|
| Use pkl graph as atomic topology | One pkl edge = one full_v6 row; avoids objectid aggregation issues |
| Compute distances on pkl edges, with full_v6 spatial pre-filter | Keeps PostGIS spatial-index benefit while avoiding `ST_Dump` / `GROUP BY`; guarantees consistency |
| Match full_v6 by objectid + endpoints | Robust one-to-one lookup even if objectid is reused across segments |
| Keep `direct_match_km` and `station_buffer_km` separate | `station_buffer_km` defines downstream start scope; `direct_match_km` defines which starts are "real direct river segments" |
| Remove Shapely deduplication | No longer needed because edges are unique by key |
| Simplify Luan name mapping | Apply only after all other name sources fail; gated by `is_luan` |

## Out of Scope

- Regenerating `full_v6` or `river_directed_v6.pkl`.
- Changing the shapefile generation pipeline in `E:\tj\line`.
- Adding new parameters to the public API.
- Implementing `include_background` (remains a no-op as documented).
