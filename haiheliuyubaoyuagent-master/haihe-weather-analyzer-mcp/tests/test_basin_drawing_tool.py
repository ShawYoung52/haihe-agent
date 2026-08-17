"""14所 basin_drawing 出图工具测试（areas 列表 + image 出图）。

口径（用户确认）：单工具参数化（sceneType/productType 由 planner 按 docstring 路由）；
图片返回代理 URL；base 默认 http://10.226.107.35:8001，env BASIN_DRAWING_API_BASE 可覆盖；
时间自动规整到 10 分钟刻度；只有带 children 的一级分区可出图。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import custom_tools.basin_drawing_tool as bdt


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


AREAS_PAYLOAD = {
    "msg": "成功",
    "code": 1,
    "success": True,
    "data": [
        {
            "areaId": 1,
            "areaName": "9分区大清河",
            "children": [{"code": "7", "name": "大清河片区"}, {"code": "8", "name": "子牙河片区"}],
        },
        {"areaId": 2, "areaName": "无子分区暂不可出图", "children": []},
    ],
}


class TestBasinDrawingAreas:
    def _setup(self, monkeypatch, calls: dict):
        def fake_get(url, timeout=None):
            calls["n"] += 1
            return _FakeResp(AREAS_PAYLOAD)

        monkeypatch.setattr(bdt.requests, "get", fake_get)
        bdt._basin_areas_cache.clear()

    def test_normalizes_tree_and_supported_count(self, monkeypatch):
        """归一化树正确；只带 children 的一级分区计入 supported_count。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls)

        r = bdt.query_basin_drawing_areas_core()
        assert r["status"] == "ok"
        assert r["supported_count"] == 1, "无 children 的一级分区不计入可出图"
        assert r["areas"][0]["areaId"] == 1
        assert r["areas"][0]["children"][0] == {"code": "7", "name": "大清河片区"}
        assert r["areas"][1]["children"] == []

    def test_second_call_hits_cache(self, monkeypatch):
        """分区静态，同参第二次命中缓存不再打 HTTP。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls)

        bdt.query_basin_drawing_areas_core()
        after_first = calls["n"]
        bdt.query_basin_drawing_areas_core()
        assert calls["n"] == after_first, "第二次应命中缓存"

    def test_stale_cache_entry_refetches(self, monkeypatch):
        """缓存条目过期（旧时间戳）时重新请求，不服务过期数据。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls)
        # 直接塞过期条目：旧时间戳 + 空 areas，若被命中则结果应为空且不打接口
        bdt._basin_areas_cache["areas"] = (0.0, {"status": "ok", "areas": [], "supported_count": 0})

        r = bdt.query_basin_drawing_areas_core()
        assert r["areas"], "过期条目不应命中，应重新拉取真实分区"
        assert calls["n"] == 1, "过期条目应触发重新请求"

    def test_error_not_cached(self, monkeypatch):
        """接口失败（status error）不写缓存。"""
        calls = {"n": 0}

        def failing_get(url, timeout=None):
            calls["n"] += 1
            raise RuntimeError("分区接口不可达")

        monkeypatch.setattr(bdt.requests, "get", failing_get)
        bdt._basin_areas_cache.clear()

        r1 = bdt.query_basin_drawing_areas_core()
        assert r1["status"] == "error"
        assert bdt._basin_areas_cache == {}, "失败结果不应写缓存"
        bdt.query_basin_drawing_areas_core()
        assert calls["n"] == 2, "未缓存应重新请求"

    def test_empty_areas_not_cached(self, monkeypatch):
        """上游返回空 data（HTTP 200 但无分区）→ no_data 不缓存，下个请求重取。"""
        calls = {"n": 0}

        def fake_get(url, timeout=None):
            calls["n"] += 1
            return _FakeResp({"code": 1, "success": True, "data": []})

        monkeypatch.setattr(bdt.requests, "get", fake_get)
        bdt._basin_areas_cache.clear()

        r1 = bdt.query_basin_drawing_areas_core()
        assert r1["status"] == "no_data", "空分区应按无数据处理"
        assert bdt._basin_areas_cache == {}, "空数据不应被 3600s 缓存"
        bdt.query_basin_drawing_areas_core()
        assert calls["n"] == 2, "空数据未缓存应重新请求"


