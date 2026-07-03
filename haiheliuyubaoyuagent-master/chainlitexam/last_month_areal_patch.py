"""显式安装“上个月面雨量”前端快速路径补丁。

该补丁只处理明确的“上月/上个月 + 面雨量/雨量”问题，避免原有
_basin_areal_rainfall 快速路径把问题误判为过去 24 小时。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any


_MODULE_MARKER = "_last_month_areal_patch_installed"


def _install_related_routes() -> None:
    """安装同属面雨量/子流域入口的后续专用路由。"""
    try:
        from subbasin_max_rainfall_patch import install_subbasin_max_rainfall_patch

        install_subbasin_max_rainfall_patch()
    except Exception as exc:
        print(f"[last_month_areal_patch] subbasin max rainfall route init failed: {exc}")


def _unwrap_tool_result(result: Any) -> Any:
    data = result
    if hasattr(data, "content"):
        data = data.content
    if isinstance(data, list) and data and isinstance(data[0], dict) and "text" in data[0]:
        data = data[0]["text"]
    if isinstance(data, str):
        try:
            return json.loads(data)
        except Exception:
            return data
    return data


def _previous_month_range() -> tuple[str, str, str]:
    now = datetime.now()
    first_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end_prev = first_this_month - timedelta(seconds=1)
    start_prev = end_prev.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    time_range = f"[{start_prev.strftime('%Y%m%d%H%M%S')},{end_prev.strftime('%Y%m%d%H%M%S')}]"
    label = f"{start_prev.strftime('%Y年%m月%d日 %H:%M')} ~ {end_prev.strftime('%Y年%m月%d日 %H:%M')}"
    month = start_prev.strftime("%Y年%m月")
    return time_range, label, month


def _should_use_last_month_areal_path(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    month_words = ("上个月", "上月", "上一个月", "上一月", "上自然月", "上个自然月")
    rain_words = ("面雨量", "分区雨量", "流域雨量", "河系雨量", "累计雨量", "降雨量", "雨量")
    forecast_words = ("未来", "明天", "后天", "预报", "预计", "会不会", "会有")
    return (
        any(k in t for k in month_words)
        and any(k in t for k in rain_words)
        and not any(k in t for k in forecast_words)
    )


def _safe_cell(mo, value: Any) -> str:
    cleaner = getattr(mo, "_clean_table_cell", None)
    if callable(cleaner):
        return cleaner(value)
    return "" if value is None else str(value).replace("|", "｜").strip()


def _pick_number(item: dict, *keys: str) -> Any:
    """取数值字段；0/0.0 是有效值，不能被 or '-' 吃掉。"""
    for key in keys:
        if key in item and item[key] not in (None, "", "None"):
            return item[key]
    return "-"


def _valid_records(records: Any) -> list[dict]:
    return [r for r in (records or []) if isinstance(r, dict) and "error" not in r]


def _build_summary(records: list[dict]) -> dict:
    valid = _valid_records(records)
    if not valid:
        return {"zone_count": 0, "max_zone": None}
    def _rain(row: dict) -> float:
        val = _pick_number(row, "avg_rainfall_mm", "avg", "average_rainfall_mm", "mean")
        try:
            return float(val)
        except Exception:
            return -1.0
    max_zone = max(valid, key=_rain)
    return {"zone_count": len(valid), "max_zone": max_zone}


def _normalize_legacy_payload(data: Any, time_label: str, month: str) -> Any:
    """旧 query_basin_areal_rainfall 返回 list，这里包成统一业务展示结构。"""
    if not isinstance(data, list):
        return data
    valid = _valid_records(data)
    if not valid:
        return {
            "status": "no_data",
            "month": month,
            "time_range_readable": time_label,
            "zone_label": "海河流域分区",
            "records": [],
            "summary": _build_summary([]),
            "message": f"{month}海河流域面雨量暂无有效数据。",
        }
    zone_label = "海河9分区" if len(valid) <= 12 else "海河流域面雨量分区"
    return {
        "status": "ok",
        "month": month,
        "time_range_readable": time_label,
        "zone_label": zone_label,
        "records": valid,
        "summary": _build_summary(valid),
    }


def _format_last_month_payload(mo, data: Any, fallback_label: str, fallback_month: str) -> str:
    if isinstance(data, dict):
        if data.get("status") == "no_data":
            return data.get("message") or f"{fallback_month}海河流域面雨量暂无有效数据。"
        records = data.get("records") or []
        month = data.get("month") or fallback_month
        time_label = data.get("time_range_readable") or fallback_label
        zone_label = data.get("zone_label") or "海河9分区"
        summary = data.get("summary") or {}
    elif isinstance(data, list):
        normalized = _normalize_legacy_payload(data, fallback_label, fallback_month)
        return _format_last_month_payload(mo, normalized, fallback_label, fallback_month)
    else:
        return f"{fallback_month}海河流域面雨量查询结果格式异常，请稍后重试。"

    valid_records = _valid_records(records)
    if not valid_records:
        return f"{month}海河流域面雨量暂无有效数据。"

    lines = [
        f"## 海河流域上月面雨量对比（{month}）\n\n",
        f"**统计时段**：{_safe_cell(mo, time_label)}（北京时）  \n",
        f"**分区体系**：{_safe_cell(mo, zone_label)}\n\n",
        "| 排名 | 分区 | 累计面雨量(mm) | 最大面雨量(mm) |\n",
        "| :--- | :--- | :--- | :--- |\n",
    ]

    for idx, item in enumerate(valid_records[:20], 1):
        zone_name = _safe_cell(
            mo,
            item.get("zone_name")
            or item.get("zone_id")
            or item.get("name")
            or item.get("分区")
            or "未知",
        )
        avg = _pick_number(item, "avg_rainfall_mm", "avg", "average_rainfall_mm", "mean")
        mx = _pick_number(item, "max_rainfall_mm", "max", "maximum_rainfall_mm", "maximum")
        lines.append(f"| {idx} | {zone_name} | {avg} | {mx} |\n")

    max_zone = summary.get("max_zone") if isinstance(summary, dict) else None
    if isinstance(max_zone, dict):
        max_zone_rain = _pick_number(max_zone, "avg_rainfall_mm", "avg", "average_rainfall_mm", "mean")
        lines.append(
            f"\n**上月累计面雨量最大的分区**："
            f"{_safe_cell(mo, max_zone.get('zone_name') or max_zone.get('zone_id') or '未知')}，"
            f"累计面雨量 {max_zone_rain} mm。\n"
        )
    return "".join(lines)


async def _ainvoke_with_timeout(tool, payload: dict, timeout: int = 120) -> Any:
    return await asyncio.wait_for(tool.ainvoke(payload), timeout=timeout)


async def _query_last_month_with_fallback(mo, last_month_tool, fallback_tool, time_range: str, time_label: str, month: str) -> Any:
    if last_month_tool:
        try:
            data = _unwrap_tool_result(await _ainvoke_with_timeout(last_month_tool, {"zone_type": "9"}))
            # 新工具如果正常返回且有业务数据，直接用。
            if isinstance(data, dict) and data.get("status") == "ok" and _valid_records(data.get("records")):
                return data
            # 新工具返回 no_data 时，也尝试旧工具兜底，避免有旧面雨量数据却被空结果覆盖。
            print(f"[last_month_areal_patch] 新上月面雨量工具无有效数据，尝试旧面雨量工具兜底")
        except Exception as exc:
            print(f"[last_month_areal_patch] 新上月面雨量工具调用失败，尝试旧面雨量工具兜底：{exc}")

    if fallback_tool:
        legacy = _unwrap_tool_result(
            await _ainvoke_with_timeout(
                fallback_tool,
                {"zone_type": "9", "time_range": time_range, "hours": None},
            )
        )
        return _normalize_legacy_payload(legacy, time_label, month)

    return {
        "status": "no_data",
        "month": month,
        "time_range_readable": time_label,
        "zone_label": "海河9分区",
        "records": [],
        "summary": _build_summary([]),
        "message": f"{month}海河流域面雨量暂无有效数据。",
    }


def install_last_month_areal_patch() -> bool:
    try:
        import message_orchestrator as mo
    except Exception as exc:
        print(f"[last_month_areal_patch] message_orchestrator 导入失败，跳过补丁：{exc}")
        return False

    if getattr(mo, _MODULE_MARKER, False):
        print("[last_month_areal_patch] 已安装过，无需重复安装")
        _install_related_routes()
        return True

    original = getattr(mo, "_try_basin_areal_rainfall_fast_path", None)
    if not callable(original):
        print("[last_month_areal_patch] 未找到面雨量快速路径，跳过补丁")
        _install_related_routes()
        return False

    async def patched_basin_areal_rainfall_fast_path(user_text: str, tools, messages, callbacks) -> bool:
        if not _should_use_last_month_areal_path(user_text):
            return await original(user_text, tools, messages, callbacks)

        last_month_tool = mo._find_tool(tools, "query_last_month_areal_rainfall")
        fallback_tool = mo._find_tool(tools, "query_basin_areal_rainfall")
        if not last_month_tool and not fallback_tool:
            return await original(user_text, tools, messages, callbacks)

        time_range, time_label, month = _previous_month_range()
        thinking_msg = await mo._show_thinking("🔍 正在查询上个月各子流域面雨量数据，请稍候...")

        try:
            data = await _query_last_month_with_fallback(
                mo,
                last_month_tool,
                fallback_tool,
                time_range,
                time_label,
                month,
            )
            text = _format_last_month_payload(mo, data, time_label, month)
            await mo._emit_fast_path_result(text, thinking_msg, messages, user_text)
            return True
        except asyncio.TimeoutError:
            await mo._emit_fast_path_result("⏱️ 上个月面雨量查询超时，请稍后重试。", thinking_msg, messages, user_text)
            return True
        except Exception as exc:
            print(f"[last_month_areal_patch] 上月面雨量快速路径兜底仍失败：{exc}")
            await mo._emit_fast_path_result(
                "上个月面雨量暂未查询到有效结果，请检查面雨量数据源或稍后重试。",
                thinking_msg,
                messages,
                user_text,
            )
            return True

    patched_basin_areal_rainfall_fast_path._last_month_patch_installed = True
    patched_basin_areal_rainfall_fast_path._last_month_original = original
    mo._try_basin_areal_rainfall_fast_path = patched_basin_areal_rainfall_fast_path
    setattr(mo, _MODULE_MARKER, True)
    print("[last_month_areal_patch] 已安装：上个月面雨量快速路径")
    _install_related_routes()
    return True
