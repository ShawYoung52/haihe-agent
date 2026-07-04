"""暴雨影响专题图实况入口。

这个模块给同事/业务服务直接调用：
- 不需要手工准备 CSV；
- 不需要手工输入站点；
- 内部查询海河流域站点实况降雨；
- 自动累计站点雨量，筛选达到暴雨阈值的站点；
- 复用专题图河网算法生成影响河流 GeoJSON 和制图样式。

推荐调用：

    from utils import create_rainstorm_impact_thematic_map_from_realtime

    result = create_rainstorm_impact_thematic_map_from_realtime(
        start_time="2026-06-30 00:00:00",
        end_time="2026-07-01 00:00:00",
        output_dir="./rainstorm_impact_output",
    )
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

try:
    from utils.rainstorm_impact_map_service import create_rainstorm_impact_thematic_map_from_station_records
except Exception:  # pragma: no cover
    from .rainstorm_impact_map_service import create_rainstorm_impact_thematic_map_from_station_records

DEFAULT_BASIN_CODES = "HHLY"
DEFAULT_DATA_CODE = "SURF_CHN_MUL_HOR"
DEFAULT_ELEMENTS = (
    "Station_Id_C,Station_Name,Lat,Lon,City,Cnty,Province,Town,Datetime,"
    "PRE_1h,PRE,PRE_24h"
)


def _music_config() -> dict[str, Any]:
    return {
        "service_ip": os.getenv("MUSIC_SERVICE_IP", "10.226.90.120"),
        "service_node_id": os.getenv("MUSIC_SERVICE_NODE_ID", "NMIC_MUSIC_CMADAAS"),
        "user_id": os.getenv("MUSIC_USER_ID", "BETJ_QXT_LYGXPT"),
        "password": os.getenv("MUSIC_PASSWORD", "Qxtly@2022ww"),
        "connect_timeout": float(os.getenv("MUSIC_CONNECT_TIMEOUT", "5")),
        "read_timeout": float(os.getenv("MUSIC_READ_TIMEOUT", os.getenv("MUSIC_TIMEOUT", "120"))),
        "max_retries": int(os.getenv("MUSIC_MAX_RETRIES", "2")),
        "retry_backoff_sec": float(os.getenv("MUSIC_RETRY_BACKOFF_SEC", "1.0")),
    }


def _build_sign(sign_params: dict[str, Any], password: str) -> str:
    items = {k: str(v) for k, v in sign_params.items() if v is not None and v != ""}
    items["pwd"] = password
    content = "&".join(f"{k}={items[k]}" for k in sorted(items.keys()))
    return hashlib.md5(content.encode("utf-8")).hexdigest().upper()


def _music_call_api(interface_id: str, **kwargs: Any) -> list[dict[str, Any]]:
    cfg = _music_config()
    params: dict[str, Any] = {
        "serviceNodeId": cfg["service_node_id"],
        "userId": cfg["user_id"],
        "dataFormat": "json",
        "interfaceId": interface_id,
        **kwargs,
    }
    query = {k: str(v) for k, v in params.items() if v is not None and v != ""}
    query["timestamp"] = str(int(time.time() * 1000))
    query["nonce"] = str(uuid.uuid4())
    query["sign"] = _build_sign(query, cfg["password"])
    url = f"http://{cfg['service_ip']}/music-ws/api?{urlencode(query, safe=':,[]()')}"

    last_exc: Exception | None = None
    for attempt in range(int(cfg["max_retries"]) + 1):
        try:
            resp = requests.get(url, timeout=(cfg["connect_timeout"], cfg["read_timeout"]))
            resp.raise_for_status()
            payload = resp.json()
            ds = payload.get("DS") if isinstance(payload, dict) else None
            if isinstance(ds, list):
                return ds
            return_code = str(payload.get("returnCode", "")) if isinstance(payload, dict) else ""
            message = str(payload.get("returnMessage", payload.get("message", ""))).lower() if isinstance(payload, dict) else ""
            if return_code == "-1" and "no record" in message:
                return []
            raise RuntimeError(f"MUSIC返回结构异常：{json.dumps(payload, ensure_ascii=False)[:500]}")
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt >= int(cfg["max_retries"]):
                break
            time.sleep(float(cfg["retry_backoff_sec"]) * (2 ** attempt) + random.uniform(0.0, 0.3))
    raise RuntimeError(f"MUSIC接口调用失败：{last_exc}")


def _parse_time(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(second=0, microsecond=0)
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y%m%d%H%M%S", "%Y%m%d%H%M"):
        try:
            return datetime.strptime(text, fmt).replace(second=0, microsecond=0)
        except Exception:
            continue
    raise ValueError(f"时间格式无法识别：{value!r}")


def _resolve_bjt_window(
    start_time: str | datetime | None,
    end_time: str | datetime | None,
    hours: int,
) -> tuple[datetime, datetime]:
    end_dt = _parse_time(end_time) or datetime.now().replace(minute=0, second=0, microsecond=0)
    start_dt = _parse_time(start_time) or (end_dt - timedelta(hours=max(int(hours or 24), 1)))
    if end_dt <= start_dt:
        raise ValueError("end_time 必须晚于 start_time")
    return start_dt, end_dt


def _to_api_time(dt: datetime, api_time_shift_hours: int) -> str:
    return (dt + timedelta(hours=int(api_time_shift_hours))).strftime("%Y%m%d%H%M%S")


def _time_range_param(start_dt: datetime, end_dt: datetime, api_time_shift_hours: int) -> str:
    start_s = _to_api_time(start_dt, api_time_shift_hours)
    end_s = _to_api_time(end_dt, api_time_shift_hours)
    return f"[{start_s},{end_s}]"


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, "", "None", "null", "-", "--"):
            return None
        number = float(value)
    except Exception:
        return None
    if not math.isfinite(number) or abs(number) >= 99999 or number < -9990:
        return None
    return number


def _first_value(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    lower = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        if key in row and row.get(key) not in (None, "", "None", "null"):
            return row.get(key)
        val = lower.get(key.lower())
        if val not in (None, "", "None", "null"):
            return val
    return None


def _rain_value(row: dict[str, Any]) -> float:
    # 优先按小时雨量累加；若接口直接给累计量，也兼容 PRE_24h/rain_24h。
    for key in ("PRE_1h", "pre_1h", "PRE", "pre", "rainfall", "rain"):
        val = _safe_float(row.get(key))
        if val is not None:
            return max(val, 0.0)
    return 0.0


def _aggregate_hourly_rows(rows: list[dict[str, Any]], *, start_dt: datetime, end_dt: datetime) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    totals: defaultdict[str, float] = defaultdict(float)
    obs_counts: defaultdict[str, int] = defaultdict(int)

    for row in rows or []:
        if not isinstance(row, dict):
            continue
        station_id = _first_value(row, ("Station_Id_C", "station_id", "stationId", "id"))
        lon = _safe_float(_first_value(row, ("Lon", "lon", "longitude", "lng")))
        lat = _safe_float(_first_value(row, ("Lat", "lat", "latitude")))
        if not station_id or lon is None or lat is None:
            continue
        sid = str(station_id).strip()
        if not sid:
            continue
        rain = _rain_value(row)
        totals[sid] += rain
        obs_counts[sid] += 1
        grouped.setdefault(sid, {
            "station_id": sid,
            "station_name": _first_value(row, ("Station_Name", "station_name", "stationName", "name")) or sid,
            "lon": lon,
            "lat": lat,
            "city": _first_value(row, ("City", "city")),
            "cnty": _first_value(row, ("Cnty", "cnty", "county")),
            "province": _first_value(row, ("Province", "province")),
            "town": _first_value(row, ("Town", "town")),
            "start_time": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        })

    records = []
    for sid, item in grouped.items():
        out = dict(item)
        out["rain_24h"] = round(float(totals[sid]), 3)
        out["obs_count"] = int(obs_counts[sid])
        records.append(out)
    records.sort(key=lambda x: float(x.get("rain_24h") or 0.0), reverse=True)
    return records


def fetch_haihe_rainfall_station_records_from_realtime(
    *,
    start_time: str | datetime | None = None,
    end_time: str | datetime | None = None,
    hours: int = 24,
    basin_codes: str = DEFAULT_BASIN_CODES,
    data_code: str = DEFAULT_DATA_CODE,
    elements: str = DEFAULT_ELEMENTS,
    api_time_shift_hours: int | None = None,
) -> dict[str, Any]:
    """查询海河流域实况站点降雨并累计成站点时段雨量。

    Args:
        start_time/end_time: 北京时间。未传时默认最近24小时。
        api_time_shift_hours: 北京时间转接口时次的偏移，默认读取 MUSIC_API_TIME_SHIFT_HOURS，未配置为 -8。
    """
    start_dt, end_dt = _resolve_bjt_window(start_time, end_time, hours)
    shift = int(os.getenv("MUSIC_API_TIME_SHIFT_HOURS", "-8")) if api_time_shift_hours is None else int(api_time_shift_hours)
    time_range = _time_range_param(start_dt, end_dt, shift)
    rows = _music_call_api(
        "getSurfEleInBasinByTimeRange",
        dataCode=data_code,
        elements=elements,
        timeRange=time_range,
        basinCodes=basin_codes,
    )
    records = _aggregate_hourly_rows(rows, start_dt=start_dt, end_dt=end_dt)
    return {
        "status": "ok",
        "basin_codes": basin_codes,
        "data_code": data_code,
        "interface_id": "getSurfEleInBasinByTimeRange",
        "time_range_bjt": {
            "start_time": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "time_range_api": time_range,
        "raw_row_count": len(rows or []),
        "station_count": len(records),
        "station_records": records,
    }


def create_rainstorm_impact_thematic_map_from_realtime(
    *,
    start_time: str | datetime | None = None,
    end_time: str | datetime | None = None,
    hours: int = 24,
    basin_codes: str = DEFAULT_BASIN_CODES,
    rain_threshold_mm: float = 50.0,
    output_dir: str | Path | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """正式实况入口：自动查询海河流域站点雨量并生成暴雨影响专题图。

    这个方法才是同事日常应该调的入口。调用方只传时间段，不传站点、不传CSV。
    内部会：
    1. 查海河流域站点小时降雨实况；
    2. 累计成站点时段雨量；
    3. 找出达到 rain_threshold_mm 的暴雨站；
    4. 生成直接影响河段、下游影响河段、触发站点 GeoJSON 和样式。
    """
    rainfall = fetch_haihe_rainfall_station_records_from_realtime(
        start_time=start_time,
        end_time=end_time,
        hours=hours,
        basin_codes=basin_codes,
        api_time_shift_hours=kwargs.pop("api_time_shift_hours", None),
    )
    records = rainfall["station_records"]
    result = create_rainstorm_impact_thematic_map_from_station_records(
        station_records=records,
        start_time=rainfall["time_range_bjt"]["start_time"],
        end_time=rainfall["time_range_bjt"]["end_time"],
        rain_threshold_mm=rain_threshold_mm,
        output_dir=output_dir,
        **kwargs,
    )
    heavy_records = [r for r in records if float(r.get("rain_24h") or 0.0) >= float(rain_threshold_mm)]
    result["rainfall_source"] = {
        "source": "MUSIC实况降雨接口",
        "interface_id": rainfall["interface_id"],
        "basin_codes": basin_codes,
        "data_code": rainfall["data_code"],
        "time_range_bjt": rainfall["time_range_bjt"],
        "time_range_api": rainfall["time_range_api"],
        "raw_row_count": rainfall["raw_row_count"],
        "station_count": rainfall["station_count"],
        "heavy_rain_station_count": len(heavy_records),
        "heavy_rain_stations": heavy_records,
    }
    result["summary"]["heavy_rain_station_count"] = len(heavy_records)
    result["summary"]["heavy_rain_stations"] = heavy_records[:50]
    return result


__all__ = [
    "fetch_haihe_rainfall_station_records_from_realtime",
    "create_rainstorm_impact_thematic_map_from_realtime",
]
