# -*- coding: utf-8 -*-
"""风险预警记录 → 灾害点匹配 + 逐级汇总 单元测试（2026-08-21）。

风险接口 /hhfw/riskWarnNew/findDataListByConfig 现已确认返回隐患点 id/name/数字等级
（用户样本：{"name":"石界滑坡","lon":113.75,"id":68,"lat":36.25,"level":5}）。
本测试锁定：等级规范化（含数字 1-5，5=最高）、有 id 记录按 id 直连静态隐患点表、
无 id 记录按经纬度就近匹配兜底、各区县隐患点总数、本次各区县各级风险数量、
逐级防范建议、以及数据库不可用时的静默降级。

按文件路径加载 risk_warning_tool（绕开 custom_tools/__init__ 的 psycopg2/networkx
重依赖链，与 test_composite_longimg_tool.py 同套路；额外 stub 掉 custom_tools 包，
使 `from custom_tools._ttl_cache import make_ttl_cache` 不触发包 __init__）。
"""
from __future__ import annotations

from collections import Counter
from threading import Barrier

import datetime
import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

# stub custom_tools 包，避免加载 risk_warning_tool 时执行 custom_tools/__init__
# （顶层 import 了需要 psycopg2/networkx/pandas 的兄弟模块）。
_pkg = types.ModuleType("custom_tools")
_pkg.__path__ = []
sys.modules.setdefault("custom_tools", _pkg)
_ttl_spec = importlib.util.spec_from_file_location(
    "custom_tools._ttl_cache", MCP_DIR / "custom_tools" / "_ttl_cache.py"
)
_ttl_mod = importlib.util.module_from_spec(_ttl_spec)
_ttl_spec.loader.exec_module(_ttl_mod)
sys.modules.setdefault("custom_tools._ttl_cache", _ttl_mod)

_spec = importlib.util.spec_from_file_location(
    "risk_warning_tool", MCP_DIR / "custom_tools" / "risk_warning_tool.py"
)
rwt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rwt)


class TestNormalizeRiskLevel:
    def test_chinese_levels(self):
        for lv in ("一级", "二级", "三级", "四级"):
            assert rwt._normalize_risk_level(lv) == lv

    def test_color_levels(self):
        assert rwt._normalize_risk_level("红色") == "一级"
        assert rwt._normalize_risk_level("橙色") == "二级"
        assert rwt._normalize_risk_level("黄色") == "三级"
        assert rwt._normalize_risk_level("蓝色") == "四级"
        # 颜色后缀（如 "蓝色预警"）也归一到对应等级
        assert rwt._normalize_risk_level("蓝色预警") == "四级"

    def test_numeric_levels_ascending(self):
        # 数字按"越大越高"（样本 level=5）：5→一级(最重)、4→二级、3→三级、2→四级；
        # 1≈无/极低风险，保留原样（由 _is_risky_level 排除）。
        assert rwt._normalize_risk_level("5") == "一级"
        assert rwt._normalize_risk_level("4") == "二级"
        assert rwt._normalize_risk_level("3") == "三级"
        assert rwt._normalize_risk_level("2") == "四级"
        assert rwt._normalize_risk_level("1") == "1"
        assert rwt._normalize_risk_level(5) == "一级"

    def test_unknown_and_empty(self):
        assert rwt._normalize_risk_level("高风险") == "高风险"  # 无法识别则保留原文
        assert rwt._normalize_risk_level(None) == ""
        assert rwt._normalize_risk_level("") == ""


class TestIsRiskyLevel:
    def test_numeric_one_is_not_risky(self):
        # 数字越大越高约定下，1=无/极低风险不列入本次风险
        assert rwt._is_risky_level("1") is False
        assert rwt._is_risky_level(1) is False
        assert rwt._is_risky_level("0") is False

    def test_higher_numeric_is_risky(self):
        assert rwt._is_risky_level("2") is True
        assert rwt._is_risky_level("5") is True

    def test_chinese_and_noise(self):
        assert rwt._is_risky_level("一级") is True
        assert rwt._is_risky_level("低风险") is False
        assert rwt._is_risky_level("") is True  # 空值保守按有风险处理


class TestNormalizeRecord:
    def test_extracts_id_and_name(self):
        rec = rwt._normalize_record(
            {"name": "石界滑坡", "lon": "113.75", "id": 68, "lat": "36.25", "level": 5}
        )
        assert rec["id"] == 68
        assert rec["name"] == "石界滑坡"
        assert rec["longitude"] == "113.75"
        assert rec["latitude"] == "36.25"
        assert rec["level"] == 5
        assert rec["level_norm"] == "一级"  # 数字 5 → 展示用归一等级

    def test_area_not_hazard_name(self):
        # 接口样本形态只有 name（隐患点名）无区县字段：area 必须是 None，
        # 不能把"石界滑坡"当区县名混入县级汇总（2026-08-21 review 修正）。
        rec = rwt._normalize_record({"name": "石界滑坡", "id": 68, "level": 5})
        assert rec["name"] == "石界滑坡"
        assert rec["area"] is None


class TestRadiusParse:
    def test_valid_and_invalid(self):
        assert rwt._parse_radius_km("5") == 5.0
        assert rwt._parse_radius_km("0.5") == 0.5
        assert rwt._parse_radius_km("abc") == 1.0   # 非法 → 默认，导入期不抛错
        assert rwt._parse_radius_km(None) == 1.0
        assert rwt._parse_radius_km("") == 1.0
        assert rwt._parse_radius_km("0") == 1.0     # 越界 → 默认
        assert rwt._parse_radius_km("20") == 1.0    # 越界 → 默认
        assert rwt._parse_radius_km("10") == 10.0   # 上界含
        assert rwt.RISK_WARNING_MATCH_RADIUS_KM == 1.0


