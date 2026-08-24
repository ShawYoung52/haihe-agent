"""滚动预报区域灾害风险表（region_hazards）测试。

覆盖两类修复：
1. query_rolling_forecast_core 区域模式（非 point_mode）按区域代表坐标查
   地质灾害/山洪/中小河流隐患点，把归一化的 region_hazards 附进结果 payload；
   查询失败/无数据静默降级（风险表是增强，不阻断天气回答）。
2. 懒加载 poi_hazard_reminder_tool 可注入（mock _region_hazard_queryer 即可，
   不触发 tools.py 的重依赖链）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import rolling_forecast_service as rfs  # noqa: E402


NOW = rfs.datetime(2026, 8, 19, 10, 0, tzinfo=rfs.TIANJIN_TIMEZONE)


class _FakeResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"resultData": {"timeList": [], "datas": {}}}


def _fake_request(*args, **kwargs):
    return _FakeResp()


def _hazards_ok(categories=None, total=None):
    return {
        "status": "ok",
        "query_type": "poi_hazard_reminders",
        "lon": 117.45,
        "lat": 40.05,
        "radius_km": 25.0,
        "total_found": total if total is not None else 3,
        "categories": categories if categories is not None else [
            {"key": "dzzh", "label": "地质灾害", "kind": "地灾", "count": 2, "records": []},
            {"key": "sh", "label": "山洪", "kind": "山洪", "count": 1, "records": []},
        ],
        "message": "查询到周边 3 个灾害隐患点。",
        "debug_reason": "",
    }


@pytest.fixture(autouse=True)
def _stub_risk_levels(monkeypatch):
    """区域风险等级走真实风险接口（HTTP）。测试默认 stub 为 None（接口降级，
    risk_levels_available=False），避免真发 HTTP/触发 custom_tools 重依赖；
    验证等级附着与渲染的用例再单独 monkeypatch 成有数据的返回。"""
    monkeypatch.setattr(rfs, "_region_risk_level_queryer", lambda lon, lat, radius, fcst_times=None: None)


def _risk_levels_ok():
    return {
        "dzzh": {
            "label": "地质灾害风险", "kind": "geologic",
            "levels": {"一级": 1, "三级": 2}, "total": 3,
            "level_advice": [{"level": "一级", "advice": "x"}, {"level": "三级", "advice": "y"}],
        },
    }


class TestQueryRegionHazards:
    def test_normalizes_ok_payload(self, monkeypatch):
        """status=ok 时归一化：保留 count>0 类型，只带 key/label/kind/count，不泄露 records。"""
        monkeypatch.setattr(rfs, "_region_hazard_queryer", lambda lon, lat, radius: _hazards_ok())
        result = rfs._query_region_hazards(117.45, 40.05)
        assert result is not None
        assert result["total_found"] == 3
        assert result["radius_km"] == 25.0
        cats = result["categories"]
        assert cats == [
            {"key": "dzzh", "label": "地质灾害", "kind": "地灾", "count": 2},
            {"key": "sh", "label": "山洪", "kind": "山洪", "count": 1},
        ]
        for c in cats:
            assert "records" not in c

    def test_drops_zero_count_categories(self, monkeypatch):
        """count=0 或缺失的类型被剔除，不渲染空行。"""
        monkeypatch.setattr(
            rfs, "_region_hazard_queryer",
            lambda lon, lat, radius: _hazards_ok(categories=[
                {"key": "dzzh", "label": "地质灾害", "kind": "地灾", "count": 0, "records": []},
                {"key": "sh", "label": "山洪", "kind": "山洪", "count": 2, "records": []},
                {"key": "zxhl", "label": "中小河流", "kind": "河流", "count": None, "records": []},
            ]),
        )
        result = rfs._query_region_hazards(117.45, 40.05)
        assert [c["key"] for c in result["categories"]] == ["sh"]

    def test_error_payload_returns_none(self, monkeypatch):
        """status=error（DB 未配置/表加载失败）返回 None，静默降级。"""
        monkeypatch.setattr(
            rfs, "_region_hazard_queryer",
            lambda lon, lat, radius: {"status": "error", "total_found": 0, "categories": [], "message": "PostgreSQL 未配置"},
        )
        assert rfs._query_region_hazards(117.45, 40.05) is None

    def test_no_data_payload_returns_none(self, monkeypatch):
        """status=no_data（周边无隐患点）返回 None，不出空表。"""
        monkeypatch.setattr(
            rfs, "_region_hazard_queryer",
            lambda lon, lat, radius: {"status": "no_data", "total_found": 0, "categories": [], "message": "周边 25 公里内暂无已知灾害隐患点。"},
        )
        assert rfs._query_region_hazards(117.45, 40.05) is None

    def test_queryer_exception_returns_none(self, monkeypatch):
        """查询器抛异常不扩散：返回 None 且不阻断调用方。"""
        def boom(lon, lat, radius):
            raise RuntimeError("db down")
        monkeypatch.setattr(rfs, "_region_hazard_queryer", boom)
        assert rfs._query_region_hazards(117.45, 40.05) is None

    def test_lazy_load_failure_returns_none(self, monkeypatch):
        """懒加载本身失败（tools.py 重依赖缺失等）也静默降级。"""
        def broken_load():
            raise ImportError("no networkx")
        monkeypatch.setattr(rfs, "_region_hazard_queryer", None)
        monkeypatch.setattr(rfs, "_load_region_hazard_queryer", broken_load)
        assert rfs._query_region_hazards(117.45, 40.05) is None


class TestQueryRegionHazardsRiskLevels:
    """区域天气#8：_query_region_hazards 叠加风险接口的"本次各灾种风险等级"。"""

    def test_attaches_risk_levels(self, monkeypatch):
        """风险接口可达 → risk_levels 附进结果，risk_levels_available=True。"""
        monkeypatch.setattr(rfs, "_region_hazard_queryer", lambda lon, lat, radius: _hazards_ok())
        monkeypatch.setattr(rfs, "_region_risk_level_queryer", lambda lon, lat, radius, fcst_times=None: _risk_levels_ok())
        result = rfs._query_region_hazards(117.45, 40.05)
        assert result["risk_levels_available"] is True
        assert result["risk_levels"]["dzzh"]["levels"] == {"一级": 1, "三级": 2}
        assert result["risk_levels"]["dzzh"]["total"] == 3

    def test_no_risk_levels_marks_unavailable(self, monkeypatch):
        """风险接口全挂（返回 None）→ risk_levels_available=False，隐患点表照常。"""
        monkeypatch.setattr(rfs, "_region_hazard_queryer", lambda lon, lat, radius: _hazards_ok())
        monkeypatch.setattr(rfs, "_region_risk_level_queryer", lambda lon, lat, radius, fcst_times=None: None)
        result = rfs._query_region_hazards(117.45, 40.05)
        assert result["risk_levels_available"] is False
        assert result["risk_levels"] is None
        assert [c["key"] for c in result["categories"]] == ["dzzh", "sh"]

    def test_reachable_no_risk_marks_available_empty(self, monkeypatch):
        """接口可达但本次无风险（返回 {}）→ available=True、risk_levels={}（渲染"本次无风险"）。"""
        monkeypatch.setattr(rfs, "_region_hazard_queryer", lambda lon, lat, radius: _hazards_ok())
        monkeypatch.setattr(rfs, "_region_risk_level_queryer", lambda lon, lat, radius, fcst_times=None: {})
        result = rfs._query_region_hazards(117.45, 40.05)
        assert result["risk_levels_available"] is True
        assert result["risk_levels"] == {}

    def test_risk_level_exception_degrades(self, monkeypatch):
        """等级查询抛异常不扩散：available=False，隐患点 categories 完好。"""
        def boom(lon, lat, radius, fcst_times=None):
            raise RuntimeError("risk api down")
        monkeypatch.setattr(rfs, "_region_hazard_queryer", lambda lon, lat, radius: _hazards_ok())
        monkeypatch.setattr(rfs, "_region_risk_level_queryer", boom)
        result = rfs._query_region_hazards(117.45, 40.05)
        assert result["risk_levels_available"] is False
        assert result["risk_levels"] is None
        assert result["total_found"] == 3

    def test_hazards_unavailable_skips_risk_levels(self, monkeypatch):
        """静态隐患点表查询失败（返回 None）→ 整体 None，不再调风险接口。"""
        calls = {"n": 0}
        monkeypatch.setattr(rfs, "_region_hazard_queryer", lambda lon, lat, radius, fcst_times=None: None)

        def counting(lon, lat, radius, fcst_times=None):
            calls["n"] += 1
            return _risk_levels_ok()
        monkeypatch.setattr(rfs, "_region_risk_level_queryer", counting)
        assert rfs._query_region_hazards(117.45, 40.05) is None
        assert calls["n"] == 0


