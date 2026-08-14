"""生产探针：验证 MUSIC ByTime 接口多时次（逗号连接）是否一次返回全部时次。

改动 B（MUSIC 多时次合并）的前提确认：`query_current_weather_observation` 现在把
6 个候选时次合并为 region/basin 各 1 次调用（12→2），只有当服务端对逗号连接 times
返回 ≥2 个不同时次时才走合并路径；否则自动回退逐时次串行（行为与改动前完全一致）。

本探针在内网服务器上运行，确认 getSurfEleInRegionByTime / getSurfEleInBasinByTime
对逗号连接 times 的实际响应。

用法（内网，需 MUSIC 账号 env）：
  cd haihe-weather-analyzer-mcp
  python probe_music_multi_times.py

预期：
  - [region]/[basin] distinct_times >= 2  → 合并路径生效（12→2）
  - distinct_times == 1                   → 服务端只回单时次，走回退（无回归）
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from current_weather_observation_service import (  # noqa: E402
    HAIHE_BASIN_CODE,
    OBSERVATION_DATA_CODE,
    OBSERVATION_ELEMENTS,
    REGION_ADMIN_CODES,
    _group_records_by_time,
    build_latest_utc_hour_candidates,
)
from haihe_mcp_tools import MusicClient, MusicConfig  # noqa: E402


def main() -> None:
    client = MusicClient(MusicConfig())
    candidates = build_latest_utc_hour_candidates()
    times_joined = ",".join(c.strftime("%Y%m%d%H%M%S") for c in candidates)
    print(f"请求时次（{len(candidates)} 个，逗号连接）: {times_joined}")

    probes = (
        (
            "region",
            lambda: client.get_surf_ele_in_region_by_time(
                admin_codes=REGION_ADMIN_CODES,
                times=times_joined,
                elements=OBSERVATION_ELEMENTS,
                data_code=OBSERVATION_DATA_CODE,
            ),
        ),
        (
            "basin",
            lambda: client.get_surf_ele_in_basin_by_time(
                basin_codes=HAIHE_BASIN_CODE,
                times=times_joined,
                elements=OBSERVATION_ELEMENTS,
                data_code=OBSERVATION_DATA_CODE,
            ),
        ),
    )
    for name, call in probes:
        try:
            records = call()
        except Exception as exc:
            print(f"[{name}] 多时次请求失败: {exc}")
            continue
        grouped = _group_records_by_time(records)
        distinct = len(grouped)
        counts = {k: len(v) for k, v in grouped.items()}
        top = dict(Counter(counts).most_common(6))
        print(f"[{name}] 返回 {len(records)} 条，distinct_times={distinct}")
        print(f"    时次分布: {top}")
        verdict = "✅ 合并路径生效（12→2）" if distinct >= 2 else "⚠️ 只回单时次，将走回退（行为不变）"
        print(f"    结论: {verdict}")


if __name__ == "__main__":
    main()