class TestMatchHazardPoints:
    def _rows(self):
        return [
            {"id": 1, "name": "甲隐患点", "lon": 117.00, "lat": 39.00,
             "county_name": "冀州区", "city_name": "天津市", "status": 0},
            {"id": 2, "name": "乙隐患点", "lon": 117.010, "lat": 39.00,
             "county_name": "冀州区", "city_name": "天津市", "status": 0},
        ]

    def test_matches_by_id_directly(self):
        # 接口返回 id=1 → 按 id 直连，不靠经纬度
        recs = [{"id": 1, "name": "甲隐患点", "level": "一级",
                 "longitude": 99.0, "latitude": 99.0}]  # 经纬度故意离谱
        out, matched = rwt._match_hazard_points(recs, "geologic", self._rows())
        assert matched == 1
        rec = out[0]
        assert rec["hazard_id"] == 1
        assert rec["county_name"] == "冀州区"
        assert rec["match_method"] == "id"
        assert "match_distance_km" not in rec  # id 直连无距离

    def test_matches_by_id_float_normalized(self):
        # JSON 序列化会把数字 id 打成 68.0 → 归一后仍按 id 直连，不做就近猜测
        # （2026-08-21 review：否则 id 直连静默失效且无 haversine 兜底）
        recs = [{"id": 68.0, "name": "甲隐患点", "level": "一级",
                 "longitude": 99.0, "latitude": 99.0}]
        rows = [dict(self._rows()[0], id=68)]  # 表侧 id 为 int 68
        out, matched = rwt._match_hazard_points(recs, "geologic", rows)
        assert matched == 1
        assert out[0]["hazard_id"] == 68
        assert out[0]["match_method"] == "id"
        assert out[0]["county_name"] == "冀州区"

    def test_id_zero_falls_back_to_haversine(self):
        # id=0 是常见"无 id"哨兵（静态表主键从 1 起）→ 视为无 id，走经纬度兜底
        recs = [{"id": 0, "level": "四级", "longitude": 117.002, "latitude": 39.0}]
        out, matched = rwt._match_hazard_points(recs, "geologic", self._rows())
        assert matched == 1
        assert out[0]["match_method"] == "haversine"
        assert out[0]["hazard_id"] == 1

    def test_id_unknown_does_not_fallback_to_distance(self):
        # 有 id 但静态表查不到 → 不就近猜测，保留原样（避免挂到错误隐患点）
        recs = [{"id": 999, "name": "未知隐患点", "level": "三级",
                 "longitude": 117.001, "latitude": 39.0}]
        out, matched = rwt._match_hazard_points(recs, "geologic", self._rows())
        assert matched == 0
        assert "hazard_id" not in out[0]
        assert out[0]["level"] == "三级"

    def test_matches_nearest_within_radius_when_no_id(self):
        recs = [{"level": "四级", "longitude": 117.002, "latitude": 39.000}]
        out, matched = rwt._match_hazard_points(recs, "geologic", self._rows())
        assert matched == 1
        rec = out[0]
        assert rec["hazard_id"] == 1  # 117.002 更靠近 id=1 (117.00)
        assert rec["hazard_name"] == "甲隐患点"
        assert rec["county_name"] == "冀州区"
        assert rec["match_method"] == "haversine"
        assert rec["match_distance_km"] > 0

    def test_no_match_when_too_far(self):
        recs = [{"level": "三级", "longitude": 118.0, "latitude": 39.0}]  # ~90km 外
        out, matched = rwt._match_hazard_points(recs, "geologic", self._rows())
        assert matched == 0
        assert "hazard_id" not in out[0]
        assert out[0]["level"] == "三级"  # 原记录保留

    def test_record_without_lon_lat_untouched(self):
        recs = [{"level": "三级", "area": "蓟州区"}]
        out, matched = rwt._match_hazard_points(recs, "geologic", self._rows())
        assert matched == 0
        # 新实现统一 dict(rec) 复制（可能加字段），内容必须原样保留、不误挂 hazard 字段
        assert out[0] == recs[0]
        assert "hazard_id" not in out[0]


class TestCountyTotals:
    def test_counts_by_county(self):
        rows = [
            {"county_name": "冀州区", "lon": 1.0, "lat": 1.0},
            {"county_name": "冀州区", "lon": 1.1, "lat": 1.1},
            {"county_name": "蓟州区", "lon": 2.0, "lat": 2.0},
            {"county_name": "", "lon": 3.0, "lat": 3.0},
        ]
        assert rwt._county_totals(rows) == {"冀州区": 2, "蓟州区": 1}


class TestSummarizeByCountyLevel:
    def test_groups_and_sorts_by_severity(self):
        recs = [
            {"level": "四级", "county_name": "冀州区"},
            {"level": "四级", "county_name": "冀州区"},
            {"level": "三级", "county_name": "冀州区"},
            {"level": "二级", "county_name": "蓟州区"},
            {"level": "低风险", "county_name": "蓟州区"},  # 非风险不统计
            {"level": "三级", "area": "宝坻区"},          # 无 county_name 时退回 area
        ]
        summary = rwt._summarize_by_county_level(recs, "county_name")
        # 排序：等级优先（最重在前），同等级按数量降序；同键稳定排序保持输入顺序。
        assert summary == [
            {"county": "蓟州区", "level": "二级", "count": 1},
            {"county": "冀州区", "level": "三级", "count": 1},
            {"county": "宝坻区", "level": "三级", "count": 1},
            {"county": "冀州区", "level": "四级", "count": 2},
        ]

    def test_numeric_levels_map_into_buckets(self):
        recs = [
            {"level": "5", "county_name": "冀州区"},
            {"level": "3", "county_name": "冀州区"},
        ]
        summary = rwt._summarize_by_county_level(recs, "county_name")
        by_key = {(x["county"], x["level"]): x["count"] for x in summary}
        assert by_key[("冀州区", "一级")] == 1  # 5→一级
        assert by_key[("冀州区", "三级")] == 1

    def test_hazard_name_not_used_as_county(self):
        # 无 county_name 且无 area 的记录 → "未知区域"，绝不拿隐患点名当县名
        recs = [{"name": "石界滑坡", "level": "三级"}]
        summary = rwt._summarize_by_county_level(recs, "county_name")
        assert summary == [{"county": "未知区域", "level": "三级", "count": 1}]


