from __future__ import annotations

from typing import Any, Dict, List, Optional

from constants import DEFAULT_BASIN_CODES, DEFAULT_OBS_ELEMENTS
from haihe_mcp_tools import (
    MusicClient,
    deduplicate_latest_records,
    filter_records_by_station_levels,
    normalize_station_level,
    safe_float,
    station_id_of,
)

ALLOWED_SORT_FIELDS = frozenset({"PRE_1h", "PRE_3h", "PRE_6h", "PRE_12h", "PRE_24h", "PRE"})


def _station_display(rec: Dict[str, Any], station_id: str) -> str:
    name = (rec.get("Station_Name") or rec.get("Station_Name_C") or "").strip()
    if name:
        return name
    city = (rec.get("City") or "").strip()
    cnty = (rec.get("Cnty") or "").strip()
    if city and cnty:
        return f"{city}{cnty}({station_id})"
    if city:
        return f"{city}({station_id})"
    return station_id or "-"


def station_rainfall_ranking(
    times: str,
    basin_codes: str = DEFAULT_BASIN_CODES,
    sort_by: str = "PRE_24h",
    allowed_station_levels: Optional[str] = "11,12,13,16",
    limit: int = 200,
    min_mm: float = 0.0,
) -> Dict[str, Any]:
    """
    按指定降水要素对流域内站点排序（降序），供前端「排序 / 站点 / 降水量」表格使用。
    """
    if not times or not str(times).strip():
        raise ValueError("times 不能为空，例如 20250723080000")

    field = (sort_by or "PRE_24h").strip()
    if field not in ALLOWED_SORT_FIELDS:
        raise ValueError(
            f"sort_by 必须是 {', '.join(sorted(ALLOWED_SORT_FIELDS))} 之一，收到: {sort_by!r}"
        )

    limit = max(1, min(int(limit), 2000))

    levels_list: Optional[List[str]] = None
    if allowed_station_levels:
        levels_list = [x.strip() for x in str(allowed_station_levels).split(",") if x.strip()]

    client = MusicClient()
    records = client.get_surf_ele_in_basin_by_time(
        basin_codes=str(basin_codes or DEFAULT_BASIN_CODES).strip() or DEFAULT_BASIN_CODES,
        times=str(times).strip(),
        elements=DEFAULT_OBS_ELEMENTS,
    )

    records = filter_records_by_station_levels(records, levels_list)
    records = deduplicate_latest_records(records)

    rows: List[Dict[str, Any]] = []
    for r in records:
        sid = station_id_of(r)
        if not sid:
            continue
        mm = safe_float(r.get(field))
        if mm < min_mm:
            continue
        rows.append(
            {
                "_mm": mm,
                "station_id": sid,
                "station": _station_display(r, sid),
                "station_level": normalize_station_level(r.get("Station_levl")),
                "lat": safe_float(r.get("Lat")),
                "lon": safe_float(r.get("Lon")),
                "city": r.get("City"),
                "cnty": r.get("Cnty"),
            }
        )

    rows.sort(key=lambda x: x["_mm"], reverse=True)
    rows = rows[:limit]

    out_list: List[Dict[str, Any]] = []
    for i, row in enumerate(rows, start=1):
        mm = row.pop("_mm")
        out_list.append(
            {
                "rank": i,
                "station": row["station"],
                "station_id": row["station_id"],
                "rainfall_mm": round(mm, 2),
                "station_level": row.get("station_level"),
                "lat": row.get("lat"),
                "lon": row.get("lon"),
                "city": row.get("city"),
                "cnty": row.get("cnty"),
            }
        )

    return {
        "times": str(times).strip(),
        "basin_codes": str(basin_codes or "HHLY").strip() or "HHLY",
        "sort_by": field,
        "count": len(out_list),
        "columns": [
            {"key": "rank", "label": "排序"},
            {"key": "station", "label": "站点"},
            {"key": "rainfall_mm", "label": "降水量"},
        ],
        "list": out_list,
    }
