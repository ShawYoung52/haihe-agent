"""
Analyze rain_impact_result GeoJSON for data alignment issues:
- isolated segments
- repeated objectids
- direct segments without downstream continuation
- jumping/gap segments
"""
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from shapely.geometry import shape, LineString, MultiLineString, Point
from shapely.strtree import STRtree

GEOJSON_PATH = Path(r"E:\fsdownload\rain_impact_result.json.river.geojson")
TOLERANCE_DEG = 1e-5  # ~1m at equator, adjust as needed


def extract_lines(geom):
    if geom.geom_type == "LineString":
        return [geom]
    elif geom.geom_type == "MultiLineString":
        return list(geom.geoms)
    return []


def endpoint_key(pt, precision=6):
    return (round(pt.x, precision), round(pt.y, precision))


def main():
    if not GEOJSON_PATH.exists():
        print(f"File not found: {GEOJSON_PATH}")
        return

    with open(GEOJSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    features = data.get("features", [])
    print(f"Total features: {len(features)}")

    # classify
    typed = defaultdict(list)
    for i, feat in enumerate(features):
        props = feat.get("properties", {})
        seg_type = props.get("impact_type", "unknown")
        typed[seg_type].append((i, feat))

    for t, items in sorted(typed.items()):
        print(f"  {t}: {len(items)}")

    # parse geometries
    parsed = []
    for i, feat in enumerate(features):
        props = feat.get("properties", {})
        geom = shape(feat.get("geometry"))
        lines = extract_lines(geom)
        parsed.append({
            "idx": i,
            "props": props,
            "geom": geom,
            "lines": lines,
            "objectid": props.get("objectid"),
            "name": props.get("river_name", "unknown"),
            "seg_type": props.get("impact_type", "unknown"),
            "is_luan": props.get("is_luan", False),
            "start": endpoint_key(Point(lines[0].coords[0])) if lines else None,
            "end": endpoint_key(Point(lines[-1].coords[-1])) if lines else None,
        })

    # repeated objectids
    oid_counter = Counter(p["objectid"] for p in parsed if p["objectid"] is not None)
    repeated = {oid: cnt for oid, cnt in oid_counter.items() if cnt > 1}
    print(f"\nRepeated objectids: {len(repeated)}")
    for oid, cnt in sorted(repeated.items(), key=lambda x: -x[1])[:15]:
        names = sorted(set(p["name"] for p in parsed if p["objectid"] == oid))
        print(f"  objectid={oid}: {cnt} segments, names={names}")

    # endpoint index for connectivity
    endpoint_to_segments = defaultdict(list)
    for p in parsed:
        if p["start"]:
            endpoint_to_segments[p["start"]].append(p["idx"])
        if p["end"]:
            endpoint_to_segments[p["end"]].append(p["idx"])

    # find isolated segments (neither start nor end connects to another segment)
    isolated = []
    for p in parsed:
        start_conn = len(endpoint_to_segments.get(p["start"], [])) > 1 if p["start"] else False
        end_conn = len(endpoint_to_segments.get(p["end"], [])) > 1 if p["end"] else False
        if not start_conn and not end_conn:
            isolated.append(p)

    print(f"\nIsolated segments (no endpoint shared with another segment): {len(isolated)}")
    for p in isolated[:20]:
        print(f"  idx={p['idx']} objectid={p['objectid']} name={p['name']} type={p['seg_type']}")

    # direct segments without downstream continuation
    # downstream continuation: a downstream segment whose start touches this segment's end
    direct_idxs = {p["idx"] for p in parsed if p["seg_type"] == "direct_buffer"}
    downstream_idxs = {p["idx"] for p in parsed if p["seg_type"] == "downstream_50km"}

    direct_without_downstream = []
    for p in parsed:
        if p["seg_type"] != "direct_buffer":
            continue
        has_downstream = False
        if p["end"]:
            for j in endpoint_to_segments.get(p["end"], []):
                if j != p["idx"] and j in downstream_idxs:
                    has_downstream = True
                    break
        if not has_downstream:
            direct_without_downstream.append(p)

    print(f"\nDirect segments without downstream continuation: {len(direct_without_downstream)}")
    # group by objectid
    oid_no_down = defaultdict(list)
    for p in direct_without_downstream:
        oid_no_down[p["objectid"]].append(p)
    print(f"  Unique objectids affected: {len(oid_no_down)}")
    for oid, segs in sorted(oid_no_down.items(), key=lambda x: -len(x[1]))[:15]:
        names = sorted(set(s["name"] for s in segs))
        print(f"    objectid={oid}: {len(segs)} direct segs, names={names}")

    # downstream segments without upstream direct connection
    downstream_without_direct = []
    for p in parsed:
        if p["seg_type"] != "downstream_50km":
            continue
        has_upstream = False
        if p["start"]:
            for j in endpoint_to_segments.get(p["start"], []):
                if j != p["idx"] and j in direct_idxs:
                    has_upstream = True
                    break
        if not has_upstream:
            downstream_without_direct.append(p)

    print(f"\nDownstream segments without direct upstream: {len(downstream_without_direct)}")
    for p in downstream_without_direct[:20]:
        print(f"  idx={p['idx']} objectid={p['objectid']} name={p['name']}")

    # spatial tree to find nearest neighbors and gaps
    all_lines = [(p["idx"], line) for p in parsed for line in p["lines"]]
    geoms = [line for _, line in all_lines]
    tree = STRtree(geoms)
    idx_for_geom = {id(line): idx for idx, line in all_lines}

    # for each segment endpoint, find distance to nearest other line endpoint (not same segment)
    print("\nEndpoint gap analysis (nearest endpoint of another segment, within 0.01 deg):")
    gap_threshold = 0.001  # ~100m
    gap_count = 0
    gap_details = []
    for p in parsed:
        for ep_name, ep_key in [("start", p["start"]), ("end", p["end"])]:
            if not ep_key:
                continue
            pt = Point(ep_key)
            # query nearby lines
            candidate_idxs = tree.query(pt.buffer(gap_threshold * 10))
            min_dist = float("inf")
            for ci in candidate_idxs:
                cand_idx = idx_for_geom.get(id(geoms[ci]))
                if cand_idx is None or cand_idx == p["idx"]:
                    continue
                dist = pt.distance(geoms[ci])
                if 0 < dist < min_dist:
                    min_dist = dist
            if min_dist != float("inf"):
                gap_count += 1
                gap_details.append({
                    "idx": p["idx"],
                    "endpoint": ep_name,
                    "objectid": p["objectid"],
                    "name": p["name"],
                    "gap_deg": min_dist,
                })
                if gap_count <= 20:
                    print(f"  idx={p['idx']} {ep_name} objectid={p['objectid']} name={p['name']} gap={min_dist:.6f} deg")

    print(f"\nTotal endpoint gaps >0 and <{gap_threshold*10} deg: {gap_count}")

    # save summary
    summary = {
        "total_features": len(features),
        "by_type": {k: len(v) for k, v in typed.items()},
        "repeated_objectids": len(repeated),
        "top_repeated": [{"objectid": oid, "count": cnt} for oid, cnt in sorted(repeated.items(), key=lambda x: -x[1])[:20]],
        "isolated_segments": len(isolated),
        "direct_without_downstream_count": len(direct_without_downstream),
        "direct_without_downstream_objectids": len(oid_no_down),
        "downstream_without_direct_count": len(downstream_without_direct),
        "endpoint_gaps": gap_count,
    }
    out_path = GEOJSON_PATH.with_suffix(".analysis.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\nSummary saved to: {out_path}")


if __name__ == "__main__":
    main()