class TestLevelAdvice:
    def test_geologic_has_four_levels(self):
        advice = rwt._level_advice_for("geologic")
        assert [a["level"] for a in advice] == ["一级", "二级", "三级", "四级"]
        assert all(a["advice"] for a in advice)

    def test_unknown_kind_falls_back(self):
        advice = rwt._level_advice_for("unknown_kind")
        assert len(advice) == 4

    def test_present_levels_only(self):
        # 只返回本次实际出现的等级（按严重度排序），避免"仅四级"也刷一级"立即转移"文案
        assert [a["level"] for a in rwt._level_advice_for("geologic", {"四级"})] == ["四级"]
        assert [a["level"] for a in rwt._level_advice_for("geologic", {"二级", "四级"})] == ["二级", "四级"]
        assert rwt._level_advice_for("geologic", set()) == []


class TestEnrichRiskResult:
    def _rows(self):
        return [
            {"id": 10, "name": "冀州地灾1", "lon": 117.00, "lat": 39.00,
             "county_name": "冀州区", "city_name": "天津市"},
            {"id": 11, "name": "冀州地灾2", "lon": 117.01, "lat": 39.00,
             "county_name": "冀州区", "city_name": "天津市"},
            {"id": 12, "name": "蓟州地灾", "lon": 118.0, "lat": 40.0,
             "county_name": "蓟州区", "city_name": "天津市"},
        ]

    def test_rows_present_enriches_and_summarizes(self, monkeypatch):
        monkeypatch.setattr(rwt, "_get_hazard_rows_for_kind", lambda kind: self._rows())
        recs = [
            {"level": "四级", "longitude": 117.001, "latitude": 39.0},
            {"level": "三级", "longitude": 118.0, "latitude": 40.0},
        ]
        result = rwt._enrich_risk_result({"records": []}, "geologic", recs)
        assert result["hazard_match"]["enabled"] is True
        assert result["hazard_match"]["matched_count"] == 2
        assert result["hazard_match"]["haversine_matched_count"] == 2
        assert result["county_totals"] == {"冀州区": 2, "蓟州区": 1}
        by_key = {(x["county"], x["level"]): x["count"] for x in result["county_risk_summary"]}
        assert by_key[("蓟州区", "三级")] == 1
        assert by_key[("冀州区", "四级")] == 1
        # 防范建议只覆盖本次实际出现的等级（本次仅四级+三级 → 不再刷一级最高级文案）
        assert [a["level"] for a in result["level_advice"]] == ["三级", "四级"]
        rec0 = result["records"][0]
        assert rec0["hazard_id"] in (10, 11)
        assert rec0["county_name"] == "冀州区"

    def test_id_join_primary_in_enrich(self, monkeypatch):
        monkeypatch.setattr(rwt, "_get_hazard_rows_for_kind", lambda kind: self._rows())
        # 接口样本形态：带 id/name/数字等级
        recs = [
            {"id": 11, "name": "冀州地灾2", "level": "5", "longitude": 99.0, "latitude": 99.0},
            {"id": 12, "name": "蓟州地灾", "level": "4", "longitude": 99.0, "latitude": 99.0},
        ]
        result = rwt._enrich_risk_result({"records": []}, "geologic", recs)
        assert result["hazard_match"]["id_matched_count"] == 2
        assert result["hazard_match"]["haversine_matched_count"] == 0
        assert result["hazard_match"]["unmatched_count"] == 0
        by_key = {(x["county"], x["level"]): x["count"] for x in result["county_risk_summary"]}
        assert by_key[("冀州区", "一级")] == 1  # 数字 5 → 一级
        assert by_key[("蓟州区", "二级")] == 1  # 数字 4 → 二级
        # id 直连的 county 来自静态表，与经纬度无关
        assert {r["county_name"] for r in result["records"]} == {"冀州区", "蓟州区"}

    def test_rows_none_degrades_to_area_grouping(self, monkeypatch):
        monkeypatch.setattr(rwt, "_get_hazard_rows_for_kind", lambda kind: None)
        recs = [
            {"level": "四级", "area": "冀州区", "longitude": 117.7, "latitude": 39.0},
            {"level": "三级", "area": "蓟州区"},
        ]
        result = rwt._enrich_risk_result({"records": []}, "geologic", recs)
        assert result["hazard_match"]["enabled"] is False
        assert result["county_totals"] == {}
        by_key = {(x["county"], x["level"]): x["count"] for x in result["county_risk_summary"]}
        assert by_key[("蓟州区", "三级")] == 1
        assert by_key[("冀州区", "四级")] == 1
        assert result["level_advice"]  # 逐级防范建议不依赖数据库


