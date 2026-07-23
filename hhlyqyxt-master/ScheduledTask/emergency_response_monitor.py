"""5 分钟级防汛应急响应级别计算。

读取 5 分钟累计降水 CSV，按国家级站点统计 12h/24h 暴雨阈值占比，
并持久化到 qy_emergency_response_monitor 表。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Union

import pandas as pd

from Models.QyEmergencyResponseMonitor import QyEmergencyResponseMonitor
from utils.MusicTool import MusicClient, MusicConfig
from utils.db import Session

logger = logging.getLogger(__name__)

NATIONAL_STATION_LEVELS = {"11", "12", "13", "16"}

# 应急响应独立数据源：海河流域（HHLY）分钟降水
HHLY_BASIN_CODES = "HHLY"
HHLY_MIN_DATA_CODE = "SURF_CHN_MUL_MIN"
HHLY_MIN_ELEMENTS = (
    "Station_levl,Lat,Lon,Alti,Station_Id_C,Datetime,IYMDHM,RYMDHM,UPDATE_TIME,"
    "City,Station_Name,Cnty,NetCode,Province,REGIONCODE,Town,Year,Mon,Day,Hour,Min,PRE"
)
HHLY_MIN_COLUMNS = HHLY_MIN_ELEMENTS.split(",")

# 降水阈值（毫米）
BAOYU_LOWER = 50.0
DABAOYU_LOWER = 100.0
TEDABAOYU_LOWER = 250.0


def _normalize_station_level(value) -> str:
    """归一为去前导零的 2 位字符串：011→11、11→11、'016'→16；None/空→'0'。"""
    try:
        return str(int(value))
    except (ValueError, TypeError):
        text = str(value or "").strip()
        return text.lstrip("0") or "0"


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


def _sum_precip_by_station(df: pd.DataFrame) -> pd.DataFrame:
    """按站点汇总降水量，返回包含 Station_Id_C 与 PRE 的 DataFrame。"""
    return df.groupby("Station_Id_C")["PRE"].sum().reset_index()


def _count_by_threshold(
    series: pd.Series, lower: float, upper: Optional[float] = None
) -> int:
    """统计满足阈值区间的元素个数。"""
    if upper is None:
        return int((series >= lower).sum())
    return int(((series >= lower) & (series < upper)).sum())


def _ratio(count: int, total: int) -> float:
    """计算占比，total 为 0 时返回 0。"""
    return round(count / total, 4) if total > 0 else 0.0


def _determine_response_level(
    ratio_12h_baoyu: float,
    ratio_24h_baoyu: float,
    ratio_24h_dabaoyu: float,
    ratio_24h_tedabaoyu: float,
) -> int:
    """按优先级确定应急响应级别，数字越小级别越高。"""
    if ratio_24h_tedabaoyu >= 0.15:
        return 1
    if ratio_24h_dabaoyu >= 0.15:
        return 2
    if ratio_12h_baoyu >= 0.20:
        return 3
    if ratio_24h_baoyu >= 0.20:
        return 4
    return 0


def _fetch_hhly_rainfall_for_emergency(
    timerange: str,
    client: Optional[Any] = None,
) -> pd.DataFrame:
    """独立拉取 HHLY 流域 5 分钟降水，供应急响应内部消费。

    timerange 形如 "[20260722080000,20260723080000]"。client 为 None 时
    现场实例化牵引侧 MusicClient(MusicConfig())。返回含
    Station_Id_C/Datetime/PRE/Station_levl 等列的 DataFrame；
    空结果返回带列的空 DataFrame；天擎异常原样向上传播由调用方处理。
    """
    own_client = client
    if own_client is None:
        own_client = MusicClient(MusicConfig())
    records = own_client.get_surf_pre_in_basin_timerange(
        basin_codes=HHLY_BASIN_CODES,
        timeRange=timerange,
        elements=HHLY_MIN_ELEMENTS,
        data_code=HHLY_MIN_DATA_CODE,
    )
    if not records:
        return pd.DataFrame(columns=HHLY_MIN_COLUMNS)
    df = pd.DataFrame(records)
    # 补齐 elements 中声明但接口未返回的列，保证下游口径稳定
    for col in HHLY_MIN_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    return df


def compute_emergency_response_stats(
    source: Union[str, pd.DataFrame] = None,
    datatime: Union[str, datetime, None] = None,
) -> Optional[dict]:
    """从 CSV 路径或 DataFrame 计算应急响应统计指标。

    Args:
        source: 5 分钟降水 CSV 文件路径，或已含 Station_Id_C/Datetime/PRE/Station_levl
            等列的 DataFrame（用于应急响应独立拉取 HHLY 后直接消费，跳过读文件）。
        datatime: 统计结束时间，窗口为 (datatime - 12h/24h, datatime]。
            为 None 时使用数据中的最大时间。

    Returns:
        包含各阈值站点数、占比和响应级别的字典；数据为空或无可解析内容时返回 None。
    """
    if isinstance(source, pd.DataFrame):
        df = source.copy()
    else:
        try:
            df = pd.read_csv(source)
        except pd.errors.EmptyDataError:
            return None
    if df is None or df.empty:
        return None

    if "Station_levl" not in df.columns:
        logger.warning("数据缺少 Station_levl 列，全部站点按非国家站处理: %s", source)
        df["Station_levl"] = ""

    df["Datetime"] = pd.to_datetime(df["Datetime"])
    if datatime is None:
        datatime = df["Datetime"].max()

    end_time = _parse_datatime(datatime)
    start_12h = end_time - timedelta(hours=12)
    start_24h = end_time - timedelta(hours=24)

    df["PRE"] = pd.to_numeric(df["PRE"], errors="coerce")
    # 缺失降水标识：大于 99988 的值视为缺测，按 0 处理
    df.loc[df["PRE"] > 99988, "PRE"] = 0.0
    # 剔除仍无法解析为数值的记录，避免全 NaN 站点计入 total
    df = df.dropna(subset=["PRE"])
    df["Station_levl_norm"] = df["Station_levl"].apply(_normalize_station_level)

    national_df = df[df["Station_levl_norm"].isin(NATIONAL_STATION_LEVELS)].copy()

    window_24h = national_df[
        (national_df["Datetime"] > start_24h) & (national_df["Datetime"] <= end_time)
    ]
    window_12h = national_df[
        (national_df["Datetime"] > start_12h) & (national_df["Datetime"] <= end_time)
    ]

    sum_pre_24h = _sum_precip_by_station(window_24h)
    sum_pre_12h = _sum_precip_by_station(window_12h)

    total = len(sum_pre_24h)

    station_12h_baoyu = _count_by_threshold(sum_pre_12h["PRE"], BAOYU_LOWER, DABAOYU_LOWER)
    station_24h_baoyu = _count_by_threshold(sum_pre_24h["PRE"], BAOYU_LOWER, DABAOYU_LOWER)
    station_24h_dabaoyu = _count_by_threshold(
        sum_pre_24h["PRE"], DABAOYU_LOWER, TEDABAOYU_LOWER
    )
    station_24h_tedabaoyu = _count_by_threshold(sum_pre_24h["PRE"], TEDABAOYU_LOWER)

    ratio_12h_baoyu = _ratio(station_12h_baoyu, total)
    ratio_24h_baoyu = _ratio(station_24h_baoyu, total)
    ratio_24h_dabaoyu = _ratio(station_24h_dabaoyu, total)
    ratio_24h_tedabaoyu = _ratio(station_24h_tedabaoyu, total)

    response_level = _determine_response_level(
        ratio_12h_baoyu,
        ratio_24h_baoyu,
        ratio_24h_dabaoyu,
        ratio_24h_tedabaoyu,
    )

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
    csv_path: Optional[str] = None,
    datatime: Union[str, datetime, None] = None,
    minute_monitor_id: Optional[int] = None,
    timerange: Optional[str] = None,
    client: Optional[Any] = None,
) -> Optional[QyEmergencyResponseMonitor]:
    """计算应急响应指标并写入数据库。

    数据来源二选一：
    - timerange：独立拉取 HHLY 流域分钟降水后判定（推荐，应急响应自带数据源）；
    - csv_path：从 5 分钟降水 CSV 判定（旧链路，向下兼容现有调用方）。
    同时提供 timerange 与 csv_path 时，timerange 优先并记录 WARNING 日志；
    都不传时抛 ValueError。同一 datatime 的记录先删后插，保证重跑无重复行。

    Args:
        csv_path: 5 分钟降水 CSV 文件路径（旧链路）。
        datatime: 统计结束时间，默认为数据最大时间。
        minute_monitor_id: 关联的分钟监测记录 ID。
        timerange: HHLY 拉取时间窗，形如 "[20260722080000,20260723080000]"（新链路）。
        client: 已实例化的 MusicClient，None 时在拉取层现场实例化。

    Returns:
        写入的 ORM 对象；数据不存在/为空时返回 None。
    """
    if timerange and csv_path:
        logger.warning("同时提供 timerange 与 csv_path，优先使用 timerange（HHLY），忽略 csv_path=%s", csv_path)
    if not timerange and not csv_path:
        raise ValueError("run_emergency_response_monitor 需要提供 csv_path 或 timerange 之一")

    if timerange:
        df = _fetch_hhly_rainfall_for_emergency(timerange, client=client)
        if df is None or df.empty:
            logger.warning("HHLY 拉取为空，不写表：timerange=%s", timerange)
            return None
        stats = compute_emergency_response_stats(df, datatime)
    else:
        if not Path(csv_path).exists():
            logger.warning("CSV 文件不存在: %s", csv_path)
            return None
        stats = compute_emergency_response_stats(csv_path, datatime)

    if stats is None:
        logger.warning("数据为空，未写入应急响应记录")
        return None

    record = QyEmergencyResponseMonitor(
        minute_monitor_id=minute_monitor_id, **stats
    )

    session = Session()
    try:
        session.query(QyEmergencyResponseMonitor).filter(
            QyEmergencyResponseMonitor.datatime == stats["datatime"]
        ).delete()
        session.add(record)
        session.commit()
        session.refresh(record)
        return record
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
