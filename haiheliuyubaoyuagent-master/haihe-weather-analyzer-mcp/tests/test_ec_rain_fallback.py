# -*- coding: utf-8 -*-
"""超 240h 点位日期的 EC 降水回退（sample_ec_point_daily_rain 及 query_rolling_forecast_core 挂钩）。

本地 .venv-test 无 GDAL/osgeo，故文件查找与采样层一律 monkeypatch，纯逻辑离线可测。
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

import haihe_mcp_tools as hmt
import rolling_forecast_service as rfs

NOW = datetime(2026, 8, 19, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


# ---- Task 3: _ec_daily_window_candidates ----

def test_candidates_prefer_24h_then_12h_then_6h():
    cands = hmt._ec_daily_window_candidates(date(2026, 9, 1))
    assert cands, "应生成候选"
    hours_seq = [h for _, h in cands]
    assert hours_seq[0] == 24
    # 所有 24h 排在所有 12h 前，所有 12h 排在所有 6h 前
    assert hours_seq == sorted(hours_seq, reverse=True)


def test_candidates_on_target_date():
    cands = hmt._ec_daily_window_candidates(date(2026, 9, 1))
    assert all(st.date() == date(2026, 9, 1) for st, _ in cands)
    assert all(st.tzinfo is not None for st, _ in cands)


# ---- Task 4: sample_ec_point_daily_rain（mock 文件查找 + 采样）----

def _patch(monkeypatch, files, samples):
    # files: dict[(start_str "YYYYmmddHH", hours)] -> path ; samples: dict[path] -> {"POI": mm}
    monkeypatch.setattr(
        hmt, "_find_ec_precip_file",
        lambda root, st, h: files.get((st.strftime("%Y%m%d%H"), h)),
    )
    monkeypatch.setattr(
        hmt, "_sample_station_forecast_rain_mm",
        lambda recs, path: samples.get(path, {}),
    )


def test_returns_first_hit(monkeypatch):
    _patch(monkeypatch,
           files={("2026090108", 24): "/ec/a.tif"},
           samples={"/ec/a.tif": {"POI": 12.5}})
    r = hmt.sample_ec_point_daily_rain(116.8, 40.4, date(2026, 9, 1))
    assert r["rain_mm"] == 12.5 and r["window_hours"] == 24


def test_none_when_no_file(monkeypatch):
    _patch(monkeypatch, files={}, samples={})
    assert hmt.sample_ec_point_daily_rain(116.8, 40.4, date(2026, 9, 1)) is None


def test_skips_file_with_no_point_value(monkeypatch):
    _patch(monkeypatch,
           files={("2026090108", 24): "/ec/a.tif", ("2026090100", 24): "/ec/b.tif"},
           samples={"/ec/a.tif": {}, "/ec/b.tif": {"POI": 3.0}})
    r = hmt.sample_ec_point_daily_rain(116.8, 40.4, date(2026, 9, 1))
    assert r["file"] == "/ec/b.tif" and r["rain_mm"] == 3.0


def test_sampler_exception_falls_through(monkeypatch):
    monkeypatch.setattr(
        hmt, "_find_ec_precip_file",
        lambda root, st, h: "/ec/x.tif" if (st.strftime("%H"), h) == ("08", 24) else None,
    )

    def boom(recs, path):
        raise RuntimeError("gdal missing")

    monkeypatch.setattr(hmt, "_sample_station_forecast_rain_mm", boom)
    assert hmt.sample_ec_point_daily_rain(116.8, 40.4, date(2026, 9, 1)) is None


# ---- Task 5: query_rolling_forecast_core out_of_range + point_mode 挂 EC 回退 ----

def test_out_of_range_point_mode_uses_ec(monkeypatch):
    monkeypatch.setattr(rfs, "_try_ec_rain_fallback", lambda *a, **k: {
        "status": "ec_rain_fallback", "target_date": "2026-09-01", "rain_mm": 5.0,
    })
    # 9月1日超 240h → calendar_error；point_mode → 走 EC
    r = rfs.query_rolling_forecast_core(
        "9月1日天气怎么样", lon=116.8, lat=40.4, point_name="密云水库", now=NOW
    )
    assert r["status"] == "ec_rain_fallback"


def test_out_of_range_point_mode_no_ec_keeps_out_of_range(monkeypatch):
    monkeypatch.setattr(rfs, "_try_ec_rain_fallback", lambda *a, **k: None)
    r = rfs.query_rolling_forecast_core(
        "9月1日天气怎么样", lon=116.8, lat=40.4, point_name="密云水库", now=NOW
    )
    assert r["status"] == "out_of_range"


def test_out_of_range_region_mode_no_ec(monkeypatch):
    # 非 point_mode 即使 EC 有数也不走点位 EC 回退
    called = []
    monkeypatch.setattr(
        rfs, "_try_ec_rain_fallback",
        lambda *a, **k: called.append(1) or {"status": "ec_rain_fallback"},
    )
    r = rfs.query_rolling_forecast_core("9月1日天气怎么样", regions="蓟州", now=NOW)
    assert r["status"] == "out_of_range" and not called


def test_resolve_ec_target_date_explicit():
    assert rfs._resolve_ec_target_date("9月1日天气怎么样", "", NOW) == date(2026, 9, 1)
    assert rfs._resolve_ec_target_date("明天天气", "2026-09-01", NOW) == date(2026, 9, 1)
    assert rfs._resolve_ec_target_date("明天天气", "", NOW) is None

