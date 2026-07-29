"""场景化验证脚本：用历史日期一键重现应急响应 + 河流影响图 + 天河报告全链路。

用法（在 hhlyqyxt-master 目录下执行）：
    python scripts/verify_emergency_scenario.py --date 2023-07-30 --hour 20

流程：
    1. 参数 --date 指定 BJT 日期，--hour 默认 20（当天 20:00 BJT）
    2. 反推 end_time = 指定 datetime（BJT），start_time = end_time - 24h
    3. 用 unionmindataby10minuteto24h 拉 HHLY_JUECE 24h 分钟降水 → ./24hourmindata.csv
    4. 用 _fetch_hhly_rainfall_for_emergency 拉 HHLY 24h 分钟降水 → ./hhly_24hourmindata.csv
    5. 直接调用 calcmaxdataseg5min() 走完整链路：
       - 5min/1h/24h 最大站点统计入库
       - 天津分县暴雨等级入库
       - 河流影响图（≥50mm 站点存在时才生成 GeoJSON）
       - 应急响应 I-IV 级判定 + 入库
       - I-IV 级触发天河报告
    6. 打印验证报告：response_level / geojsonurl / impact_city / 报告调用结果
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# 支持从 hhlyqyxt-master 目录直接执行
_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from ScheduledTask.emergency_response_monitor import _fetch_hhly_rainfall_for_emergency  # noqa: E402
from ScheduledTask import stationProcessMin as spm  # noqa: E402
from ScheduledTask.stationProcessMin import (  # noqa: E402
    calcmaxdataseg5min,
    readmindatabytimerange,
)

# 验证专用 CSV（不与生产的 24hourmindata.csv / hhly_24hourmindata.csv 冲突）
VERIFY_JUECE_CSV = "./verify_juece_24hourmindata.csv"
VERIFY_HHLY_CSV = "./verify_hhly_24hourmindata.csv"


def _fetch_juece_24h(end_time_bjt: datetime) -> pd.DataFrame:
    """按小时循环拉 24h HHLY_JUECE 分钟降水，聚合为 5 分钟。"""
    # 拉取窗口：BJT 转 UTC (-8h)
    end_utc = end_time_bjt - timedelta(hours=8)
    start_utc = end_utc - timedelta(hours=24)

    temptime = start_utc + timedelta(hours=1)
    df = None
    while temptime <= end_utc:
        tempstart = temptime - timedelta(hours=1) + timedelta(minutes=1)
        res = readmindatabytimerange(
            tempstart.strftime("%Y%m%d%H%M%S"),
            temptime.strftime("%Y%m%d%H%M%S"),
        )
        df = res if df is None else pd.concat([df, res])
        temptime += timedelta(hours=1)

    if df is None or df.empty:
        return pd.DataFrame()

    # UTC → BJT，并 resample 到 5 分钟
    df["Datetime"] = pd.to_datetime(df["Datetime"], format="%Y-%m-%d %H:%M:%S") + pd.Timedelta(hours=8)
    df["PRE"] = df["PRE"].astype("float")
    df.loc[df["PRE"] > 99988, "PRE"] = 0
    df_5min = (
        df.set_index("Datetime")
        .groupby("Station_Id_C")
        .resample("5min", label="right", closed="right")
        .agg({
            "PRE": "sum",
            "Station_levl": "first",
            "Lat": "first", "Lon": "first",
            "City": "first", "Station_Name": "first",
            "Cnty": "first", "Province": "first", "Town": "first",
        })
        .reset_index()
    )
    return df_5min


def _fetch_hhly_24h(end_time_bjt: datetime) -> pd.DataFrame:
    """按小时循环拉 24h HHLY 分钟降水（应急响应数据源），5min 聚合与生产同口径。"""
    end_utc = end_time_bjt - timedelta(hours=8)
    start_utc = end_utc - timedelta(hours=24)

    temptime = start_utc + timedelta(hours=1)
    df = None
    while temptime <= end_utc:
        tempstart = temptime - timedelta(hours=1) + timedelta(minutes=1)
        tr = f"[{tempstart.strftime('%Y%m%d%H%M%S')},{temptime.strftime('%Y%m%d%H%M%S')}]"
        res = _fetch_hhly_rainfall_for_emergency(tr)
        if res is not None and not res.empty:
            df = res if df is None else pd.concat([df, res], ignore_index=True)
        temptime += timedelta(hours=1)

    if df is None or df.empty:
        return pd.DataFrame()

    # 5min 聚合，与 stationProcessMin._append_hhly_5min_to_rolling_csv 同口径
    for col in ("Station_levl", "Lat", "Lon", "City", "Station_Name", "Cnty", "Province", "Town"):
        if col not in df.columns:
            df[col] = ""
    df_5min = (
        df.set_index("Datetime")
        .groupby("Station_Id_C")
        .resample("5min", label="right", closed="right")
        .agg({
            "PRE": "sum",
            "Station_levl": "first",
            "Lat": "first", "Lon": "first",
            "City": "first", "Station_Name": "first",
            "Cnty": "first", "Province": "first", "Town": "first",
        })
        .reset_index()
    )
    return df_5min


def main():
    parser = argparse.ArgumentParser(description="场景化验证：历史日期一键重现应急响应+报告全链路")
    parser.add_argument("--date", required=True, help="BJT 日期，YYYY-MM-DD，如 2023-07-30")
    parser.add_argument("--hour", type=int, default=20, help="BJT 小时，默认 20（对应当天 20:00 BJT）")
    args = parser.parse_args()

    end_time = datetime.strptime(f"{args.date} {args.hour:02d}:00:00", "%Y-%m-%d %H:%M:%S")
    print(f"=== 场景验证 ===")
    print(f"end_time (BJT) = {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"start_time (BJT) = {(end_time - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')}")

    # Step 1: 准备 HHLY_JUECE CSV
    print("\n[1/3] 拉取 HHLY_JUECE 24h 分钟降水 ...")
    juece_df = _fetch_juece_24h(end_time)
    if juece_df.empty:
        print(f"  ✗ 无 HHLY_JUECE 数据。检查 MUSIC 接口 / 时段。")
        return 1
    print(f"  ✓ 得到 {len(juece_df)} 条 5min 记录，{juece_df['Station_Id_C'].nunique()} 个站点")
    if os.path.exists(VERIFY_JUECE_CSV):
        os.remove(VERIFY_JUECE_CSV)
    juece_df.to_csv(VERIFY_JUECE_CSV, index=False, encoding="utf-8-sig")
    print(f"  ✓ 写入 {VERIFY_JUECE_CSV}（验证专用，不碰生产 CSV）")

    # Step 2: 准备 HHLY CSV
    print("\n[2/3] 拉取 HHLY 24h 分钟降水（应急响应数据源）...")
    hhly_df = _fetch_hhly_24h(end_time)
    if hhly_df.empty:
        print(f"  ✗ 无 HHLY 数据。应急响应将无法判定。")
    else:
        print(f"  ✓ 得到 {len(hhly_df)} 条记录，{hhly_df['Station_Id_C'].nunique()} 个站点")
        if os.path.exists(VERIFY_HHLY_CSV):
            os.remove(VERIFY_HHLY_CSV)
        hhly_df.to_csv(VERIFY_HHLY_CSV, index=False, encoding="utf-8-sig")
        print(f"  ✓ 写入 {VERIFY_HHLY_CSV}（验证专用，不碰生产 HHLY CSV）")

    # Step 3: 临时替换全局变量，让 calcmaxdataseg5min 读我们的 CSV
    print("\n[3/3] 调用 calcmaxdataseg5min()（完整业务流程）...")
    print("  - 5min/1h/24h 站点最大统计入库")
    print("  - 天津分县暴雨等级入库")
    print("  - 河流影响图（如≥50mm 站点存在则生成 GeoJSON）")
    print("  - 应急响应级别判定 + 入库")
    print("  - I-IV 级触发天河报告")
    _orig_tempfile = spm.tempfile
    _orig_hhly_tempfile = spm.hhly_tempfile
    try:
        spm.tempfile = VERIFY_JUECE_CSV
        spm.hhly_tempfile = VERIFY_HHLY_CSV
        calcmaxdataseg5min()
        print("\n=== ✓ 场景验证完成 ===")
        print(f"检查数据库表 qy_minute_monitor（含 geojsonurl / impact_city）")
        print(f"检查数据库表 qy_emergency_response_monitor（含 response_level）")
        print(f"验证 CSV（可删除）：{VERIFY_JUECE_CSV} / {VERIFY_HHLY_CSV}")
        return 0
    except Exception as e:
        print(f"\n=== ✗ calcmaxdataseg5min 执行失败：{e} ===")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        spm.tempfile = _orig_tempfile
        spm.hhly_tempfile = _orig_hhly_tempfile


if __name__ == "__main__":
    raise SystemExit(main())
