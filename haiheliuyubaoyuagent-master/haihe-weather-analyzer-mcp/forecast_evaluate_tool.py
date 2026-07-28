# haihe-weather-analyzer-mcp/forecast_evaluate_tool.py
"""预报检验评估 MCP 工具。

封装 forecast_evaluate/scripts/ 核心函数，提供统一的 'evaluate_forecast' 工具。
通过检验 API (10.226.107.74:31002) 获取 TS/PC/BIAS/MAE/ME 等指标，
支持降水（晴雨/分级/累计）与温度检验，含进程内缓存（TTL=1h）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# 导入 forecast_evaluate 核心函数
_EVALUATE_SCRIPTS = Path(__file__).resolve().parents[2] / "forecast_evaluate 2" / "forecast_evaluate" / "scripts"
if str(_EVALUATE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_EVALUATE_SCRIPTS))

from config import Config as EvalConfig
from forecast_evaluate import request_scores, run_rain_eva, run_temp_eva
from analyzer import ForecastAnalyzer

# 进程内缓存
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 3600  # 1 小时


def _cache_key(*args: Any) -> str:
    raw = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(key: str) -> dict[str, Any] | None:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    stored_at, data = entry
    if time.time() - stored_at > _CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return None
    logger.info("[forecast_evaluate] cache hit key=%s", key[:12])
    return data


def _cache_set(key: str, data: dict[str, Any]) -> None:
    _CACHE[key] = (time.time(), data)
    logger.info("[forecast_evaluate] cache set key=%s (size=%d)", key[:12], len(_CACHE))


def _format_evaluate_result(api_result: dict, element: str, test_type: str,
                            rain_type: str | None) -> dict[str, Any]:
    """将检验API返回的原始数据转化为 LLM 可消费的结构化 JSON。"""
    analyzer = ForecastAnalyzer(api_result)
    report = analyzer.generate_detailed_report()

    metrics: dict[str, dict[str, Any]] = {}
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

    summary = report.get("summary", "")
    time_range = api_result.get("time_range", {})

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


def register_forecast_evaluate_tool(mcp: FastMCP) -> None:

    @mcp.tool()
    def evaluate_forecast(
        element: str,
        test_type: str,
        rain_type: str = "",
        begin_time: str = "",
        end_time: str = "",
        time_session: int = 24,
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
        now = datetime.now()
        month_begin = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        yesterday_end = (now - timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)

        b_time = begin_time if begin_time else month_begin.strftime("%Y-%m-%d %H:%M:%S")
        e_time = end_time if end_time else yesterday_end.strftime("%Y-%m-%d %H:%M:%S")

        # 缓存查找
        ck = _cache_key(element, test_type, rain_type, b_time, e_time, time_session)
        cached = _cache_get(ck)
        if cached is not None:
            return cached

        # 实时调用检验API
        try:
            if is_rain:
                api_result = run_rain_eva(
                    test_type=test_type,
                    rain_type=rain_type,
                    begin_time=b_time,
                    end_time=e_time,
                    time_session=time_session,
                    save_json=False,
                )
            else:
                api_result = run_temp_eva(
                    test_type=test_type,
                    begin_time=b_time,
                    end_time=e_time,
                    time_session=time_session,
                    save_json=False,
                )

            if "error" in api_result:
                return {"error": api_result["error"]}

            if not api_result.get("request_success"):
                raw = api_result.get("raw_response", {})
                return {"error": f"检验API返回失败: {raw.get('code', 'unknown')}"}

            formatted = _format_evaluate_result(api_result, element, test_type, rain_type)
            _cache_set(ck, formatted)
            return formatted

        except Exception as exc:
            logger.exception("[forecast_evaluate] 工具执行异常")
            return {"error": f"预报检验查询失败: {type(exc).__name__}: {exc}"}