class TestRegionRiskLevels:
    """区域天气#8：query_region_risk_levels 按代表坐标半径查风险接口等级分布。"""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        rwt._region_levels_cache.clear()
        yield
        rwt._region_levels_cache.clear()

    def _fetch(self, records_by_kind):
        def fake(kind, extra_params=None, timeout_sec=30):
            val = records_by_kind.get(kind)
            if val is None:
                raise RuntimeError("风险接口失败")
            return {"data": val}
        return fake

    # 蓟州代表点附近（radius 25km）
    LON, LAT, R = 117.40, 40.09, 25.0

    def test_counts_levels_within_radius(self, monkeypatch):
        monkeypatch.setattr(rwt, "_fetch_risk_warning", self._fetch({
            "geologic": [
                {"id": 1, "name": "A", "level": "5", "lon": 117.41, "lat": 40.10},  # 一级，在半径内
                {"id": 2, "name": "B", "level": "3", "lon": 117.42, "lat": 40.08},  # 三级，在半径内
                {"id": 3, "name": "C", "level": "1", "lon": 117.41, "lat": 40.10},  # 1=无风险，排除
                {"id": 4, "name": "D", "level": "4", "lon": 116.0, "lat": 39.0},    # 超半径，排除
            ],
            "mountain": [],       # 可达但无风险
            "river": None,        # 接口失败，跳过
        }))
        result = rwt.query_region_risk_levels(self.LON, self.LAT, self.R)
        assert set(result.keys()) == {"dzzh", "zxhl"}
        assert result["zxhl"] is None  # river 接口失败 → None 打标（渲染层"接口暂不可用"）
        assert result["dzzh"]["levels"] == {"一级": 1, "三级": 1}
        assert result["dzzh"]["total"] == 2
        assert result["dzzh"]["label"] == "地质灾害风险"
        # level_advice 只含本次出现的等级（一级+三级）
        assert [a["level"] for a in result["dzzh"]["level_advice"]] == ["一级", "三级"]

    def test_reachable_but_no_risk_returns_empty_dict(self, monkeypatch):
        monkeypatch.setattr(rwt, "_fetch_risk_warning", self._fetch({
            "geologic": [], "mountain": [{"id": 9, "level": "1", "lon": 117.41, "lat": 40.10}],
            "river": [],
        }))
        assert rwt.query_region_risk_levels(self.LON, self.LAT, self.R) == {}

    def test_single_kind_failure_marked_none(self, monkeypatch):
        # 只有地灾接口失败：该灾种 None 打标，不得静默吞掉显示成"本次无风险"
        monkeypatch.setattr(rwt, "_fetch_risk_warning", self._fetch({
            "geologic": None, "mountain": [], "river": [],
        }))
        assert rwt.query_region_risk_levels(self.LON, self.LAT, self.R) == {"dzzh": None}

    def test_all_kinds_fail_returns_none(self, monkeypatch):
        monkeypatch.setattr(rwt, "_fetch_risk_warning", self._fetch({
            "geologic": None, "mountain": None, "river": None,
        }))
        assert rwt.query_region_risk_levels(self.LON, self.LAT, self.R) is None

    def test_three_hazard_kinds_are_fetched_concurrently(self, monkeypatch):
        barrier = Barrier(3)

        def fetch(kind, extra_params=None, timeout_sec=30):
            barrier.wait(timeout=1)
            return {"data": []}

        monkeypatch.setattr(rwt, "_fetch_risk_warning", fetch)

        assert rwt.query_region_risk_levels(self.LON, self.LAT, self.R) == {}

    def test_missing_coords_skipped(self, monkeypatch):
        monkeypatch.setattr(rwt, "_fetch_risk_warning", self._fetch({
            "geologic": [
                {"id": 1, "level": "5"},                                  # 无经纬度，跳过
                {"id": 2, "level": "5", "lon": 117.41, "lat": 40.10},     # 有效
            ],
            "mountain": [], "river": [],
        }))
        result = rwt.query_region_risk_levels(self.LON, self.LAT, self.R)
        assert result["dzzh"]["levels"] == {"一级": 1}

    def test_result_cached_and_none_not_cached(self, monkeypatch):
        calls = {"n": 0}

        def counting(kind, extra_params=None, timeout_sec=30):
            calls["n"] += 1
            return {"data": [{"id": 1, "level": "5", "lon": 117.41, "lat": 40.10}]}

        monkeypatch.setattr(rwt, "_fetch_risk_warning", counting)
        rwt.query_region_risk_levels(self.LON, self.LAT, self.R)
        first = calls["n"]
        rwt.query_region_risk_levels(self.LON, self.LAT, self.R)  # 命中缓存，不再打接口
        assert calls["n"] == first

        # None（接口全挂）不缓存，下次仍重试
        def failing(kind, extra_params=None, timeout_sec=30):
            calls["n"] += 1
            raise RuntimeError("down")

        monkeypatch.setattr(rwt, "_fetch_risk_warning", failing)
        assert rwt.query_region_risk_levels(118.0, 41.0, 25.0) is None
        after_first_fail = calls["n"]
        assert rwt.query_region_risk_levels(118.0, 41.0, 25.0) is None
        assert calls["n"] > after_first_fail  # 第二次仍调接口（None 未缓存）


