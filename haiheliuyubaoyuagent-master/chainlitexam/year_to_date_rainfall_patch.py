"""显式安装“今年累计降雨量是多少”前端快速路径补丁。

默认口径：今年 1 月 1 日 00:00 到当前时刻，海河流域累计面雨量；
同时给出海河9分区/子流域累计面雨量排名。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any


_MODULE_MARKER = "_year_to_date_rainfall_patch_installed"


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


def _should_use_year_to_date_rainfall_path(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    year_words = ("今年", "本年", "今年以来", "年内", "今年到现在")
    cumulative_words = ("累计", "总雨量", "累计雨量", "累计降雨", "累计降雨量", "累计降水", "累计降水量")
    rain_words = ("雨量", "降雨", "降水", "降雨量", "降水量")
    # 排除更具体的问题，避免抢“今年哪个子流域降雨最多”“今年自动站累计雨量”等后续专用问题。
    exclude_words = (
        "哪个", "最多", "最大", "最高", "第一", "排第一",
        "子流域", "分区", "河系",
        "自动站", "站点", "气象站", "雨量站",
        "历史", "同期", "去年", "上个月", "上月",
        "未来", "明天", "后天", "预报", "预计", "会不会", "会有",
    )
    return (
        any(k in t for k in year_words)
        and any(k in t for k in cumulative_words)
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


def _build_year_to_date_window() -> tuple[str, str]:
    now = datetime.now()
    start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    time_range = f"[{start.strftime('%Y%m%d%H%M%S')},{now.strftime('%Y%m%d%H%M%S')}]"
    time_label = f"{start:%Y-%m-%d %H:%M:%S} ~ {now:%Y-%m-%d %H:%M:%S}"
    return time_range, time_label


def _zone_name(row: dict) -> str:
    return str(
        row.get("zone_name")
        or row.get("zone_id")
        or row.get("name")
        or row.get("分区")
        or "未知分区"
    )


def _normalize_records(data: Any) -> tuple[list[dict], str]:
    if isinstance(data, dict):
        records = _valid_records(data.get("records") or data.get("data") or [])
        if not records and isinstance(data.get("summary"), dict) and isinstance(data["summary"].get("records"), list):
            records = _valid_records(data["summary"].get("records"))
        zone_label = data.get("zone_label") or data.get("zone_type_label") or "海河9分区"
    elif isinstance(data, list):
        records = _valid_records(data)
        zone_label = "海河9分区" if len(records) <= 12 else "海河流域面雨量分区"
    else:
        records = []
        zone_label = "海河9分区"
    records.sort(key=_rain_value, reverse=True)
    return records, zone_label


def _format_payload(mo, data: Any, time_label: str) -> str:
    records, zone_label = _normalize_records(data)
    if not records:
        return "今年以来暂未查询到有效累计降雨量数据。"

    values = [_rain_value(r) for r in records if _rain_value(r) >= 0]
    basin_avg = round(sum(values) / len(values), 1) if values else "-"
    max_record = records[0]
    min_record = records[-1]

    lines = [
        "## 今年累计降雨量\n\n",
        f"**统计时段**：{_safe_cell(mo, time_label)}（北京时）  \n",
        f"**统计范围**：海河流域  \n",
        f"**分区体系**：{_safe_cell(mo, zone_label)}  \n",
        f"**今年以来流域平均累计降雨量**：{basin_avg} mm  \n",
        f"**累计降雨量最大分区**：{_safe_cell(mo, _zone_name(max_record))}，"
        f"{_pick_number(max_record, 'avg_rainfall_mm', 'avg', 'average_rainfall_mm', 'mean')} mm  \n",
        f"**累计降雨量最小分区**：{_safe_cell(mo, _zone_name(min_record))}，"
        f"{_pick_number(min_record, 'avg_rainfall_mm', 'avg', 'average_rainfall_mm', 'mean')} mm\n",
    ]

    lines.append("\n### 今年以来各分区累计降雨量\n\n")
    lines.append("| 排名 | 分区 | 累计降雨量(mm) | 最大雨量(mm) |\n")
    lines.append("| :--- | :--- | :--- | :--- |\n")
    for idx, item in enumerate(records[:20], 1):
        lines.append(
            f"| {idx} | "
            f"{_safe_cell(mo, _zone_name(item))} | "
            f"{_pick_number(item, 'avg_rainfall_mm', 'avg', 'average_rainfall_mm', 'mean')} | "
            f"{_pick_number(item, 'max_rainfall_mm', 'max', 'maximum_rainfall_mm', 'maximum')} |\n"
        )

    return "".join(lines)


def install_year_to_date_rainfall_patch() -> bool:
    try:
        import message_orchestrator as mo
    except Exception as exc:
        print(f"[year_to_date_rainfall_patch] message_orchestrator 导入失败，跳过补丁：{exc}")
        return False

    if getattr(mo, _MODULE_MARKER, False):
        print("[year_to_date_rainfall_patch] 已安装过，无需重复安装")
        return True

    original = getattr(mo, "_try_basin_areal_rainfall_fast_path", None)
    if not callable(original):
        print("[year_to_date_rainfall_patch] 未找到面雨量快速路径，跳过补丁")
        return False

    async def patched_basin_areal_rainfall_fast_path(user_text: str, tools, messages, callbacks) -> bool:
        if not _should_use_year_to_date_rainfall_path(user_text):
            return await original(user_text, tools, messages, callbacks)

        tool = mo._find_tool(tools, "query_basin_areal_rainfall")
        if not tool:
            return await original(user_text, tools, messages, callbacks)

        time_range, time_label = _build_year_to_date_window()
        thinking_msg = await mo._show_thinking("🔍 正在查询今年以来累计降雨量，请稍候...")
        try:
            # FastMCP/Pydantic 会把缺省 int 参数补成 None，因此显式传整数 hours。
            # 后端工具在 time_range 存在时按 time_range 统计，hours 只用于通过参数校验。
            payload = {"zone_type": "9", "time_range": time_range, "hours": 24}
            result = await asyncio.wait_for(tool.ainvoke(payload), timeout=120)
            data = _unwrap_tool_result(result)
            text = _format_payload(mo, data, time_label)
            await mo._emit_fast_path_result(text, thinking_msg, messages, user_text)
            return True
        except asyncio.TimeoutError:
            await mo._emit_fast_path_result("⏱️ 今年累计降雨量查询超时，请稍后重试。", thinking_msg, messages, user_text)
            return True
        except Exception as exc:
            print(f"[year_to_date_rainfall_patch] 今年累计降雨量快速路径失败：{exc}")
            await mo._emit_fast_path_result("今年累计降雨量查询遇到异常，请稍后重试。", thinking_msg, messages, user_text)
            return True

    patched_basin_areal_rainfall_fast_path._year_to_date_rainfall_patch_installed = True
    patched_basin_areal_rainfall_fast_path._year_to_date_rainfall_original = original
    mo._try_basin_areal_rainfall_fast_path = patched_basin_areal_rainfall_fast_path
    setattr(mo, _MODULE_MARKER, True)
    print("[year_to_date_rainfall_patch] 已安装：今年累计降雨量快速路径")
    return True
