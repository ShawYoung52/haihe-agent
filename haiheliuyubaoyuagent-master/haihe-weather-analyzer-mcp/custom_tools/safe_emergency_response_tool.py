"""安全版实况应急响应判定 MCP 工具。

旧的 evaluate_haihe_emergency_response 在内部异常时会让 MCP 适配器抛出
UnboundLocalError，前端只能看到“查询遇到异常”。本工具复用原核心判定函数，
但把异常转换为结构化 error 返回，避免适配器二次报错。
"""
from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

from constants import DEFAULT_BASIN_CODES, DEFAULT_THRESHOLDS_MM
from haihe_mcp_tools import evaluate_emergency_response_core

logger = logging.getLogger(__name__)


def _error_payload(times: str, basin_codes: str, exc: Exception) -> dict[str, Any]:
    text = str(exc)
    lower = text.lower()
    if "no record" in lower or "无记录" in text or "暂无数据" in text:
        message = "未查询到该时次的应急响应判定数据，可能该时段无有效分钟降水资料。"
    else:
        message = "当前无法获取应急响应判定数据，请稍后重试。"
    return {
        "status": "error",
        "error": text[:500],
        "message": message,
        "query": {
            "times": times,
            "basin_codes": basin_codes,
        },
    }


def register_safe_emergency_response_tool(mcp: FastMCP) -> None:
    @mcp.tool()
    def safe_evaluate_haihe_emergency_response(
        basin_codes: str = DEFAULT_BASIN_CODES,
        times: str = "",
        neighbor_km: float = 50.0,
        sustain_hourly_threshold_mm: float = 0.1,
        allowed_station_levels: str = "11,12,13,16",
        rainstorm_12h: float = DEFAULT_THRESHOLDS_MM["rainstorm_12h"],
        rainstorm_24h: float = DEFAULT_THRESHOLDS_MM["rainstorm_24h"],
        severe_rainstorm_24h: float = DEFAULT_THRESHOLDS_MM["severe_rainstorm_24h"],
        extraordinary_24h: float = DEFAULT_THRESHOLDS_MM["extraordinary_24h"],
        include_records: bool = False,
    ) -> dict[str, Any]:
        """安全查询海河流域实况应急响应判定结果。"""
        try:
            result = evaluate_emergency_response_core(
                basin_codes=basin_codes,
                times=times,
                neighbor_km=neighbor_km,
                sustain_hourly_threshold_mm=sustain_hourly_threshold_mm,
                allowed_station_levels=allowed_station_levels,
                rainstorm_12h=rainstorm_12h,
                rainstorm_24h=rainstorm_24h,
                severe_rainstorm_24h=severe_rainstorm_24h,
                extraordinary_24h=extraordinary_24h,
                include_records=include_records,
            )
            result.setdefault("status", "ok")
            return result
        except Exception as exc:
            logger.exception("[safe_emergency_response] failed times=%s basin_codes=%s", times, basin_codes)
            return _error_payload(times=times, basin_codes=basin_codes, exc=exc)
