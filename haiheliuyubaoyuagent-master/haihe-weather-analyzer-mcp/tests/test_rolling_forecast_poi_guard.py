# -*- coding: utf-8 -*-
"""query_rolling_forecast 对"区域表查不到的具体点位"的守卫（密云水库静默退回天津市区的修复）。

谓词逻辑直接 import rolling_forecast_service 测；工具接线用"读源码 marker"静态检查
（与 test_rolling_forecast_basin_guard.py 同套路，避免 FastMCP 上下文）。
"""
import re
from pathlib import Path

import rolling_forecast_service as rfs

HMT = Path(__file__).resolve().parent.parent / "haihe_mcp_tools.py"


class TestIsUnresolvedPoiForecastQuery:
    def test_miyun_reservoir_is_unresolved(self):
        assert rfs.is_unresolved_poi_forecast_query("未来三天密云水库有降水吗？") is True

    def test_miyun_reservoir_weather_is_unresolved(self):
        assert rfs.is_unresolved_poi_forecast_query("密云水库天气怎么样？") is True

    def test_tianjin_university_is_unresolved(self):
        # 天津大学不是区域表里的区，含"大学"点位词 → 视为具体点位（应转 POI 地理编码）
        assert rfs.is_unresolved_poi_forecast_query("天津大学明天天气怎么样") is True

    def test_bare_no_location_defaults_tianjin(self):
        assert rfs.is_unresolved_poi_forecast_query("今天天气怎么样") is False

    def test_wo_shi_quan_shi_not_poi(self):
        assert rfs.is_unresolved_poi_forecast_query("我市未来三天天气") is False
        assert rfs.is_unresolved_poi_forecast_query("全市明天有雨吗") is False

    def test_known_region_not_unresolved(self):
        assert rfs.is_unresolved_poi_forecast_query("西青明天天气") is False
        assert rfs.is_unresolved_poi_forecast_query("天津市区未来三天") is False

    def test_region_wins_over_poi_keyword(self):
        # 含"大学"但也含已知区域"滨海新区" → 区域命中优先，不算未解析
        assert rfs.is_unresolved_poi_forecast_query("滨海新区大学城明天天气") is False

    def test_regions_param_also_considered(self):
        assert rfs.is_unresolved_poi_forecast_query("明天天气", regions="蓟州") is False
        assert rfs.is_unresolved_poi_forecast_query("明天天气", regions="密云水库") is True


class TestQueryRollingForecastPoiGuardWiring:
    def test_guard_wired_after_basin_guard(self):
        src = HMT.read_text(encoding="utf-8")
        marker = "def query_rolling_forecast("
        idx = src.index(marker)
        body = src[idx: idx + 6000]
        assert "is_unresolved_poi_forecast_query" in body
        assert "query_decision_weather_for_poi" in body
        # 点位模式（已带 lon/lat）不拦截
        assert "lon is None or lat is None" in body

    def test_helper_imported(self):
        src = HMT.read_text(encoding="utf-8")
        assert "is_unresolved_poi_forecast_query" in src


class TestPoiGuardDecisionWeatherKeywordSync:
    """POI 守卫词表 ↔ 决策天气前置过滤/规则抽槽 必须同口径。

    守卫把带点位词的问题路由给 query_decision_weather_for_poi，下游若收不住
    （_decision_weather_prefilter 拒、或规则抽槽抽不出位置名）就空手而返
    ——密云水库生产回归正是如此。静态读 chainlitexam 源码比对，防词表漂移。
    """

    DWC = Path(__file__).resolve().parents[2] / "chainlitexam" / "tools" / "decision_weather_core.py"

    @staticmethod
    def _list_strings(block_name: str) -> list:
        src = TestPoiGuardDecisionWeatherKeywordSync.DWC.read_text(encoding="utf-8")
        # 兼容 list [...] 与 tuple (...) 两种写法（重构后为模块级 tuple）
        m = re.search(rf"{re.escape(block_name)}\s*=\s*[\[(](.*?)[\])]", src, re.S)
        assert m, f"未找到 {block_name}"
        return re.findall(r'"([^"]*)"', m.group(1))

    def test_every_guard_keyword_covered_by_prefilter_suffixes(self):
        suffixes = self._list_strings("DECISION_WEATHER_PREFILTER_SUFFIXES")
        missing = [k for k in rfs.POI_PLACE_KEYWORDS if not any(s in k for s in suffixes)]
        assert not missing, f"POI 守卫关键词在决策天气前置过滤 DECISION_WEATHER_PREFILTER_SUFFIXES 中缺覆盖: {missing}"

    def test_every_guard_keyword_extractable_by_rule_slots(self):
        suffixes = self._list_strings("_DECISION_WEATHER_SUFFIXES")
        missing = [k for k in rfs.POI_PLACE_KEYWORDS if not any(s in k for s in suffixes)]
        assert not missing, f"POI 守卫关键词在规则抽槽 _DECISION_WEATHER_SUFFIXES 中缺覆盖: {missing}"
