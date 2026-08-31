"""Real river core -> legacy resolver -> rolling discovery/lead integration (IO only faked)."""
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import river_query_forecast as rqf
import rolling_forecast_grid as grid
from analyzers import RainfallAnalyzer as rainfall

NOW = datetime(2026, 8, 27, 15, 20, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def source_io(monkeypatch):
    calls = []
    available = {"20260827080000": "present.nc"}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(grid, "datetime", Clock)
    # Actual discovery still chooses cycles and walks backwards; only the disk lookup is fake.
    monkeypatch.setattr(grid, "_pick_latest_file", lambda directory, cycle: available.get(cycle.strftime("%Y%m%d%H%M%S")))
    monkeypatch.setattr(rainfall, "find_ec_forecast_tif", lambda *a: None)
    monkeypatch.setattr(grid, "materialize_rolling_forecast_accumulated", lambda path, start, end, **kw: calls.append((path, start, end, kw)) or "rain.tif")
    monkeypatch.setattr(rqf, "load_river_corridor", lambda *a: rqf.RiverCorridor("泃河", "泃河", 4326, object(), 5.0))
    monkeypatch.setattr(rqf.rsf, "_load_zone_boundaries_from_db", lambda *a: [{"zone_name": "海河", "geometry": object()}])
    monkeypatch.setattr(rqf.rsf, "_compute_rainfall_stats_for_geometry", lambda *a, **kw: {
        "average_rainfall_mm": 0.0, "max_rainfall_mm": 0.0, "min_rainfall_mm": 0.0, "valid_count": 4,
    })
    return available, calls


@pytest.mark.parametrize("query,start,end,lead", [
    ("明天泃河有雨吗？", "2026-08-28T00:00:00+08:00", "2026-08-29T00:00:00+08:00", (16, 40)),
    ("今晚泃河有雨吗？", "2026-08-27T18:00:00+08:00", "2026-08-28T00:00:00+08:00", (10, 16)),
    ("明天海河流域天气怎么样", "2026-08-28T00:00:00+08:00", "2026-08-29T00:00:00+08:00", (16, 40)),
    ("今晚海河流域有雨吗？", "2026-08-27T18:00:00+08:00", "2026-08-28T00:00:00+08:00", (10, 16)),
])
def test_supported_window_materializes_exact_beijing_leads(source_io, query, start, end, lead):
    _, calls = source_io
    result = rqf.query_river_rainfall_forecast_core(query, {}, now=NOW.astimezone(timezone.utc))
    assert result["status"] == "ok"
    assert calls[0][:3] == ("present.nc", *lead)
    assert result["periods"][0]["start_time"] == start
    assert result["periods"][0]["end_time"] == end
    assert "cycle=20260827080000" in result["periods"][0]["data_source"]


@pytest.mark.parametrize("query", ["今天海河流域天气怎么样", "今天泃河有雨吗？"])
def test_today_cannot_label_present_cycle_08_to_08_as_midnight_to_midnight(source_io, query):
    _, calls = source_io
    result = rqf.query_river_rainfall_forecast_core(query, {}, now=NOW)
    assert result["status"] == "forecast_unavailable"
    assert result["periods"] == []
    assert calls == []


@pytest.mark.parametrize("query", ["今天海河流域天气怎么样", "今天泃河有雨吗？"])
def test_today_uses_discoverable_previous_cycle_for_exact_natural_day(source_io, query):
    available, calls = source_io
    available["20260826200000"] = "previous.nc"
    result = rqf.query_river_rainfall_forecast_core(query, {}, now=NOW)
    assert result["status"] == "ok"
    assert calls[0][:3] == ("previous.nc", 4, 28)
    assert "cycle=20260826200000" in result["periods"][0]["data_source"]
    assert result["periods"][0]["start_time"] == "2026-08-27T00:00:00+08:00"


def test_legacy_resolver_still_preserves_its_clamped_window(source_io):
    _, calls = source_io
    path, _ = rqf.rsf._resolve_forecast_file(24, datetime(2026, 8, 27), "")
    assert path == "rain.tif"
    assert calls[0][:3] == ("present.nc", 0, 24)


def test_strict_materializer_rejects_a_missing_hour_before_raster_output(monkeypatch, tmp_path):
    # The only faked dependency is NetCDF IO, not the coverage/accumulation implementation.
    class Precip:
        sizes = {"time": 23}
        def sel(self, **kwargs):
            return self
        def __getitem__(self, key):
            return SimpleNamespace(values=list(range(16, 30)) + list(range(31, 40)))
        def sum(self, **kwargs):
            pytest.fail("partial hourly data must not be accumulated as a complete day")
    class Dataset:
        def __getitem__(self, key):
            return Precip()
        def close(self):
            pass
    monkeypatch.setitem(sys.modules, "xarray", SimpleNamespace(open_dataset=lambda *a, **kw: Dataset()))
    assert grid.materialize_rolling_forecast_accumulated("partial.nc", 16, 40, output_dir=tmp_path, require_full_window=True) is None
