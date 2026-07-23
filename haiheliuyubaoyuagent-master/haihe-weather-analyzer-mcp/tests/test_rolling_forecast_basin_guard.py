"""query_rolling_forecast 流域误路由硬防护测试。"""
from __future__ import annotations

import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import rolling_forecast_service as rfs  # noqa: E402


class TestIsBasinWeatherQuery:
    def test_basin_and_river_system_queries_match(self):
        positives = [
            "今天海河流域天气怎么样",
            "海河流域明天会不会下雨",
            "流域未来三天天气",
            "大清河流域未来三天天气",
            "子牙河明天有雨吗",
            "永定河未来24小时降雨",
            "北三河天气如何",
            "漳卫南运河未来三天降雨",
            "漳卫河天气",
            "徒骇马颊河有雨吗",
            "黑龙港运东天气",
            "滦河未来天气",
            "潮白河天气",
            "蓟运河天气",
            "各河系降雨对比",
        ]
        for text in positives:
            assert rfs.is_basin_weather_query(text), text

    def test_tianjin_queries_do_not_match(self):
        negatives = [
            "今天天津天气怎么样",
            "明天我市天气如何",
            "西青区后天天气",
            "滨海新区未来一周天气",
            "最近会有大暴雨吗",
            "周末适合户外活动吗",
            "海河夜景附近明天天气怎么样",  # 点位/河岸，不算流域
            "",
        ]
        for text in negatives:
            assert not rfs.is_basin_weather_query(text), text

    def test_poi_context_river_names_do_not_match(self):
        """裸河名在 POI 语境（公园/湿地/附近/沿线等）不算流域问题。"""
        negatives = [
            "周末去潮白河国家湿地公园露营，天气怎么样",
            "永定河森林公园明天适合出游吗",
            "子牙河附近的西青郊野公园周末天气",
            "潮白河沿线明天天气怎么样",
            "滦河站附近天气",
        ]
        for text in negatives:
            assert not rfs.is_basin_weather_query(text), text


class TestDocstringExclusions:
    def test_query_rolling_forecast_docstring_excludes_basin(self):
        source = (MCP_DIR / "haihe_mcp_tools.py").read_text(encoding="utf-8")
        marker = "def query_rolling_forecast("
        idx = source.find(marker)
        assert idx >= 0
        doc = source[idx:idx + 4000]
        assert "海河流域" in doc
        assert "get_river_system_rainfall_forecast" in doc
        assert "禁止" in doc

    def test_river_system_tool_docstring_covers_today(self):
        source = (MCP_DIR / "tools.py").read_text(encoding="utf-8")
        marker = "def get_river_system_rainfall_forecast("
        idx = source.find(marker)
        assert idx >= 0
        doc = source[idx:idx + 3000]
        assert "今天海河流域天气怎么样" in doc
        assert "无论今天、明天还是未来" in doc

    def test_wrapper_skips_guard_in_point_mode(self):
        """点位模式（lon/lat 由调用方解析好）必须跳过流域守卫，避免决策天气 POI 误伤。"""
        source = (MCP_DIR / "haihe_mcp_tools.py").read_text(encoding="utf-8")
        marker = "def query_rolling_forecast("
        idx = source.find(marker)
        assert idx >= 0
        end = source.find("return query_rolling_forecast_core(", idx)
        body = source[idx:end]
        assert "lon is None or lat is None" in body
