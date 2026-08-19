# -*- coding: utf-8 -*-
"""服务端实跑：核对 EC 累计降水文件命名/有效时次约定 + 点位采样是否出数。

背景：超滚动预报 240h（10 天）的点位日期会走 EC 降水回退（query_rolling_forecast_core
out_of_range + point_mode → sample_ec_point_daily_rain）。本脚本在能访问 EC 数据目录的
内网机器上实跑，确认候选窗口是否能命中真实文件、点位采样是否出数。

用法：
    python probe_ec_rain_layout.py [YYYY-MM-DD] [lon] [lat]
    # 例：python probe_ec_rain_layout.py 2026-09-01 116.8 40.4
"""
import sys
from datetime import date

import haihe_mcp_tools as hmt


def main() -> None:
    d = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2026, 9, 1)
    lon = float(sys.argv[2]) if len(sys.argv) > 2 else 116.8
    lat = float(sys.argv[3]) if len(sys.argv) > 3 else 40.4
    print("EC_OUTPUT_PATH =", hmt.DEFAULT_EC_OUTPUT_PATH)
    print(f"target_date={d}  lon={lon} lat={lat}")
    for st, h in hmt._ec_daily_window_candidates(d):
        p = hmt._find_ec_precip_file(hmt.DEFAULT_EC_OUTPUT_PATH, st, h)
        print(f"  candidate start={st:%Y-%m-%d %H:%M} {h}h -> {p or '（无）'}")
    r = hmt.sample_ec_point_daily_rain(lon, lat, d)
    print("sample_ec_point_daily_rain ->", r)
    if r is None:
        print("提示：未命中任何 EC 累计降水文件（或点位采样为空）。请核对 EC 目录布局/有效时次约定。")


if __name__ == "__main__":
    main()
