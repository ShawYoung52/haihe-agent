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

    def test_malformed_static_counts_degrade_without_breaking_weather(self, monkeypatch):
        monkeypatch.setattr(
            rfs, "_region_hazard_queryer",
            lambda lon, lat, radius: _hazards_ok(
                categories=[{"key": "dzzh", "label": "地质灾害", "count": "--"}],
                total="--",
            ),
        )

        result = rfs._query_region_hazards(117.45, 40.05)

        assert result["total_found"] == 0
        assert result["categories"] == []

    def test_error_payload_preserves_risk_unavailable_state(self, monkeypatch):
        """静态库失败时仍保留风险等级的降级状态。"""
        monkeypatch.setattr(
            rfs, "_region_hazard_queryer",
            lambda lon, lat, radius: {"status": "error", "total_found": 0, "categories": [], "message": "PostgreSQL 未配置"},
        )
        result = rfs._query_region_hazards(117.45, 40.05)
        assert result["hazards_available"] is False
        assert result["risk_levels_available"] is False

    def test_no_data_payload_keeps_confirmed_static_availability(self, monkeypatch):
        """status=no_data 表示静态库可达且周边无隐患点。"""
        monkeypatch.setattr(
            rfs, "_region_hazard_queryer",
            lambda lon, lat, radius: {"status": "no_data", "total_found": 0, "categories": [], "message": "周边 25 公里内暂无已知灾害隐患点。"},
        )
        result = rfs._query_region_hazards(117.45, 40.05)
        assert result["hazards_available"] is True
        assert result["categories"] == []

    def test_queryer_exception_marks_static_data_unavailable(self, monkeypatch):
        """查询器抛异常不扩散，但显式标记静态数据不可用。"""
        def boom(lon, lat, radius):
            raise RuntimeError("db down")
        monkeypatch.setattr(rfs, "_region_hazard_queryer", boom)
        assert rfs._query_region_hazards(117.45, 40.05)["hazards_available"] is False

    def test_lazy_load_failure_marks_static_data_unavailable(self, monkeypatch):
        """懒加载失败不影响风险等级状态返回。"""
        def broken_load():
            raise ImportError("no networkx")
        monkeypatch.setattr(rfs, "_region_hazard_queryer", None)
        monkeypatch.setattr(rfs, "_load_region_hazard_queryer", broken_load)
        assert rfs._query_region_hazards(117.45, 40.05)["hazards_available"] is False


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

    def test_hazards_unavailable_still_queries_risk_levels(self, monkeypatch):
        """静态隐患点表查询失败不得阻断实时风险等级。"""
        calls = {"n": 0}
        monkeypatch.setattr(rfs, "_region_hazard_queryer", lambda lon, lat, radius, fcst_times=None: None)

        def counting(lon, lat, radius, fcst_times=None):
            calls["n"] += 1
            return _risk_levels_ok()
        monkeypatch.setattr(rfs, "_region_risk_level_queryer", counting)
        result = rfs._query_region_hazards(117.45, 40.05)
        assert result["categories"] == []
        assert result["risk_levels"]["dzzh"]["levels"] == {"一级": 1, "三级": 2}
        assert result["hazards_available"] is False
        assert calls["n"] == 1


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

    def test_point_mode_attaches_actual_risk_levels_without_region_hazard_table(self, monkeypatch):
        """点位天气把风险接口等级交给前端，但仍不混入区域静态隐患表。"""
        calls = {"n": 0}
        monkeypatch.setattr(
            rfs, "_query_region_risk_levels",
            lambda lon, lat, fcst_times=None: calls.update(n=calls["n"] + 1) or {
                "dzzh": {"label": "地质灾害", "levels": {"四级": 1}, "total": 1}
            },
        )
        monkeypatch.setattr(rfs.requests, "get", _fake_request)
        rfs._rolling_forecast_cache.clear()
        result = rfs.query_rolling_forecast_core(
            user_query="今天天津港天气怎么样", lon=117.75, lat=39.0, point_name="天津港", now=NOW
        )
        assert "region_hazards" not in result
        assert result["point_risk_levels"]["dzzh"]["levels"] == {"四级": 1}
        assert result["point_risk_levels_available"] is True
        assert calls["n"] == 1

    def test_point_mode_future_window_queries_risk_interface(self, monkeypatch):
        """未来点位窗口也必须实际查询对应时次，空结果按业务口径显示无风险。"""
        calls = {"n": 0}
        monkeypatch.setattr(
            rfs, "_query_region_risk_levels",
            lambda lon, lat, fcst_times=None: calls.update(n=calls["n"] + 1) or {},
        )
        monkeypatch.setattr(rfs.requests, "get", _fake_request)
        rfs._rolling_forecast_cache.clear()

        result = rfs.query_rolling_forecast_core(
            user_query="本周末天津港天气怎么样", lon=117.75, lat=39.0, point_name="天津港", now=NOW
        )

        assert result["point_risk_levels_available"] is True
        assert result["point_risk_levels"] == {}
        assert calls["n"] == 1

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


