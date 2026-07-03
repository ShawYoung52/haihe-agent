"""面雨量快速路径可用性兜底。

目的：
1. 新增9分区专用MCP工具异常/超时时，不把 FastMCP 的 call_tool_result 包装层错误暴露给用户；
2. 自动回退老 query_basin_areal_rainfall；
3. 回退结果只有 <=12 条时才允许展示，避免再次展示 531/140/303 等细分区。
"""
from __future__ import annotations

import asyncio
from typing import Any

from . import rainfall_fast_paths as rfp


_INSTALLED = False


def _no_data(message: str, time_range: str = "") -> dict:
    return {
        "status": "no_data",
        "zone_label": "海河9分区",
        "time_range": time_range,
        "records": [],
        "summary": {},
        "message": message,
    }


def _record_count(data: Any) -> int:
    if isinstance(data, dict):
        records = data.get("records") or data.get("data") or []
    elif isinstance(data, list):
        records = data
    else:
        records = []
    return len([r for r in records if isinstance(r, dict) and "error" not in r])


def _force_zone9_label(data: Any) -> Any:
    if isinstance(data, dict):
        data["zone_label"] = "海河9分区"
        data["zone_type"] = "9"
    return data


async def _call_legacy_basin_areal_if_zone9(mo, tools, time_range: str, timeout: int = 60) -> Any:
    tool = mo._find_tool(tools, "query_basin_areal_rainfall")
    if not tool:
        return None
    try:
        data = rfp._unwrap_tool_result(
            await asyncio.wait_for(
                tool.ainvoke({"zone_type": "9", "time_range": time_range, "hours": 24}),
                timeout=timeout,
            )
        )
    except Exception as exc:
        print(f"[areal_fallback_patch] 老面雨量工具回退失败：{exc}")
        return None
    count = _record_count(data)
    if 0 < count <= 12:
        return _force_zone9_label(data)
    if count > 12:
        print(f"[areal_fallback_patch] 老面雨量工具返回 {count} 条，判定为细分区，不展示")
    return None


async def _safe_call_areal_tool(mo, tools, time_range: str, use_last_month: bool = False) -> Any:
    if use_last_month:
        tool = mo._find_tool(tools, "query_last_month_areal_rainfall")
        if tool:
            try:
                data = rfp._unwrap_tool_result(
                    await asyncio.wait_for(tool.ainvoke({"zone_type": "9"}), timeout=75)
                )
                if _record_count(data) > 0:
                    return _force_zone9_label(data)
            except Exception as exc:
                print(f"[areal_fallback_patch] 上月9分区工具失败，准备回退：{exc}")

        # 上月专用工具失败时，先回退通用9分区工具。
        tool = mo._find_tool(tools, "query_period_areal_rainfall_9")
        if tool:
            try:
                data = rfp._unwrap_tool_result(
                    await asyncio.wait_for(
                        tool.ainvoke({"zone_type": "9", "time_range": time_range}),
                        timeout=75,
                    )
                )
                if _record_count(data) > 0:
                    return _force_zone9_label(data)
            except Exception as exc:
                print(f"[areal_fallback_patch] 通用9分区面雨量工具失败，准备回退老工具：{exc}")

        legacy = await _call_legacy_basin_areal_if_zone9(mo, tools, time_range)
        if legacy is not None:
            return legacy
        return _no_data("上一个自然月暂无有效海河9分区面雨量数据。", time_range)

    tool = mo._find_tool(tools, "query_period_areal_rainfall_9")
    if tool:
        try:
            data = rfp._unwrap_tool_result(
                await asyncio.wait_for(tool.ainvoke({"zone_type": "9", "time_range": time_range}), timeout=75)
            )
            if _record_count(data) > 0:
                return _force_zone9_label(data)
        except Exception as exc:
            print(f"[areal_fallback_patch] 通用9分区面雨量工具失败，准备回退老工具：{exc}")

    legacy = await _call_legacy_basin_areal_if_zone9(mo, tools, time_range)
    if legacy is not None:
        return legacy
    return _no_data("当前时段暂无有效海河9分区面雨量数据。", time_range)


async def _safe_call_year_to_date_tool(mo, tools, time_range: str) -> Any:
    tool = mo._find_tool(tools, "query_year_to_date_areal_rainfall")
    if tool:
        try:
            data = rfp._unwrap_tool_result(
                await asyncio.wait_for(tool.ainvoke({"zone_type": "9"}), timeout=90)
            )
            if _record_count(data) > 0:
                return _force_zone9_label(data)
        except Exception as exc:
            print(f"[areal_fallback_patch] 今年9分区累计工具失败，准备回退：{exc}")

    # 今年累计也先尝试通用9分区工具，再回退老工具。
    tool = mo._find_tool(tools, "query_period_areal_rainfall_9")
    if tool:
        try:
            data = rfp._unwrap_tool_result(
                await asyncio.wait_for(tool.ainvoke({"zone_type": "9", "time_range": time_range}), timeout=90)
            )
            if _record_count(data) > 0:
                return _force_zone9_label(data)
        except Exception as exc:
            print(f"[areal_fallback_patch] 今年通用9分区面雨量工具失败，准备回退老工具：{exc}")

    legacy = await _call_legacy_basin_areal_if_zone9(mo, tools, time_range, timeout=90)
    if legacy is not None:
        return legacy
    return _no_data("今年以来暂未查询到有效海河9分区累计降雨量数据。", time_range)


def install_areal_fallback_patch() -> bool:
    global _INSTALLED
    if _INSTALLED:
        print("[areal_fallback_patch] 已安装过，无需重复安装")
        return True
    rfp._call_areal_tool = _safe_call_areal_tool
    rfp._call_year_to_date_tool = _safe_call_year_to_date_tool
    _INSTALLED = True
    print("[areal_fallback_patch] 已安装：面雨量9分区可用性兜底")
    return True