class TestRegionRiskLevelsMultiDay:
    """风险接口逐日调用（2026-08-24 用户口径）：风险数据按起报时次只出 24h
    （08→次日 08、20→次日 20），"未来三天"按 明天/后天/大后天 08:00 各调一次、
    等级统计合并到同一列；无资料时次跳过（渲染层按用户要求显示"无风险"）。
    fcst_times 为 None 时保持原行为（单次默认最近起报时次）。
    """

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        rwt._region_levels_cache.clear()
        yield
        rwt._region_levels_cache.clear()

    LON, LAT, R = 116.50, 39.50, 25.0

    def _rec(self, lon, lat, level, rid=1):
        return {"id": rid, "name": "测试点", "level": level, "lon": lon, "lat": lat}

    def test_merges_level_counts_across_days(self, monkeypatch):
        calls: dict[str, list] = {}

        def fetch(kind, extra_params=None, timeout_sec=30):
            t = (extra_params or {}).get("fcstTime")
            calls.setdefault(kind, []).append(t)
            if t == "20260825080000":
                return {"data": [self._rec(self.LON, self.LAT, "5", 1),
                                 self._rec(self.LON, self.LAT, "3", 2)]}
            if t == "20260826080000":
                return {"data": [self._rec(self.LON, self.LAT, "2", 3)]}
            return {"data": []}

        monkeypatch.setattr(rwt, "_fetch_risk_warning", fetch)
        times = ["20260825080000", "20260826080000", "20260827080000"]
        levels = rwt.query_region_risk_levels(self.LON, self.LAT, self.R, fcst_times=times)
        info = levels["dzzh"]
        assert info["levels"] == {"一级": 1, "三级": 1, "四级": 1}  # 三天累计
        assert info["total"] == 3
        # 逐日各调一次（dzzh=geologic）
        assert calls["geologic"] == times

    def test_no_data_days_skipped_others_merged(self, monkeypatch):
        def fetch(kind, extra_params=None, timeout_sec=30):
            t = (extra_params or {}).get("fcstTime")
            if t == "20260825080000":  # 该日无资料（未来时次常见）→ 跳过
                raise rwt.RiskInterfaceNoDataError("资料在当前时刻下无数据")
            return {"data": [self._rec(self.LON, self.LAT, "4", 1)]}

        monkeypatch.setattr(rwt, "_fetch_risk_warning", fetch)
        levels = rwt.query_region_risk_levels(
            self.LON, self.LAT, self.R, fcst_times=["20260825080000", "20260826080000"]
        )
        assert levels["dzzh"]["levels"] == {"二级": 1}
        assert levels["sh"]["levels"] == {"二级": 1}

    def test_all_days_no_data_marks_sentinel(self, monkeypatch):
        def fetch(kind, extra_params=None, timeout_sec=30):
            raise rwt.RiskInterfaceNoDataError("资料在当前时刻下无数据")

        monkeypatch.setattr(rwt, "_fetch_risk_warning", fetch)
        levels = rwt.query_region_risk_levels(
            self.LON, self.LAT, self.R, fcst_times=["20260825080000", "20260826080000"]
        )
        assert levels is not None  # 接口是通的 → 不是整体 None
        assert all(v == rwt.RISK_LEVELS_NO_DATA for v in levels.values())

    def test_any_kind_failure_marks_none_even_with_earlier_data(self, monkeypatch):
        def fetch(kind, extra_params=None, timeout_sec=30):
            t = (extra_params or {}).get("fcstTime")
            if t == "20260827080000":
                raise RuntimeError("HTTP 500")
            return {"data": [self._rec(self.LON, self.LAT, "5", 1)]}

        monkeypatch.setattr(rwt, "_fetch_risk_warning", fetch)
        levels = rwt.query_region_risk_levels(
            self.LON, self.LAT, self.R, fcst_times=["20260825080000", "20260827080000"]
        )
        assert levels["dzzh"] is None  # 有一次失败 → 该灾种"接口暂不可用"

    def test_no_fcst_times_single_default_call(self, monkeypatch):
        seen: list = []

        def fetch(kind, extra_params=None, timeout_sec=30):
            seen.append((kind, extra_params))
            return {"data": [self._rec(self.LON, self.LAT, "5", 1)]}

        monkeypatch.setattr(rwt, "_fetch_risk_warning", fetch)
        levels = rwt.query_region_risk_levels(self.LON, self.LAT, self.R)
        assert levels["dzzh"]["levels"] == {"一级": 1}
        # 未传 fcstTime → 每灾种单次默认调用（fcstTime 由 fetch 层补最近起报时次）
        assert Counter(k for k, _ in seen) == Counter(["geologic", "mountain", "river"])
        assert all(extra in (None, {}) for _, extra in seen)

    def test_cache_key_includes_fcst_times(self, monkeypatch):
        calls: list = []

        def fetch(kind, extra_params=None, timeout_sec=30):
            calls.append((extra_params or {}).get("fcstTime"))
            return {"data": [self._rec(self.LON, self.LAT, "5", 1)]}

        monkeypatch.setattr(rwt, "_fetch_risk_warning", fetch)
        a = ["20260825080000"]
        b = ["20260826080000"]
        rwt.query_region_risk_levels(self.LON, self.LAT, self.R, fcst_times=a)
        rwt.query_region_risk_levels(self.LON, self.LAT, self.R, fcst_times=b)  # 不同时次→重调
        rwt.query_region_risk_levels(self.LON, self.LAT, self.R, fcst_times=a)  # 同键→缓存命中
        assert Counter(calls) == Counter(["20260825080000"] * 3 + ["20260826080000"] * 3)


class TestRegionRiskLevelsFallback:
    """默认最近起报时次无资料 → 回退前一个起报时次一次（2026-08-24 甲方口径：24h 内要出风险）。

    场景：上午问"今天下午/晚上"，最近 08:00 时次的数据可能还没出；前一周期（昨日 20:00，
    覆盖昨晚 20:00→今晚 20:00）仍覆盖今日下午/晚上，应回退取它的数据，而不是显示
    "该时次暂无数据"。显式 fcst_times（外部传入）不做回退，保持逐日合并语义。
    """

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        rwt._region_levels_cache.clear()
        yield
        rwt._region_levels_cache.clear()

    LON, LAT, R = 117.40, 40.09, 25.0

    def test_default_no_data_falls_back_to_previous_cycle(self, monkeypatch):
        monkeypatch.setattr(rwt, "_default_fcst_time", lambda: "20260824080000")
        seen: list = []

        def fetch(kind, extra_params=None, timeout_sec=30):
            t = (extra_params or {}).get("fcstTime")
            seen.append(t)
            if t == "20260823200000":  # 前一周期（昨日20:00）有数据
                return {"data": [{"id": 1, "level": "5", "lon": self.LON, "lat": self.LAT}]}
            raise rwt.RiskInterfaceNoDataError("资料在当前时刻下无数据")  # 默认时次无资料

        monkeypatch.setattr(rwt, "_fetch_risk_warning", fetch)
        result = rwt.query_region_risk_levels(self.LON, self.LAT, self.R)
        assert result["dzzh"]["levels"] == {"一级": 1}
        # 每灾种两次调用：默认(None) + 回退前一周期(20260823200000)
        assert seen.count(None) == 3
        assert seen.count("20260823200000") == 3

    def test_default_has_data_no_fallback(self, monkeypatch):
        seen: list = []

        def fetch(kind, extra_params=None, timeout_sec=30):
            seen.append((extra_params or {}).get("fcstTime"))
            return {"data": [{"id": 1, "level": "5", "lon": self.LON, "lat": self.LAT}]}

        monkeypatch.setattr(rwt, "_fetch_risk_warning", fetch)
        result = rwt.query_region_risk_levels(self.LON, self.LAT, self.R)
        assert result["dzzh"]["levels"] == {"一级": 1}
        assert Counter(seen) == Counter([None, None, None])  # 每灾种只调一次默认，无回退

    def test_fallback_success_with_no_risk_clears_earlier_no_data(self, monkeypatch):
        monkeypatch.setattr(rwt, "_default_fcst_time", lambda: "20260824080000")

        def fetch(kind, extra_params=None, timeout_sec=30):
            if (extra_params or {}).get("fcstTime") is None:
                raise rwt.RiskInterfaceNoDataError("资料在当前时刻下无数据")
            return {"data": []}

        monkeypatch.setattr(rwt, "_fetch_risk_warning", fetch)

        assert rwt.query_region_risk_levels(self.LON, self.LAT, self.R) == {}

    def test_default_and_fallback_both_no_data_marks_sentinel(self, monkeypatch):
        monkeypatch.setattr(rwt, "_default_fcst_time", lambda: "20260824080000")

        def fetch(kind, extra_params=None, timeout_sec=30):
            raise rwt.RiskInterfaceNoDataError("资料在当前时刻下无数据")

        monkeypatch.setattr(rwt, "_fetch_risk_warning", fetch)
        result = rwt.query_region_risk_levels(self.LON, self.LAT, self.R)
        assert result is not None
        assert all(v == rwt.RISK_LEVELS_NO_DATA for v in result.values())

    def test_explicit_fcst_times_no_fallback(self, monkeypatch):
        seen: list = []

        def fetch(kind, extra_params=None, timeout_sec=30):
            seen.append((extra_params or {}).get("fcstTime"))
            raise rwt.RiskInterfaceNoDataError("资料在当前时刻下无数据")

        monkeypatch.setattr(rwt, "_fetch_risk_warning", fetch)
        result = rwt.query_region_risk_levels(self.LON, self.LAT, self.R, fcst_times=["20260825080000"])
        # 显式时次不做回退（保持逐日合并语义），每灾种只调该时次一次
        assert Counter(seen) == Counter(["20260825080000"] * 3)
        assert all(v == rwt.RISK_LEVELS_NO_DATA for v in result.values())


