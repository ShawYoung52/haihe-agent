"""显式安装“去年最大日降雨量”前端快速路径补丁。"""
from __future__ import annotations

import asyncio
import json
from typing import Any


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


def _should_use_last_year_max_daily_rainfall_path(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    year_words = ("去年", "上一年", "上年", "上一个自然年", "去年全年")
    max_words = ("最大", "最高", "最多", "极大")
    daily_words = ("日降雨量", "日降水量", "单日降雨量", "单日降水量", "最大日雨量", "最大日降雨", "日雨量")
    rain_words = ("降雨", "降水", "雨量")
    forecast_words = ("未来", "明天", "后天", "预报", "预计")
    return (
        any(k in t for k in year_words)
        and any(k in t for k in max_words)
        and (any(k in t for k in daily_words) or ("日" in t and any(k in t for k in rain_words)))
        and not any(k in t for k in forecast_words)
    )


def _safe_cell(mo, value: Any) -> str:
    cleaner = getattr(mo, "_clean_table_cell", None)
    if callable(cleaner):
        return cleaner(value)
    return "" if value is None else str(value).replace("|", "｜").strip()


def _pick_number(item: dict, *keys: str) -> Any:
    for key in keys:
        if key in item and item[key] not in (None, "", "None"):
            return item[key]
    return "-"


def _record_location(record: dict) -> str:
    parts = []
    for key in ("province", "city", "county", "town"):
        value = str(record.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    return " ".join(parts) if parts else "-"


def _format_last_year_max_daily_payload(mo, data: Any) -> str:
    if not isinstance(data, dict):
        return "去年最大日降雨量查询结果格式异常，请稍后重试。"
    if data.get("status") == "no_data":
        return data.get("message") or "去年海河流域暂无有效日降雨量数据。"

    year = data.get("year") or "去年"
    time_label = data.get("time_range_readable") or f"{year}年全年"
    max_record = data.get("max_record") or (data.get("summary") or {}).get("max_record") or {}
    records = data.get("records") or []

    if not isinstance(max_record, dict) or not max_record:
        return f"{year}年海河流域暂无有效日降雨量数据。"

    station_name = _safe_cell(mo, max_record.get("station_name") or "未知站点")
    station_id = _safe_cell(mo, max_record.get("station_id") or "-")
    date = _safe_cell(mo, max_record.get("date") or "-")
    rainfall = _pick_number(max_record, "daily_rainfall_mm", "MAX_PRE_Time_0808", "PRE_Time_0808")
    location = _safe_cell(mo, _record_location(max_record))

    lines = [
        f"## {year}年海河流域最大日降雨量\n\n",
        f"**统计时段**：{_safe_cell(mo, time_label)}（北京时）  \n",
        f"**最大日降雨量**：{rainfall} mm  \n",
        f"**出现日期**：{date}  \n",
        f"**出现站点**：{station_name}（站号：{station_id}）  \n",
        f"**站点位置**：{location}\n",
    ]

    valid_records = [r for r in records if isinstance(r, dict)]
    if len(valid_records) > 1:
        lines.append("\n### 去年日降雨量排名前列站点\n\n")
        lines.append("| 排名 | 日期 | 站点 | 站号 | 日降雨量(mm) | 位置 |\n")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |\n")
        for idx, item in enumerate(valid_records[:10], 1):
            lines.append(
                f"| {idx} | "
                f"{_safe_cell(mo, item.get('date') or '-')} | "
                f"{_safe_cell(mo, item.get('station_name') or '未知站点')} | "
                f"{_safe_cell(mo, item.get('station_id') or '-')} | "
                f"{_pick_number(item, 'daily_rainfall_mm', 'MAX_PRE_Time_0808', 'PRE_Time_0808')} | "
                f"{_safe_cell(mo, _record_location(item))} |\n"
            )

    return "".join(lines)


def install_last_year_max_daily_rainfall_patch() -> bool:
    try:
        import message_orchestrator as mo
    except Exception as exc:
        print(f"[last_year_max_daily_rainfall_patch] message_orchestrator 导入失败，跳过补丁：{exc}")
        return False

    original = getattr(mo, "_try_rainfall_analysis_fast_path", None)
    if not callable(original):
        print("[last_year_max_daily_rainfall_patch] 未找到降雨分析快速路径，跳过补丁")
        return False
    if getattr(original, "_last_year_max_daily_rainfall_patch_installed", False):
        print("[last_year_max_daily_rainfall_patch] 已安装过，无需重复安装")
        return True

    async def patched_rainfall_analysis_fast_path(user_text: str, tools, messages, callbacks) -> bool:
        if not _should_use_last_year_max_daily_rainfall_path(user_text):
            return await original(user_text, tools, messages, callbacks)

        tool = mo._find_tool(tools, "query_last_year_max_daily_rainfall")
        if not tool:
            return await original(user_text, tools, messages, callbacks)

        thinking_msg = await mo._show_thinking("🔍 正在查询去年最大日降雨量，请稍候...")
        try:
            result = await asyncio.wait_for(
                tool.ainvoke({"top_n": 10, "allow_slow_fallback": False}),
                timeout=75,
            )
            data = _unwrap_tool_result(result)
            text = _format_last_year_max_daily_payload(mo, data)
            await mo._emit_fast_path_result(text, thinking_msg, messages, user_text)
            return True
        except asyncio.TimeoutError:
            await mo._emit_fast_path_result("⏱️ 去年最大日降雨量统计接口响应超时，请稍后重试。", thinking_msg, messages, user_text)
            return True
        except Exception as exc:
            print(f"[last_year_max_daily_rainfall_patch] 去年最大日降雨量快速路径失败：{exc}")
            await mo._emit_fast_path_result("去年最大日降雨量查询遇到异常，请稍后重试。", thinking_msg, messages, user_text)
            return True

    patched_rainfall_analysis_fast_path._last_year_max_daily_rainfall_patch_installed = True
    mo._try_rainfall_analysis_fast_path = patched_rainfall_analysis_fast_path
    print("[last_year_max_daily_rainfall_patch] 已安装：去年最大日降雨量快速路径")
    return True
