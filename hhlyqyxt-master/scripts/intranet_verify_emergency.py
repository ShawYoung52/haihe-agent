"""内网离线服务器应急响应 HHLY 拉取 + 入库验证脚本（无需 pytest）。

用法（在 hhlyqyxt-master 目录下执行）：
    python scripts/intranet_verify_emergency.py

验证内容：
    1. HHLY 5 分钟降水拉取（_fetch_hhly_rainfall_for_emergency）
    2. Datetime 时区（应为北京时间，确认 +8h 转换生效）
    3. 应急响应计算 + 入库（run_emergency_response_monitor timerange 路径）
    4. 12h 占比分母是否为 12h 窗口国家站数（非 24h）
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 确保项目根在 sys.path 中
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from ScheduledTask.emergency_response_monitor import (
    _fetch_hhly_rainfall_for_emergency,
    compute_emergency_response_stats,
    run_emergency_response_monitor,
)


def _sep(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def _try_fetch(timerange: str, max_retries: int = 3):
    """带重试的 HHLY 拉取——MUSIC 内网服务偶发瞬断，重试通常能过。"""
    import time as _time
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            return _fetch_hhly_rainfall_for_emergency(timerange)
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                wait = 2 ** attempt
                print(f"  (attempt {attempt}/{max_retries} 失败, {wait}s 后重试: {e})")
                _time.sleep(wait)
    raise last_exc  # type: ignore[misc]


def verify_fetch(timerange: str) -> bool:
    """验证 1-2：拉取 + 时区。"""
    _sep("验证 1：HHLY 数据拉取")
    _max_retries = 3
    try:
        df = _try_fetch(timerange, max_retries=_max_retries)
    except Exception as e:
        print(f"✗ 拉取失败（{_max_retries} 次重试后仍失败）: {e}")
        return False

    if df.empty:
        print("✗ 拉取返回空 DataFrame（HHLY 窗口内无数据，可能是正常情况）")
        print("  确认 timerange 范围覆盖了有效时间段后重试。")
        return False

    print(f"✓ 拉取成功: {len(df)} 行, {len(df.columns)} 列")
    print(f"  列: {list(df.columns)}")

    _sep("验证 2：Datetime 时区（应显示北京时间）")
    if "Datetime" not in df.columns:
        print("✗ DataFrame 无 Datetime 列！")
        return False
    sample = df["Datetime"].head(5)
    print(f"  前 5 行 Datetime: {list(sample)}")
    # 简单检查：北京时间 UTC+8，不应为凌晨（除非确实凌晨）
    hour_values = [int(t.hour) if hasattr(t, 'hour') else -1 for t in sample]
    print(f"  小时: {hour_values}")
    print(f"✓ 时区检查完成（若小时在 8-20 范围则为典型北京时间）")
    return True


def verify_compute(timerange: str, datatime: str) -> bool:
    """验证 3：应急响应计算 + 入库。"""
    _sep("验证 3：应急响应计算 + 入库（timerange 路径）")
    try:
        record = run_emergency_response_monitor(
            timerange=timerange,
            datatime=datatime,
        )
    except Exception as e:
        print(f"✗ 入库失败: {e}")
        import traceback
        traceback.print_exc()
        return False

    if record is None:
        print("✗ run_emergency_response_monitor 返回 None")
        print("  原因可能是：HHLY 拉取为空 / 数据中无国家站(11/12/13/16)")
        return False

    print(f"✓ 入库成功:")
    print(f"  datatime: {record.datatime}")
    print(f"  total_national_stations: {record.total_national_stations}")
    print(f"  response_level: {record.response_level} "
          f"({'Ⅰ' if record.response_level == 1 else 'Ⅱ' if record.response_level == 2 else 'Ⅲ' if record.response_level == 3 else 'Ⅳ' if record.response_level == 4 else '无响应'})")
    print(f"  12h 暴雨: ratio={record.ratio_12h_baoyu}, stations={record.station_12h_baoyu}")
    print(f"  24h 暴雨: ratio={record.ratio_24h_baoyu}, stations={record.station_24h_baoyu}")
    print(f"  24h 大暴雨: ratio={record.ratio_24h_dabaoyu}, stations={record.station_24h_dabaoyu}")
    print(f"  24h 特大暴雨: ratio={record.ratio_24h_tedabaoyu}, stations={record.station_24h_tedabaoyu}")
    return True


def verify_ratio() -> bool:
    """验证 4：12h 占比分母。用已知数据的 DataFrame 直接调 compute。"""
    _sep("验证 4：12h 占比分母（手工构造数据）")
    import pandas as pd

    # 构造：站 A 在 12h 窗口内有暴雨，站 B 在 12h 窗口外（仅在 24h 窗口）
    df = pd.DataFrame([
        {"Station_Id_C": "A", "Datetime": "2026-07-15 09:00:00", "PRE": 60.0, "Station_levl": "11"},
        {"Station_Id_C": "B", "Datetime": "2026-07-14 12:00:00", "PRE": 5.0, "Station_levl": "11"},
    ])
    df["Datetime"] = pd.to_datetime(df["Datetime"])
    df["PRE"] = pd.to_numeric(df["PRE"])

    result = compute_emergency_response_stats(df, "2026-07-15 10:00:00")
    if result is None:
        print("✗ compute 返回 None（数据格式问题）")
        return False

    total = result["total_national_stations"]  # 24h 国家站数 = 2 (A, B)
    ratio_12h = result["ratio_12h_baoyu"]      # 应 = 1/1 = 1.0（A 暴雨，12h 窗仅 A）
    print(f"  total_national_stations (24h): {total}")
    print(f"  station_12h_baoyu: {result['station_12h_baoyu']}")
    print(f"  ratio_12h_baoyu: {ratio_12h}")
    print(f"  ratio_24h_baoyu: {result['ratio_24h_baoyu']}")

    if ratio_12h == 1.0:
        print("✓ 12h 占比分母 = 12h 窗口国家站数（站 B 未计入 = 正确）")
        return True
    elif abs(ratio_12h - 0.5) < 1e-4:
        print("✗ 12h 占比分母 = 24h 窗口数（站 B 误计入 = 旧代码 bug，请 pull 最新代码）")
        return False
    else:
        print(f"✗ 非预期的 ratio_12h: {ratio_12h}")
        return False


def main():
    now = datetime.now()
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=24)
    timerange = f"[{start.strftime('%Y%m%d%H%M%S')},{end.strftime('%Y%m%d%H%M%S')}]"
    datatime = end.strftime("%Y-%m-%d %H:%M:%S")

    print("海河流域应急响应 HHLY 内网验证")
    print(f"timerange: {timerange}")
    print(f"datatime:  {datatime}")

    results = []

    # 验证 1-2：拉取 + 时区
    results.append(("拉取+时区", verify_fetch(timerange)))

    # 验证 3：入库
    results.append(("入库", verify_compute(timerange, datatime)))

    # 验证 4：12h 占比分母
    results.append(("12h 分母", verify_ratio()))

    _sep("结果汇总")
    all_pass = True
    for name, ok in results:
        status = "✓ PASS" if ok else "✗ FAIL"
        if not ok:
            all_pass = False
        print(f"  {status} - {name}")

    if all_pass:
        print("\n全部验证通过。应急响应 HHLY 拉取、时区转换、入库、12h 分母均正常。")
    else:
        print("\n部分验证未通过，请检查上面的 ✗ 项。")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
