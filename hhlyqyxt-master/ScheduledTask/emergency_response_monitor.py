"""5 分钟级防汛应急响应级别计算。

读取 5 分钟累计降水 CSV，按国家级站点统计 12h/24h 暴雨阈值占比，
并持久化到 qy_emergency_response_monitor 表。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from Models.QyEmergencyResponseMonitor import QyEmergencyResponseMonitor
from utils.db import Session

logger = logging.getLogger(__name__)

NATIONAL_STATION_LEVELS = {"011", "012", "013", "016"}

# 降水阈值（毫米）
BAOYU_LOWER = 50.0
DABAOYU_LOWER = 100.0
TEDABAOYU_LOWER = 250.0


def _normalize_station_level(value) -> str:
    """将 Station_levl 归一化为 3 位零填充字符串。"""
    try:
        return str(int(value)).zfill(3)
    except (ValueError, TypeError):
        return str(value).zfill(3)


def _parse_datatime(datatime: Union[str, datetime]) -> datetime:
    """支持 datetime 对象或常见字符串格式。"""
    if isinstance(datatime, datetime):
        return datatime
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(datatime, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析 datatime: {datatime}")


def compute_emergency_response_stats(csv_path: str, datatime: Union[str, datetime]) -> dict:
    """从 CSV 计算应急响应统计指标。

    Args:
        csv_path: 5 分钟降水 CSV 文件路径。
        datatime: 统计结束时间，窗口为 (datatime - 12h/24h, datatime]。

    Returns:
        包含各阈值站点数、占比和响应级别的字典。
    """
    end_time = _parse_datatime(datatime)
    start_12h = end_time - timedelta(hours=12)
    start_24h = end_time - timedelta(hours=24)

    df = pd.read_csv(csv_path)
    df["Datetime"] = pd.to_datetime(df["Datetime"])
    df["PRE"] = pd.to_numeric(df["PRE"], errors="coerce")
    # 缺失降水标识：大于 99988 的值视为缺测，按 0 处理
    df.loc[df["PRE"] > 99988, "PRE"] = 0.0
    # 剔除仍无法解析为数值的记录，避免全 NaN 站点计入 total
    df = df.dropna(subset=["PRE"])
    df["Station_levl_norm"] = df["Station_levl"].apply(_normalize_station_level)

    # 国家级站点
    national_df = df[df["Station_levl_norm"].isin(NATIONAL_STATION_LEVELS)].copy()

    # 累计每个站点在 24h 窗口内的降水量（12h 窗口为其子集）
    window_24h = national_df[
        (national_df["Datetime"] > start_24h) & (national_df["Datetime"] <= end_time)
    ]
    sum_pre_24h = (
        window_24h.groupby("Station_Id_C")["PRE"].sum().reset_index()
        if not window_24h.empty
        else pd.DataFrame(columns=["Station_Id_C", "PRE"])
    )

    window_12h = national_df[
        (national_df["Datetime"] > start_12h) & (national_df["Datetime"] <= end_time)
    ]
    sum_pre_12h = (
        window_12h.groupby("Station_Id_C")["PRE"].sum().reset_index()
        if not window_12h.empty
        else pd.DataFrame(columns=["Station_Id_C", "PRE"])
    )

    total = len(sum_pre_24h)

    def _count_by_threshold(series: pd.Series, lower: float, upper: Optional[float] = None) -> int:
        if upper is None:
            return int((series >= lower).sum())
        return int(((series >= lower) & (series < upper)).sum())

    station_12h_baoyu = _count_by_threshold(sum_pre_12h["PRE"], BAOYU_LOWER, DABAOYU_LOWER)
    station_24h_baoyu = _count_by_threshold(sum_pre_24h["PRE"], BAOYU_LOWER, DABAOYU_LOWER)
    station_24h_dabaoyu = _count_by_threshold(sum_pre_24h["PRE"], DABAOYU_LOWER, TEDABAOYU_LOWER)
    station_24h_tedabaoyu = _count_by_threshold(sum_pre_24h["PRE"], TEDABAOYU_LOWER)

    def _ratio(count: int, total: int) -> float:
        return round(count / total, 4) if total > 0 else 0.0

    ratio_12h_baoyu = _ratio(station_12h_baoyu, total)
    ratio_24h_baoyu = _ratio(station_24h_baoyu, total)
    ratio_24h_dabaoyu = _ratio(station_24h_dabaoyu, total)
    ratio_24h_tedabaoyu = _ratio(station_24h_tedabaoyu, total)

    # 响应级别：最高优先级（数字越小级别越高）
    response_level = 0
    if ratio_24h_tedabaoyu >= 0.15:
        response_level = 1
    elif ratio_24h_dabaoyu >= 0.15:
        response_level = 2
    elif ratio_12h_baoyu >= 0.20:
        response_level = 3
    elif ratio_24h_baoyu >= 0.20:
        response_level = 4

    return {
        "datatime": end_time,
        "total_national_stations": total,
        "station_12h_baoyu": station_12h_baoyu,
        "ratio_12h_baoyu": ratio_12h_baoyu,
        "station_24h_baoyu": station_24h_baoyu,
        "ratio_24h_baoyu": ratio_24h_baoyu,
        "station_24h_dabaoyu": station_24h_dabaoyu,
        "ratio_24h_dabaoyu": ratio_24h_dabaoyu,
        "station_24h_tedabaoyu": station_24h_tedabaoyu,
        "ratio_24h_tedabaoyu": ratio_24h_tedabaoyu,
        "response_level": response_level,
    }


def run_emergency_response_monitor(
    csv_path: str,
    datatime: Optional[Union[str, datetime]] = None,
    minute_monitor_id: Optional[int] = None,
) -> Optional[QyEmergencyResponseMonitor]:
    """计算应急响应指标并写入数据库。

    Args:
        csv_path: 5 分钟降水 CSV 文件路径。
        datatime: 统计结束时间，默认为 CSV 中最大时间。
        minute_monitor_id: 关联的分钟监测记录 ID。

    Returns:
        写入的 ORM 对象；CSV 不存在时返回 None。
    """
    if not Path(csv_path).exists():
        logger.warning("CSV 文件不存在: %s", csv_path)
        return None

    if datatime is None:
        df = pd.read_csv(csv_path)
        df["Datetime"] = pd.to_datetime(df["Datetime"])
        datatime = df["Datetime"].max()

    stats = compute_emergency_response_stats(csv_path, datatime)

    record = QyEmergencyResponseMonitor(
        datatime=stats["datatime"],
        minute_monitor_id=minute_monitor_id,
        total_national_stations=stats["total_national_stations"],
        station_12h_baoyu=stats["station_12h_baoyu"],
        ratio_12h_baoyu=stats["ratio_12h_baoyu"],
        station_24h_baoyu=stats["station_24h_baoyu"],
        ratio_24h_baoyu=stats["ratio_24h_baoyu"],
        station_24h_dabaoyu=stats["station_24h_dabaoyu"],
        ratio_24h_dabaoyu=stats["ratio_24h_dabaoyu"],
        station_24h_tedabaoyu=stats["station_24h_tedabaoyu"],
        ratio_24h_tedabaoyu=stats["ratio_24h_tedabaoyu"],
        response_level=stats["response_level"],
    )

    session = Session()
    try:
        session.add(record)
        session.commit()
        return record
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
