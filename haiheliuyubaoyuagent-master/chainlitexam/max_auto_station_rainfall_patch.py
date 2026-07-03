"""显式安装“哪个自动站雨量最大”前端快速路径补丁。

默认口径：用户未指定时间时，查询今天 00:00 到当前时刻的海河流域自动站累计雨量，返回最大站。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any


_MODULE_MARKER = "_max_auto_station_rainfall_patch_installed"


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


def _should_use_max_auto_station_rainfall_path(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    station_words = ("自动站", "站点", "气象站", "监测站", "雨量站")
    rain_words = ("雨量", "降雨", "降水")
    max_words = ("最大", "最多", "最高", "第一", "排第一", "最大的是", "哪个")
    # 只抢短时段/默认今日问题。长历史问题先交给专用路由，避免把“去年哪个自动站雨量最大”误按今天回答。
    unsupported_history_words = ("去年", "前年", "上年", "上一年", "上个月", "上月", "上周", "历史", "全年", "整年")
    exclude_words = (
        "面雨量", "分区", "子流域", "流域平均",
        "去年最大日降雨", "最大日降雨量", *unsupported_history_words,
    )
    return (
        any(k in t for k in station_words)
        and any(k in t for k in rain_words)
        and any(k in t for k in max_words)
        and not any(k in t for k in exclude_words)
    )


def _safe_cell(mo, value: Any) -> str:
    cleaner = getattr(mo, "_clean_table_cell", None)
    if callable(cleaner):
        return cleaner(value)
    return "" if value is None else str(value).replace("|", "｜").strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", "None"):
            return default
        return float(value)
    except Exception:
        return default


def _build_time_window(user_text: str) -> tuple[str, str, str]:
    """返回 start_time, end_time, readable。默认今日 00:00 ~ 当前时刻。"""
    now = datetime.now()
    t = user_text or ""

    if "昨天" in t or "昨日" in t:
        start = (now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1))
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
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
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now

    return (
        start.strftime("%Y%m%d%H%M%S"),
        end.strftime("%Y%m%d%H%M%S"),
        f"{start:%Y-%m-%d %H:%M:%S} ~ {end:%Y-%m-%d %H:%M:%S}",
    )


def _station_location(station: dict) -> str:
    parts = []
    for key in ("province", "city", "cnty", "county", "town"):
        value = str(station.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    return " ".join(parts) if parts else "-"


def _collect_top_stations(data: dict) -> list[dict]:
    stations: list[dict] = []
    max_station = data.get("max_station")
    if isinstance(max_station, dict):
        stations.append(max_station)

    for level_item in data.get("level_analysis") or []:
        if not isinstance(level_item, dict):
            continue
        for station in level_item.get("stations") or []:
            if isinstance(station, dict):
                stations.append(station)

    seen = set()
    unique: list[dict] = []
    for station in stations:
        sid = str(station.get("station_id") or station.get("Station_Id_C") or station.get("name") or "")
        key = (sid, str(station.get("rainfall") or station.get("rain") or ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(station)

    unique.sort(key=lambda s: _safe_float(s.get("rainfall") or s.get("rain") or s.get("value")), reverse=True)
    return unique


def _format_max_station_payload(mo, data: Any, fallback_time_label: str) -> str:
    if not isinstance(data, dict):
        return "自动站雨量查询结果格式异常，请稍后重试。"

    max_station = data.get("max_station")
    if not isinstance(max_station, dict):
        return "当前未查询到有效自动站降雨数据。"

    rainfall = _safe_float(max_station.get("rainfall") or max_station.get("rain") or max_station.get("value"))
    station_name = _safe_cell(mo, max_station.get("name") or max_station.get("station_name") or "未知站点")
    station_id = _safe_cell(mo, max_station.get("station_id") or max_station.get("Station_Id_C") or "-")
    location = _safe_cell(mo, _station_location(max_station))
    time_label = _safe_cell(mo, data.get("time_range_readable") or fallback_time_label)
    total_stations = data.get("total_stations") or 0
    max_level = _safe_cell(mo, data.get("max_level") or "-")

    lines = [
        "## 自动站最大雨量\n\n",
        f"**统计时段**：{time_label}（北京时）  \n",
        f"**雨量最大的自动站**：{station_name}（站号：{station_id}）  \n",
        f"**累计雨量**：{rainfall:.1f} mm  \n",
        f"**站点位置**：{location}  \n",
    ]
    if max_level and max_level != "-":
        lines.append(f"**降雨等级**：{max_level}  \n")
    if total_stations:
        lines.append(f"**参与统计站点数**：{total_stations} 个\n")

    top_stations = _collect_top_stations(data)
    if len(top_stations) > 1:
        lines.append("\n### 雨量排名前列自动站\n\n")
        lines.append("| 排名 | 站点 | 站号 | 累计雨量(mm) | 位置 |\n")
        lines.append("| :--- | :--- | :--- | :--- | :--- |\n")
        for idx, station in enumerate(top_stations[:10], 1):
            lines.append(
                f"| {idx} | "
                f"{_safe_cell(mo, station.get('name') or station.get('station_name') or '未知站点')} | "
                f"{_safe_cell(mo, station.get('station_id') or station.get('Station_Id_C') or '-')} | "
                f"{_safe_float(station.get('rainfall') or station.get('rain') or station.get('value')):.1f} | "
                f"{_safe_cell(mo, _station_location(station))} |\n"
            )

    return "".join(lines)


def install_max_auto_station_rainfall_patch() -> bool:
    try:
        import message_orchestrator as mo
    except Exception as exc:
        print(f"[max_auto_station_rainfall_patch] message_orchestrator 导入失败，跳过补丁：{exc}")
        return False

    if getattr(mo, _MODULE_MARKER, False):
        print("[max_auto_station_rainfall_patch] 已安装过，无需重复安装")
        return True

    original = getattr(mo, "_try_rainfall_analysis_fast_path", None)
    if not callable(original):
        print("[max_auto_station_rainfall_patch] 未找到降雨分析快速路径，跳过补丁")
        return False

    async def patched_rainfall_analysis_fast_path(user_text: str, tools, messages, callbacks) -> bool:
        if not _should_use_max_auto_station_rainfall_path(user_text):
            return await original(user_text, tools, messages, callbacks)

        tool = mo._find_tool(tools, "local_analyze_rainfall_by_time") or mo._find_tool(tools, "analyze_rainfall_by_time")
        if not tool:
            return await original(user_text, tools, messages, callbacks)

        start_time, end_time, time_label = _build_time_window(user_text)
        thinking_msg = await mo._show_thinking("🔍 正在查询自动站最大雨量，请稍候...")
        try:
            result = await asyncio.wait_for(
                tool.ainvoke({
                    "time_str": end_time,
                    "start_time": start_time,
                    "end_time": end_time,
                }),
                timeout=45,
            )
            data = _unwrap_tool_result(result)
            text = _format_max_station_payload(mo, data, time_label)
            await mo._emit_fast_path_result(text, thinking_msg, messages, user_text)
            return True
        except asyncio.TimeoutError:
            await mo._emit_fast_path_result("⏱️ 自动站雨量查询超时，请稍后重试。", thinking_msg, messages, user_text)
            return True
        except Exception as exc:
            print(f"[max_auto_station_rainfall_patch] 自动站最大雨量快速路径失败：{exc}")
            await mo._emit_fast_path_result("自动站雨量查询遇到异常，请稍后重试。", thinking_msg, messages, user_text)
            return True

    patched_rainfall_analysis_fast_path._max_auto_station_rainfall_patch_installed = True
    patched_rainfall_analysis_fast_path._max_auto_station_rainfall_original = original
    mo._try_rainfall_analysis_fast_path = patched_rainfall_analysis_fast_path
    setattr(mo, _MODULE_MARKER, True)
    print("[max_auto_station_rainfall_patch] 已安装：自动站最大雨量快速路径")
    return True