class TestFcstTimeRequired:
    """后端 findDataListByConfig 必传 fcstTime（yyyyMMddHHmmss）。

    2026-08-24 服务器 curl 证实：
      model+type 不带 fcstTime          → HTTP 500
      fcstTime=20260824080000           → 200
      fcstTime=2026-08-24 08:00:00      → 400
      历史日期 fcstTime（20260710…）    → HTTP 200（后端接受历史起报时次；
        无数据的时次回业务错误 {"code":400,"success":false,"msg":"资料在当前时刻下无数据"}）
    三类（EC 山洪/中小河流 + SCMOC 地灾）统一由 fcstTime 推导 startTime/endTime
    （startTime=fcstTime、endTime=+24h；2026-08-24 甲方反馈"接口不可用"后确认三类
    都需带该时间段）。默认 fcstTime 跟随 time_source 系统时间（验收切换系统时间时
    按"当时"的起报时次取数，用户 2026-08-24 口径："风险时间要按当时的时间判断"）。
    """

    class _FakeResp:
        ok = True
        status_code = 200
        text = '{"data": []}'

        def json(self):
            return {"data": []}

    def _capture_get(self, monkeypatch):
        captured = {}

        def fake_get(url, params=None, headers=None, timeout=None):
            captured["url"] = url
            captured["params"] = dict(params or {})
            return self._FakeResp()

        monkeypatch.setattr(rwt.requests, "get", fake_get)
        return captured

    def test_fetch_guarantees_fcst_time_when_missing(self, monkeypatch):
        captured = self._capture_get(monkeypatch)
        rwt._fetch_risk_warning("river", {})
        params = captured["params"]
        assert params["model"] == "EC"
        assert params["type"] == 1
        assert re.fullmatch(r"\d{14}", str(params.get("fcstTime", ""))), params

    def test_fetch_preserves_explicit_fcst_time(self, monkeypatch):
        captured = self._capture_get(monkeypatch)
        rwt._fetch_risk_warning("mountain", {"fcstTime": "20260101080000"})
        assert captured["params"]["fcstTime"] == "20260101080000"
        assert captured["params"]["model"] == "EC"
        assert captured["params"]["type"] == 2

    def test_fetch_strips_start_end_time(self, monkeypatch):
        # 调用方传入的 startTime/endTime 一律剥离、改由 fcstTime 推导（防与 fcstTime
        # 不一致）。2026-08-24 起三类（含 EC 山洪/中小河流）都按 fcstTime 推导并发送。
        captured = self._capture_get(monkeypatch)
        rwt._fetch_risk_warning("river", {
            "startTime": "20260101080000",
            "endTime": "20260102080000",
            "fcstTime": "20260103080000",
        })
        params = captured["params"]
        assert params["fcstTime"] == "20260103080000"
        assert params["startTime"] == "20260103080000"  # 推导值覆盖调用方值
        assert params["endTime"] == "20260104080000"

    def test_ec_types_also_derive_time_range(self, monkeypatch):
        # 2026-08-24 甲方反馈"接口不可用"：EC 两类（山洪/中小河流）同样要带
        # startTime/endTime（startTime=fcstTime、endTime=+24h），不再只发 fcstTime。
        for kind, mtype in (("river", 1), ("mountain", 2)):
            captured = self._capture_get(monkeypatch)
            rwt._fetch_risk_warning(kind, {"fcstTime": "20260824080000"})
            params = captured["params"]
            assert params["model"] == "EC" and params["type"] == mtype
            assert params["startTime"] == "20260824080000"
            assert params["endTime"] == "20260825080000"

    def test_scmoc_derives_time_range_from_fcst_time(self, monkeypatch):
        # SCMOC 地灾：fcstTime + startTime + endTime 缺一不可（缺 → 500），
        # startTime=fcstTime、endTime=fcstTime+24h（2026-08-24 接口开发确认的调法）
        captured = self._capture_get(monkeypatch)
        rwt._fetch_risk_warning("geologic", {"fcstTime": "20260824080000"})
        params = captured["params"]
        assert params["fcstTime"] == "20260824080000"
        assert params["startTime"] == "20260824080000"
        assert params["endTime"] == "20260825080000"

    def test_scmoc_time_range_month_boundary(self, monkeypatch):
        captured = self._capture_get(monkeypatch)
        rwt._fetch_risk_warning("geologic", {"fcstTime": "20260831200000"})
        assert captured["params"]["endTime"] == "20260901200000"

    def test_scmoc_caller_time_range_overridden_by_derived(self, monkeypatch):
        # 调用方传入的 startTime/endTime 不可信（可能与 fcstTime 不一致），一律以推导为准
        captured = self._capture_get(monkeypatch)
        rwt._fetch_risk_warning("geologic", {
            "fcstTime": "20260824080000",
            "startTime": "20260101000000",
            "endTime": "20260102000000",
        })
        params = captured["params"]
        assert params["startTime"] == "20260824080000"
        assert params["endTime"] == "20260825080000"

    def test_scmoc_auto_fcst_time_also_gets_range(self, monkeypatch):
        captured = self._capture_get(monkeypatch)
        rwt._fetch_risk_warning("geologic", {})
        params = captured["params"]
        assert re.fullmatch(r"\d{14}", str(params.get("fcstTime", ""))), params
        assert params["startTime"] == params["fcstTime"]
        assert re.fullmatch(r"\d{14}", str(params.get("endTime", ""))), params

    def test_scmoc_bad_fcst_time_skips_range_derivation(self, monkeypatch):
        # 显式传入非法 fcstTime：不崩、不编造时间段，原样发出（后端会 400/500，由调用方负责）
        captured = self._capture_get(monkeypatch)
        rwt._fetch_risk_warning("geologic", {"fcstTime": "not-a-time"})
        params = captured["params"]
        assert params["fcstTime"] == "not-a-time"
        assert "startTime" not in params
        assert "endTime" not in params

    def test_default_fcst_time_follows_sim_time(self, monkeypatch):
        # 验收切换系统时间（如模拟 2026-07-10 15:00）→ 默认 fcstTime 折到"当时"
        # 的最近起报时次（该日 08 时次），而不是真实时钟——用户 2026-08-24 口径：
        # "风险时间肯定也要按照当时的时间来判断"。后端已证实接受历史起报时次。
        bjt = datetime.timezone(datetime.timedelta(hours=8))
        sim = datetime.datetime(2026, 7, 10, 15, 0, tzinfo=bjt)
        monkeypatch.setattr(rwt.time_source, "now", lambda tz=None: sim)
        assert rwt._default_fcst_time() == "20260710080000"

    def test_default_fcst_time_uses_real_now_without_override(self, monkeypatch):
        # 无模拟覆盖时 time_source.now 即真实时间：与真实"现在"推算的最近起报时次一致
        # （允许跨边界 1 个周期内的误差：直接断言与 _latest_fcst_cycle(real_now) 相同即可）
        real_now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
        fcst = rwt._default_fcst_time()
        assert re.fullmatch(r"\d{14}", fcst)
        assert fcst == rwt._latest_fcst_cycle(real_now)