class TestCoreAttachesRegionHazards:
    def _run(self, monkeypatch, user_query="蓟州天气怎么样", **core_kwargs):
        monkeypatch.setattr(rfs.requests, "get", _fake_request)
        rfs._rolling_forecast_cache.clear()
        monkeypatch.setattr(rfs, "_query_region_hazards", lambda lon, lat, attach_risk_levels=True: _hazards_ok())
        return rfs.query_rolling_forecast_core(user_query=user_query, now=NOW, **core_kwargs)

    def test_region_mode_attaches_hazards(self, monkeypatch):
        """区域模式正常天气结果附带 region_hazards（含显示名）。"""
        result = self._run(monkeypatch)
        assert result.get("query_mode") == "region"
        hazards = result.get("region_hazards")
        assert isinstance(hazards, list) and len(hazards) == 1
        entry = hazards[0]
        assert entry["region"] == "蓟州"
        assert entry["region_display"] == "蓟州区"
        assert entry["total_found"] == 3
        assert [c["key"] for c in entry["categories"]] == ["dzzh", "sh"]

    def test_point_mode_no_hazards(self, monkeypatch):
        """point_mode（已给 lon/lat）不附着区域风险表——点位由决策天气路径管。"""
        calls = {"n": 0}
        monkeypatch.setattr(
            rfs, "_query_region_hazards",
            lambda lon, lat, attach_risk_levels=True: calls.update(n=calls["n"] + 1) or _hazards_ok(),
        )
        monkeypatch.setattr(rfs.requests, "get", _fake_request)
        rfs._rolling_forecast_cache.clear()
        result = rfs.query_rolling_forecast_core(
            user_query="密云水库未来天气", lon=116.8, lat=40.4, point_name="密云水库", now=NOW
        )
        assert "region_hazards" not in result
        assert calls["n"] == 0

    def test_hazards_failure_degrades_weather_unchanged(self, monkeypatch):
        """隐患查询全失败（返回 None）不阻断天气回答，也不出现 region_hazards 字段。"""
        monkeypatch.setattr(rfs, "_query_region_hazards", lambda lon, lat, attach_risk_levels=True: None)
        monkeypatch.setattr(rfs.requests, "get", _fake_request)
        rfs._rolling_forecast_cache.clear()
        result = rfs.query_rolling_forecast_core(user_query="蓟州天气怎么样", now=NOW)
        assert "region_hazards" not in result
        assert result.get("query_mode") == "region"
        assert isinstance(result.get("periods"), list)

    def test_multi_region_attaches_each(self, monkeypatch):
        """多区域查询为每个区域分别收集风险数据。"""
        captured = []
        monkeypatch.setattr(
            rfs, "_query_region_hazards",
            lambda lon, lat, attach_risk_levels=True: captured.append((float(lon), float(lat))) or _hazards_ok(),
        )
        monkeypatch.setattr(rfs.requests, "get", _fake_request)
        rfs._rolling_forecast_cache.clear()
        result = rfs.query_rolling_forecast_core(user_query="蓟州和宝坻未来三天天气怎么样", now=NOW)
        regions = result.get("region_hazards") or []
        assert len(regions) == 2
        assert [e["region"] for e in regions] == ["蓟州", "宝坻"]
        # 坐标应来自 ROLLING_FORECAST_COORDS
        assert (117.45, 40.05) in captured
        assert (117.28, 39.73) in captured


