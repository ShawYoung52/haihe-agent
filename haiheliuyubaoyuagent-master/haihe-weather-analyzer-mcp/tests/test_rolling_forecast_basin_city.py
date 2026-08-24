"""外埠城市（唐山/承德/北京等）滚动预报路由测试（2026-08-24）。

口径（用户原话）："除了天津用滚动预报，其它不都是用数据湖海河流域那个数据吗"——
滚动预报网格（数据湖 GRID_TJQX_LYPUB，111-120°E/34-43°N）覆盖整个海河流域；
问句点名流域内地级市（唐山/承德/秦皇岛/北京/保定…）时按该市坐标采样网格，
不再静默退回天津市区代表点（"明天唐山的天气怎么样"曾错回【天津市区灾害风险】表，
张冠李戴）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import rolling_forecast_service as rfs  # noqa: E402


NOW = rfs.datetime(2026, 8, 24, 10, 0, tzinfo=rfs.TIANJIN_TIMEZONE)


class _FakeResp:
    def __init__(self, captured):
        self._captured = captured

    def raise_for_status(self):
        pass

    def json(self):
        return {"resultData": {"timeList": [], "datas": {}}}


class TestMatchBasinCities:
    def test_tangshan(self):
        assert rfs.match_basin_cities("明天唐山的天气怎么样") == ["唐山"]

    def test_city_suffix_form(self):
        assert rfs.match_basin_cities("唐山市未来三天天气") == ["唐山"]

    def test_no_match_without_city(self):
        assert rfs.match_basin_cities("明天天气怎么样") == []
        assert rfs.match_basin_cities("我市未来一周天气") == []

    def test_tianjin_district_is_not_basin_city(self):
        # 天津 11 区县走 ROLLING_FORECAST_COORDS，不在外埠城市表
        assert rfs.match_basin_cities("蓟州明天天气") == []

    def test_multiple_cities(self):
        assert rfs.match_basin_cities("唐山和秦皇岛明天天气") == ["唐山", "秦皇岛"]

    def test_dedup(self):
        assert rfs.match_basin_cities("唐山市唐山港天气") == ["唐山"]


class TestCoreBasinCityRouting:
    def _run(self, monkeypatch, user_query, **core_kwargs):
        captured = {}

        def fake_get(url, params=None, timeout=None, **kwargs):
            captured["params"] = dict(params or {})
            return _FakeResp(captured)

        monkeypatch.setattr(rfs.requests, "get", fake_get)
        rfs._rolling_forecast_cache.clear()
        # 隔离隐患点/风险等级增强（本类只测坐标路由口径）
        monkeypatch.setattr(rfs, "_query_region_hazards", lambda lon, lat, fcst_times=None: None)
        result = rfs.query_rolling_forecast_core(user_query=user_query, now=NOW, **core_kwargs)
        return result, captured.get("params") or {}

    def test_tangshan_uses_city_coord_not_tianjin_default(self, monkeypatch):
        """"明天唐山的天气怎么样"：按唐山坐标采海河网格，不再退回天津市区代表点。"""
        result, params = self._run(monkeypatch, "明天唐山的天气怎么样")
        assert result["query_regions"] == ["唐山"]
        assert "118.18" in params.get("lon", "")
        assert "117.14" not in params.get("lon", "")  # 天津市区代表点坐标不得出现

    def test_no_city_keeps_tianjin_default(self, monkeypatch):
        """无地点问法保持默认天津市区代表点（原行为不变）。"""
        result, params = self._run(monkeypatch, "明天天气怎么样")
        assert result["query_regions"] == ["天津市区"]
        assert "117.14" in params.get("lon", "")

    def test_mixed_tianjin_and_basin_city(self, monkeypatch):
        """天津区域 + 外埠城市混合：各自坐标都采样，谁也不顶掉谁。"""
        result, params = self._run(monkeypatch, "蓟州和唐山明天天气怎么样")
        assert result["query_regions"] == ["蓟州", "唐山"]
        assert "117.45" in params.get("lon", "")  # 蓟州
        assert "118.18" in params.get("lon", "")  # 唐山

    def test_basin_city_attaches_region_hazards(self, monkeypatch):
        """外埠城市同样走区域模式：附【唐山市灾害风险】表（隐患表/风险接口覆盖全流域）。"""
        captured = {}

        def fake_get(url, params=None, timeout=None, **kwargs):
            return _FakeResp(captured)

        monkeypatch.setattr(rfs.requests, "get", fake_get)
        rfs._rolling_forecast_cache.clear()
        hazards_calls = []
        monkeypatch.setattr(
            rfs,
            "_query_region_hazards",
            lambda lon, lat, fcst_times=None: hazards_calls.append((lon, lat)) or {
                "total_found": 1,
                "radius_km": 25.0,
                "categories": [{"key": "dzzh", "label": "地质灾害", "kind": "地灾", "count": 1}],
            },
        )
        result = rfs.query_rolling_forecast_core(user_query="明天唐山的天气怎么样", now=NOW)
        hazards = result.get("region_hazards") or []
        assert len(hazards) == 1
        assert hazards[0]["region"] == "唐山"
        assert hazards[0]["region_display"] == "唐山市"  # 显示名经 _display_region 补"市"
        # 隐患点查询坐标是唐山，不是天津市区
        assert hazards_calls and abs(float(hazards_calls[0][0]) - 118.18) < 0.01

    def test_point_mode_unaffected(self, monkeypatch):
        """显式 lon/lat 点位模式不受城市路由影响。"""
        result, params = self._run(
            monkeypatch, "唐山某点位天气", lon=118.5, lat=39.7, point_name="指定点位"
        )
        assert result["query_mode"].endswith("point")
        assert "118.5" in params.get("lon", "")
        assert "118.18" not in params.get("lon", "")

    def test_city_coord_table_all_inside_grid(self):
        """城市表所有坐标必须落在数据湖海河网格内（111-120°E / 34-43°N），
        否则采样只能拿到越界空值——加新城市时先核坐标。"""
        for name, coord in rfs.BASIN_CITY_COORDS.items():
            lon_s, lat_s = coord.split("_")
            lon, lat = float(lon_s), float(lat_s)
            assert 111.0 <= lon <= 120.0, f"{name} 经度越界: {lon}"
            assert 34.0 <= lat <= 43.0, f"{name} 纬度越界: {lat}"

    def test_city_table_no_overlap_with_tianjin_regions(self):
        """外埠城市表不得与天津 11 区县重名（避免双表冲突）。"""
        overlap = set(rfs.BASIN_CITY_COORDS) & set(rfs.ROLLING_FORECAST_COORDS)
        assert not overlap, f"城市表与天津区域表重名: {overlap}"