class TestLatestFcstCycle:
    BJT = datetime.timezone(datetime.timedelta(hours=8))

    def _now(self, day, hour):
        return datetime.datetime(2026, 8, day, hour, 30, tzinfo=self.BJT)

    def test_before_8_uses_yesterday_20(self):
        assert rwt._latest_fcst_cycle(self._now(24, 7)) == "20260823200000"
        assert rwt._latest_fcst_cycle(self._now(24, 0)) == "20260823200000"

    def test_between_8_and_20_uses_today_08(self):
        assert rwt._latest_fcst_cycle(self._now(24, 8)) == "20260824080000"
        assert rwt._latest_fcst_cycle(self._now(24, 19)) == "20260824080000"

    def test_at_or_after_20_uses_today_20(self):
        assert rwt._latest_fcst_cycle(self._now(24, 20)) == "20260824200000"
        assert rwt._latest_fcst_cycle(self._now(24, 23)) == "20260824200000"


class _FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class TestToolParamForwarding:
    """query_risk_warning 工具层：只转发 fcstTime，不发 startTime/endTime。"""

    def _tool_and_capture(self, monkeypatch):
        mcp = _FakeMcp()
        rwt.register_risk_warning_tool(mcp)
        captured = {}

        def fake_fetch(kind, extra_params=None, timeout_sec=30):
            captured["extra"] = dict(extra_params or {})
            return {"data": []}

        monkeypatch.setattr(rwt, "_fetch_risk_warning", fake_fetch)
        # 隔离 DB 侧的隐患点 enrich（本类只测参数转发口径）
        monkeypatch.setattr(rwt, "_enrich_risk_result", lambda result, kind, records: result)
        return mcp.tools["query_risk_warning"], captured

    def test_no_time_args_sends_only_fcst_time(self, monkeypatch):
        # 缓存键不含 region 但含 extra_params_json，用唯一 tag 防跨用例缓存命中
        tool, captured = self._tool_and_capture(monkeypatch)
        result = tool("river", extra_params_json='{"_t": "case1"}')
        extra = captured["extra"]
        assert "startTime" not in extra
        assert "endTime" not in extra
        assert re.fullmatch(r"\d{14}", str(extra.get("fcstTime", ""))), extra
        assert result["query"]["fcst_time"] == extra["fcstTime"]

    def test_explicit_fcst_time_forwarded(self, monkeypatch):
        tool, captured = self._tool_and_capture(monkeypatch)
        result = tool("river", fcst_time="20260101080000", extra_params_json='{"_t": "case2"}')
        assert captured["extra"]["fcstTime"] == "20260101080000"
        assert result["query"]["fcst_time"] == "20260101080000"

    def test_explicit_start_end_time_not_forwarded(self, monkeypatch):
        tool, captured = self._tool_and_capture(monkeypatch)
        tool(
            "river",
            start_time="20260101080000",
            end_time="20260102080000",
            extra_params_json='{"_t": "case3"}',
        )
        extra = captured["extra"]
        assert "startTime" not in extra
        assert "endTime" not in extra