class TestGenerateBasinRainfallImage:
    def _setup(self, monkeypatch, calls: dict, data="http://10.226.107.35:8001/hhly/img/2026/08/12/DYPQ/ECMF/a.png"):
        def fake_post(url, json=None, timeout=None):
            calls["n"] += 1
            calls["url"] = url
            calls["body"] = json
            return _FakeResp({"code": 1, "success": True, "data": data})

        monkeypatch.setattr(bdt.requests, "post", fake_post)

    def test_body_assembled_and_ten_minute_normalized(self, monkeypatch):
        """body 组装正确；beginTime 10 分钟规整；返回代理 URL。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls)

        r = bdt.generate_basin_rainfall_image_core(
            scene_type="FORECAST", product_type="AREA_RAIN", parent_area_id=1,
            area_codes="7,8", begin_time="2026-08-03 20:07", end_time="2026-08-04 20:00",
            main_title="9分区大清河预报降水图", sub_title="2026年8月3日20时—8月4日20时",
            forecast_time="2026-08-03 20:00",
        )
        assert r["status"] == "ok"
        assert r["image_url"].startswith("http://10.226.107.35:8001/")
        body = calls["body"]
        assert body["sceneType"] == "FORECAST"
        assert body["productType"] == "AREA_RAIN"
        assert body["parentAreaId"] == 1
        assert body["areaCodes"] == ["7", "8"]
        assert body["beginTime"] == "2026-08-03 20:00", "应规整到 10 分钟刻度"
        assert body["forecastTime"] == "2026-08-03 20:00"
        assert body["mainTitle"] == "9分区大清河预报降水图"
        assert body["showRainValue"] is True
        assert "forceCreate=0" in calls["url"]

    def test_area_codes_all(self, monkeypatch):
        """area_codes='ALL' → ['ALL']。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls)
        r = bdt.generate_basin_rainfall_image_core(
            scene_type="REALTIME", product_type="STATION_RAIN", parent_area_id=1,
            area_codes="ALL", begin_time="2026-08-03 20:00", end_time="2026-08-04 20:00",
            main_title="站点雨量图", sub_title="",
        )
        assert r["status"] == "ok"
        assert calls["body"]["areaCodes"] == ["ALL"]

    def test_relative_url_joined_with_base(self, monkeypatch):
        """响应 data 是相对路径时拼接 base host。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls, data="/hhly/meteor_img_profile/xxx.png")
        r = bdt.generate_basin_rainfall_image_core(
            scene_type="REALTIME", product_type="STATION_RAIN", parent_area_id=1,
            area_codes="ALL", begin_time="2026-08-03 20:00", end_time="2026-08-04 20:00",
            main_title="站点雨量图", sub_title="",
        )
        assert r["status"] == "ok"
        assert r["image_url"] == "http://10.226.107.35:8001/hhly/meteor_img_profile/xxx.png"

    def test_forecast_time_defaults_to_latest_cycle(self, monkeypatch):
        """FORECAST 未传 forecast_time → 默认最近 08/20 起报时次。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls)
        monkeypatch.setattr(bdt, "_latest_synoptic_cycle",
                            lambda now=None: datetime(2026, 8, 3, 20, 0))
        r = bdt.generate_basin_rainfall_image_core(
            scene_type="FORECAST", product_type="GRID_RAIN", parent_area_id=1,
            area_codes="ALL", begin_time="2026-08-03 20:00", end_time="2026-08-04 20:00",
            main_title="格点预报降水图", sub_title="",
        )
        assert r["status"] == "ok"
        assert calls["body"]["forecastTime"] == "2026-08-03 20:00"

    def test_invalid_product_type_returns_error_no_http(self, monkeypatch):
        """非法 productType → 结构化 error，不打接口。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls)
        r = bdt.generate_basin_rainfall_image_core(
            scene_type="REALTIME", product_type="BAD_TYPE", parent_area_id=1,
            area_codes="ALL", begin_time="2026-08-03 20:00", end_time="2026-08-04 20:00",
            main_title="x", sub_title="",
        )
        assert r["status"] == "error"
        assert calls["n"] == 0, "参数非法不应打接口"

    def test_span_over_ten_days_returns_error(self, monkeypatch):
        """时间跨度超过 10 天 → 结构化 error，不打接口。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls)
        r = bdt.generate_basin_rainfall_image_core(
            scene_type="REALTIME", product_type="STATION_RAIN", parent_area_id=1,
            area_codes="ALL", begin_time="2026-08-01 00:00", end_time="2026-08-20 00:00",
            main_title="x", sub_title="",
        )
        assert r["status"] == "error"
        assert "10" in r["message"]
        assert calls["n"] == 0, "跨度超限不应打接口"

    def test_force_create_non_numeric_returns_error(self, monkeypatch):
        """force_create 非数字 → 结构化 error，不打接口（不抛未捕获异常）。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls)
        r = bdt.generate_basin_rainfall_image_core(
            scene_type="REALTIME", product_type="STATION_RAIN", parent_area_id=1,
            area_codes="ALL", begin_time="2026-08-03 20:00", end_time="2026-08-04 20:00",
            main_title="x", sub_title="", force_create="是",
        )
        assert r["status"] == "error"
        assert calls["n"] == 0, "force_create 非法不应打接口"
