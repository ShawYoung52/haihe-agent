"""今年以来累计面雨量 MCP 工具。

业务口径：今年 1 月 1 日 00:00 到当前时刻，海河9分区累计面雨量。
实现方式：复用“上月面雨量”已验证的快链路：天擎面雨量细分区产品 -> 空间映射汇总到海河9分区。
"""
from __future__ import annotations

import os
from datetime import datetime
import time_source
from typing import Any

from fastmcp import FastMCP

from custom_tools._ttl_cache import make_ttl_cache
from .last_month_areal_rainfall_tool import (
    _aggregate_fine_rows_to_zone9,
    _aggregate_raw_areal_rows,
    _summarize_rows,
)


def _year_to_date_range(now: datetime | None = None) -> tuple[str, str, str]:
    now = now or time_source.now()
    start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return (
        start.strftime("%Y%m%d%H%M%S"),
        now.strftime("%Y%m%d%H%M%S"),
        f"{start:%Y-%m-%d %H:%M:%S} ~ {now:%Y-%m-%d %H:%M:%S}",
    )


def _no_data_payload(time_range: str, readable: str, reason: str = "") -> dict:
    return {
        "status": "no_data",
        "query_type": "year_to_date_areal_rainfall",
        "time_range": time_range,
        "time_range_readable": readable,
        "zone_type": "9",
        "zone_label": "海河9分区",
        "records": [],
        "summary": _summarize_rows([]),
        "message": "今年以来暂无有效海河9分区累计降雨量数据。",
        "debug_reason": reason[:200] if reason else "",
    }


def _query_raw_year_to_date(time_range: str) -> tuple[list[dict] | None, str | None]:
    """查询今年以来天擎面雨量数据。

    优先使用统计接口，避免拉取过大的逐时原始数据；统计接口不可用时，再退到原始面雨量接口。
    注意：这里是后端工具内部兜底，不新增前端路由复杂度。
    """
    try:
        from utils.TQ_utils import getSevpEleByTimeRangeHistory, statSevpEleByTimeRangeHistory
    except Exception:
        return None, None

    try:
        raw = statSevpEleByTimeRangeHistory(time_range=time_range, elements="V_AREA_ID,Datetime")
        if raw:
            return raw, "SUM_V_RAIN_1H"
    except Exception:
        pass

    try:
        raw = getSevpEleByTimeRangeHistory(time_range=time_range, elements="Datetime,V_AREA_ID,V_RAIN_1H")
        if raw:
            return raw, "V_RAIN_1H"
    except Exception:
        pass

    return None, None


def _to_zone9_rows(raw: list[dict], rain_field: str) -> list[dict]:
    fine_rows = _aggregate_raw_areal_rows(raw, rain_field)
    if not fine_rows:
        return []
    if len(fine_rows) <= 12:
        # 数据源已经返回9分区或近似9分区口径。
        return fine_rows
    # 数据源返回细分区时，统一映射汇总到海河9分区。
    return _aggregate_fine_rows_to_zone9(fine_rows)


def register_year_to_date_areal_rainfall_tool(mcp: FastMCP) -> None:
    # 窗口截至当前时刻，数据小时级更新：短 TTL 120s（键含年初起点，TTL 管新鲜度）。
    _decorator, _ytd_cache, _ytd_lock = make_ttl_cache(
        int(os.getenv("YEAR_TO_DATE_AREAL_CACHE_TTL", "120")),
        lambda zone_type="9": f"{zone_type}|{_year_to_date_range()[0]}",
    )

    @mcp.tool()
    @_decorator
    def query_year_to_date_areal_rainfall(zone_type: str = "9") -> dict:
        """查询今年以来海河9分区累计面雨量。"""
        zone_type = str(zone_type or "9").strip()
        start_s, end_s, readable = _year_to_date_range()
        time_range = f"[{start_s},{end_s}]"

        if zone_type != "9":
            return _no_data_payload(time_range, readable, reason=f"unsupported_zone_type:{zone_type}")

        raw, rain_field = _query_raw_year_to_date(time_range)
        if not raw or not rain_field:
            return _no_data_payload(time_range, readable, reason="raw_empty")

        rows = _to_zone9_rows(raw, rain_field)
        if not rows:
            return _no_data_payload(time_range, readable, reason="zone9_empty")

        if len(rows) > 12:
            # 理论上 _to_zone9_rows 已经汇总，这里只是防御，避免再次展示细分区。
            return _no_data_payload(time_range, readable, reason=f"unexpected_zone_count:{len(rows)}")

        return {
            "status": "ok",
            "query_type": "year_to_date_areal_rainfall",
            "time_range": time_range,
            "time_range_readable": readable,
            "zone_type": "9",
            "zone_label": "海河9分区",
            "records": rows,
            "summary": _summarize_rows(rows),
        }
