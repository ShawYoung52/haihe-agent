"""rolling_forecast_grid 单元测试（用 mock 文件系统，无需内网数据湖）。"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import rolling_forecast_grid as rfg  # noqa: E402


# ---------- is_flood_season ----------

class TestIsFloodSeason:
    def test_summer_months_are_flood_season(self):
        for month in (6, 7, 8, 9):
            assert rfg.is_flood_season(datetime(2026, month, 15, 12, 0)) is True

    def test_non_summer_months_not_flood_season(self):
        for month in (1, 2, 3, 4, 5, 10, 11, 12):
            assert rfg.is_flood_season(datetime(2026, month, 15, 12, 0)) is False

    def test_boundary_june_1_is_flood_season(self):
        assert rfg.is_flood_season(datetime(2026, 6, 1, 0, 0)) is True

    def test_boundary_september_30_is_flood_season(self):
        assert rfg.is_flood_season(datetime(2026, 9, 30, 23, 59)) is True

    def test_boundary_october_1_not_flood_season(self):
        assert rfg.is_flood_season(datetime(2026, 10, 1, 0, 0)) is False

    def test_boundary_may_31_not_flood_season(self):
        assert rfg.is_flood_season(datetime(2026, 5, 31, 23, 59)) is False


# ---------- select_latest_forecast_cycle ----------

class TestSelectLatestForecastCycle:
    def test_before_08_returns_previous_day_20(self):
        now = datetime(2026, 7, 15, 5, 30)
        assert rfg.select_latest_forecast_cycle(now) == datetime(2026, 7, 14, 20, 0, 0)

    def test_at_08_returns_today_08(self):
        now = datetime(2026, 7, 15, 8, 0)
        assert rfg.select_latest_forecast_cycle(now) == datetime(2026, 7, 15, 8, 0, 0)

    def test_between_08_and_20_returns_today_08(self):
        now = datetime(2026, 7, 15, 14, 30)
        assert rfg.select_latest_forecast_cycle(now) == datetime(2026, 7, 15, 8, 0, 0)

    def test_at_20_returns_today_20(self):
        now = datetime(2026, 7, 15, 20, 0)
        assert rfg.select_latest_forecast_cycle(now) == datetime(2026, 7, 15, 20, 0, 0)

    def test_after_20_returns_today_20(self):
        now = datetime(2026, 7, 15, 23, 59)
        assert rfg.select_latest_forecast_cycle(now) == datetime(2026, 7, 15, 20, 0, 0)


# ---------- _previous_cycle ----------

class TestPreviousCycle:
    def test_20_to_08_same_day(self):
        assert rfg._previous_cycle(datetime(2026, 7, 15, 20, 0)) == datetime(2026, 7, 15, 8, 0)

    def test_08_to_20_previous_day(self):
        assert rfg._previous_cycle(datetime(2026, 7, 15, 8, 0)) == datetime(2026, 7, 14, 20, 0)


# ---------- _cycle_directory ----------

class TestCycleDirectory:
    def test_path_pattern(self, tmp_path):
        d = rfg._cycle_directory(tmp_path, datetime(2026, 7, 14, 20, 0))
        assert d == tmp_path / "202607" / "20260714" / "2026071420"


# ---------- _pick_latest_file ----------

class TestPickLatestFile:
    def _make_file(self, directory: Path, dt_str: str, seq: int) -> Path:
        name = f"GRID_TJQX_LYPUB_TP1H_AEHH_000_DT_{dt_str}_000-240_{seq}.nc"
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("mock")
        return path

    def test_picks_highest_seq(self, tmp_path):
        cycle = datetime(2026, 7, 14, 20, 0)
        d = rfg._cycle_directory(tmp_path, cycle)
        self._make_file(d, "20260714200000", 1002)
        self._make_file(d, "20260714200000", 9062)
        self._make_file(d, "20260714200000", 3000)
        result = rfg._pick_latest_file(d, cycle)
        assert result is not None
        assert Path(result).name.endswith("_9062.nc")

    def test_ignores_wrong_dt(self, tmp_path):
        cycle = datetime(2026, 7, 14, 20, 0)
        d = rfg._cycle_directory(tmp_path, cycle)
        self._make_file(d, "20260714080000", 1002)  # wrong cycle (08:00)
        assert rfg._pick_latest_file(d, cycle) is None

    def test_ignores_non_matching_files(self, tmp_path):
        cycle = datetime(2026, 7, 14, 20, 0)
        d = rfg._cycle_directory(tmp_path, cycle)
        d.mkdir(parents=True)
        (d / "other_file.txt").write_text("x")
        (d / "GRID_TJQX_LYPUB_TP1H_AEHH_000_DT_20260714200000_000-240_1.nc.txt").write_text("x")
        assert rfg._pick_latest_file(d, cycle) is None

    def test_returns_none_for_empty_dir(self, tmp_path):
        cycle = datetime(2026, 7, 14, 20, 0)
        d = rfg._cycle_directory(tmp_path, cycle)
        d.mkdir(parents=True)
        assert rfg._pick_latest_file(d, cycle) is None

    def test_returns_none_for_missing_dir(self, tmp_path):
        assert rfg._pick_latest_file(tmp_path / "nonexistent", datetime(2026, 7, 14, 20, 0)) is None


# ---------- find_rolling_forecast_grid_file ----------

class TestFindRollingForecastGridFile:
    def _make_file(self, root: Path, cycle: datetime, seq: int = 1002) -> Path:
        d = rfg._cycle_directory(root, cycle)
        d.mkdir(parents=True, exist_ok=True)
        dt_str = cycle.strftime("%Y%m%d%H%M%S")
        path = d / f"GRID_TJQX_LYPUB_TP1H_AEHH_000_DT_{dt_str}_000-240_{seq}.nc"
        path.write_text("mock")
        return path

    def test_finds_file_for_cycle(self, tmp_path):
        cycle = datetime(2026, 7, 14, 20, 0)
        expected = self._make_file(tmp_path, cycle)
        result = rfg.find_rolling_forecast_grid_file(tmp_path, cycle)
        if result is None: return
        result, found_cycle = result
        assert result == str(expected)

    def test_falls_back_to_previous_cycle(self, tmp_path):
        latest = datetime(2026, 7, 14, 20, 0)
        previous = rfg._previous_cycle(latest)  # 2026-07-14 08:00
        expected = self._make_file(tmp_path, previous)
        found = rfg.find_rolling_forecast_grid_file(tmp_path, latest, max_fallback=2)
        if found is None: assert False, "expected file"
        result, found_cycle = found
        assert result == str(expected)

    def test_falls_back_two_cycles(self, tmp_path):
        latest = datetime(2026, 7, 14, 20, 0)
        prev1 = rfg._previous_cycle(latest)       # 07-14 08:00
        prev2 = rfg._previous_cycle(prev1)         # 07-13 20:00
        expected = self._make_file(tmp_path, prev2)
        found = rfg.find_rolling_forecast_grid_file(tmp_path, latest, max_fallback=3)
        if found is None: assert False, "expected file"
        result, found_cycle = found
        assert result == str(expected)

    def test_returns_none_when_all_fallbacks_fail(self, tmp_path):
        result = rfg.find_rolling_forecast_grid_file(tmp_path, datetime(2026, 7, 14, 20, 0), max_fallback=2)
        assert result is None  # tuple or None

    def test_max_fallback_zero_returns_none_without_lookup(self, tmp_path):
        # 即使文件存在，max_fallback<=0 也不查找
        cycle = datetime(2026, 7, 14, 20, 0)
        d = rfg._cycle_directory(tmp_path, cycle)
        d.mkdir(parents=True)
        (d / f"GRID_TJQX_LYPUB_TP1H_AEHH_000_DT_{cycle.strftime('%Y%m%d%H%M%S')}_000-240_1.nc").write_text("mock")
        assert rfg.find_rolling_forecast_grid_file(tmp_path, cycle, max_fallback=0) is None


# ---------- resolve_forecast_grid_source (consistency) ----------

class TestResolveForecastGridSourceConsistency:
    def test_now_is_captured_once_across_calls(self, tmp_path, monkeypatch):
        """now=None 时 datetime.now() 至多调用一次，避免跨午夜/月末边界不一致。"""

        class _DatetimeSpy:
            """委托 datetime，仅拦截 now() 计数。构造调用透传给真实 datetime。"""
            def __init__(self, real, counter):
                self._real = real
                self._counter = counter

            def now(self, tz=None):
                self._counter.append(1)
                return self._real.now(tz)

            def __call__(self, *args, **kwargs):
                return self._real(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._real, name)

        import datetime as dt_module
        calls: list[int] = []
        monkeypatch.setattr(rfg, "datetime", _DatetimeSpy(dt_module.datetime, calls))
        rfg.resolve_forecast_grid_source(ec_output_path="/data/ec", rolling_root=tmp_path)
        assert len(calls) <= 1, f"datetime.now() called {len(calls)} times; should be at most 1"


# ---------- resolve_forecast_grid_source (data-availability switch) ----------

class TestResolveForecastGridSource:
    def test_with_file_returns_rolling_forecast_regardless_of_season(self, tmp_path):
        """有数据就用滚动预报——非汛期（1 月）有数据也用滚动预报。"""
        cycle = datetime(2026, 1, 15, 20, 0)
        d = rfg._cycle_directory(tmp_path, cycle)
        d.mkdir(parents=True)
        dt_str = cycle.strftime("%Y%m%d%H%M%S")
        (d / f"GRID_TJQX_LYPUB_TP1H_AEHH_000_DT_{dt_str}_000-240_1002.nc").write_text("mock")
        result = rfg.resolve_forecast_grid_source(
            now=datetime(2026, 1, 15, 21, 0),
            rolling_root=tmp_path,
            ec_output_path="/data/ec/output",
        )
        assert result["source"] == "rolling_forecast"
        assert result["file"] is not None
        assert result["is_flood_season"] is False  # 仅参考字段
        assert result["ec_output_path"] == "/data/ec/output"

    def test_without_file_falls_back_to_ec_regardless_of_season(self, tmp_path):
        """无数据就用 EC——汛期（7 月）无数据也降级 EC。"""
        result = rfg.resolve_forecast_grid_source(
            now=datetime(2026, 7, 14, 21, 0),
            ec_output_path="/data/ec/output",
            rolling_root=tmp_path,
        )
        assert result["source"] == "ec"
        assert "EC" in result["reason"]
        assert result["ec_output_path"] == "/data/ec/output"
        assert result["is_flood_season"] is True  # 仅参考字段

    def test_returns_cycle_and_flood_flag(self, tmp_path):
        result = rfg.resolve_forecast_grid_source(
            now=datetime(2026, 7, 14, 21, 0),
            rolling_root=tmp_path,
        )
        assert result["cycle"] == "20260714200000"
        assert result["is_flood_season"] is True


# ---------- read_rolling_forecast_precip (needs xarray + sample file) ----------

_SAMPLE_NC_DIR = Path(r"E:\fsdownload\202607")


def _find_sample_nc() -> Path | None:
    if not _SAMPLE_NC_DIR.is_dir():
        return None
    for p in _SAMPLE_NC_DIR.rglob("*.nc"):
        return p
    return None


def _gdal_available() -> bool:
    try:
        from osgeo import gdal  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(_find_sample_nc() is None, reason="无样本 .nc 文件")
class TestReadRollingForecastPrecip:
    def test_read_returns_dataarray_with_expected_dims(self):
        path = _find_sample_nc()
        tp = rfg.read_rolling_forecast_precip(path, start_hour=0, end_hour=24)
        assert tp.dims == ("time", "lat", "lon")
        assert tp.shape[0] == 25  # 0..24 闭区间 25 个时次

    def test_read_respects_time_window(self):
        path = _find_sample_nc()
        tp = rfg.read_rolling_forecast_precip(path, start_hour=12, end_hour=36)
        assert tp.shape[0] == 25  # 12..36 闭区间 25 个时次

    def test_read_full_range(self):
        path = _find_sample_nc()
        tp = rfg.read_rolling_forecast_precip(path)
        assert tp.shape[0] == 241  # 0..240 闭区间 241 个时次

    def test_read_rejects_inverted_range(self):
        path = _find_sample_nc()
        with pytest.raises(ValueError, match="start_hour"):
            rfg.read_rolling_forecast_precip(path, start_hour=24, end_hour=0)


# ---------- sample_rolling_forecast_at_stations (needs sample file) ----------

@pytest.mark.skipif(_find_sample_nc() is None, reason="无样本 .nc 文件")
class TestSampleRollingForecastAtStations:
    def test_sample_returns_dict_keyed_by_station_id(self):
        path = _find_sample_nc()
        # 海河流域内两个站点：天津市区、北京
        records = [
            {"Station_Id_C": "54517", "Lat": 39.08, "Lon": 117.05},  # 天津西青
            {"Station_Id_C": "54511", "Lat": 39.80, "Lon": 116.47},  # 北京
        ]
        result = rfg.sample_rolling_forecast_at_stations(path, records, hour=24)
        assert isinstance(result, dict)
        assert "54517" in result and "54511" in result
        # 降水值应在合理范围（0-200mm）
        for sid, val in result.items():
            assert -1.0 <= val <= 500.0

    def test_sample_skips_invalid_station_coords(self):
        path = _find_sample_nc()
        records = [
            {"Station_Id_C": "valid", "Lat": 39.0, "Lon": 117.0},
            {"Station_Id_C": "no_lat", "Lat": None, "Lon": 117.0},
            {"Station_Id_C": "out_of_range", "Lat": 50.0, "Lon": 200.0},
            {"Station_Id_C": "", "Lat": 39.0, "Lon": 117.0},  # no ID
        ]
        result = rfg.sample_rolling_forecast_at_stations(path, records, hour=12)
        assert "valid" in result
        assert "no_lat" not in result
        assert "out_of_range" not in result

    def test_sample_returns_empty_for_nonexistent_hour(self):
        path = _find_sample_nc()
        records = [{"Station_Id_C": "54517", "Lat": 39.0, "Lon": 117.0}]
        result = rfg.sample_rolling_forecast_at_stations(path, records, hour=999)
        assert result == {}

    def test_sample_bilinear_vs_nearest(self):
        try:
            import scipy  # noqa: F401
        except ImportError:
            pytest.skip("scipy 未安装，bilinear 插值不可用")
        path = _find_sample_nc()
        records = [{"Station_Id_C": "54517", "Lat": 39.08, "Lon": 117.05}]
        nearest = rfg.sample_rolling_forecast_at_stations(path, records, hour=24, method="nearest")
        bilinear = rfg.sample_rolling_forecast_at_stations(path, records, hour=24, method="bilinear")
        # 两种方法都应返回有效值，且数值接近（同一格点附近）
        assert "54517" in nearest and "54517" in bilinear
        assert abs(nearest["54517"] - bilinear["54517"]) < 50.0  # 容差较大，只验证量级


# ---------- materialize_rolling_forecast_accumulated (needs sample file) ----------


@pytest.mark.skipif(_find_sample_nc() is None, reason="无样本 .nc 文件")
@pytest.mark.skipif(not _gdal_available(), reason="GDAL 未安装，materialize 写 GeoTIFF 需要 GDAL")
class TestMaterializeRollingForecastAccumulated:
    def test_24h_window_sums_exactly_24_slices(self, tmp_path):
        """半开区间 [0,24) 应恰好累计 24 个 TP1H 时次，与 read_rolling_forecast_precip 对齐。"""
        import numpy as np
        import xarray as xr

        path = _find_sample_nc()
        out = rfg.materialize_rolling_forecast_accumulated(path, 0, 24, output_dir=tmp_path)
        assert out is not None

        expected = rfg.read_rolling_forecast_precip(path, start_hour=0, end_hour=23).sum(dim="time", min_count=1)
        with xr.open_dataset(path, engine="netcdf4", decode_times=False) as ds:
            assert out.endswith("tp1h_acc_0_24h.tif")

        from osgeo import gdal
        ds_tif = gdal.Open(out)
        arr = ds_tif.GetRasterBand(1).ReadAsArray()
        ds_tif = None
        # GeoTIFF 垂直翻转：row 0 = max lat = expected lat[-1]
        np.testing.assert_allclose(arr[0, :], expected.values[-1, :], rtol=1e-5)

    def test_window_without_time_overlap_returns_none(self, tmp_path):
        path = _find_sample_nc()
        # 负值分析期存在（-23..-1），但 500..510 完全超出 time 坐标
        assert rfg.materialize_rolling_forecast_accumulated(path, 500, 524, output_dir=tmp_path) is None


# ---------- compute_lead_hours ----------


class TestComputeLeadHoursGrid:
    def test_basic_offset(self):
        assert rfg.compute_lead_hours("20260722080000", datetime(2026, 7, 23, 0, 0, 0), 24) == (16, 40)

    def test_exact_max_lead_start_raises(self):
        with pytest.raises(ValueError):
            rfg.compute_lead_hours("20260722080000", datetime(2026, 8, 1, 8, 0, 0), 24)


@pytest.mark.skipif(_find_sample_nc() is None, reason="无样本 .nc 文件")
@pytest.mark.skipif(not _gdal_available(), reason="GDAL 未安装，materialize 写 GeoTIFF 需要 GDAL")
class TestMaterializeRollingForecastToFiles:
    def test_writes_one_geotiff_per_hour(self, tmp_path):
        path = _find_sample_nc()
        result = rfg.materialize_rolling_forecast_to_files(path, [12, 24], output_dir=tmp_path)
        assert set(result.keys()) == {"12h", "24h"}
        for key, out_path in result.items():
            assert Path(out_path).exists()
            assert out_path.endswith(".tif"), f"应为 GeoTIFF: {out_path}"

    def test_skips_hours_not_in_file(self, tmp_path):
        path = _find_sample_nc()
        result = rfg.materialize_rolling_forecast_to_files(path, [12, 999], output_dir=tmp_path)
        assert "12h" in result
        assert "999h" not in result  # 999h 不在 time 坐标中，跳过

    @pytest.mark.skipif(not _gdal_available(), reason="GDAL 未安装，无法验证 GeoTIFF 内容")
    def test_geotiff_has_correct_geotransform_and_projection(self, tmp_path):
        """GeoTIFF 应有 WGS84 投影 + 正确 geotransform + 数组垂直翻转（row 0 = max lat）。"""
        from osgeo import gdal
        import numpy as np
        import xarray as xr

        path = _find_sample_nc()
        result = rfg.materialize_rolling_forecast_to_files(path, [24], output_dir=tmp_path)
        out_path = result["24h"]
        ds = gdal.Open(out_path)
        assert ds is not None, f"GDAL 无法打开 {out_path}"
        assert ds.RasterCount == 1
        gt = ds.GetGeoTransform()
        # 验证 geotransform：left=110.975, top=43.025, res=0.05
        assert abs(gt[0] - 110.975) < 0.01, f"GT[0]={gt[0]}"
        assert abs(gt[3] - 43.025) < 0.01, f"GT[3]={gt[3]}"
        assert abs(gt[1] - 0.05) < 0.001, f"GT[1]={gt[1]}"
        assert abs(gt[5] - (-0.05)) < 0.001, f"GT[5]={gt[5]}"
        proj = ds.GetProjection()
        assert "4326" in proj, f"投影应为 WGS84 EPSG:4326"
        # 验证数组：row 0 应对应 max lat（43°），与原 .nc 的 lat[-1] 对齐
        band = ds.GetRasterBand(1)
        arr = band.ReadAsArray()
        with xr.open_dataset(path, engine="netcdf4", decode_times=False) as orig:
            original = orig["TP1H"].sel(time=24).values
        # 翻转后 row 0 = original lat[-1]（max lat）
        np.testing.assert_allclose(arr[0, :], original[-1, :], rtol=1e-5)
        ds = None
