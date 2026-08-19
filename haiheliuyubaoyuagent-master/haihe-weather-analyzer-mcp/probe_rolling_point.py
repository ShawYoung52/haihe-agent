# -*- coding: utf-8 -*-
"""服务端实跑：核对「密云水库」点位问答的两个疑点。

背景：『未来三天密云水库有降水吗？』的回答里出现两个异常——
  ① 点位名变成「密云水库医院」（search_poi 检索返回名，非用户问的水库本体）；
  ② 逐日表 天气/气温/风力 三列全 "—"，只有结论里的累计降水 0.0mm。
本脚本在能访问 ES 与滚动预报接口的内网机器上实跑，定性：
  - POI 检索到底返回了哪些条目（精确/模糊、各自经纬度）→ 定性疑点①；
  - 点位滚动预报各时段是否只回降水(TP1H)、天气/气温/风为空 → 定性疑点②。

用法：
    python probe_rolling_point.py [keyword] [lon] [lat]
    # 例：python probe_rolling_point.py 密云水库 116.8 40.4
"""
import sys
from datetime import datetime

import haihe_mcp_tools as hmt
import rolling_forecast_service as rfs


def main() -> None:
    keyword = sys.argv[1] if len(sys.argv) > 1 else "密云水库"
    lon = float(sys.argv[2]) if len(sys.argv) > 2 else 116.8
    lat = float(sys.argv[3]) if len(sys.argv) > 3 else 40.4

    print("=== 1) search_poi 检索结果（疑点①点位名）===")
    try:
        poi = hmt._search_poi_core(keyword, size=8)
        pois = (poi or {}).get("pois") or []
        print(f"match_type={(poi or {}).get('match_type')}  total={(poi or {}).get('total')}  返回 {len(pois)} 条")
        for i, p in enumerate(pois):
            print(
                f"  [{i}] name={p.get('name')!r} lon={p.get('longitude')} lat={p.get('latitude')} "
                f"cat={p.get('category_1')}/{p.get('category_2')} addr={p.get('address')}"
            )
        if not pois:
            print("  （无任何 POI 返回）")
    except Exception as exc:
        print("  search_poi 失败：", type(exc).__name__, exc)

    print("\n=== 2) 滚动预报点位要素（calendar_daily_point，未来三天，疑点②空表）===")
    now = datetime.now(rfs.TIANJIN_TIMEZONE)
    try:
        r = rfs.query_rolling_forecast_core(
            f"未来三天{keyword}有降水吗", lon=lon, lat=lat, point_name=keyword, now=now,
        )
    except Exception as exc:
        print("  query_rolling_forecast_core 失败：", type(exc).__name__, exc)
        return
    print(
        "query_mode =", r.get("query_mode"),
        " api_code =", r.get("api_code"),
        " api_message =", r.get("api_message"),
    )
    periods = r.get("periods") or []
    print(f"periods 共 {len(periods)} 条：")
    for p in periods:
        print(
            f"  {p.get('period_label')}  WEA={p.get('WEA')!r} TMAX={p.get('TMAX')!r} "
            f"TMIN={p.get('TMIN')!r} EDA={p.get('EDA')!r} VISMIN={p.get('VISMIN')!r} TP1H={p.get('TP1H')!r}"
        )
    if not periods:
        print("  （无 periods——点位预报无数据，需另查）")
    elif all(
        p.get("WEA") is None and p.get("TMAX") is None and p.get("TMIN") is None and p.get("EDA") is None
        for p in periods
    ):
        has_rain = any(p.get("TP1H") is not None for p in periods)
        print(
            "\n结论：该点位滚动预报天气/气温/风全空"
            + ("、仅降水(TP1H)有值 → 决策天气对这类点位应只渲染降水列，不要出全空表。" if has_rain else "、连降水也无 → 点位预报整体无数据。")
        )
    else:
        print("\n结论：点位预报天气/气温/风有值——空表不是数据问题，需查字段映射。")


if __name__ == "__main__":
    main()
