"""点位/区域历史实况查询服务。

业务场景：用户问“8月10号某某地方天气怎么样”这类历史日期天气时，
滚动预报仅覆盖未来时效，本模块改查天擎逐小时站点实况（SURF_CHN_MUL_HOR）
指定历史时段的最近观测站数据，聚合成与滚动预报 ``periods`` 兼容的逐日行，
供决策天气与普通问答直接复用表格/回答组装。

聚合口径：按北京时 02/08/14/20 四个气象观测整点取最近站逐小时实况，
日最高/最低气温取观测时次极值，累计降水取观测时次 1 小时降水量之和，
风况取观测时次最大平均风，天气现象由实测降水/风/能见度代码确定性推导（零编造）。
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, time, timedelta
from typing import Any

from fastmcp import FastMCP

from constants import DEFAULT_BASIN_CODES, DEFAULT_OBS_ELEMENTS
from custom_tools._ttl_cache import make_ttl_cache
from custom_tools.poi_nearest_observation_tool import (
    FULL_OBS_ELEMENTS,
    OBS_ELEMENT_CANDIDATES,
    _clean_observation,
    _nearest_station,
    _pick_first_poi,
    _query_basin_rows,
    _query_region_rows,
    _safe_float,
    _station_id,
    _station_name,
    _valid_station_rows,
)
from haihe_mcp_tools import MusicClient, MusicConfig, _wind_direction_to_text, _wind_speed_to_level


logger = logging.getLogger(__name__)

HOURLY_DATA_CODE = "SURF_CHN_MUL_HOR"
TIANJIN_ADMIN_CODE = "120000"
# 北京时气象观测整点（02/08/14/20 时），该时次站点数据最全。
SYNOPSIS_HOURS = (2, 8, 14, 20)
MAX_HISTORICAL_DAYS = 10


def _error_payload(message: str, debug_reason: str = "") -> dict:
    return {
        "status": "error",
        "query_type": "historical_observation",
        "message": message,
        "debug_reason": str(debug_reason)[:300] if debug_reason else "",
    }


def _parse_bjt_time(value: str) -> datetime | None:
    """解析北京时时间；接受日期或日期+时刻，未给出时刻默认当日 00:00。"""
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y%m%d%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _time_s_for_api(bjt_time: datetime) -> str:
    """北京时 → 天擎 UTC 时次（北京时间 -8 小时）。"""
    return (bjt_time - timedelta(hours=8)).strftime("%Y%m%d%H%M%S")


def _query_hour_valid_rows(
    client: MusicClient,
    bjt_time: datetime,
    basin_codes: str,
    admin_code: str,
) -> tuple[list[dict] | None, str]:
    """查询某一北京时整点的有效站点行（region→basin、full→basic 逐级兜底）。"""
    time_s = _time_s_for_api(bjt_time)
    query_modes = (
        ("region", lambda t, e: _query_region_rows(client, t, e, admin_code)),
        ("basin", lambda t, e: _query_basin_rows(client, t, e, basin_codes)),
    )
    last_error = ""
    for mode, query_func in query_modes:
        for elements in OBS_ELEMENT_CANDIDATES:
            try:
                rows = query_func(time_s, elements)
            except Exception as exc:
                last_error = f"{mode}@{time_s}: {str(exc)[:120]}"
                logger.warning("[historical_weather] %s", last_error)
                continue
            valid = _valid_station_rows(rows)
            if valid:
                return valid, ""
    return None, last_error


def _max_wind(hour_obs: list[tuple[datetime, dict]]) -> tuple[float | None, float | None]:
    best_speed: float | None = None
    best_dir: float | None = None
    for _, obs in hour_obs:
        speed = obs.get("2分钟平均风速(m/s)")
        if speed is None:
            continue
        if best_speed is None or speed > best_speed:
            best_speed = float(speed)
            direction = obs.get("2分钟平均风向(°)")
            best_dir = float(direction) if direction is not None else None
    return best_speed, best_dir


def _derive_weather_text(total_rain: float | None, hour_obs: list[tuple[datetime, dict]]) -> str:
    """由实测数据确定性推导天气现象：优先降雨量级，其次低能见度，其次大风。"""
    if total_rain is not None and total_rain > 0.1:
        if total_rain >= 50:
            return "暴雨"
        if total_rain >= 25:
            return "大雨"
        if total_rain >= 10:
            return "中雨"
        return "小雨"
    vis_values = [
        obs.get("1分钟水平能见度(m)")
        for _, obs in hour_obs
        if obs.get("1分钟水平能见度(m)") is not None
    ]
    if vis_values and min(vis_values) < 1000:
        return "雾/低能见度"
    max_speed, _ = _max_wind(hour_obs)
    if max_speed is not None and (_wind_speed_to_level(max_speed) or 0) >= 8:
        return "大风"
    # total_rain 为 None = 该站无降水观测（无雨量要素/缺报），不得编造“无降雨”
    return "无降雨" if total_rain == 0.0 else "无降水数据"


def _build_historical_day_row(day: date, hour_obs: list[tuple[datetime, dict]], point_name: str) -> dict:
    temps = [obs.get("气温(℃)") for _, obs in hour_obs if obs.get("气温(℃)") is not None]
    rains = [
        obs.get("1小时降水量(mm)")
        for _, obs in hour_obs
        if obs.get("1小时降水量(mm)") is not None
    ]
    # rains 为空 → total_rain=None（无降水观测），下游不会据此断言“无降雨”
    total_rain = round(sum(float(value) for value in rains), 1) if rains else None
    weather = _derive_weather_text(total_rain, hour_obs)
    max_speed, max_dir = _max_wind(hour_obs)
    level = _wind_speed_to_level(max_speed)
    wind_text = ""
    if max_dir is not None:
        direction = _wind_direction_to_text(max_dir)
        wind_text = f"{direction} {level}级" if level is not None else direction
    elif level is not None:
        wind_text = f"{level}级"
    vis_values = [
        obs.get("1分钟水平能见度(m)")
        for _, obs in hour_obs
        if obs.get("1分钟水平能见度(m)") is not None
    ]
    min_vis_m = min(vis_values) if vis_values else None
    return {
        "region": point_name or "该点位",
        "region_display": point_name or "该点位",
        "start_time": f"{day.isoformat()} 00:00",
        "end_time": f"{(day + timedelta(days=1)).isoformat()} 00:00",
        "period_label": f"{day.month}月{day.day}日",
        "weather": weather,
        "tmax": round(max(temps), 1) if temps else None,
        "tmin": round(min(temps), 1) if temps else None,
        "EDA": wind_text,
        "wind": wind_text,
        "rain_1h": total_rain,
        "rainfall_mm": total_rain,
        "TP1H": total_rain,
        "visibility_min_km": round(min_vis_m / 1000, 1) if min_vis_m is not None else None,
        "visibility_min_m": min_vis_m,
        "sampled_hours": len(hour_obs),
    }


def _no_data_payload(lon, lat, point_name, start, end) -> dict:
    return {
        "status": "no_data",
        "query_type": "historical_observation",
        "query_mode": "historical_obs",
        "lon": lon,
        "lat": lat,
        "point_name": point_name or "",
        "start_time": start,
        "end_time": end,
        "message": (
            f"未查询到 {start} 至 {end} 的历史实况数据，该时段内无可用自动站观测。"
        ),
    }


def _query_historical_obs_core(
    lon: float | None = None,
    lat: float | None = None,
    start_time: str = "",
    end_time: str = "",
    point_name: str = "",
    keyword: str = "",
    max_distance_km: float = 80.0,
    basin_codes: str = DEFAULT_BASIN_CODES,
    admin_code: str = TIANJIN_ADMIN_CODE,
) -> dict:
    """查询点位/区域指定历史时段的最远站实况并聚合为逐日行。"""
    if not (end_time or "").strip():
        start = _parse_bjt_time(start_time)
        if start is None:
            return _error_payload(
                "时间参数不合法，请提供 start_time（如 2026-08-10 或 2026-08-10 00:00）。",
                f"start_time={start_time!r}",
            )
        end_time = (start + timedelta(days=1)).strftime("%Y-%m-%d 00:00")
    poi = None
    if lon is not None and lat is not None:
        poi = {
            "longitude": float(lon),
            "latitude": float(lat),
            "name": point_name or keyword or "指定点位",
        }
    else:
        try:
            resolved = _pick_first_poi(keyword) if keyword else None
        except Exception as exc:
            return _error_payload(f"点位检索失败：{exc}")
        if not resolved:
            return _error_payload("未解析到有效点位经纬度，请提供查询名称或直接传 lon/lat。")
        poi = resolved
    point_name = point_name or str(poi.get("name") or keyword or "指定点位")

    start = _parse_bjt_time(start_time)
    end = _parse_bjt_time(end_time)
    if start is None or end is None:
        return _error_payload(
            "时间参数不合法，请提供 start_time/end_time（如 2026-08-10 或 2026-08-10 00:00）。",
            f"start_time={start_time!r}, end_time={end_time!r}",
        )
    if end <= start:
        return _error_payload("结束时间必须晚于开始时间。", f"start={start_time}, end={end_time}")
    if (end - start).days > MAX_HISTORICAL_DAYS:
        return _error_payload(f"历史实况单次查询最多覆盖 {MAX_HISTORICAL_DAYS} 天。", f"range={(end - start).days}天")

    try:
        client = MusicClient(MusicConfig())
    except Exception as exc:
        return _error_payload("历史实况服务不可用，请稍后重试。", str(exc)[:200])

    rows: list[dict] = []
    anchor = None
    anchor_reason = ""
    day = start.date()
    while day <= end.date():
        hour_obs: list[tuple[datetime, dict]] = []
        for hour in SYNOPSIS_HOURS:
            bjt_time = datetime.combine(day, time(hour))
            if bjt_time < start or bjt_time >= end:
                continue
            rows_all, reason = _query_hour_valid_rows(client, bjt_time, basin_codes, admin_code)
            if not rows_all:
                anchor_reason = reason or anchor_reason
                continue
            nearest = _nearest_station(poi, rows_all)
            if nearest is None or float(nearest["distance_km"]) > float(max_distance_km):
                anchor_reason = (
                    f"{bjt_time}: nearest_distance_km={nearest['distance_km'] if nearest else '无'}"
                    f">max={max_distance_km}"
                )
                continue
            if anchor is None:
                anchor = nearest
            # 当日优先取锚定站自身该时次记录（同站聚合防混站），锚定站缺报时才回退该时次最近站
            record = nearest
            if anchor is not None:
                anchor_id = _station_id(anchor.get("record") or {})
                for row in rows_all:
                    if _station_id(row) == anchor_id:
                        record = {"record": row, "distance_km": anchor.get("distance_km")}
                        break
            hour_obs.append((bjt_time, _clean_observation(record["record"])))
        if hour_obs:
            rows.append(_build_historical_day_row(day, hour_obs, point_name))
        day += timedelta(days=1)

    if not rows:
        return _no_data_payload(lon, lat, point_name, start_time, end_time)

    anchor_record = (anchor or {}).get("record") or {}
    return {
        "status": "ok",
        "query_type": "historical_observation",
        "query_mode": "historical_obs",
        "data_source": "自动站历史实况",
        "lon": lon,
        "lat": lat,
        "point_name": point_name,
        "forecast_start_time": start.strftime("%Y-%m-%d %H:%M"),
        "forecast_end_time": end.strftime("%Y-%m-%d %H:%M"),
        "periods": rows,
        "nearest_station": {
            "station_id": _station_id(anchor_record),
            "station_name": _station_name(anchor_record),
            "longitude": _safe_float(anchor_record.get("Lon")),
            "latitude": _safe_float(anchor_record.get("Lat")),
            "distance_km": (anchor or {}).get("distance_km"),
        },
        "message": "已查询到该点位最近观测站的历史实况（按 02/08/14/20 时观测整点聚合）。",
    }


def register_historical_weather_tool(mcp: FastMCP) -> None:
    # 过去日期实况不可变，同「点位|时间窗|半径|流域|区划」600s 内命中。
    _decorator, _hist_weather_cache, _hist_weather_lock = make_ttl_cache(
        int(os.getenv("HISTORICAL_WEATHER_CACHE_TTL", "600")),
        lambda keyword="", lon=None, lat=None, start_time="", end_time="",
               point_name="", max_distance_km=80.0, basin_codes=DEFAULT_BASIN_CODES,
               admin_code=TIANJIN_ADMIN_CODE: (
            f"{keyword}|{lon}|{lat}|{start_time}|{end_time}|{max_distance_km}|{basin_codes}|{admin_code}"
        ),
    )

    @mcp.tool()
    @_decorator
    def query_poi_historical_weather(
        keyword: str = "",
        lon: float | None = None,
        lat: float | None = None,
        start_time: str = "",
        end_time: str = "",
        point_name: str = "",
        max_distance_km: float = 80.0,
        basin_codes: str = DEFAULT_BASIN_CODES,
        admin_code: str = TIANJIN_ADMIN_CODE,
    ) -> dict:
        """查询指定点位或区域在某个历史日期/时段的自动站实况。

        用于回答“8月10号某某地方天气怎么样”“昨天某某地实况”等历史日期天气问题。
        滚动预报只覆盖未来时效；本工具按点位经纬度（或 keyword 解析 POI）查
        天擎逐小时站点实况（SURF_CHN_MUL_HOR）最近观测站，按 02/08/14/20 时
        观测整点聚合成逐日行（日最高/最低气温、累计降水、风况、天气现象均来自实测，
        代码确定性推导，不编造）。

        时间参数为北京时，接受“2026-08-10”或“2026-08-10 00:00”；不传 end_time 时
        默认查询 start_time 当天 00:00 至次日 00:00。单次最多查询 10 天。
        返回 periods 结构与滚动预报兼容；无该时段观测时 status 为 no_data。

        Args:
            keyword: 可选，点位/区域名称（不传 lon/lat 时用于解析经纬度）。
            lon: 可选，经度；与 lat 同时提供时优先使用。
            lat: 可选，纬度。
            start_time: 必填，北京时开始时间（日期或日期+时刻）。
            end_time: 可选，北京时结束时间；缺省为 start_time 当天次日 00:00。
            point_name: 可选，点位显示名称。
            max_distance_km: 最近观测站匹配半径（默认 80 公里）。
            basin_codes: 可选，海河流域编码兜底查询。
            admin_code: 可选，行政区编码（默认天津 120000）。

        Returns:
            历史实况结果 dict：status=ok|no_data|error，ok 时含 periods 逐日行与最近观测站信息。
        """
        if not (start_time or "").strip():
            return _error_payload("请提供 start_time（北京时历史日期，如 2026-08-10）。")
        return _query_historical_obs_core(
            lon=lon,
            lat=lat,
            start_time=start_time,
            end_time=end_time,
            point_name=point_name,
            keyword=keyword,
            max_distance_km=max_distance_km,
            basin_codes=basin_codes,
            admin_code=admin_code,
        )
