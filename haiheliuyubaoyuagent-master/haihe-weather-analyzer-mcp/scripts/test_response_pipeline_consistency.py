#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
四段工具链回归检查（轻量脚本，不依赖 pytest）。

覆盖两类回归点：
1) 观测口径：旧判定函数 vs 四段拼装结果一致；
2) 预报口径：缺文件 / 空站点两类错误在旧入口与新入口结构一致。

用法：
  python scripts/test_response_pipeline_consistency.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _test_observation_consistency() -> None:
    import haihe_mcp_tools as hm
    from constants import DEFAULT_THRESHOLDS_MM

    sample_records = [
        {
            "Station_Id_C": "A001",
            "Station_levl": "11",
            "Lat": 39.10,
            "Lon": 117.20,
            "PRE_1h": 0.5,
            "PRE_12h": 60.0,
            "PRE_24h": 85.0,
            "Year": "2026",
            "Mon": "03",
            "Day": "08",
            "Hour": "08",
            "Station_Name": "测试站1",
        },
        {
            "Station_Id_C": "A002",
            "Station_levl": "12",
            "Lat": 39.20,
            "Lon": 117.30,
            "PRE_1h": 0.4,
            "PRE_12h": 52.0,
            "PRE_24h": 58.0,
            "Year": "2026",
            "Mon": "03",
            "Day": "08",
            "Hour": "08",
            "Station_Name": "测试站2",
        },
        {
            "Station_Id_C": "A003",
            "Station_levl": "13",
            "Lat": 39.30,
            "Lon": 117.40,
            "PRE_1h": 0.0,
            "PRE_12h": 10.0,
            "PRE_24h": 20.0,
            "Year": "2026",
            "Mon": "03",
            "Day": "08",
            "Hour": "08",
            "Station_Name": "测试站3",
        },
    ]
    allowed = ""
    allowed_list = [x.strip() for x in allowed.split(",") if x.strip()]

    old = hm.evaluate_observation_response(sample_records, allowed_station_levels=allowed_list)
    filtered = hm._observation_filter_core(sample_records, allowed)
    evaluated = hm._observation_evaluate_core(
        records=filtered["records"],
        allowed_station_levels=filtered["allowed_station_levels"],
        neighbor_km=50.0,
        sustain_hourly_threshold_mm=0.1,
        rainstorm_12h=DEFAULT_THRESHOLDS_MM["rainstorm_12h"],
        rainstorm_24h=DEFAULT_THRESHOLDS_MM["rainstorm_24h"],
        severe_rainstorm_24h=DEFAULT_THRESHOLDS_MM["severe_rainstorm_24h"],
        extraordinary_24h=DEFAULT_THRESHOLDS_MM["extraordinary_24h"],
    )
    new = hm._observation_report_core(
        evaluation=evaluated,
        basin_codes="HHLY",
        times="20260308080000",
        allowed_station_levels=filtered["allowed_station_levels"],
    )

    _assert(old.get("level") == new.get("level"), "观测判定 level 不一致")
    _assert(old.get("triggered") == new.get("triggered"), "观测判定 triggered 不一致")


def _test_forecast_missing_file_consistency() -> None:
    import haihe_mcp_tools as hm

    st = "2026030800"
    bad_path = os.path.join(_ROOT, "data", "path_not_exists_for_regression")

    old_msg = ""
    new_msg = ""
    try:
        hm.evaluate_haihe_forecast_emergency_response_core(start_time=st, ec_output_path=bad_path)
    except Exception as exc:
        old_msg = str(exc)
    try:
        hm._forecast_fetch_core(
            start_time=st,
            basin_codes="HHLY",
            ec_output_path=bad_path,
            allowed_station_levels="",
        )
    except Exception as exc:
        new_msg = str(exc)

    _assert("未找到起报" in old_msg, "旧预报入口缺文件报错不符合预期")
    _assert("未找到起报" in new_msg, "新预报入口缺文件报错不符合预期")


def _test_forecast_empty_station_consistency() -> None:
    import haihe_mcp_tools as hm

    old_find = hm._find_ec_precip_file
    old_music = hm.MusicClient

    class _FakeMusicClient:
        def get_surf_ele_in_basin_by_time(self, **kwargs):
            return []

    def _fake_find(ec_output_path: str, start_time: datetime, forecast_hours: int):
        # 只要给 12h/24h 非空路径，流程就会继续走到“空站点”错误。
        if forecast_hours in (12, 24):
            return f"/tmp/fake_{forecast_hours}h.tif"
        return None

    try:
        hm._find_ec_precip_file = _fake_find
        hm.MusicClient = _FakeMusicClient

        old_msg = ""
        new_msg = ""
        try:
            hm.evaluate_haihe_forecast_emergency_response_core(
                start_time="2026030800",
                ec_output_path="/tmp",
            )
        except Exception as exc:
            old_msg = str(exc)
        try:
            hm._forecast_fetch_core(
                start_time="2026030800",
                basin_codes="HHLY",
                ec_output_path="/tmp",
                allowed_station_levels="",
            )
        except Exception as exc:
            new_msg = str(exc)

        _assert("没有可用于预报判定的国家站" in old_msg, "旧预报入口空站点报错不符合预期")
        _assert("没有可用于预报判定的国家站" in new_msg, "新预报入口空站点报错不符合预期")
    finally:
        hm._find_ec_precip_file = old_find
        hm.MusicClient = old_music


def main() -> None:
    _test_observation_consistency()
    _test_forecast_missing_file_consistency()
    _test_forecast_empty_station_consistency()
    print("PASS: response pipeline consistency checks passed.")


if __name__ == "__main__":
    main()
