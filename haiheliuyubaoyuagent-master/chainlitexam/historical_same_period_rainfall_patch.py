"""显式安装“历史同期平均降雨量”前端快速路径补丁。"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any


_MODULE_MARKER = "_historical_same_period_rainfall_patch_installed"


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


def _should_use_historical_same_period_path(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    same_period_words = ("历史同期", "同期平均", "历年同期", "多年同期", "历史平均", "常年同期")
    rain_words = ("雨量", "降雨", "降水", "降雨量", "降水量")
    exclude_words = ("面雨量", "分区", "子流域", "未来", "预报", "预计", "会不会", "会有")
    return (
        any(k in t for k in same_period_words)
        and any(k in t for k in rain_words)
        and not any(k in t for k in exclude_words)
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


def _parse_year_count(text: str) -> int:
    t = text or ""
    for n in (30, 20, 15, 10, 5):
        if f"近{n}年" in t or f"{n}年" in t:
            return n
    return 10


def _build_reference_window(user_text: str) -> tuple[str, str, str]:
    """返回参考时段 start/end/readable。默认今天00:00到当前整点。"""
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    t = user_text or ""
    if "昨天" in t or "昨日" in t:
        start = now.replace(hour=0) - timedelta(days=1)
        end = now.replace(hour=0)
    elif "过去24小时" in t or "近24小时" in t or "最近24小时" in t:
        end = now
        start = now - timedelta(hours=24)
    elif "过去6小时" in t or "近6小时" in t or "最近6小时" in t:
        end = now
        start = now - timedelta(hours=6)
    elif "过去3小时" in t or "近3小时" in t or "最近3小时" in t:
        end = now
        start = now - timedelta(hours=3)
    elif "过去1小时" in t or "近1小时" in t or "最近1小时" in t:
        end = now
        start = now - timedelta(hours=1)
    else:
        start = now.replace(hour=0)
        end = now
    return (
        start.strftime("%Y%m%d%H%M%S"),
        end.strftime("%Y%m%d%H%M%S"),
        f"{start:%Y-%m-%d %H:%M:%S} ~ {end:%Y-%m-%d %H:%M:%S}",
    )


def _record_location(record: dict) -> str:
    parts = []
    for key in ("province", "city", "county", "cnty", "town"):
        value = str(record.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    return " ".join(parts) if parts else "-"


def _format_payload(mo, data: Any, fallback_time_label: str) -> str:
    if not isinstance(data, dict):
        return "历史同期平均降雨量查询结果格式异常，请稍后重试。"
    if data.get("status") == "no_data":
        return data.get("message") or "历史同期暂无有效自动站降雨量数据。"

    reference_label = _safe_cell(mo, data.get("reference_time_range_readable") or fallback_time_label)
    year_range = _safe_cell(mo, data.get("historical_year_range") or "-")
    valid_year_count = data.get("valid_year_count") or 0
    avg = _pick_number(data, "historical_average_rainfall_mm")
    summary = data.get("summary") or {}
    max_year = summary.get("max_year") if isinstance(summary, dict) else None
    min_year = summary.get("min_year") if isinstance(summary, dict) else None
    yearly_records = [r for r in (data.get("yearly_records") or []) if isinstance(r, dict)]

    lines = [
        "## 历史同期平均降雨量\n\n",
        f"**参考时段**：{reference_label}（北京时）  \n",
        f"**历史年份范围**：{year_range}  \n",
        f"**有效年份数**：{valid_year_count} 年  \n",
        f"**历史同期平均降雨量**：{avg} mm\n",
    ]

    if isinstance(max_year, dict):
        lines.append(
            f"\n**同期雨量最大年份**：{_safe_cell(mo, max_year.get('year'))}年，"
            f"平均降雨量 {_pick_number(max_year, 'average_rainfall_mm')} mm。"
        )
        max_station = max_year.get("max_station")
        if isinstance(max_station, dict):
            lines.append(
                f"最大站为 {_safe_cell(mo, max_station.get('station_name') or max_station.get('station_id') or '未知站点')}，"
                f"累计雨量 {_pick_number(max_station, 'rainfall_mm')} mm。"
            )
        lines.append("\n")
    if isinstance(min_year, dict):
        lines.append(
            f"**同期雨量最小年份**：{_safe_cell(mo, min_year.get('year'))}年，"
            f"平均降雨量 {_pick_number(min_year, 'average_rainfall_mm')} mm。\n"
        )

    if yearly_records:
        lines.append("\n### 各年份同期平均降雨量\n\n")
        lines.append("| 年份 | 平均降雨量(mm) | 参与站点数 | 最大站 | 最大站雨量(mm) | 位置 |\n")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |\n")
        for item in yearly_records[:30]:
            max_station = item.get("max_station") if isinstance(item.get("max_station"), dict) else {}
            lines.append(
                f"| {_safe_cell(mo, item.get('year'))} | "
                f"{_pick_number(item, 'average_rainfall_mm')} | "
                f"{_pick_number(item, 'station_count')} | "
                f"{_safe_cell(mo, max_station.get('station_name') or max_station.get('station_id') or '-')} | "
                f"{_pick_number(max_station, 'rainfall_mm')} | "
                f"{_safe_cell(mo, _record_location(max_station))} |\n"
            )

    return "".join(lines)


def install_historical_same_period_rainfall_patch() -> bool:
    try:
        import message_orchestrator as mo
    except Exception as exc:
        print(f"[historical_same_period_rainfall_patch] message_orchestrator 导入失败，跳过补丁：{exc}")
        return False

    if getattr(mo, _MODULE_MARKER, False):
        print("[historical_same_period_rainfall_patch] 已安装过，无需重复安装")
        return True

    original = getattr(mo, "_try_rainfall_analysis_fast_path", None)
    if not callable(original):
        print("[historical_same_period_rainfall_patch] 未找到降雨分析快速路径，跳过补丁")
        return False

    async def patched_rainfall_analysis_fast_path(user_text: str, tools, messages, callbacks) -> bool:
        if not _should_use_historical_same_period_path(user_text):
            return await original(user_text, tools, messages, callbacks)

        tool = mo._find_tool(tools, "query_historical_same_period_avg_rainfall")
        if not tool:
            return await original(user_text, tools, messages, callbacks)

        start_time, end_time, time_label = _build_reference_window(user_text)
        years = _parse_year_count(user_text)
        thinking_msg = await mo._show_thinking("🔍 正在查询历史同期平均降雨量，请稍候...")
        try:
            result = await asyncio.wait_for(
                tool.ainvoke({
                    "reference_start_time": start_time,
                    "reference_end_time": end_time,
                    "years": years,
                }),
                timeout=90,
            )
            data = _unwrap_tool_result(result)
            text = _format_payload(mo, data, time_label)
            await mo._emit_fast_path_result(text, thinking_msg, messages, user_text)
            return True
        except asyncio.TimeoutError:
            await mo._emit_fast_path_result("⏱️ 历史同期平均降雨量查询超时，请稍后重试。", thinking_msg, messages, user_text)
            return True
        except Exception as exc:
            print(f"[historical_same_period_rainfall_patch] 历史同期平均降雨量快速路径失败：{exc}")
            await mo._emit_fast_path_result("历史同期平均降雨量查询遇到异常，请稍后重试。", thinking_msg, messages, user_text)
            return True

    patched_rainfall_analysis_fast_path._historical_same_period_rainfall_patch_installed = True
    patched_rainfall_analysis_fast_path._historical_same_period_rainfall_original = original
    mo._try_rainfall_analysis_fast_path = patched_rainfall_analysis_fast_path
    setattr(mo, _MODULE_MARKER, True)
    print("[historical_same_period_rainfall_patch] 已安装：历史同期平均降雨量快速路径")
    return True