class TestWiring:
    def test_query_risk_warning_calls_enrich(self):
        src = (MCP_DIR / "custom_tools" / "risk_warning_tool.py").read_text(encoding="utf-8")
        assert "_enrich_risk_result(result, kind, all_records)" in src

    def test_hazard_key_mapping_covers_all_kinds(self):
        assert rwt.HAZARD_KIND_TO_KEY == {"geologic": "dzzh", "mountain": "sh", "river": "zxhl"}

    def test_numeric_map_handles_five(self):
        assert rwt._NUMERIC_LEVEL_MAP["5"] == "一级"
        assert rwt._NUMERIC_LEVEL_MAP["2"] == "四级"

    def test_start_end_time_only_derived_for_scmoc(self):
        # 调用方提供的 startTime/endTime 一律剥离；仅 SCMOC 地灾由 fcstTime 推导补上
        src = (MCP_DIR / "custom_tools" / "risk_warning_tool.py").read_text(encoding="utf-8")
        assert 'params.pop("startTime", None)' in src
        assert 'params.pop("endTime", None)' in src
        assert 'setdefault("startTime"' not in src
        assert 'extra["startTime"]' not in src
        assert 'extra["endTime"]' not in src

    def test_fcst_time_follows_system_time_source(self):
        # 风险 fcstTime 必须跟随系统时间（time_source）——验收切换系统时间时
        # 风险研判按"当时"的起报时次取数，而不是真实时钟（2026-08-24 用户口径，
        # 后端 curl 证实历史起报时次 HTTP 200 可用）。
        src = (MCP_DIR / "custom_tools" / "risk_warning_tool.py").read_text(encoding="utf-8")
        assert "import time_source" in src
        assert "time_source.now" in src


class TestBusinessNoData:
    """无数据起报时次 ≠ 本次无风险（2026-08-24 服务器 curl 实证）。

    后端对"该起报时次没有资料"返回 HTTP 200 + 业务错误：
      {"code":400,"success":false,"data":{},"msg":"资料在当前时刻下无数据"}
    若当普通空结果处理，区域风险表会把"该时次暂无数据"误显示为"本次无风险"
    （与"接口暂不可用"、"本次无风险"是三态，必须区分）。
    """

    class _NoDataResp:
        ok = True
        status_code = 200
        text = '{"code":400,"success":false,"data":{},"msg":"资料在当前时刻下无数据"}'

        def json(self):
            return {"code": 400, "success": False, "data": {}, "msg": "资料在当前时刻下无数据"}

    class _OkEmptyResp:
        ok = True
        status_code = 200
        text = '{"code":200,"success":true,"data":[]}'

        def json(self):
            return {"code": 200, "success": True, "data": []}

    def test_fetch_raises_no_data_error(self, monkeypatch):
        monkeypatch.setattr(rwt.requests, "get", lambda *a, **k: self._NoDataResp())
        with pytest.raises(rwt.RiskInterfaceNoDataError):
            rwt._fetch_risk_warning("river", {})

    def test_fetch_ok_empty_does_not_raise(self, monkeypatch):
        # 可达且无风险（success=true + 空 data）不抛错，走正常空结果路径
        monkeypatch.setattr(rwt.requests, "get", lambda *a, **k: self._OkEmptyResp())
        payload = rwt._fetch_risk_warning("river", {})
        assert payload.get("success") is True

    def test_fetch_other_business_failure_raises_runtime(self, monkeypatch):
        # success=false 但非"无数据"（其它业务失败）→ 普通失败（接口暂不可用路径）
        class _OtherFail:
            ok = True
            status_code = 200
            text = '{"code":500,"success":false,"msg":"服务器内部错误"}'

            def json(self):
                return {"code": 500, "success": False, "msg": "服务器内部错误"}

        monkeypatch.setattr(rwt.requests, "get", lambda *a, **k: _OtherFail())
        with pytest.raises(RuntimeError) as excinfo:
            rwt._fetch_risk_warning("river", {})
        assert not isinstance(excinfo.value, rwt.RiskInterfaceNoDataError)

    def test_tool_returns_no_data_status(self, monkeypatch):
        mcp = _FakeMcp()
        rwt.register_risk_warning_tool(mcp)

        def no_data_fetch(kind, extra_params=None, timeout_sec=30):
            raise rwt.RiskInterfaceNoDataError("资料在当前时刻下无数据")

        monkeypatch.setattr(rwt, "_fetch_risk_warning", no_data_fetch)
        tool = mcp.tools["query_risk_warning"]
        result = tool("geologic", extra_params_json='{"_t": "nodata1"}')
        assert result["status"] == "no_data"
        assert "暂无数据" in result["message"]
        # 绝不能出现"无风险"字样——无数据 ≠ 无风险
        assert "无风险" not in result["message"]

    def test_region_risk_levels_marks_no_data_sentinel(self, monkeypatch):
        # geologic=该时次无数据、mountain=可达无风险、river=接口失败（三态并存）
        def fetch(kind, extra_params=None, timeout_sec=30):
            if kind == "geologic":
                raise rwt.RiskInterfaceNoDataError("资料在当前时刻下无数据")
            if kind == "mountain":
                return {"code": 200, "success": True, "data": []}
            raise RuntimeError("HTTP 500")

        monkeypatch.setattr(rwt, "_fetch_risk_warning", fetch)
        # 用本文件未用过的坐标，避免命中 REGION_RISK_LEVELS_CACHE
        levels = rwt.query_region_risk_levels(112.34, 36.78, 25.0)
        assert levels is not None  # 有灾种接口可达（无数据也算可达）→ 不是整体 None
        assert levels["dzzh"] == rwt.RISK_LEVELS_NO_DATA  # 渲染层显示"该时次暂无数据"
        assert "sh" not in levels  # 可达无风险 → 键缺席 → 渲染"本次无风险"
        assert levels["zxhl"] is None  # 接口失败 → 渲染"接口暂不可用"

    def test_region_risk_levels_all_no_data_is_reachable(self, monkeypatch):
        # 全部灾种都是"该时次无数据"：接口是通的，不得返回 None（None=全挂）。
        def fetch(kind, extra_params=None, timeout_sec=30):
            raise rwt.RiskInterfaceNoDataError("资料在当前时刻下无数据")

        monkeypatch.setattr(rwt, "_fetch_risk_warning", fetch)
        levels = rwt.query_region_risk_levels(112.35, 36.79, 25.0)
        assert levels is not None
        assert all(v == rwt.RISK_LEVELS_NO_DATA for v in levels.values())