class TestRiskLevelsForecastTimes:
    """风险接口按目标窗口逐日起报查询；无窗口沿用默认周期回退。"""

    def test_future_window_passes_daily_cycles(self, monkeypatch):
        """“未来三天”传三个逐日起报时次，不能跳过风险接口。"""
        captured = {}

        def fake_hazards(lon, lat, risk_fcst_times=None):
            captured["risk_fcst_times"] = risk_fcst_times
            return {
                "total_found": 1, "radius_km": 25.0,
                "categories": [{"key": "dzzh", "label": "地质灾害", "kind": "地灾", "count": 1}],
            }

        monkeypatch.setattr(rfs.requests, "get", _fake_request)
        rfs._rolling_forecast_cache.clear()
        monkeypatch.setattr(rfs, "_query_region_hazards", fake_hazards)
        rfs.query_rolling_forecast_core(user_query="蓟州未来三天天气怎么样", now=NOW)
        assert captured["risk_fcst_times"] == [
            "20260820080000", "20260821080000", "20260822080000",
        ]

    def test_plain_query_uses_default_cycle_fallback(self, monkeypatch):
        """普通“蓟州天气”没有明确日历窗口，沿用默认周期及前一周期回退。"""
        captured = {}

        def fake_hazards(lon, lat, risk_fcst_times=None):
            captured["risk_fcst_times"] = risk_fcst_times
            return {
                "total_found": 1, "radius_km": 25.0,
                "categories": [{"key": "dzzh", "label": "地质灾害", "kind": "地灾", "count": 1}],
            }

        monkeypatch.setattr(rfs.requests, "get", _fake_request)
        rfs._rolling_forecast_cache.clear()
        monkeypatch.setattr(rfs, "_query_region_hazards", fake_hazards)
        rfs.query_rolling_forecast_core(user_query="蓟州天气怎么样", now=NOW)
        assert captured["risk_fcst_times"] is None


class TestPointRiskWindow:
    """2026-08-31 用户口径：点位风险只显示"有数据的时刻"，多天也要标注数据时段。

    点位统一用最近起报时次（None，无资料回退前一周期），不再按目标日逐日请求
    未来时次；payload 透传 point_risk_window（数据时段）与 point_risk_beyond_from
    （超窗说明）。NOW = 2026-08-19 10:00 → 最近起报 08:00，窗口 [8月19日8时, 8月20日8时)。
    """

    def _run(self, monkeypatch, user_query, capture):
        def fake_levels(lon, lat, fcst_times=None):
            capture["fcst_times"] = fcst_times
            return {}

        monkeypatch.setattr(rfs, "_query_region_risk_levels", fake_levels)
        monkeypatch.setattr(rfs.requests, "get", _fake_request)
        rfs._rolling_forecast_cache.clear()
        return rfs.query_rolling_forecast_core(
            user_query=user_query, lon=117.75, lat=39.0, point_name="天津港", now=NOW
        )

    def test_multi_day_uses_latest_cycle_not_future_cycles(self, monkeypatch):
        capture = {}
        self._run(monkeypatch, "本周末天津港天气怎么样", capture)
        assert capture["fcst_times"] is None

    def test_window_attached_with_labels(self, monkeypatch):
        capture = {}
        result = self._run(monkeypatch, "本周末天津港天气怎么样", capture)
        assert result["point_risk_window"] == {
            "start_label": "8月19日8时",
            "end_label": "8月20日8时",
        }

    def test_multi_day_marks_beyond_from(self, monkeypatch):
        capture = {}
        result = self._run(monkeypatch, "本周末天津港天气怎么样", capture)
        assert result["point_risk_beyond_from"] == "8月20日8时"

    def test_today_single_day_no_beyond_note(self, monkeypatch):
        capture = {}
        result = self._run(monkeypatch, "今天天津港天气怎么样", capture)
        assert "point_risk_beyond_from" not in result
        # 今天单日仍标数据时段
        assert result["point_risk_window"]["start_label"] == "8月19日8时"

    def test_beyond_label_helper_directly(self):
        window = rfs._latest_risk_cycle_window(NOW)
        assert window["start_label"] == "8月19日8时"
        assert window["end_label"] == "8月20日8时"
        # 单日今天（8-19，1 天）结束 8-20 零点 ≤ 覆盖 8-20 08 时 → 不超窗
        assert rfs._risk_window_beyond_label(
            {"forecast_start_date": "2026-08-19", "forecast_days": 1}, window
        ) is None
        # 三天窗口结束 8-22 零点 > 覆盖 8-20 08 时 → 超窗，标注覆盖结束时刻
        assert rfs._risk_window_beyond_label(
            {"forecast_start_date": "2026-08-19", "forecast_days": 3}, window
        ) == "8月20日8时"
        assert rfs._risk_window_beyond_label(None, window) is None
