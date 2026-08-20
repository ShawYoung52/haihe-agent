"""从 MUSIC 按时间窗导出历史 5 分钟降水 CSV（供暴雨影响河流工具回测）。

用法（在 hhlyqyxt-master 目录下执行）：
    python scripts/export_historical_rain_csv.py \
        --date 2026-08-19 \
        --output /tmp/20260819_rain.csv

    # 或显式指定窗口（BJT）：
    python scripts/export_historical_rain_csv.py \
        --start "2026-08-19 08:00" --end "2026-08-20 08:00" \
        --output /tmp/20260819_rain.csv

口径与生产 `ScheduledTask/stationProcessMin.py::_append_hhly_5min_to_rolling_csv` 完全一致：
  - 数据源：HHLY 流域分钟降水（SURF_CHN_PRE_MIN，含 Q_PRE 质量标志）
  - Q_PRE ∈ {"0","3","4"} 或空 视为可信，其余丢弃
  - PRE 缺测哨兵 > 99988 置 0
  - 站点元信息按站取众数；5min 桶 label/closed=right，PRE=sum、元信息=first
  - 时间窗按 BJT 传入，转 UTC 调 MUSIC，返回 Datetime +8h 回 BJT

导出列（与 `rainfall_impact_geojson.aggregate_5min_station_pre_to_24h` 兼容）：
    Station_Id_C, Datetime, PRE, Lat, Lon, City, Station_Name, Cnty, Province, Town, Station_levl
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# 脚本位于 scripts/ 下，Python sys.path[0] 是 scripts/ 本身；
# 需把项目根（本脚本的父目录）加进 sys.path，才能 import ScheduledTask/utils 等包。
_CURRENT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _CURRENT_DIR.parent
for _item in (_CURRENT_DIR, _PROJECT_ROOT):
    if str(_item) not in sys.path:
        sys.path.insert(0, str(_item))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# 与生产 `_append_hhly_5min_to_rolling_csv` 一致：可信 Q_PRE 标志
_TRUSTED_Q_PRE = {"0", "3", "4"}
_MISSING_PRE_SENTINEL = 99988

_META_COLS = ["Station_levl", "Lat", "Lon", "City", "Station_Name", "Cnty", "Province", "Town"]


def _parse_bjt(value: str) -> datetime:
    """解析 BJT 时间字符串，支持 YYYY-MM-DD 或 YYYY-MM-DD HH:MM[:SS]。"""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间（BJT）：{value!r}")


def build_timerange(start_bjt: datetime, end_bjt: datetime) -> str:
    """MUSIC 时间窗（UTC）：BJT 前后各 -8h，返回 "[YYYYMMDDHHMMSS,YYYYMMDDHHMMSS]"。

    与生产 `_music_timerange_5min` 同思路；为保证 5min 首桶完整性，start 再前移 1 个桶。
    """
    start_utc = start_bjt - timedelta(hours=8) - timedelta(minutes=5)
    end_utc = end_bjt - timedelta(hours=8)
    return (
        f"[{start_utc.strftime('%Y%m%d%H%M%S')},"
        f"{end_utc.strftime('%Y%m%d%H%M%S')}]"
    )


def filter_trusted_qpre(df: pd.DataFrame) -> pd.DataFrame:
    """Q_PRE 质量过滤：可信标志 {"0","3","4"} 或空；与生产同口径。"""
    q_pre_str = df["Q_PRE"].fillna("").astype(str).str.strip()
    return df[q_pre_str.isin(_TRUSTED_Q_PRE) | (q_pre_str == "")].copy()


def normalize_pre(df: pd.DataFrame) -> pd.DataFrame:
    """PRE 数值化：无法解析置 0、缺测哨兵 >99988 置 0；Datetime 归一 BJT。"""
    out = df.copy()
    out["PRE"] = pd.to_numeric(out["PRE"], errors="coerce").fillna(0.0)
    out.loc[out["PRE"] > _MISSING_PRE_SENTINEL, "PRE"] = 0.0
    out["Datetime"] = pd.to_datetime(out["Datetime"], errors="coerce")
    return out.dropna(subset=["Datetime"])


def fill_station_meta_mode(df: pd.DataFrame) -> pd.DataFrame:
    """站点元信息按站取众数覆盖回原表（防同站元信息跨时刻抖动）。"""
    out = df.copy()
    station_mode = (
        out.groupby("Station_Id_C")[_META_COLS]
        .agg(lambda s: s.mode().iat[0] if not s.mode().empty else (s.iloc[0] if len(s) else ""))
    )
    for col in _META_COLS:
        out[col] = out["Station_Id_C"].map(station_mode[col])
    return out


def resample_to_5min(df: pd.DataFrame) -> pd.DataFrame:
    """5min 聚合（PRE=sum，元信息=first），与生产 `_append_hhly_5min_to_rolling_csv` 同口径。"""
    agg_cols = {"PRE": "sum"}
    agg_cols.update({c: "first" for c in _META_COLS})
    out = (
        df.set_index("Datetime")
        .groupby("Station_Id_C")
        .resample("5min", label="right", closed="right")
        .agg(agg_cols)
        .reset_index()
    )
    return out


def fetch_hhly_minute(timerange: str) -> pd.DataFrame:
    """拉取 HHLY 分钟降水（含 Q_PRE），Datetime 已 +8h 转 BJT。"""
    # 复用生产应急响应拉取函数（同仓库内，天然与现役口径一致；返回即 BJT）。
    from ScheduledTask.emergency_response_monitor import _fetch_hhly_rainfall_for_emergency
    return _fetch_hhly_rainfall_for_emergency(timerange)


def split_window(start_bjt: datetime, end_bjt: datetime, segment_hours: int) -> list[tuple[datetime, datetime]]:
    """把 [start, end) 切成多段（每段 segment_hours 小时），返回 [(seg_start, seg_end), ...]。"""
    if segment_hours <= 0:
        return [(start_bjt, end_bjt)]
    segs: list[tuple[datetime, datetime]] = []
    cur = start_bjt
    while cur < end_bjt:
        nxt = min(cur + timedelta(hours=segment_hours), end_bjt)
        segs.append((cur, nxt))
        cur = nxt
    return segs


def fetch_hhly_minute_by_segments(
    start_bjt: datetime,
    end_bjt: datetime,
    segment_hours: int = 4,
    *,
    retries: int = 3,
) -> pd.DataFrame:
    """分段拉取 HHLY 分钟降水：大窗口拆成多段，逐段调 MUSIC 再合并去重。

    背景：一次性拉 24h 全流域分钟数据（约 20~30 万行）会让 MUSIC 服务端数据库
    连接超时断开（E50022/-7001）；拆成 4h 等小段后每段 1~5 万行，服务端可正常返回。
    段与段之间用 build_timerange 的 5min 缓冲重叠，合并时按 站+时刻 去重。
    """
    segs = split_window(start_bjt, end_bjt, segment_hours)
    frames: list[pd.DataFrame] = []
    for idx, (seg_start, seg_end) in enumerate(segs, 1):
        tr = build_timerange(seg_start, seg_end)
        logger.info("拉取分段 %d/%d: [%s, %s)  UTC=%s",
                    idx, len(segs),
                    seg_start.strftime("%Y-%m-%d %H:%M"),
                    seg_end.strftime("%Y-%m-%d %H:%M"),
                    tr)
        frames.append(fetch_hhly_minute_with_retry(tr, retries=retries))
    non_empty = [f for f in frames if f is not None and not f.empty]
    if not non_empty:
        return pd.DataFrame()
    merged = pd.concat(non_empty, ignore_index=True)
    # 段间 5min 缓冲会重复；按 站+时刻 去重（保留先拉到的）
    if {"Station_Id_C", "Datetime"}.issubset(merged.columns):
        merged = merged.drop_duplicates(subset=["Station_Id_C", "Datetime"], keep="first")
    return merged


def fetch_hhly_minute_with_retry(
    timerange: str,
    *,
    retries: int = 3,
    base_wait_s: float = 3.0,
) -> pd.DataFrame:
    """拉取 HHLY 分钟降水，MUSIC 瞬时故障（-7001 数据库连接断开等）重试。

    与 intranet_verify_emergency.py 的"3 次重试防 MUSIC 瞬断"同思路：
    服务端长时间空闲/网络抖动常返回 -7001 之类瞬时错误，重试即恢复。
    """
    import time as _time
    from utils.MusicTool import MusicApiError

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return fetch_hhly_minute(timerange)
        except Exception as exc:  # noqa: BLE001 - 重试框架内收口
            last_exc = exc
            if attempt >= retries:
                break
            wait = base_wait_s * attempt
            logger.warning("MUSIC 拉取失败（第 %d/%d 次）：%s，%.0fs 后重试",
                           attempt, retries, exc, wait)
            _time.sleep(wait)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("MUSIC 拉取失败（重试次数无效）")


def slice_window(df: pd.DataFrame, start_bjt: datetime, end_bjt: datetime) -> pd.DataFrame:
    """裁剪到 [start_bjt, end_bjt) 半开区间（BJT）。"""
    return df[(df["Datetime"] >= start_bjt) & (df["Datetime"] < end_bjt)].copy()


def main() -> int:
    parser = argparse.ArgumentParser(description="从 MUSIC 导出历史 5 分钟降水 CSV（BJT）")
    parser.add_argument("--date", help="快捷日期（BJT）：窗口=[date 08:00, date+1 08:00)（气象日界，可用 --day-start-hour 改）")
    parser.add_argument("--start", help="窗口开始（BJT，如 2026-08-19 08:00）")
    parser.add_argument("--end", help="窗口结束（BJT，如 2026-08-20 08:00）")
    parser.add_argument("--day-start-hour", type=int, default=8,
                        help="--date 模式下窗口起始小时（默认 8=气象日界；0=自然日）")
    parser.add_argument("--segment-hours", type=int, default=4,
                        help="分段拉取时长（小时）。大窗口一次拉会触发 MUSIC -7001 连接断开，"
                             "拆成小段规避；0=不分段")
    parser.add_argument("--output", required=True, help="输出 CSV 路径")
    args = parser.parse_args()

    if args.date and (args.start or args.end):
        parser.error("--date 与 --start/--end 二选一")
    if args.date:
        day = _parse_bjt(args.date).replace(hour=0, minute=0, second=0, microsecond=0)
        start_bjt = day + timedelta(hours=args.day_start_hour)
        end_bjt = start_bjt + timedelta(hours=24)
    elif args.start and args.end:
        start_bjt = _parse_bjt(args.start)
        end_bjt = _parse_bjt(args.end)
    else:
        parser.error("必须提供 --date 或 --start/--end")
    if end_bjt <= start_bjt:
        parser.error("end 必须晚于 start")

    timerange = build_timerange(start_bjt, end_bjt)
    logger.info("MUSIC timerange(UTC)=%s  窗口(BJT)=[%s, %s)",
                timerange,
                start_bjt.strftime("%Y-%m-%d %H:%M"),
                end_bjt.strftime("%Y-%m-%d %H:%M"))

    raw = fetch_hhly_minute_by_segments(start_bjt, end_bjt, args.segment_hours)
    logger.info("HHLY 分钟原始记录数：%d", len(raw))
    if raw.empty:
        logger.warning("该时段无 HHLY 数据，导出空表")
        pd.DataFrame(columns=[
            "Station_Id_C", "Datetime", "PRE", "Lat", "Lon", "City",
            "Station_Name", "Cnty", "Province", "Town", "Station_levl",
        ]).to_csv(args.output, index=False, encoding="utf-8-sig")
        return 0

    df = filter_trusted_qpre(raw)
    df = normalize_pre(df)
    df = fill_station_meta_mode(df)
    df = resample_to_5min(df)
    df = slice_window(df, start_bjt, end_bjt)
    df = df.sort_values(["Station_Id_C", "Datetime"]).reset_index(drop=True)

    df.to_csv(args.output, index=False, encoding="utf-8-sig")
    logger.info("已导出 %d 行、%d 个站点 → %s", len(df), df["Station_Id_C"].nunique(), args.output)

    # 快速自检：24h 累计达到暴雨阈值（>=50mm）的站点
    rain_24h = (
        df.groupby("Station_Id_C")["PRE"].sum().reset_index()
        .rename(columns={"PRE": "rain_24h"})
        .sort_values("rain_24h", ascending=False)
    )
    top = rain_24h.head(10)
    trigger = rain_24h[rain_24h["rain_24h"] >= 50.0]
    print("\n=== 24h 累计雨量 Top10 ===")
    for _, row in top.iterrows():
        print(f"  {row['Station_Id_C']}: {row['rain_24h']:.1f} mm")
    print(f"达到暴雨阈值(>=50mm)的站点数：{len(trigger)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
