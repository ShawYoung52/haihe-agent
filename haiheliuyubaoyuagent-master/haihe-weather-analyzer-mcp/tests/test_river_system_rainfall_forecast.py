"""河系降雨预报工具单元测试。"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import river_system_forecast as rsf  # noqa: E402
import rolling_forecast_grid as rfg  # noqa: E402
from analyzers.RainfallAnalyzer import resolve_forecast_raster_path  # noqa: E402


def _gdal_available() -> bool:
    try:
        from osgeo import gdal, ogr  # noqa: F401
        return True
    except ImportError:
        return False


class TestToolExists:
    def test_get_river_system_rainfall_forecast_is_callable(self):
        assert hasattr(rsf, "get_river_system_rainfall_forecast")
        assert callable(rsf.get_river_system_rainfall_forecast)


class TestLoadZoneBoundaries:
    @pytest.mark.skipif(not _gdal_available(), reason="GDAL 未安装，无法解析 WKB")
    def test_load_zone_boundaries_parses_db_rows(self, monkeypatch):
        # 有效的 WKB POINT(0 0) 与 POINT(1 1)
        fake_rows = [
            {"zone_name": "大清河", "zone_code": "h9_01", "geom_wkb": bytes.fromhex("010100000000000000000000000000000000000000")},
            {"zone_name": "子牙河", "zone_code": "h9_02", "geom_wkb": bytes.fromhex("0101000000000000000000F03F000000000000F03F")},
        ]

        class FakeCursor:
            def execute(self, *args, **kwargs):
                pass

            def fetchall(self):
                return fake_rows

        class FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def cursor(self, *args, **kwargs):
                return FakeCursor()

        monkeypatch.setattr(rsf.psycopg2, "connect", lambda **kwargs: FakeConn())

        # 假设辅助函数存在
        loader = getattr(rsf, "_load_zone_boundaries_from_db", None)
        if loader is None:
            pytest.skip("_load_zone_boundaries_from_db 尚未实现")

        zones = loader(zone_type="9", zone_name=None, config={"postgres": {}})
        assert len(zones) == 2
        assert zones[0]["zone_name"] == "大清河"
        assert zones[1]["zone_name"] == "子牙河"


@pytest.mark.skipif(not _gdal_available(), reason="GDAL 未安装")
class TestComputeRainfallStatsForGeometry:
    def _make_square_polygon(self, minx, miny, maxx, maxy):
        from osgeo import ogr
        ring = ogr.Geometry(ogr.wkbLinearRing)
        ring.AddPoint(minx, miny)
        ring.AddPoint(maxx, miny)
        ring.AddPoint(maxx, maxy)
        ring.AddPoint(minx, maxy)
        ring.AddPoint(minx, miny)
        poly = ogr.Geometry(ogr.wkbPolygon)
        poly.AddGeometry(ring)
        return poly

    def _make_synthetic_raster(self, tmp_path, values, nodata=-9999.0):
        from osgeo import gdal
        path = tmp_path / "synthetic.tif"
        rows, cols = values.shape
        driver = gdal.GetDriverByName("GTiff")
        ds = driver.Create(str(path), cols, rows, 1, gdal.GDT_Float32)
        # 左上角 116, 40, 分辨率 0.1, 向下递增
        ds.SetGeoTransform((116.0, 0.1, 0.0, 40.0, 0.0, -0.1))
        ds.SetProjection('GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]')
        band = ds.GetRasterBand(1)
        band.WriteArray(values)
        band.SetNoDataValue(nodata)
        ds = None
        return str(path)

    def test_stats_for_fully_covered_polygon(self, tmp_path):
        import numpy as np

        compute = getattr(rsf, "_compute_rainfall_stats_for_geometry", None)
        if compute is None:
            pytest.skip("_compute_rainfall_stats_for_geometry 尚未实现")

        values = np.array([
            [1.0, 2.0, 3.0],
            [4.0, 5.0, 6.0],
            [7.0, 8.0, 9.0],
        ], dtype=np.float32)
        raster_path = self._make_synthetic_raster(tmp_path, values)
        # 多边形覆盖中间一格：116.1-116.2, 39.8-39.9（中心值 5.0）
        poly = self._make_square_polygon(116.05, 39.75, 116.25, 39.95)

        stats = compute(poly, raster_path, data_source_label="TEST")
        assert stats["average_rainfall_mm"] == pytest.approx(5.0, abs=0.1)
        assert stats["max_rainfall_mm"] == pytest.approx(5.0, abs=0.1)
        assert stats["min_rainfall_mm"] == pytest.approx(5.0, abs=0.1)

    def test_stats_with_nodata_inside_polygon(self, tmp_path):
        import numpy as np

        compute = getattr(rsf, "_compute_rainfall_stats_for_geometry", None)
        if compute is None:
            pytest.skip("_compute_rainfall_stats_for_geometry 尚未实现")

        values = np.array([
            [1.0, -9999.0, 3.0],
            [4.0, 5.0, 6.0],
            [7.0, 8.0, 9.0],
        ], dtype=np.float32)
        raster_path = self._make_synthetic_raster(tmp_path, values)
        poly = self._make_square_polygon(116.0, 39.7, 116.3, 40.0)

        stats = compute(poly, raster_path, data_source_label="TEST")
        assert stats["average_rainfall_mm"] > 0
        assert stats["max_rainfall_mm"] == pytest.approx(9.0, abs=0.1)

    def test_stats_returns_none_for_no_overlap(self, tmp_path):
        import numpy as np

        compute = getattr(rsf, "_compute_rainfall_stats_for_geometry", None)
        if compute is None:
            pytest.skip("_compute_rainfall_stats_for_geometry 尚未实现")

        values = np.ones((3, 3), dtype=np.float32)
        raster_path = self._make_synthetic_raster(tmp_path, values)
        # 多边形在栅格范围外
        poly = self._make_square_polygon(120.0, 38.0, 121.0, 39.0)

        stats = compute(poly, raster_path, data_source_label="TEST")
        assert stats["average_rainfall_mm"] == 0.0
        assert stats["max_rainfall_mm"] == 0.0
        assert stats["min_rainfall_mm"] == 0.0


class TestGetRiverSystemRainfallForecast:
    @pytest.mark.asyncio
    async def test_returns_zones_when_data_available(self, monkeypatch):
        tool = rsf.get_river_system_rainfall_forecast

        def fake_load_boundaries(zone_type, zone_name, config):
            return [
                {"zone_name": "大清河", "geometry": "POINT(0 0)"},
                {"zone_name": "子牙河", "geometry": "POINT(1 1)"},
            ]

        def fake_compute(geometry, raster_path, data_source_label, source_srid=4326):
            return {
                "average_rainfall_mm": 5.0,
                "max_rainfall_mm": 10.0,
                "min_rainfall_mm": 0.0,
            }

        def fake_resolve_file(forecast_hours, start_time, ec_output_path):
            return "/fake/path.tif", "TEST_SOURCE"

        monkeypatch.setattr(rsf, "_load_zone_boundaries_from_db", fake_load_boundaries)
        monkeypatch.setattr(rsf, "_compute_rainfall_stats_for_geometry", fake_compute)
        monkeypatch.setattr(rsf, "_resolve_forecast_file", fake_resolve_file)

        result = tool(
            river_system="全流域",
            start_time="2026-07-23 02:00:00",
            forecast_hours=24,
        )
        assert isinstance(result, dict)
        assert "zones" in result
        assert len(result["zones"]) == 2
        assert result["zones"][0]["zone_name"] == "大清河"

    @pytest.mark.asyncio
    async def test_returns_error_when_boundary_load_fails(self, monkeypatch):
        tool = rsf.get_river_system_rainfall_forecast

        def fake_load_boundaries(*args, **kwargs):
            raise RuntimeError("DB connection failed")

        monkeypatch.setattr(rsf, "_load_zone_boundaries_from_db", fake_load_boundaries)

        result = tool(
            river_system="全流域",
            start_time="2026-07-23 02:00:00",
            forecast_hours=24,
        )
        assert isinstance(result, dict)
        assert "error" in result
        assert "无法获取" in result["error"] or "请稍后" in result["error"]

    @pytest.mark.asyncio
    async def test_returns_empty_zones_when_no_forecast_file(self, monkeypatch):
        tool = rsf.get_river_system_rainfall_forecast

        def fake_load_boundaries(zone_type, zone_name, config):
            return [{"zone_name": "大清河", "geometry": "POINT(0 0)"}]

        def fake_resolve_file(forecast_hours, start_time, ec_output_path):
            return None, "ECMWF AIFS（无可用预报文件）"

        monkeypatch.setattr(rsf, "_load_zone_boundaries_from_db", fake_load_boundaries)
        monkeypatch.setattr(rsf, "_resolve_forecast_file", fake_resolve_file)

        result = tool(
            river_system="全流域",
            start_time="2026-07-23 02:00:00",
            forecast_hours=24,
        )
        assert isinstance(result, dict)
        assert result.get("zones") == []
        assert "无可用预报文件" in result.get("data_source", "")


class TestComputeLeadHours:
    """滚动预报窗口相对 cycle 的 lead 小时偏移计算。"""

    def test_tomorrow_window_offsets_from_cycle(self):
        start, end = rfg.compute_lead_hours(
            "20260722080000", datetime(2026, 7, 23, 0, 0, 0), 24
        )
        assert (start, end) == (16, 40)

    def test_past_start_clamps_to_zero(self):
        start, end = rfg.compute_lead_hours(
            "20260722080000", datetime(2026, 7, 22, 0, 0, 0), 24
        )
        assert (start, end) == (0, 24)

    def test_end_clamps_to_max_lead(self):
        start, end = rfg.compute_lead_hours(
            "20260722080000", datetime(2026, 8, 1, 0, 0, 0), 24
        )
        assert start == 232 and end == 240

    def test_start_beyond_max_lead_raises(self):
        with pytest.raises(ValueError):
            rfg.compute_lead_hours(
                "20260722080000", datetime(2026, 8, 5, 0, 0, 0), 24
            )


class TestResolveForecastRasterPathRollingAccumulation:
    """滚动预报分支必须按 start_time 偏移并做窗口累计，否则今天/明天数据相同。"""

    def _patch_source(self, monkeypatch):
        def fake_resolve_source(**kwargs):
            return {
                "source": "rolling_forecast",
                "file": "/fake/rolling.nc",
                "cycle": "20260722080000",
            }

        monkeypatch.setattr(rfg, "resolve_forecast_grid_source", fake_resolve_source)

    def test_uses_lead_offset_and_accumulates_window(self, monkeypatch):
        self._patch_source(monkeypatch)
        calls = {}

        def fake_accumulated(nc_path, start_hour, end_hour, **kwargs):
            calls["args"] = (nc_path, start_hour, end_hour)
            return "/fake/accum.tif"

        monkeypatch.setattr(rfg, "materialize_rolling_forecast_accumulated", fake_accumulated)

        path, label = resolve_forecast_raster_path(
            24, datetime(2026, 7, 23, 0, 0, 0), "/fake/ec"
        )
        assert path == "/fake/accum.tif"
        assert calls["args"] == ("/fake/rolling.nc", 16, 40)
        assert "20260722080000" in label

    def test_today_and_tomorrow_use_different_windows(self, monkeypatch):
        self._patch_source(monkeypatch)
        windows = []

        def fake_accumulated(nc_path, start_hour, end_hour, **kwargs):
            windows.append((start_hour, end_hour))
            return "/fake/accum.tif"

        monkeypatch.setattr(rfg, "materialize_rolling_forecast_accumulated", fake_accumulated)

        resolve_forecast_raster_path(24, datetime(2026, 7, 22, 8, 0, 0), "/fake/ec")
        resolve_forecast_raster_path(24, datetime(2026, 7, 23, 8, 0, 0), "/fake/ec")
        assert windows[0] != windows[1]

    def test_falls_back_to_ec_when_accumulate_fails(self, monkeypatch, tmp_path):
        self._patch_source(monkeypatch)

        def boom(*args, **kwargs):
            raise RuntimeError("nc corrupt")

        monkeypatch.setattr(rfg, "materialize_rolling_forecast_accumulated", boom)

        ec_dir = tmp_path / "ec"
        ec_dir.mkdir()
        (ec_dir / "ec_2026072300_rain_total_24h.tif").write_text("mock")

        path, label = resolve_forecast_raster_path(
            24, datetime(2026, 7, 23, 0, 0, 0), str(ec_dir)
        )
        assert path is not None and path.endswith("ec_2026072300_rain_total_24h.tif")
        assert label == "ECMWF AIFS"
