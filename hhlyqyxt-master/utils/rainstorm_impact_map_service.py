"""暴雨影响河流专题图服务。

保留两个职责：
1. 生成服务：按时间段生成专题图文件，并返回 HTTP 地址；
2. 入库服务：从已生成的 rainstorm_impact_map.json 地址中提取入库需要的 JSON 地址。
"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import os
import tempfile
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

try:
    from .rainfall_impact_geojson import build_rain24h_impact_river_geojson
except Exception:  # pragma: no cover
    from rainfall_impact_geojson import build_rain24h_impact_river_geojson

BASIN_CODE_HAIHE = "HHLY"
MUSIC_DATA_CODE_HOURLY = "SURF_CHN_MUL_HOR"
MUSIC_RAIN_ELEMENTS = "Station_Id_C,Station_Name,Lat,Lon,City,Cnty,Province,Town,Datetime,PRE_1h,PRE"
CORE_INPUT_FIELDS = ["Station_Id_C", "Datetime", "PRE", "Lon", "Lat", "Station_Name", "City", "Cnty", "Province", "Town"]
OUTPUT_DIR_ENV = "RAINSTORM_IMPACT_OUTPUT_DIR"
PUBLIC_BASE_URL_ENV = "RAINSTORM_IMPACT_PUBLIC_BASE_URL"

RAINSTORM_IMPACT_STYLE = {
    "style_name": "rainstorm_impact_map_v1",
    "title": "暴雨影响河流专题图",
    "crs": "EPSG:4326",
    "layer_order": ["river_background", "downstream_50km", "direct_buffer", "impact_stations", "labels"],
    "layers": {
        "downstream_50km": {"name": "下游影响河段", "color": "#FB8C00", "width": 3, "opacity": 0.9, "filter": {"property": "impact_type", "equals": "downstream_50km"}},
        "direct_buffer": {"name": "直接影响河段", "color": "#E53935", "width": 4, "opacity": 0.95, "filter": {"property": "impact_type", "equals": "direct_buffer"}},
        "impact_stations": {"name": "暴雨触发站", "color": "#1E88E5", "strokeColor": "#FFFFFF", "radius": 6, "opacity": 0.95},
    },
    "legend": [
        {"label": "直接影响河段", "type": "line", "color": "#E53935", "width": 4},
        {"label": "下游影响河段", "type": "line", "color": "#FB8C00", "width": 3},
        {"label": "暴雨触发站", "type": "circle", "color": "#1E88E5", "radius": 6},
    ],
}


def create_rainstorm_impact_map(**kwargs: Any) -> dict[str, Any]:
    """生成暴雨影响河流专题图文件包，并返回 rainstorm_impact_map.json 的 HTTP 地址。"""
    context = _build_rainstorm_context(**kwargs)
    return _write_product_response(context, product_type="thematic_map", main_file="map_package_json")


def create_rainstorm_impact_geojson_file(**kwargs: Any) -> dict[str, Any]:
    """生成暴雨影响河流 GeoJSON 文件，并返回 river_impact.geojson 的 HTTP 地址。"""
    context = _build_rainstorm_context(**kwargs)
    return _write_product_response(context, product_type="geojson", main_file="river_impact_geojson")


def get_rainstorm_impact_json_urls_from_url(url: str, *, timeout: int = 30) -> dict[str, Any]:
    """从 rainstorm_impact_map.json 地址中提取专题图、影响河流和暴雨站点 JSON 地址。"""
    map_package = _fetch_map_package(url, timeout=timeout)
    layers = _required_dict(map_package, "layers")
    rivers_layer = _required_dict(layers, "rivers")
    stations_layer = _required_dict(layers, "stations")
    return {
        "status": "ok",
        "map_json_url": url.strip(),
        "river_json_url": _required_text(rivers_layer, "url"),
        "station_json_url": _required_text(stations_layer, "url"),
        "summary": map_package.get("summary") or {},
        "rainfall_source": map_package.get("rainfall_source") or {},
    }


def get_rainstorm_impact_map_style() -> dict[str, Any]:
    """返回专题图样式配置。"""
    return copy.deepcopy(RAINSTORM_IMPACT_STYLE)


def _build_rainstorm_context(
    *,
    start_time: str | datetime | None = None,
    end_time: str | datetime | None = None,
    hours: int = 24,
    rain_threshold_mm: float = 50.0,
    basin_codes: str = BASIN_CODE_HAIHE,
    output_dir: str | Path | None = None,
    public_base_url: str | None = None,
    api_time_shift_hours: int | None = None,
    station_buffer_km: float = 30.0,
    downstream_km: float = 50.0,
    direct_match_km: float = 3.0,
    river_table: str = "haihe_river_directed_full_v5",
    schema: str = "public",
    graph_path: str | Path | None = None,
) -> dict[str, Any]:
    start_dt, end_dt = _resolve_time_window(start_time, end_time, hours)
    rows = _query_haihe_rainfall_rows(start_dt, end_dt, basin_codes, api_time_shift_hours)
    core = _build_impact_core(rows, rain_threshold_mm, station_buffer_km, downstream_km, river_table, schema, graph_path, direct_match_km)
    stations = _aggregate_station_rainfall(rows)
    heavy_stations = [item for item in stations if item["rain_24h"] >= float(rain_threshold_mm)]
    return {
        "core": core,
        "output_root": _resolve_output_root(output_dir),
        "public_base_url": public_base_url,
        "job_id": _build_job_id(start_dt, end_dt),
        "summary": _build_summary(core),
        "rainfall_source": {
            "interface_id": "getSurfEleInBasinByTimeRange",
            "data_code": MUSIC_DATA_CODE_HOURLY,
            "basin_codes": basin_codes,
            "time_range_bjt": _format_time_range(start_dt, end_dt),
            "station_count": len(stations),
            "heavy_rain_station_count": len(heavy_stations),
            "heavy_rain_stations": heavy_stations,
        },
    }


def _build_impact_core(
    rows: list[dict[str, Any]],
    rain_threshold_mm: float,
    station_buffer_km: float,
    downstream_km: float,
    river_table: str,
    schema: str,
    graph_path: str | Path | None,
    direct_match_km: float,
) -> dict[str, Any]:
    core_input_path = _write_core_algorithm_input(rows)
    try:
        return build_rain24h_impact_river_geojson(
            csv_path=str(core_input_path),
            rain_threshold_mm=rain_threshold_mm,
            station_buffer_km=station_buffer_km,
            downstream_km=downstream_km,
            river_table=river_table,
            schema=schema,
            graph_path=graph_path,
            direct_match_km=direct_match_km,
        )
    finally:
        core_input_path.unlink(missing_ok=True)


def _write_product_response(context: dict[str, Any], *, product_type: str, main_file: str) -> dict[str, Any]:
    paths = _write_geojson_files(context)
    urls = _to_http_urls(paths, context["output_root"], context["public_base_url"])
    if product_type == "thematic_map":
        paths["map_package_json"] = _write_map_package(context, urls)
        urls = _to_http_urls(paths, context["output_root"], context["public_base_url"])
    return {
        "status": "ok",
        "product_type": product_type,
        "summary": context["summary"],
        "rainfall_source": context["rainfall_source"],
        "geojson_url": urls["river_impact_geojson"],
        "map_package_url": urls.get("map_package_json"),
        "delivery": {
            "address_type": "http",
            "main_file": {"name": main_file, "path": paths[main_file], "address": urls[main_file]},
            "files": urls,
        },
    }


def _write_geojson_files(context: dict[str, Any]) -> dict[str, str]:
    core = context["core"]
    output_dir = context["output_root"] / context["job_id"]
    return {
        "river_impact_geojson": _write_json(output_dir / "river_impact.geojson", core.get("river_geojson") or _empty_feature_collection()),
        "impact_stations_geojson": _write_json(output_dir / "impact_stations.geojson", core.get("station_geojson") or _empty_feature_collection()),
        "summary_json": _write_json(output_dir / "summary.json", context["summary"]),
        "style_json": _write_json(output_dir / "style.json", get_rainstorm_impact_map_style()),
    }


def _write_map_package(context: dict[str, Any], urls: dict[str, str]) -> str:
    package = {
        "product_type": "thematic_map",
        "title": RAINSTORM_IMPACT_STYLE["title"],
        "summary": context["summary"],
        "rainfall_source": context["rainfall_source"],
        "layers": {
            "rivers": {"url": urls["river_impact_geojson"], "style": RAINSTORM_IMPACT_STYLE["layers"]},
            "stations": {"url": urls["impact_stations_geojson"], "style": RAINSTORM_IMPACT_STYLE["layers"]["impact_stations"]},
        },
        "legend": RAINSTORM_IMPACT_STYLE["legend"],
        "files": urls,
    }
    return _write_json(context["output_root"] / context["job_id"] / "rainstorm_impact_map.json", package)


def _query_haihe_rainfall_rows(start_dt: datetime, end_dt: datetime, basin_codes: str, api_time_shift_hours: int | None) -> list[dict[str, Any]]:
    shift = int(os.getenv("MUSIC_API_TIME_SHIFT_HOURS", "-8")) if api_time_shift_hours is None else int(api_time_shift_hours)
    return _music_call(
        "getSurfEleInBasinByTimeRange",
        dataCode=MUSIC_DATA_CODE_HOURLY,
        elements=MUSIC_RAIN_ELEMENTS,
        timeRange=_music_time_range(start_dt, end_dt, shift),
        basinCodes=basin_codes,
    )


def _music_call(interface_id: str, **params: Any) -> list[dict[str, Any]]:
    config = _music_config()
    query = _music_query(interface_id, config, params)
    url = f"http://{config['service_ip']}/music-ws/api?{urlencode(query, safe=':,[]()')}"
    try:
        response = requests.get(url, timeout=(config["connect_timeout"], config["read_timeout"]))
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"MUSIC接口请求失败：{interface_id}") from exc
    except ValueError as exc:
        raise RuntimeError(f"MUSIC接口返回非JSON：{interface_id}") from exc
    data = payload.get("DS") if isinstance(payload, dict) else None
    if isinstance(data, list):
        return data
    if _is_music_no_record(payload):
        return []
    raise RuntimeError(f"MUSIC接口返回结构异常：{json.dumps(payload, ensure_ascii=False)[:500]}")


def _fetch_map_package(url: str, *, timeout: int) -> dict[str, Any]:
    if not isinstance(url, str) or not url.strip().startswith(("http://", "https://")):
        raise ValueError("url 必须是 rainstorm_impact_map.json 的 HTTP 地址")
    try:
        response = requests.get(url.strip(), timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"请求 rainstorm_impact_map.json 失败：{url}") from exc
    except ValueError as exc:
        raise RuntimeError("rainstorm_impact_map.json 返回内容不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("rainstorm_impact_map.json 顶层必须是 JSON 对象")
    return payload


def _music_config() -> dict[str, Any]:
    password = os.getenv("MUSIC_PASSWORD")
    if not password:
        raise RuntimeError("缺少 MUSIC_PASSWORD 环境变量，无法请求 MUSIC 接口")
    return {
        "service_ip": os.getenv("MUSIC_SERVICE_IP", "10.226.90.120"),
        "service_node_id": os.getenv("MUSIC_SERVICE_NODE_ID", "NMIC_MUSIC_CMADAAS"),
        "user_id": os.getenv("MUSIC_USER_ID", "BETJ_QXT_LYGXPT"),
        "password": password,
        "connect_timeout": float(os.getenv("MUSIC_CONNECT_TIMEOUT", "5")),
        "read_timeout": float(os.getenv("MUSIC_READ_TIMEOUT", os.getenv("MUSIC_TIMEOUT", "120"))),
    }


def _music_query(interface_id: str, config: dict[str, Any], params: dict[str, Any]) -> dict[str, str]:
    query = {
        "serviceNodeId": config["service_node_id"],
        "userId": config["user_id"],
        "dataFormat": "json",
        "interfaceId": interface_id,
        "timestamp": str(int(time.time() * 1000)),
        "nonce": str(uuid.uuid4()),
        **{key: value for key, value in params.items() if value not in (None, "")},
    }
    query["sign"] = _music_sign(query, config["password"])
    return {key: str(value) for key, value in query.items()}


def _music_sign(query: dict[str, Any], password: str) -> str:
    values = {key: str(value) for key, value in query.items() if value not in (None, "")}
    values["pwd"] = password
    raw = "&".join(f"{key}={values[key]}" for key in sorted(values))
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def _is_music_no_record(payload: Any) -> bool:
    return isinstance(payload, dict) and str(payload.get("returnCode")) == "-1" and "no record" in str(payload.get("returnMessage", "")).lower()


def _resolve_time_window(start_time: str | datetime | None, end_time: str | datetime | None, hours: int) -> tuple[datetime, datetime]:
    end_dt = _parse_time(end_time) or datetime.now().replace(minute=0, second=0, microsecond=0)
    start_dt = _parse_time(start_time) or end_dt - timedelta(hours=max(int(hours or 24), 1))
    if end_dt <= start_dt:
        raise ValueError("end_time 必须晚于 start_time")
    return start_dt, end_dt


def _parse_time(value: str | datetime | None) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.replace(second=0, microsecond=0)
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y%m%d%H%M%S", "%Y%m%d%H%M"):
        try:
            return datetime.strptime(text, fmt).replace(second=0, microsecond=0)
        except ValueError:
            continue
    raise ValueError(f"时间格式无法识别：{value!r}")


def _music_time_range(start_dt: datetime, end_dt: datetime, shift_hours: int) -> str:
    start = (start_dt + timedelta(hours=shift_hours)).strftime("%Y%m%d%H%M%S")
    end = (end_dt + timedelta(hours=shift_hours)).strftime("%Y%m%d%H%M%S")
    return f"[{start},{end}]"


def _format_time_range(start_dt: datetime, end_dt: datetime) -> dict[str, str]:
    return {"start_time": start_dt.strftime("%Y-%m-%d %H:%M:%S"), "end_time": end_dt.strftime("%Y-%m-%d %H:%M:%S")}


def _aggregate_station_rainfall(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[str, float] = defaultdict(float)
    stations: dict[str, dict[str, Any]] = {}
    for row in rows:
        station_id = str(_first(row, "Station_Id_C", "station_id") or "").strip()
        lon = _to_float(_first(row, "Lon", "lon"))
        lat = _to_float(_first(row, "Lat", "lat"))
        rain = _to_float(_first(row, "PRE_1h", "PRE", "rainfall"))
        if not station_id or lon is None or lat is None or rain is None:
            continue
        totals[station_id] += max(rain, 0.0)
        stations.setdefault(station_id, {
            "station_id": station_id,
            "station_name": _first(row, "Station_Name", "station_name") or station_id,
            "lon": lon,
            "lat": lat,
            "city": _first(row, "City", "city"),
            "cnty": _first(row, "Cnty", "cnty"),
        })
    for station_id, station in stations.items():
        station["rain_24h"] = round(totals[station_id], 3)
    return sorted(stations.values(), key=lambda item: item["rain_24h"], reverse=True)


def _write_core_algorithm_input(rows: list[dict[str, Any]]) -> Path:
    temp_file = tempfile.NamedTemporaryFile("w", suffix=".csv", encoding="utf-8", newline="", delete=False)
    path = Path(temp_file.name)
    with temp_file:
        writer = csv.DictWriter(temp_file, fieldnames=CORE_INPUT_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(_to_core_input_row(row))
    return path


def _to_core_input_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "Station_Id_C": _first(row, "Station_Id_C", "station_id"),
        "Datetime": _first(row, "Datetime", "datetime", "time"),
        "PRE": _to_float(_first(row, "PRE_1h", "PRE", "rainfall")) or 0.0,
        "Lon": _first(row, "Lon", "lon"),
        "Lat": _first(row, "Lat", "lat"),
        "Station_Name": _first(row, "Station_Name", "station_name"),
        "City": _first(row, "City", "city"),
        "Cnty": _first(row, "Cnty", "cnty"),
        "Province": _first(row, "Province", "province"),
        "Town": _first(row, "Town", "town"),
    }


def _build_summary(core: dict[str, Any]) -> dict[str, Any]:
    rivers = core.get("river_geojson") or _empty_feature_collection()
    stations = core.get("station_geojson") or _empty_feature_collection()
    river_names = sorted({
        str((feature.get("properties") or {}).get("river_name") or "").strip()
        for feature in rivers.get("features", [])
        if isinstance(feature, dict) and str((feature.get("properties") or {}).get("river_name") or "").strip()
    })
    station_summary = core.get("station_summary") or {}
    return {
        "affected_river_count": len(river_names),
        "affected_rivers": river_names,
        "impact_station_count": station_summary.get("impact_station_count", 0),
        "max_rain_24h": station_summary.get("max_rain_24h", 0),
        "river_feature_count": len(rivers.get("features", [])),
        "station_feature_count": len(stations.get("features", [])),
    }


def _to_http_urls(paths: dict[str, str], output_root: Path, public_base_url: str | None) -> dict[str, str]:
    base_url = _public_base_url(public_base_url)
    return {name: f"{base_url}/{_relative_path(Path(path), output_root).as_posix()}" for name, path in paths.items()}


def _public_base_url(value: str | None) -> str:
    base_url = (value or os.getenv(PUBLIC_BASE_URL_ENV, "")).strip().rstrip("/")
    if not base_url:
        raise RuntimeError(f"缺少HTTP发布地址：请传 public_base_url 或配置 {PUBLIC_BASE_URL_ENV}")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("public_base_url 必须以 http:// 或 https:// 开头")
    return base_url


def _resolve_output_root(output_dir: str | Path | None) -> Path:
    return Path(output_dir or os.getenv(OUTPUT_DIR_ENV) or Path.cwd() / "rainstorm_impact_output")


def _relative_path(path: Path, root: Path) -> Path:
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        return Path(path.name)


def _build_job_id(start_dt: datetime, end_dt: datetime) -> str:
    return f"rainstorm_impact_{start_dt:%Y%m%d%H%M}_{end_dt:%Y%m%d%H%M}_{uuid.uuid4().hex[:8]}"


def _write_json(path: Path, data: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _empty_feature_collection() -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": []}


def _required_dict(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"rainstorm_impact_map.json 缺少对象字段：{key}")
    return value


def _required_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"rainstorm_impact_map.json 缺少文本字段：{key}")
    return value.strip()


def _first(row: dict[str, Any], *keys: str) -> Any:
    lower = {str(key).lower(): value for key, value in row.items()}
    for key in keys:
        value = row.get(key, lower.get(key.lower()))
        if value not in (None, "", "None", "null"):
            return value
    return None


def _to_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or abs(number) >= 99999 or number < -9990:
        return None
    return number


__all__ = [
    "create_rainstorm_impact_geojson_file",
    "create_rainstorm_impact_map",
    "get_rainstorm_impact_json_urls_from_url",
    "get_rainstorm_impact_map_style",
]
