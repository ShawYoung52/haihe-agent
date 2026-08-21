# haihe-weather-analyzer-mcp/forecast_evaluate_tool.py
"""预报检验评估 MCP 工具。

封装 forecast_evaluate/scripts/ 核心函数，提供统一的 'evaluate_forecast' 和
'generate_forecast_charts' 工具。
通过检验 API (10.226.107.74:31002) 获取 TS/PC/BIAS/MAE/ME 等指标，
支持降水（晴雨/分级/累计）与温度检验，含进程内缓存（TTL=1h）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta
import time_source
from pathlib import Path
from typing import Any

from haihe_mcp_tools import TIANJIN_TIMEZONE

from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# 导入 forecast_evaluate 核心函数
_EVALUATE_SCRIPTS = Path(__file__).resolve().parents[2] / "forecast_evaluate 2" / "forecast_evaluate" / "scripts"
if str(_EVALUATE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_EVALUATE_SCRIPTS))

from config import Config as EvalConfig
from forecast_evaluate import request_scores, run_rain_eva, run_temp_eva, generate_charts
from analyzer import ForecastAnalyzer

# 进程内缓存：格式化结果与原始 API 数据分层保存。
# 原始数据缓存供文字/图表工具共用，避免“先查结果、再生成图”重复访问上游。
_CACHE_TTL_SECONDS = 3600  # 1 小时
try:
    _CACHE_MAX_SIZE = max(1, int(os.getenv("FORECAST_EVALUATE_CACHE_MAX_SIZE", "128")))
except (TypeError, ValueError):
    _CACHE_MAX_SIZE = 128

_CACHE: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
_RAW_CACHE: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
_CACHE_LOCK = threading.RLock()
_RAW_INFLIGHT: dict[str, threading.Event] = {}


def _cache_key(*args: Any) -> str:
    raw = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get_from(
    cache: OrderedDict[str, tuple[float, dict[str, Any]]], key: str,
) -> dict[str, Any] | None:
    entry = cache.get(key)
    if entry is None:
        return None
    stored_at, data = entry
    if _CACHE_TTL_SECONDS <= 0 or time.time() - stored_at >= _CACHE_TTL_SECONDS:
        cache.pop(key, None)
        return None
    cache.move_to_end(key)
    return data


def _cache_get(key: str) -> dict[str, Any] | None:
    with _CACHE_LOCK:
        data = _cache_get_from(_CACHE, key)
    if data is None:
        return None
    logger.info("[forecast_evaluate] cache hit key=%s", key[:12])
    return data


def _cache_set_to(
    cache: OrderedDict[str, tuple[float, dict[str, Any]]], key: str, data: dict[str, Any],
) -> None:
    now = time.time()
    with _CACHE_LOCK:
        expired = [
            existing_key
            for existing_key, (stored_at, _) in cache.items()
            if _CACHE_TTL_SECONDS <= 0 or now - stored_at >= _CACHE_TTL_SECONDS
        ]
        for existing_key in expired:
            cache.pop(existing_key, None)
        if _CACHE_TTL_SECONDS <= 0:
            return
        cache[key] = (now, data)
        cache.move_to_end(key)
        while len(cache) > _CACHE_MAX_SIZE:
            cache.popitem(last=False)


def _cache_set(key: str, data: dict[str, Any]) -> None:
    _cache_set_to(_CACHE, key, data)
    logger.info("[forecast_evaluate] cache set key=%s (size=%d)", key[:12], len(_CACHE))


def _request_cache_key(
    element: str,
    test_type: str,
    parsed: dict[str, Any],
    time_session: int,
    area_codes: str,
) -> str:
    return _cache_key(
        element, test_type, parsed["rain_type"],
        parsed["b_time"], parsed["e_time"], time_session, area_codes,
    )


def _format_evaluate_result(api_result: dict, element: str, test_type: str,
                            rain_type: str | None) -> dict[str, Any]:
    """将检验API返回的原始数据转化为 LLM 可消费的结构化 JSON。"""
    analyzer = ForecastAnalyzer(api_result)
    report = analyzer.generate_detailed_report(include_charts=False)

    metrics: dict[str, dict[str, Any]] = {}
    chart_paths_flat: dict[str, dict[str, str]] = {}
    for category, sub_details in report.get("details", {}).items():
        if isinstance(sub_details, dict):
            for metric_name, data in sub_details.items():
                ranking: list[tuple[str, float]] = data.get("ranking", [])
                best = ranking[0] if ranking else ("", 0.0)
                metrics[metric_name] = {
                    "ranking": [[name, round(val, 2)] for name, val in ranking],
                    "best": best[0],
                    "best_value": round(best[1], 2),
                    "unit": _metric_unit(metric_name),
                }

                # --- 规范化 image_path：Task 3 返回 [(chart_type, path), ...]，
                #     但 format_report_to_markdown 期望单 string ---
                image_path = data.get("image_path")
                if isinstance(image_path, list) and len(image_path) > 0:
                    # 优选柱状图路径
                    selected = image_path[0][1]
                    data["image_path"] = selected
                    # 同时累积 chart_paths_flat
                    exam_name = data.get("examName", metric_name)
                    chart_paths_flat[exam_name] = {}
                    for ct, p in image_path:
                        chart_paths_flat[exam_name][ct] = str(p)
                elif isinstance(image_path, str):
                    exam_name = data.get("examName", metric_name)
                    chart_paths_flat[exam_name] = {"bar": image_path}

    summary = report.get("summary", "")
    time_range = api_result.get("time_range", {})

    # --- 完整 Markdown 报告 ---
    report_markdown = analyzer.format_report_to_markdown(report)

    # --- 较差样本 ---
    poor_samples = report.get("poor_samples", [])

    return {
        "element": EvalConfig.ALL_ELEMENTS.get(element, element),
        "element_code": element,
        "test_type": EvalConfig.TEST_TYPE_NAMES.get(test_type, test_type),
        "test_type_code": test_type,
        "time_range": time_range,
        "rain_type": rain_type,
        "data_source": "检验API",
        "metrics": metrics,
        "summary": summary,
        "report_markdown": report_markdown,
        "poor_samples": poor_samples,
        "chart_paths": chart_paths_flat,
    }


def _metric_unit(metric_name: str) -> str:
    if "准确率" in metric_name or "PC" in metric_name:
        return "%"
    if "MAE" in metric_name or "ME" in metric_name:
        return "°C"
    if "TS" in metric_name:
        return ""
    if "偏差" in metric_name or "BIAS" in metric_name:
        return ""
    return ""


def _parse_evaluate_params(
    element: str, test_type: str, rain_type: str,
    begin_time: str, end_time: str,
) -> dict[str, Any]:
    """廉价步骤：参数校验 + 默认时间解析（不调检验 API）。

    Returns dict with 'is_rain', 'rain_type', 'b_time', 'e_time', or 'error'.
    """
    # 参数校验
    valid_elements = set(EvalConfig.ALL_ELEMENTS.keys())
    if element not in valid_elements:
        return {"error": f"无效要素 {element}，可选: {sorted(valid_elements)}"}

    valid_test_types = set(EvalConfig.TEST_TYPE_NAMES.keys())
    if test_type not in valid_test_types:
        return {"error": f"无效检验维度 {test_type}，可选: {sorted(valid_test_types)}"}

    is_rain = element in EvalConfig.RAIN_ELEMENTS
    if is_rain and rain_type not in ("ng", "g", "acc", ""):
        return {"error": f"降水需要指定 rain_type: ng/g/acc，当前: {rain_type!r}"}
    if not is_rain:
        rain_type = None  # type: ignore[assignment]

    # 默认时间：本月 1 日 ~ 昨天
    now = time_source.now(TIANJIN_TIMEZONE)
    month_begin = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    yesterday_end = (now - timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)

    b_time = begin_time if begin_time else month_begin.strftime("%Y-%m-%d %H:%M:%S")
    e_time = end_time if end_time else yesterday_end.strftime("%Y-%m-%d %H:%M:%S")

    return {
        "is_rain": is_rain,
        "rain_type": rain_type,
        "b_time": b_time,
        "e_time": e_time,
    }


def _fetch_evaluate_api(
    parsed: dict[str, Any], test_type: str, time_session: int, area_codes: str,
) -> dict[str, Any]:
    """昂贵步骤：调用检验 API。Returns dict with 'api_result' or 'error'."""
    try:
        area = area_codes if area_codes else None
        if parsed["is_rain"]:
            api_result = run_rain_eva(
                test_type=test_type, rain_type=parsed["rain_type"],
                begin_time=parsed["b_time"], end_time=parsed["e_time"],
                time_session=time_session, save_json=False, area_codes=area,
            )
        else:
            api_result = run_temp_eva(
                test_type=test_type, begin_time=parsed["b_time"], end_time=parsed["e_time"],
                time_session=time_session, save_json=False, area_codes=area,
            )

        if "error" in api_result:
            return {"error": api_result["error"]}

        if not api_result.get("request_success"):
            raw = api_result.get("raw_response", {})
            return {"error": f"检验API返回失败: {raw.get('code', 'unknown')}"}

        return {"api_result": api_result}
    except Exception as exc:
        logger.exception("[forecast_evaluate] 检验API调用异常")
        return {"error": "预报检验查询失败，请稍后重试。"}


def _fetch_evaluate_api_cached(
    cache_key: str,
    parsed: dict[str, Any],
    test_type: str,
    time_session: int,
    area_codes: str,
) -> dict[str, Any]:
    """读取共享原始数据缓存；相同并发请求仅允许一个线程访问上游。"""
    while True:
        with _CACHE_LOCK:
            cached = _cache_get_from(_RAW_CACHE, cache_key)
            if cached is not None:
                logger.info("[forecast_evaluate] raw cache hit key=%s", cache_key[:12])
                return {"api_result": cached}

            inflight = _RAW_INFLIGHT.get(cache_key)
            if inflight is None:
                inflight = threading.Event()
                _RAW_INFLIGHT[cache_key] = inflight
                is_owner = True
            else:
                is_owner = False

        if not is_owner:
            inflight.wait()
            continue

        try:
            fetched = _fetch_evaluate_api(parsed, test_type, time_session, area_codes)
            if "api_result" in fetched:
                _cache_set_to(_RAW_CACHE, cache_key, fetched["api_result"])
            return fetched
        finally:
            with _CACHE_LOCK:
                completed = _RAW_INFLIGHT.pop(cache_key, None)
                if completed is not None:
                    completed.set()


def _validate_params_and_fetch(
    element: str, test_type: str, rain_type: str,
    begin_time: str, end_time: str, time_session: int, area_codes: str,
) -> dict[str, Any]:
    """校验 + 默认时间 + 共享原始数据缓存（供图表工具复用）。"""
    parsed = _parse_evaluate_params(element, test_type, rain_type, begin_time, end_time)
    if "error" in parsed:
        return parsed
    ck = _request_cache_key(element, test_type, parsed, time_session, area_codes)
    fetched = _fetch_evaluate_api_cached(ck, parsed, test_type, time_session, area_codes)
    if "error" in fetched:
        return fetched
    return {**parsed, **fetched}


def _evaluate_forecast_core(
    element: str,
    test_type: str,
    rain_type: str = "",
    begin_time: str = "",
    end_time: str = "",
    time_session: int = 24,
    area_codes: str = "",
) -> dict[str, Any]:
    """预报检验评分查询（带 1h 缓存，命中时不调检验 API）。

    顺序：廉价校验/解析 → 缓存命中判断 → 昂贵取数（仅 miss 时）。
    """
    parsed = _parse_evaluate_params(element, test_type, rain_type, begin_time, end_time)
    if "error" in parsed:
        return parsed

    ck = _request_cache_key(element, test_type, parsed, time_session, area_codes)
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    fetched = _fetch_evaluate_api_cached(ck, parsed, test_type, time_session, area_codes)
    if "error" in fetched:
        return fetched

    formatted = _format_evaluate_result(
        fetched["api_result"], element, test_type, parsed["rain_type"],
    )
    _cache_set(ck, formatted)
    return formatted


def register_forecast_evaluate_tool(mcp: FastMCP) -> None:

    @mcp.tool()
    def evaluate_forecast(
        element: str,
        test_type: str,
        rain_type: str = "",
        begin_time: str = "",
        end_time: str = "",
        time_session: int = 24,
        area_codes: str = "",
    ) -> dict[str, Any]:
        """查询预报检验评分数据。

        支持 TS评分、准确率(PC)、偏差(BIAS)、平均绝对误差(MAE)、平均误差(ME)
        等指标的查询。对比产品为国家指导、天津预报、ECMWF。

        :param element: 检验要素，rain24=24h降水，tmax24=最高温，tmin24=最低温，t2m=2m温度
        :param test_type: 检验维度，daily=逐日，time_session=逐时效，area=分地区
        :param rain_type: 降水子类（仅降水需要），ng=晴雨，g=分级暴雨，acc=累计
        :param begin_time: 开始时间 YYYY-MM-DD HH:MM:SS，默认本月1日
        :param end_time: 结束时间 YYYY-MM-DD HH:MM:SS，默认昨天
        :param time_session: 预报时效(小时)，24/48/72，默认24
        """
        return _evaluate_forecast_core(
            element, test_type, rain_type,
            begin_time, end_time, time_session, area_codes,
        )

    @mcp.tool()
    def generate_forecast_charts(
        element: str,
        test_type: str,
        rain_type: str = "",
        chart_types: str = "bar,line",
        begin_time: str = "",
        end_time: str = "",
        time_session: int = 24,
        area_codes: str = "",
    ) -> dict[str, Any]:
        """为预报检验数据生成可视化图表。

        支持柱状图(bar)、趋势折线图(line)、热力图(heatmap)。
        返回所有生成图表文件的绝对路径，供前端渲染。

        :param element: 检验要素，rain24/tmax24/tmin24/t2m
        :param test_type: 检验维度，daily/time_session/area
        :param rain_type: 降水子类，ng/g/acc
        :param chart_types: 图表类型，逗号分隔，如 "bar,line,heatmap"
        :param begin_time: 开始时间 YYYY-MM-DD HH:MM:SS
        :param end_time: 结束时间 YYYY-MM-DD HH:MM:SS
        :param time_session: 预报时效(小时)
        """
        # 解析 chart_types
        types = [t.strip() for t in chart_types.split(",") if t.strip()]
        if not types:
            types = ["bar"]

        fetched = _validate_params_and_fetch(
            element, test_type, rain_type,
            begin_time, end_time, time_session, area_codes,
        )
        if "error" in fetched:
            return fetched

        chart_paths = generate_charts(fetched["api_result"], chart_types=types)

        # 拍平: {exam_name: [(type, path), ...]} -> [{exam_name, type, path}]
        flattened: list[dict[str, str]] = []
        for exam_name, paths in chart_paths.items():
            for ct, p in paths:
                flattened.append({
                    "exam_name": exam_name,
                    "chart_type": ct,
                    "path": str(p),
                })

        return {
            "element": EvalConfig.ALL_ELEMENTS.get(element, element),
            "test_type": EvalConfig.TEST_TYPE_NAMES.get(test_type, test_type),
            "time_range": {"begin": fetched["b_time"], "end": fetched["e_time"]},
            "charts": flattened,
        }
