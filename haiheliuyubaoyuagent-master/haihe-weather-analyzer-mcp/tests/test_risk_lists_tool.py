# -*- coding: utf-8 -*-
"""天津灾害风险清单接口（/conclusion/risk-lists）单元测试（2026-10-08）。

接口文档（甲方 2026-09-29《天津灾害风险清单接口说明》）：
    GET http://10.226.107.130:8001/conclusion/risk-lists?start_time=YYYYMMDDHHMMSS
返回聚合清单：township_risks（乡镇×灾种×等级）、industry_risks、key_objects、
call_advice。无逐隐患点经纬度/id。三清单全空 = 本次无风险（call_advice 恒返回，
不作判据）。

锁定：等级映射（低/中/高/极高→四~一级）、灾种标签映射、township 区县解析、
灾种/区域过滤与聚合、输出结构与旧渲染层兼容（levels 一~四级计数字典）。
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


# ---------------------------------------------------------------- 样例 payload

SAMPLE_PAYLOAD = {
    "township_risks": [
        {
            "township": "蓟县官庄镇",
            "risk_level": "极高",
            "main_risks": [{"hazard_type": "地灾", "count": 5}],
        },
        {
            "township": "蓟县穿芳峪镇",
            "risk_level": "高",
            "main_risks": [
                {"hazard_type": "山洪", "count": 3},
                {"hazard_type": "地灾", "count": 2},
            ],
        },
        {
            "township": "宝坻区牛道口镇",
            "risk_level": "中",
            "main_risks": [{"hazard_type": "中小河流洪水", "count": 4}],
        },
        {
            "township": "西青区杨柳青镇",
            "risk_level": "低",
            "main_risks": [{"hazard_type": "中小河流洪水", "count": 1}],
        },
    ],
    "industry_risks": [
        {
            "industry": "商务",
            "risk_level": "极高",
            "key_objects": ["酒店住宿", "生活服务"],
            "suggested_measures": ["保供稳价"],
        }
    ],
    "key_objects": [
        {
            "object_type": "酒店住宿",
            "count": 4,
            "risk_level": "极高",
            "locations": ["蓟县官庄镇", "蓟县穿芳峪镇"],
        }
    ],
    "call_advice": ["立即向相关乡镇发送预警信息", "加强山洪沟、地灾隐患点巡查"],
}

EMPTY_PAYLOAD = {
    "township_risks": [],
    "industry_risks": [],
    "key_objects": [],
    "call_advice": ["立即向相关乡镇发送预警信息"],
}


# ---------------------------------------------------------------- 等级映射

class TestRiskListLevelMap:
    def test_chinese_levels_to_severity(self):
        assert rwt._risk_list_level("极高") == "一级"
        assert rwt._risk_list_level("高") == "二级"
        assert rwt._risk_list_level("中") == "三级"
        assert rwt._risk_list_level("低") == "四级"

    def test_unknown_level_passthrough(self):
        assert rwt._risk_list_level("未知等级") == "未知等级"

    def test_empty_level(self):
        assert rwt._risk_list_level("") == ""
        assert rwt._risk_list_level(None) == ""


# ---------------------------------------------------------------- 灾种标签

class TestRiskListHazardLabels:
    def test_hazard_type_to_key(self):
        assert rwt._RISK_LIST_HAZARD_TO_KEY["地灾"] == "dzzh"
        assert rwt._RISK_LIST_HAZARD_TO_KEY["山洪"] == "sh"
        assert rwt._RISK_LIST_HAZARD_TO_KEY["中小河流洪水"] == "zxhl"

    def test_kind_to_labels_covers_three_kinds(self):
        assert set(rwt._RISK_LIST_KIND_LABELS) == {"geologic", "mountain", "river"}
        assert rwt._RISK_LIST_KIND_LABELS["geologic"] == ("地灾",)
        assert rwt._RISK_LIST_KIND_LABELS["mountain"] == ("山洪",)
        assert rwt._RISK_LIST_KIND_LABELS["river"] == ("中小河流洪水",)


# ---------------------------------------------------------------- township 区县解析

class TestTownshipCountyParsing:
    def test_jixian_maps_to_jizhou(self):
        assert rwt._township_county("蓟县官庄镇") == "蓟州区"

    def test_prefixed_county_name(self):
        assert rwt._township_county("宝坻区牛道口镇") == "宝坻区"
        assert rwt._township_county("西青区杨柳青镇") == "西青区"

    def test_unknown_township(self):
        assert rwt._township_county("某某镇") == ""

    def test_region_match_by_alias(self):
        assert rwt._township_matches_region("蓟县官庄镇", "蓟州") is True
        assert rwt._township_matches_region("蓟县官庄镇", "蓟县") is True
        assert rwt._township_matches_region("蓟县官庄镇", "蓟州区") is True
        assert rwt._township_matches_region("宝坻区牛道口镇", "宝坻") is True
        assert rwt._township_matches_region("宝坻区牛道口镇", "蓟州") is False


# ---------------------------------------------------------------- 记录提取与过滤

class TestExtractRiskListRecords:
    def test_extract_all_records(self):
        records = rwt._extract_risk_list_records(SAMPLE_PAYLOAD)
        assert len(records) == 4
        first = records[0]
        assert first["township"] == "蓟县官庄镇"
        assert first["level"] == "极高"
        assert first["level_norm"] == "一级"
        assert first["county"] == "蓟州区"

    def test_filter_by_kind_geologic(self):
        records = rwt._extract_risk_list_records(SAMPLE_PAYLOAD, kind="geologic")
        # 官庄(地灾5) + 穿芳峪(地灾2)，山洪主险不算
        assert len(records) == 2
        assert all(
            any(m.get("hazard_type") == "地灾" for m in r["main_risks"])
            for r in records
        )

    def test_filter_by_kind_river(self):
        records = rwt._extract_risk_list_records(SAMPLE_PAYLOAD, kind="river")
        assert {r["township"] for r in records} == {"宝坻区牛道口镇", "西青区杨柳青镇"}

    def test_filter_by_region(self):
        records = rwt._extract_risk_list_records(SAMPLE_PAYLOAD, region="蓟州")
        assert {r["township"] for r in records} == {"蓟县官庄镇", "蓟县穿芳峪镇"}

    def test_kind_and_region_combined(self):
        records = rwt._extract_risk_list_records(SAMPLE_PAYLOAD, kind="geologic", region="宝坻")
        assert records == []

    def test_empty_payload_no_records(self):
        assert rwt._extract_risk_list_records(EMPTY_PAYLOAD) == []

    def test_non_dict_payload(self):
        assert rwt._extract_risk_list_records(None) == []
        assert rwt._extract_risk_list_records({"township_risks": "bad"}) == []


# ---------------------------------------------------------------- 区域等级聚合（结构同旧渲染层）

class TestAggregateRiskListLevels:
    def test_region_levels_structure(self):
        levels = rwt._aggregate_risk_list_levels(SAMPLE_PAYLOAD, "蓟州")
        assert levels is not None
        dzzh = levels["dzzh"]
        assert dzzh["label"] == "地质灾害风险"
        assert dzzh["kind"] == "geologic"
        # 官庄 5 处极高(一级) + 穿芳峪 2 处高(二级)
        assert dzzh["levels"] == {"一级": 5, "二级": 2}
        assert dzzh["total"] == 7
        sh = levels["sh"]
        assert sh["levels"] == {"二级": 3}
        # 中小河流洪水在蓟州无记录 → 键缺席（渲染"本次无风险"）
        assert "zxhl" not in levels

    def test_region_without_records_empty_dict(self):
        levels = rwt._aggregate_risk_list_levels(SAMPLE_PAYLOAD, "静海")
        assert levels == {}

    def test_no_coverage_for_non_tianjin(self):
        levels = rwt._aggregate_risk_list_levels(SAMPLE_PAYLOAD, "唐山")
        assert levels == rwt.RISK_LEVELS_NO_COVERAGE

    def test_call_advice_attached(self):
        levels = rwt._aggregate_risk_list_levels(SAMPLE_PAYLOAD, "蓟州")
        assert levels["dzzh"]["level_advice"] == SAMPLE_PAYLOAD["call_advice"]


# ---------------------------------------------------------------- fetch 层（mock requests）

class TestFetchRiskLists:
    def test_fetch_hits_risk_lists_route(self, monkeypatch):
        calls = {}

        class _Resp:
            ok = True
            status_code = 200

            def json(self):
                return SAMPLE_PAYLOAD

        def fake_get(url, params=None, headers=None, timeout=None):
            calls["url"] = url
            calls["params"] = params
            return _Resp()

        monkeypatch.setattr(rwt.requests, "get", fake_get)
        payload = rwt._fetch_risk_lists("20261008080000")
        assert payload["township_risks"]
        assert calls["url"].endswith("/conclusion/risk-lists")
        assert calls["params"]["start_time"] == "20261008080000"

    def test_fetch_default_base_host(self, monkeypatch):
        calls = {}

        class _Resp:
            ok = True
            status_code = 200

            def json(self):
                return EMPTY_PAYLOAD

        def fake_get(url, params=None, headers=None, timeout=None):
            calls["url"] = url
            return _Resp()

        monkeypatch.setattr(rwt.requests, "get", fake_get)
        monkeypatch.delenv("RISK_LISTS_API_BASE", raising=False)
        monkeypatch.delenv("RISK_WARN_BASE", raising=False)
        rwt._fetch_risk_lists("20261008080000")
        assert "10.226.107.130:8001" in calls["url"]

    def test_fetch_http_error_raises(self, monkeypatch):
        class _Resp:
            ok = False
            status_code = 500
            text = "boom"

        monkeypatch.setattr(rwt.requests, "get", lambda *a, **kw: _Resp())
        with pytest.raises(RuntimeError):
            rwt._fetch_risk_lists("20261008080000")

    def test_fetch_connection_error_raises(self, monkeypatch):
        def boom(*a, **kw):
            raise rwt.requests.RequestException("conn refused")

        monkeypatch.setattr(rwt.requests, "get", boom)
        with pytest.raises(RuntimeError):
            rwt._fetch_risk_lists("20261008080000")


# ---------------------------------------------------------------- query_risk_warning 工具出口

class TestQueryRiskWarningRiskLists:
    def test_ok_with_kind_filter(self, monkeypatch):
        monkeypatch.setattr(rwt, "_fetch_risk_lists", lambda start_time, timeout_sec=30: SAMPLE_PAYLOAD)
        result = rwt._query_risk_warning_core(risk_kind="geologic")
        assert result["status"] == "ok"
        assert result["risk_kind"] == "geologic"
        assert result["risk_count"] == 2
        assert result["county_totals"] == {"蓟州区": 2}
        summary = result["county_risk_summary"]
        assert {"county": "蓟州区", "level": "一级", "count": 1} in summary
        assert {"county": "蓟州区", "level": "二级", "count": 1} in summary
        assert result["level_advice"] == SAMPLE_PAYLOAD["call_advice"]

    def test_no_kind_returns_all_townships(self, monkeypatch):
        monkeypatch.setattr(rwt, "_fetch_risk_lists", lambda start_time, timeout_sec=30: SAMPLE_PAYLOAD)
        result = rwt._query_risk_warning_core(risk_kind="")
        assert result["status"] == "ok"
        assert result["risk_count"] == 4

    def test_empty_lists_is_ok_no_risk(self, monkeypatch):
        monkeypatch.setattr(rwt, "_fetch_risk_lists", lambda start_time, timeout_sec=30: EMPTY_PAYLOAD)
        result = rwt._query_risk_warning_core(risk_kind="geologic")
        assert result["status"] == "ok"
        assert result["risk_count"] == 0
        assert "未发现" in result["message"] or "无" in result["message"]

    def test_fetch_failure_is_error_not_no_risk(self, monkeypatch):
        def boom(start_time, timeout_sec=30):
            raise RuntimeError("接口连接失败")

        monkeypatch.setattr(rwt, "_fetch_risk_lists", boom)
        result = rwt._query_risk_warning_core(risk_kind="geologic")
        assert result["status"] == "error"
        assert "无风险" not in result.get("message", "")

    def test_bad_kind_returns_error(self, monkeypatch):
        monkeypatch.setattr(rwt, "_fetch_risk_lists", lambda start_time, timeout_sec=30: SAMPLE_PAYLOAD)
        result = rwt._query_risk_warning_core(risk_kind="冰雹")
        assert result["status"] == "error"

    def test_start_time_passthrough(self, monkeypatch):
        seen = {}

        def fake(start_time, timeout_sec=30):
            seen["start_time"] = start_time
            return SAMPLE_PAYLOAD

        monkeypatch.setattr(rwt, "_fetch_risk_lists", fake)
        rwt._query_risk_warning_core(risk_kind="geologic", fcst_time="20261001080000")
        assert seen["start_time"] == "20261001080000"


# ---------------------------------------------------------------- query_region_risk_levels（新签名）

class TestQueryRegionRiskLevelsRiskLists:
    def setup_method(self):
        rwt._region_levels_cache.clear()

    def teardown_method(self):
        rwt._region_levels_cache.clear()

    def test_tianjin_region_aggregates(self, monkeypatch):
        monkeypatch.setattr(rwt, "_fetch_risk_lists", lambda start_time, timeout_sec=30: SAMPLE_PAYLOAD)
        levels = rwt.query_region_risk_levels(117.4, 40.0, 25.0, region="蓟州")
        assert isinstance(levels, dict)
        assert levels["dzzh"]["levels"] == {"一级": 5, "二级": 2}

    def test_non_tianjin_region_no_coverage(self, monkeypatch):
        monkeypatch.setattr(rwt, "_fetch_risk_lists", lambda start_time, timeout_sec=30: SAMPLE_PAYLOAD)
        assert rwt.query_region_risk_levels(118.2, 39.6, 25.0, region="唐山") == rwt.RISK_LEVELS_NO_COVERAGE

    def test_fetch_failure_returns_none(self, monkeypatch):
        def boom(start_time, timeout_sec=30):
            raise RuntimeError("down")

        monkeypatch.setattr(rwt, "_fetch_risk_lists", boom)
        assert rwt.query_region_risk_levels(117.4, 40.0, 25.0, region="蓟州") is None

    def test_reachable_no_risk_empty_dict(self, monkeypatch):
        monkeypatch.setattr(rwt, "_fetch_risk_lists", lambda start_time, timeout_sec=30: EMPTY_PAYLOAD)
        assert rwt.query_region_risk_levels(117.4, 40.0, 25.0, region="蓟州") == {}