class TestRiskLevelsGating:
    """"本次风险等级"列按 24h 门控（2026-08-24 甲方口径）。

    原"跨日窗口逐日调风险接口合并"的 _risk_fcst_times_from_window 已移除——风险接口
    每起报时次只出 24h，未来 24h 之外没有对应时段的风险资料，该列只在 24h 内窗口出现。
    具体行为详见 tests/test_rolling_forecast_time_of_day.py 的
    TestRiskFcstWindowApplies / TestCoreRiskAttachGating。
    """

    def test_future_window_no_risk_levels_attached(self, monkeypatch):
        """"未来三天"（起始日=明天，24h 外）→ attach_risk_levels=False，不接等级。"""
        captured = {}

        def fake_hazards(lon, lat, attach_risk_levels=True):
            captured["attach_risk_levels"] = attach_risk_levels
            return {
                "total_found": 1, "radius_km": 25.0,
                "categories": [{"key": "dzzh", "label": "地质灾害", "kind": "地灾", "count": 1}],
            }

        monkeypatch.setattr(rfs.requests, "get", _fake_request)
        rfs._rolling_forecast_cache.clear()
        monkeypatch.setattr(rfs, "_query_region_hazards", fake_hazards)
        rfs.query_rolling_forecast_core(user_query="蓟州未来三天天气怎么样", now=NOW)
        assert captured["attach_risk_levels"] is False

    def test_plain_query_attaches_risk_levels(self, monkeypatch):
        """普通"蓟州天气"（无窗口，24h 内）→ attach_risk_levels=True，接等级。"""
        captured = {}

        def fake_hazards(lon, lat, attach_risk_levels=True):
            captured["attach_risk_levels"] = attach_risk_levels
            return {
                "total_found": 1, "radius_km": 25.0,
                "categories": [{"key": "dzzh", "label": "地质灾害", "kind": "地灾", "count": 1}],
            }

        monkeypatch.setattr(rfs.requests, "get", _fake_request)
        rfs._rolling_forecast_cache.clear()
        monkeypatch.setattr(rfs, "_query_region_hazards", fake_hazards)
        rfs.query_rolling_forecast_core(user_query="蓟州天气怎么样", now=NOW)
        assert captured["attach_risk_levels"] is True
