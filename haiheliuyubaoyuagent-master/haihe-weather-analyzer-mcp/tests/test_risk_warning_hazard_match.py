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

import importlib.util
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
        assert set(result.keys()) == {"dzzh"}
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

    def test_all_kinds_fail_returns_none(self, monkeypatch):
        monkeypatch.setattr(rwt, "_fetch_risk_warning", self._fetch({
            "geologic": None, "mountain": None, "river": None,
        }))
        assert rwt.query_region_risk_levels(self.LON, self.LAT, self.R) is None

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


class TestWiring:
    def test_query_risk_warning_calls_enrich(self):
        src = (MCP_DIR / "custom_tools" / "risk_warning_tool.py").read_text(encoding="utf-8")
        assert "_enrich_risk_result(result, kind, all_records)" in src

    def test_hazard_key_mapping_covers_all_kinds(self):
        assert rwt.HAZARD_KIND_TO_KEY == {"geologic": "dzzh", "mountain": "sh", "river": "zxhl"}

    def test_numeric_map_handles_five(self):
        assert rwt._NUMERIC_LEVEL_MAP["5"] == "一级"
        assert rwt._NUMERIC_LEVEL_MAP["2"] == "四级"
