"""B1: forecast_evaluate_tool 缓存顺序缺陷测试。

缺陷：evaluate_forecast 先调 _validate_params_and_fetch（内部已调检验 API）再查缓存，
1h 缓存每次命中仍付全量 API 调用，缓存形同虚设。修复后拆成
「廉价校验/解析 → 缓存命中判断 → 昂贵取数（仅 miss 时）」。
测试锁定：同参第二次调用不再调检验 API（缓存命中跳过取数）。
"""

from __future__ import annotations

import sys
import importlib.util
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))


_INSTALLED_TEST_STUBS: dict[str, types.ModuleType] = {}


def _install_test_stub(name: str, module: types.ModuleType) -> None:
    """只在缺失时安装依赖 stub，并在被测模块导入后恢复全局模块表。"""
    if name not in sys.modules:
        sys.modules[name] = module
        _INSTALLED_TEST_STUBS[name] = module

# 该测试只验证 forecast_evaluate_tool 的编排与缓存，不加载完整 MCP 服务。
# 裸测试环境没有 fastmcp；同时避免 haihe_mcp_tools 导入数据库/GIS 等无关依赖。
fastmcp_stub = types.ModuleType("fastmcp")
fastmcp_stub.FastMCP = object
_install_test_stub("fastmcp", fastmcp_stub)

haihe_tools_stub = types.ModuleType("haihe_mcp_tools")
haihe_tools_stub.TIANJIN_TIMEZONE = timezone(timedelta(hours=8))
_install_test_stub("haihe_mcp_tools", haihe_tools_stub)

# analyzer 只使用 rich.print；系统测试环境可能未安装 rich。
rich_stub = types.ModuleType("rich")
rich_stub.print = print
_install_test_stub("rich", rich_stub)


class _EvalConfigStub:
    ALL_ELEMENTS = {
        "rain24": "24小时降水",
        "tmax24": "最高温",
        "tmin24": "最低温",
        "t2m": "2米温度",
    }
    RAIN_ELEMENTS = {"rain24": "24小时降水"}
    TEMP_ELEMENTS = {"tmax24": "最高温", "tmin24": "最低温", "t2m": "2米温度"}
    TEST_TYPE_NAMES = {"daily": "逐日", "time_session": "逐时效", "area": "分地区"}
    PRODUCT_NAMES = {}
    RAIN_SUBTYPE_NAMES = {}
    EXAM_DESCRIPTIONS = {}
    TJ_AREA_NAMES = {}


config_stub = types.ModuleType("config")
config_stub.Config = _EvalConfigStub
config_stub.PathConfig = type("PathConfig", (), {})
_install_test_stub("config", config_stub)

forecast_stub = types.ModuleType("forecast_evaluate")
forecast_stub.request_scores = lambda **kwargs: {}
forecast_stub.run_rain_eva = lambda **kwargs: {}
forecast_stub.run_temp_eva = lambda **kwargs: {}
forecast_stub.generate_charts = lambda *args, **kwargs: {}
_install_test_stub("forecast_evaluate", forecast_stub)

analyzer_stub = types.ModuleType("analyzer")
analyzer_stub.ForecastAnalyzer = object
_install_test_stub("analyzer", analyzer_stub)

import forecast_evaluate_tool as fet

# forecast_evaluate_tool 已把所需对象绑定到自身命名空间；立即移除本测试安装的
# sys.modules stub，避免 pytest 同进程收集后续生产模块时误用不完整假模块。
for _stub_name, _stub_module in _INSTALLED_TEST_STUBS.items():
    if sys.modules.get(_stub_name) is _stub_module:
        sys.modules.pop(_stub_name, None)


