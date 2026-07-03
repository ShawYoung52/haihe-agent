"""显式安装“哪个子流域降雨最多”前端快速路径补丁。

默认口径：用户未指定时间时，查询今天 00:00 到当前时刻的海河9分区/子流域累计面雨量，返回降雨最多的子流域。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any


_MODULE_MARKER = "_subbasin_max_rainfall_patch_installed"


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


def _should_use_subbasin_max_rainfall_path(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    basin_words = ("子流域", "分区", "河系")
    rain_words = ("雨量", "降雨", "降水", "降雨量", "降水量")
    max_words = ("最多", "最大", "最高", "最强", "第一", "排第一", "哪个")
    # 长历史和预报问题先交给专用路由，避免默认按今天回答。
    exclude_words = (
        "上个月", "上月", "去年", "前年", "历史", "同期", "全年", "整年",
        "未来", "明天", "后天", "预报", "预计", "会不会", "会有",
        "自动站", "站点", "气象站", "雨量站",
    )
    return (
        any(k in t for k in basin_words)
        and any(k in t for k in rain_words)
        and any(k in t for k in max_words)
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


def _to_float(value: Any, default: float = -1.0) -> float:
    try:
        if value in (None, "", "None", "-"):
            return default
        return float(value)
    except Exception:
        return default


def _valid_records(records: Any) -> list[dict]:
    return [r for r in (records or []) if isinstance(r, dict) and "error" not in r]


def _rain_value(row: dict) -> float:
    return _to_float(_pick_number(row, "avg_rainfall_mm", "avg", "average_rainfall_mm", "mean"))


def _build_time_window(user_text: str) -> tuple[str, str, str]:
    now = datetime.now()
    t = user_text or ""

    if "昨天" in t or "昨日" in t:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif "前天" in t:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)
        end = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
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
        f"[{start.strftime('%Y%m%d%H%M%S')},{end.strftime('%Y%m%d%H%M%S')}]",
        f"{start:%Y-%m-%d %H:%M:%S} ~ {end:%Y-%m-%d %H:%M:%S}",
        start.strftime("%Y%m%d%H%M%S"),
    )


def _zone_name(row: dict) -> str:
    return str(
        row.get("zone_name")
        or row.get("zone_id")
        or row.get("name")
        or row.get("分区")
        or "未知子流域"
    )


def _normalize_payload(data: Any, time_label: str) -> dict:
    if isinstance(data, dict):
        records = _valid_records(data.get("records") or data.get("data") or [])
        if not records and isinstance(data.get("summary"), dict) and isinstance(data["summary"].get("records"), list):
            records = _valid_records(data["summary"].get("records"))
        zone_label = data.get("zone_label") or data.get("zone_type_label") or "海河9分区/子流域"
    elif isinstance(data, list):
        records = _valid_records(data)
        zone_label = "海河9分区/子流域" if len(records) <= 12 else "海河流域面雨量分区"
    else:
        records = []
        zone_label = "海河9分区/子流域"

    records.sort(key=_rain_value, reverse=True)
    return {
        "records": records,
        "zone_label": zone_label,
        "time_label": time_label,
        "max_record": records[0] if records else None,
    }


def _format_payload(mo, data: Any, time_label: str) -> str:
    normalized = _normalize_payload(data, time_label)
    records = normalized["records"]
    max_record = normalized["max_record"]
    if not records or not isinstance(max_record, dict):
        return "当前时段暂未查询到有效子流域降雨数据。"

    zone_label = _safe_cell(mo, normalized.get("zone_label") or "海河9分区/子流域")
    max_name = _safe_cell(mo, _zone_name(max_record))
    max_rain = _pick_number(max_record, "avg_rainfall_mm", "avg", "average_rainfall_mm", "mean")
    max_point = _pick_number(max_record, "max_rainfall_mm", "max", "maximum_rainfall_mm", "maximum")

    lines = [
        "## 子流域降雨最多结果\n\n",
        f"**统计时段**：{_safe_cell(mo, time_label)}（北京时）  \n",
        f"**分区体系**：{zone_label}  \n",
        f"**降雨最多的子流域**：{max_name}  \n",
        f"**累计面雨量**：{max_rain} mm  \n",
    ]
    if max_point != "-":
        lines.append(f"**该子流域内最大雨量**：{max_point} mm\n")

    lines.append("\n### 子流域降雨排名\n\n")
    lines.append("| 排名 | 子流域/分区 | 累计面雨量(mm) | 最大雨量(mm) |\n")
    lines.append("| :--- | :--- | :--- | :--- |\n")
    for idx, item in enumerate(records[:20], 1):
        lines.append(
            f"| {idx} | "
            f"{_safe_cell(mo, _zone_name(item))} | "
            f"{_pick_number(item, 'avg_rainfall_mm', 'avg', 'average_rainfall_mm', 'mean')} | "
            f"{_pick_number(item, 'max_rainfall_mm', 'max', 'maximum_rainfall_mm', 'maximum')} |\n"
        )

    return "".join(lines)


def install_subbasin_max_rainfall_patch() -> bool:
    try:
        import message_orchestrator as mo
    except Exception as exc:
        print(f"[subbasin_max_rainfall_patch] message_orchestrator 导入失败，跳过补丁：{exc}")
        return False

    if getattr(mo, _MODULE_MARKER, False):
        print("[subbasin_max_rainfall_patch] 已安装过，无需重复安装")
        return True

    original = getattr(mo, "_try_basin_areal_rainfall_fast_path", None)
    if not callable(original):
        print("[subbasin_max_rainfall_patch] 未找到面雨量快速路径，跳过补丁")
        return False

    async def patched_basin_areal_rainfall_fast_path(user_text: str, tools, messages, callbacks) -> bool:
        if not _should_use_subbasin_max_rainfall_path(user_text):
            return await original(user_text, tools, messages, callbacks)

        tool = mo._find_tool(tools, "query_basin_areal_rainfall")
        if not tool:
            return await original(user_text, tools, messages, callbacks)

        time_range, time_label, _ = _build_time_window(user_text)
        thinking_msg = await mo._show_thinking("🔍 正在查询各子流域降雨量，请稍候...")
        try:
            payload = {"zone_type": "9", "time_range": time_range}
            result = await asyncio.wait_for(tool.ainvoke(payload), timeout=90)
            data = _unwrap_tool_result(result)
            text = _format_payload(mo, data, time_label)
            await mo._emit_fast_path_result(text, thinking_msg, messages, user_text)
            return True
        except asyncio.TimeoutError:
            await mo._emit_fast_path_result("⏱️ 子流域降雨量查询超时，请稍后重试。", thinking_msg, messages, user_text)
            return True
        except Exception as exc:
            print(f"[subbasin_max_rainfall_patch] 子流域降雨最多快速路径失败：{exc}")
            await mo._emit_fast_path_result("子流域降雨量查询遇到异常，请稍后重试。", thinking_msg, messages, user_text)
            return True

    patched_basin_areal_rainfall_fast_path._subbasin_max_rainfall_patch_installed = True
    patched_basin_areal_rainfall_fast_path._subbasin_max_rainfall_original = original
    mo._try_basin_areal_rainfall_fast_path = patched_basin_areal_rainfall_fast_path
    setattr(mo, _MODULE_MARKER, True)
    print("[subbasin_max_rainfall_patch] 已安装：子流域降雨最多快速路径")
    return True
