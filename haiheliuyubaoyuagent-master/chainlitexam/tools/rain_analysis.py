"""
基于天擎(MUSIC)站点数据的降雨分析工具，供 LangChain Agent 按需调用。

查询某个时刻的站点降雨数据 → 按降雨级别分组 → 空间分析（行政区划、77分区/河系、河流）
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from langchain_core.tools import tool
from sqlalchemy import text

from utils.MusicTool import MusicClient, MusicConfig
from utils.db import engine

# 降雨等级定义（与参考项目 monitorservice.py 一致）
RAIN_LEVELS = [
    ("特大暴雨", 250.0, float("inf")),
    ("大暴雨", 100.0, 250.0),
    ("暴雨", 50.0, 100.0),
    ("大雨", 25.0, 50.0),
    ("中雨", 10.0, 25.0),
    ("小雨", 0.1, 10.0),
]


def _query_music_station_data(
    timestr: str,
    start_time: str | None = None,
    end_time: str | None = None,
) -> list[dict[str, Any]]:
    """从 MUSIC 接口拉取站点降雨数据"""
    client = MusicClient(MusicConfig())
    dt = datetime.strptime(timestr, "%Y%m%d%H%M%S")
    # 默认以 timestr 为结束参考点，回退 32h~8h 作为常用实况窗口
    time_start = start_time or (dt - timedelta(hours=32)).strftime("%Y%m%d%H%M%S")
    time_end = end_time or (dt - timedelta(hours=8)).strftime("%Y%m%d%H%M%S")
    timerange = f"[{time_start},{time_end}]"
    return client.stat_surf_pre_in_basin_new("HHLY_JUECE", timerange)


def _group_stations_by_level(raw_data: list[dict]) -> list[dict]:
    """聚合站点数据并按降雨等级分组"""
    df = pd.DataFrame(raw_data)
    if df.empty:
        return []

    df["Lat"] = df["Lat"].astype(float)
    df["Lon"] = df["Lon"].astype(float)
    df["SUM_PRE_1H"] = df["SUM_PRE_1H"].astype(float)
    df = df[df["SUM_PRE_1H"] < 99999]

    if df.empty:
        return []

    # 按站点聚合
    df = (
        df.groupby("Station_Id_C", as_index=False, sort=False)
        .agg({
            "Lat": "first", "Lon": "first",
            "City": "first", "Station_Name": "first",
            "Cnty": "first", "Province": "first", "Town": "first",
            "SUM_PRE_1H": "sum",
        })
    )

    def _label(rain: float) -> str | None:
        for name, lo, hi in RAIN_LEVELS:
            if lo <= rain < hi:
                return name
        return None

    df["level"] = df["SUM_PRE_1H"].apply(_label)

    # 按等级分组整理
    grouped = {}
    for _, row in df[df["level"].notna()].iterrows():
        lv = row["level"]
        if lv not in grouped:
            grouped[lv] = []
        grouped[lv].append({
            "station_id": row["Station_Id_C"],
            "name": row["Station_Name"],
            "province": row["Province"],
            "city": row["City"],
            "cnty": row["Cnty"],
            "town": row["Town"],
            "lon": float(row["Lon"]),
            "lat": float(row["Lat"]),
            "rainfall": float(row["SUM_PRE_1H"]),
        })

    # 按等级从重到轻排序输出
    result = []
    for name, lo, hi in RAIN_LEVELS:
        stations = grouped.get(name, [])
        if stations:
            stations.sort(key=lambda s: s["rainfall"], reverse=True)
            result.append({"level": name, "stations": stations})

    return result


def _spatial_analysis(level_groups: list[dict]) -> list[dict]:
    """对每个降雨等级下的站点做空间分析（行政区划、77分区、河流）"""
    if not level_groups:
        return level_groups

    try:
        with engine.connect() as conn:
            for group in level_groups:
                admin_set: set[str] = set()
                zone77_set: set[str] = set()
                river_set: set[str] = set()

                for s in group["stations"]:
                    lon, lat = s["lon"], s["lat"]

                    # 1. 行政区划
                    try:
                        row = conn.execute(
                            text("""
                                SELECT province_name, city_name, county_name, full_name
                                FROM haihe_admin_division
                                WHERE ST_Within(ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), geom)
                                LIMIT 1
                            """),
                            {"lon": lon, "lat": lat},
                        ).fetchone()
                        if row:
                            s["admin"] = {
                                "province": row[0], "city": row[1],
                                "county": row[2], "full_name": row[3],
                            }
                            admin_set.add(f"{row[0]} {row[1]} {row[2]}")
                    except Exception as e:
                        print(f"[降雨分析] 行政区划查询失败：{e}")

                    # 2. 77分区（河系）
                    try:
                        row = conn.execute(
                            text("""
                                SELECT zone_name, zone_code
                                FROM haihe_zone_77
                                WHERE ST_Within(ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), geom)
                                LIMIT 1
                            """),
                            {"lon": lon, "lat": lat},
                        ).fetchone()
                        if row:
                            s["zone_77"] = {"name": row[0], "code": row[1]}
                            zone77_set.add(f"{row[0]}（{row[1]}）")
                    except Exception as e:
                        print(f"[降雨分析] 77分区查询失败：{e}")

                    # 3. 附近河流
                    try:
                        rows = conn.execute(
                            text("""
                                SELECT DISTINCT river_name
                                FROM haihe_river_directed_full_v6
                                WHERE ST_DWithin(
                                    geom,
                                    ST_SetSRID(ST_MakePoint(:lon, :lat), 4326),
                                    0.15
                                )
                                AND river_name IS NOT NULL AND river_name != ''
                                ORDER BY river_name
                            """),
                            {"lon": lon, "lat": lat},
                        ).fetchall()
                        for r in rows:
                            river_set.add(r[0])
                    except Exception as e:
                        print(f"[降雨分析] 河流查询失败：{e}")

                group["admin_divisions"] = sorted(admin_set)
                group["zone_77_regions"] = sorted(zone77_set)
                group["affected_rivers"] = sorted(river_set)
    except Exception as e:
        print(f"[降雨分析] 空间分析整体失败，跳过：{e}")
        for group in level_groups:
            group.setdefault("admin_divisions", [])
            group.setdefault("zone_77_regions", [])
            group.setdefault("affected_rivers", [])

    return level_groups


def _build_summary(max_rain: float, max_level: str | None,
                   level_groups: list[dict]) -> str:
    """生成自然语言总结"""
    if max_rain <= 0:
        return "当前时段海河流域无有效降雨数据。"
    if not max_level:
        return f"当前最大降雨量{max_rain:.1f}mm，未达到暴雨级别（<50mm）。"

    top = level_groups[0] if level_groups else {}
    admin_count = len(top.get("admin_divisions", []))
    zone_count = len(top.get("zone_77_regions", []))
    river_count = len(top.get("affected_rivers", []))
    station_count = len(top.get("stations", []))

    return (
        f"当前最大降雨量{max_rain:.1f}mm，达到「{max_level}」级别，"
        f"{max_level}级站点共{station_count}个。"
        f"涉及行政区划{admin_count}个，"
        f"涉及77分区子流域{zone_count}个，"
        f"影响河流{river_count}条。"
    )


def _normalize_time_str(time_str: str) -> str:
    """统一时间格式为 YYYYMMDDHHMMSS"""
    raw = time_str.strip()
    if len(raw) == 10:  # YYYYMMDDHH
        raw = raw + "0000"
    elif len(raw) == 19:  # YYYY-MM-DD HH:MM:SS
        raw = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").strftime("%Y%m%d%H%M%S")
    return raw


@tool
def local_analyze_rainfall_by_time(
    time_str: str,
    start_time: str | None = None,
    end_time: str | None = None,
) -> str:
    """
    【本地调试用】基于天擎站点数据，分析某个时刻的降雨情况。
    与 MCP 后端的 analyze_rainfall_by_time 区分，避免同名冲突。
    返回最大雨量级别、涉及的行政区划、77分区河系、受影响河流。

    参数 time_str：查询时刻，支持格式：
    - "YYYYMMDDHH"（如 2026051700）
    - "YYYYMMDDHHMMSS"（如 20260517000000）
    - "YYYY-MM-DD HH:MM:SS"（如 2026-05-17 00:00:00）
    参数 start_time/end_time：可选，显式指定查询时间范围，格式同 time_str。
    返回 JSON 字符串。
    """
    timestr = _normalize_time_str(time_str)
    start_s = _normalize_time_str(start_time) if start_time else None
    end_s = _normalize_time_str(end_time) if end_time else None

    # 1. 拉取天擎站点数据
    raw_data = _query_music_station_data(timestr, start_s, end_s)

    # 2. 按降雨等级分组
    level_groups = _group_stations_by_level(raw_data)

    max_rain = 0.0
    if level_groups and level_groups[0].get("stations"):
        max_rain = level_groups[0]["stations"][0]["rainfall"]
    max_level = level_groups[0]["level"] if level_groups else None

    # 3. 空间分析（失败也不中断主流程）
    level_groups = _spatial_analysis(level_groups)

    data_dt = datetime.strptime(timestr, "%Y%m%d%H%M%S")
    data_time = data_dt.strftime("%Y-%m-%d %H:%M:%S")
    time_range_readable = ""
    if start_s and end_s:
        time_range_readable = (
            f"{datetime.strptime(start_s, '%Y%m%d%H%M%S').strftime('%Y-%m-%d %H:%M')} ~ "
            f"{datetime.strptime(end_s, '%Y%m%d%H%M%S').strftime('%Y-%m-%d %H:%M')}"
        )

    result = {
        "time": timestr,
        "data_time": data_time,
        "time_range_readable": time_range_readable,
        "total_stations": sum(len(g["stations"]) for g in level_groups),
        "max_rainfall": round(max_rain, 1),
        "max_level": max_level,
        "max_station": level_groups[0]["stations"][0] if level_groups and level_groups[0].get("stations") else None,
        "level_analysis": [
            {
                "level": g["level"],
                "station_count": len(g["stations"]),
                "stations": g["stations"],
                "admin_divisions": g.get("admin_divisions", []),
                "zone_77_regions": g.get("zone_77_regions", []),
                "affected_rivers": g.get("affected_rivers", []),
            }
            for g in level_groups
        ],
        "summary": _build_summary(max_rain, max_level, level_groups),
    }

    return json.dumps(result, ensure_ascii=False)


def build_rain_analysis_tools():
    """返回降雨分析工具列表，供主模型绑定"""
    return [local_analyze_rainfall_by_time]