def _load_real_analyzer_module():
    """在不导入 Matplotlib 的前提下加载真实 analyzer.py。"""
    analyzer_path = fet._EVALUATE_SCRIPTS / "analyzer.py"
    spec = importlib.util.spec_from_file_location("forecast_analyzer_under_test", analyzer_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _fake_rain_eva(calls: dict):
    def fake(**kwargs):
        calls["n"] += 1
        return {"request_success": True, "data": [], "raw_response": {}}
    return fake


class TestForecastEvaluateCacheOrder:
    def _setup(self, monkeypatch, calls: dict, ttl: int):
        monkeypatch.setattr(fet, "run_rain_eva", _fake_rain_eva(calls))
        monkeypatch.setattr(fet, "run_temp_eva", _fake_rain_eva(calls))
        monkeypatch.setattr(fet, "_format_evaluate_result",
                            lambda api_result, element, test_type, rain_type: {"element": element, "ok": True})
        monkeypatch.setattr(fet, "_CACHE_TTL_SECONDS", ttl)
        fet._CACHE.clear()
        if hasattr(fet, "_RAW_CACHE"):
            fet._RAW_CACHE.clear()
        if hasattr(fet, "_RAW_INFLIGHT"):
            fet._RAW_INFLIGHT.clear()

    def test_cache_hit_skips_api(self, monkeypatch):
        """同参第二次调用命中缓存，不再调检验 API。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls, 3600)

        r1 = fet._evaluate_forecast_core(
            "rain24", "daily", "ng", "2026-08-01 00:00:00", "2026-08-13 00:00:00", 24, ""
        )
        assert r1["ok"] is True
        after_first = calls["n"]
        r2 = fet._evaluate_forecast_core(
            "rain24", "daily", "ng", "2026-08-01 00:00:00", "2026-08-13 00:00:00", 24, ""
        )
        assert r1 == r2
        assert calls["n"] == after_first, f"第二次应命中缓存，实际多调了 {calls['n'] - after_first} 次检验 API"

    def test_distinct_params_do_not_share(self, monkeypatch):
        """不同参数（要素/时间）不互相命中。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls, 3600)

        fet._evaluate_forecast_core(
            "rain24", "daily", "ng", "2026-08-01 00:00:00", "2026-08-13 00:00:00", 24, ""
        )
        after_first = calls["n"]
        fet._evaluate_forecast_core(
            "tmax24", "daily", "", "2026-08-01 00:00:00", "2026-08-13 00:00:00", 24, ""
        )
        assert calls["n"] > after_first, "不同参数应重新调检验 API"

    def test_cache_expires_after_ttl(self, monkeypatch):
        """TTL=0 强制过期后重新调检验 API。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls, 0)

        fet._evaluate_forecast_core(
            "rain24", "daily", "ng", "2026-08-01 00:00:00", "2026-08-13 00:00:00", 24, ""
        )
        after_first = calls["n"]
        fet._evaluate_forecast_core(
            "rain24", "daily", "ng", "2026-08-01 00:00:00", "2026-08-13 00:00:00", 24, ""
        )
        assert calls["n"] > after_first, "TTL=0 应重新调检验 API"

    def test_invalid_params_never_cached(self, monkeypatch):
        """参数校验失败（error）不写缓存，也不调检验 API。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls, 3600)

        r1 = fet._evaluate_forecast_core("bad_element", "daily", "", "", "", 24, "")
        assert "error" in r1
        assert calls["n"] == 0, "参数非法不应调检验 API"
        fet._evaluate_forecast_core("bad_element", "daily", "", "", "", 24, "")
        assert calls["n"] == 0, "参数非法结果不应写缓存"

    def test_text_result_explicitly_skips_chart_generation(self, monkeypatch):
        """文字版评分不应生成 PNG；图表只由 generate_forecast_charts 按需生成。"""
        calls: list[bool] = []

        class FakeAnalyzer:
            def __init__(self, api_result):
                self.api_result = api_result

            def generate_detailed_report(self, *, include_charts: bool):
                calls.append(include_charts)
                return {"details": {}, "summary": "ok", "poor_samples": []}

            def format_report_to_markdown(self, report):
                return "# report"

        monkeypatch.setattr(fet, "ForecastAnalyzer", FakeAnalyzer)
        result = fet._format_evaluate_result(
            {"time_range": {"begin": "2026-08-01", "end": "2026-08-02"}},
            "rain24", "daily", "ng",
        )

        assert calls == [False]
        assert result["chart_paths"] == {}

    def test_analyzer_can_build_report_without_charts(self, monkeypatch):
        """真实分析器的无图模式不能触发 generate_charts。"""
        analyzer_module = _load_real_analyzer_module()

        def fail_if_called(*args, **kwargs):
            raise AssertionError("文字报告不应生成图表")

        monkeypatch.setattr(analyzer_module, "generate_charts", fail_if_called)
        analyzer = analyzer_module.ForecastAnalyzer({
            "raw_response": {"data": {"examData": [], "examColumnName": []}},
            "element_code": "rain24",
            "test_type_code": "daily",
            "time_range": {"begin": "2026-08-01 00:00:00", "end": "2026-08-02 00:00:00"},
        })

        report = analyzer.generate_detailed_report(include_charts=False)
        assert report["details"] == {}

    def test_chart_fetch_reuses_raw_api_result_from_text_query(self, monkeypatch):
        """先查文字、再要图表时，应复用同一份原始 API 数据。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls, 3600)

        fet._evaluate_forecast_core(
            "rain24", "daily", "ng", "2026-08-01 00:00:00", "2026-08-13 00:00:00", 24, ""
        )
        fetched = fet._validate_params_and_fetch(
            "rain24", "daily", "ng", "2026-08-01 00:00:00", "2026-08-13 00:00:00", 24, ""
        )

        assert "api_result" in fetched
        assert calls["n"] == 1, "文字查询后的图表请求不应再次调用检验 API"

    def test_concurrent_identical_raw_requests_are_coalesced(self, monkeypatch):
        """同进程并发的相同请求只允许一个线程访问上游 API。"""
        calls = {"n": 0}
        calls_lock = threading.Lock()

        def slow_api(**kwargs):
            with calls_lock:
                calls["n"] += 1
            time.sleep(0.05)
            return {"request_success": True, "data": [], "raw_response": {}}

        self._setup(monkeypatch, calls, 3600)
        monkeypatch.setattr(fet, "run_rain_eva", slow_api)

        def fetch_once(_):
            return fet._validate_params_and_fetch(
                "rain24", "daily", "ng", "2026-08-01 00:00:00", "2026-08-13 00:00:00", 24, ""
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(fetch_once, range(4)))

        assert all("api_result" in item for item in results)
        assert calls["n"] == 1

    def test_result_and_raw_caches_have_capacity_limits(self, monkeypatch):
        """不同参数持续进入时，格式化结果和原始数据缓存都不能无限增长。"""
        calls = {"n": 0}
        self._setup(monkeypatch, calls, 3600)
        monkeypatch.setattr(fet, "_CACHE_MAX_SIZE", 2, raising=False)

        for hour in (24, 48, 72):
            fet._evaluate_forecast_core(
                "rain24", "daily", "ng", "2026-08-01 00:00:00", "2026-08-13 00:00:00", hour, ""
            )

        assert len(fet._CACHE) <= 2
        assert len(fet._RAW_CACHE) <= 2
