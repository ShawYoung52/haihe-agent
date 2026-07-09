"""降雨类自然语言快速路径。

原则：
1. 已经稳定的问题不改链路；
2. “今年累计降雨量”走专用 query_year_to_date_areal_rainfall；
3. “上个月面雨量”走专用 query_last_month_areal_rainfall；
4. “哪个子流域降雨最多”恢复使用原 query_basin_areal_rainfall，不再引用已删除的 query_period_areal_rainfall_9。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any


_MARKER_AREAL = "_rainfall_fast_paths_areal_installed"
_MARKER_STATION = "_rainfall_fast_paths_station_installed"


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


def _zone_name(row: dict, default: str = "未知分区") -> str:
    return str(row.get("zone_name") or row.get("zone_id") or row.get("name") or row.get("分区") or default)


def _rain_value(row: dict) -> float:
    return _to_float(_pick_number(row, "avg_rainfall_mm", "avg", "average_rainfall_mm", "mean"))


def _normalize_zone9(data: Any) -> tuple[list[dict], str, dict]:
    """只接受9分区形态结果；超过12条视为细分区，不展示。"""
    if isinstance(data, dict):
        if data.get("status") == "no_data":
            summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
            return [], "海河9分区", summary
        records = _valid_records(data.get("records") or data.get("data") or [])
        summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
        zone_label = data.get("zone_label") or data.get("zone_type_label") or "海河9分区"
    elif isinstance(data, list):
        records = _valid_records(data)
        summary = {}
        zone_label = "海河9分区" if len(records) <= 12 else "海河流域面雨量分区"
    else:
        records, summary, zone_label = [], {}, "海河9分区"

    records.sort(key=_rain_value, reverse=True)
    if len(records) > 12:
        return [], "海河9分区", summary
    return records, "海河9分区" if "9" in str(zone_label) or len(records) <= 12 else str(zone_label), summary


def _station_location(record: dict) -> str:
    parts = []
    for key in ("province", "city", "county", "cnty", "town"):
        value = str(record.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    return " ".join(parts) if parts else "-"


def _today_like_window(user_text: str) -> tuple[str, str]:
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
    return f"[{start:%Y%m%d%H%M%S},{end:%Y%m%d%H%M%S}]", f"{start:%Y-%m-%d %H:%M:%S} ~ {end:%Y-%m-%d %H:%M:%S}"


def _previous_month_range() -> tuple[str, str, str]:
    now = datetime.now()
    first_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end_prev = first_this_month - timedelta(seconds=1)
    start_prev = end_prev.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (
        f"[{start_prev:%Y%m%d%H%M%S},{end_prev:%Y%m%d%H%M%S}]",
        f"{start_prev:%Y年%m月%d日 %H:%M} ~ {end_prev:%Y年%m月%d日 %H:%M}",
        f"{start_prev:%Y年%m月}",
    )


def _year_to_date_window() -> tuple[str, str]:
    now = datetime.now()
    start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return f"[{start:%Y%m%d%H%M%S},{now:%Y%m%d%H%M%S}]", f"{start:%Y-%m-%d %H:%M:%S} ~ {now:%Y-%m-%d %H:%M:%S}"


def _is_last_month_areal(text: str) -> bool:
    t = text or ""
    return (
        any(k in t for k in ("上个月", "上月", "上一个月", "上一月", "上自然月", "上个自然月"))
        and any(k in t for k in ("面雨量", "分区雨量", "流域雨量", "河系雨量", "累计雨量", "降雨量", "雨量"))
        and not any(k in t for k in ("未来", "明天", "后天", "预报", "预计", "会不会", "会有"))
    )


def _is_subbasin_max(text: str) -> bool:
    t = text or ""
    return (
        any(k in t for k in ("子流域", "分区", "河系"))
        and any(k in t for k in ("雨量", "降雨", "降水", "降雨量", "降水量"))
        and any(k in t for k in ("最多", "最大", "最高", "最强", "第一", "排第一", "哪个"))
        and not any(k in t for k in (
            "上个月", "上月", "去年", "前年", "历史", "同期", "全年", "整年",
            "未来", "明天", "后天", "预报", "预计", "会不会", "会有",
            "自动站", "站点", "气象站", "雨量站",
        ))
    )


def _is_year_to_date(text: str) -> bool:
    t = text or ""
    return (
        any(k in t for k in ("今年", "本年", "今年以来", "年内", "今年到现在"))
        and any(k in t for k in ("累计", "总雨量", "累计雨量", "累计降雨", "累计降雨量", "累计降水", "累计降水量"))
        and any(k in t for k in ("雨量", "降雨", "降水", "降雨量", "降水量"))
        and not any(k in t for k in (
            "哪个", "最多", "最大", "最高", "第一", "排第一", "子流域", "分区", "河系",
            "自动站", "站点", "气象站", "雨量站", "历史", "同期", "去年", "上个月", "上月",
            "未来", "明天", "后天", "预报", "预计", "会不会", "会有",
        ))
    )


def _format_last_month(mo, data: Any, time_label: str, month: str) -> str:
    if isinstance(data, dict) and data.get("status") == "no_data":
        return data.get("message") or f"{month}暂无有效海河9分区面雨量数据。"
    records, zone_label, summary = _normalize_zone9(data)
    if not records:
        return f"{month}暂无有效海河9分区面雨量数据。"
    basin_avg = _pick_number(summary, "simple_mean_of_zone_rainfall_mm") if summary else "-"
    zone_total = _pick_number(summary, "total_of_zone_rainfall_mm") if summary else "-"
    lines = [
        f"## 海河流域上月面雨量对比（{month}）\n\n",
        f"**统计时段**：{_safe_cell(mo, time_label)}（北京时）  \n",
        f"**分区体系**：{_safe_cell(mo, zone_label)}  \n",
    ]
    if basin_avg != "-":
        lines.append(f"**上月流域累计面雨量**：{basin_avg} mm  \n")
    if zone_total != "-":
        lines.append(f"**9分区累计值合计**：{zone_total} mm\n")
    lines.append("\n| 排名 | 分区 | 累计面雨量(mm) | 最大面雨量(mm) |\n| :--- | :--- | :--- | :--- |\n")
    for idx, item in enumerate(records[:9], 1):
        lines.append(f"| {idx} | {_safe_cell(mo, _zone_name(item))} | {_pick_number(item, 'avg_rainfall_mm', 'avg', 'average_rainfall_mm', 'mean')} | {_pick_number(item, 'max_rainfall_mm', 'max', 'maximum_rainfall_mm', 'maximum')} |\n")
    max_zone = records[0]
    lines.append(f"\n**上月累计面雨量最大的分区**：{_safe_cell(mo, _zone_name(max_zone))}，累计面雨量 {_pick_number(max_zone, 'avg_rainfall_mm', 'avg', 'average_rainfall_mm', 'mean')} mm。\n")
    return "".join(lines)


def _format_subbasin_max(mo, data: Any, time_label: str) -> str:
    if isinstance(data, dict) and data.get("status") == "no_data":
        return data.get("message") or "当前时段暂无有效子流域面雨量数据。"
    records, zone_label, _ = _normalize_zone9(data)
    if not records:
        return "当前时段暂无有效子流域面雨量数据。"
    top = records[0]
    lines = [
        "## 子流域降雨最多结果\n\n",
        f"**统计时段**：{_safe_cell(mo, (data.get('time_range_readable') if isinstance(data, dict) else None) or time_label)}（北京时）  \n",
        f"**分区体系**：{_safe_cell(mo, zone_label)}  \n",
        f"**降雨最多的子流域**：{_safe_cell(mo, _zone_name(top, '未知子流域'))}  \n",
        f"**累计面雨量**：{_pick_number(top, 'avg_rainfall_mm', 'avg', 'average_rainfall_mm', 'mean')} mm  \n",
    ]
    max_point = _pick_number(top, "max_rainfall_mm", "max", "maximum_rainfall_mm", "maximum")
    if max_point != "-":
        lines.append(f"**该子流域内最大雨量**：{max_point} mm\n")
    lines.append("\n### 子流域/分区降雨排名\n\n| 排名 | 子流域/分区 | 累计面雨量(mm) | 最大雨量(mm) |\n| :--- | :--- | :--- | :--- |\n")
    for idx, item in enumerate(records[:9], 1):
        lines.append(f"| {idx} | {_safe_cell(mo, _zone_name(item, '未知子流域'))} | {_pick_number(item, 'avg_rainfall_mm', 'avg', 'average_rainfall_mm', 'mean')} | {_pick_number(item, 'max_rainfall_mm', 'max', 'maximum_rainfall_mm', 'maximum')} |\n")
    return "".join(lines)


def _format_year_to_date(mo, data: Any, time_label: str) -> str:
    if isinstance(data, dict) and data.get("status") == "no_data":
        return data.get("message") or "今年以来暂未查询到有效海河9分区累计降雨量数据。"
    records, zone_label, summary = _normalize_zone9(data)
    if not records:
        return "今年以来暂未查询到有效海河9分区累计降雨量数据。"
    values = [_rain_value(r) for r in records if _rain_value(r) >= 0]
    basin_avg = _pick_number(summary, "simple_mean_of_zone_rainfall_mm") if summary else "-"
    if basin_avg == "-":
        basin_avg = round(sum(values) / len(values), 2) if values else "-"
    zone_total = _pick_number(summary, "total_of_zone_rainfall_mm") if summary else "-"
    if zone_total == "-":
        zone_total = round(sum(values), 2) if values else "-"
    top, low = records[0], records[-1]
    lines = [
        "## 今年累计降雨量\n\n",
        f"**统计时段**：{_safe_cell(mo, (data.get('time_range_readable') if isinstance(data, dict) else None) or time_label)}（北京时）  \n",
        "**统计范围**：海河流域  \n",
        f"**分区体系**：{_safe_cell(mo, zone_label)}  \n",
        f"**今年以来流域累计面雨量**：{basin_avg} mm  \n",
        f"**9分区累计值合计**：{zone_total} mm  \n",
        f"**累计降雨量最大分区**：{_safe_cell(mo, _zone_name(top))}，{_pick_number(top, 'avg_rainfall_mm', 'avg', 'average_rainfall_mm', 'mean')} mm  \n",
        f"**累计降雨量最小分区**：{_safe_cell(mo, _zone_name(low))}，{_pick_number(low, 'avg_rainfall_mm', 'avg', 'average_rainfall_mm', 'mean')} mm\n",
        "\n### 今年以来海河9分区累计降雨量\n\n| 排名 | 分区 | 累计降雨量(mm) | 最大雨量(mm) |\n| :--- | :--- | :--- | :--- |\n",
    ]
    for idx, item in enumerate(records[:9], 1):
        lines.append(f"| {idx} | {_safe_cell(mo, _zone_name(item))} | {_pick_number(item, 'avg_rainfall_mm', 'avg', 'average_rainfall_mm', 'mean')} | {_pick_number(item, 'max_rainfall_mm', 'max', 'maximum_rainfall_mm', 'maximum')} |\n")
    return "".join(lines)


async def _call_areal_tool(mo, tools, time_range: str, use_last_month: bool = False) -> Any:
    if use_last_month:
        tool = mo._find_tool(tools, "query_last_month_areal_rainfall")
        if tool:
            return _unwrap_tool_result(await asyncio.wait_for(tool.ainvoke({"zone_type": "9"}), timeout=120))

    tool = mo._find_tool(tools, "query_basin_areal_rainfall")
    if tool:
        return _unwrap_tool_result(
            await asyncio.wait_for(
                tool.ainvoke({"zone_type": "9", "time_range": time_range, "hours": 24}),
                timeout=90,
            )
        )

    return {
        "status": "no_data",
        "zone_label": "海河9分区",
        "records": [],
        "message": "当前时段暂无有效子流域面雨量数据。",
    }


async def _call_year_to_date_tool(mo, tools, time_range: str) -> Any:
    tool = mo._find_tool(tools, "query_year_to_date_areal_rainfall")
    if tool:
        return _unwrap_tool_result(await asyncio.wait_for(tool.ainvoke({"zone_type": "9"}), timeout=120))
    return {
        "status": "no_data",
        "zone_label": "海河9分区",
        "records": [],
        "message": "今年以来暂未查询到有效海河9分区累计降雨量数据。",
    }


def _is_last_year_max_daily(text: str) -> bool:
    t = text or ""
    return (
        any(k in t for k in ("去年", "上一年", "上年", "上一个自然年", "去年全年"))
        and any(k in t for k in ("最大", "最高", "最多", "极大"))
        and (any(k in t for k in ("日降雨量", "日降水量", "单日降雨量", "单日降水量", "最大日雨量", "最大日降雨", "日雨量")) or ("日" in t and any(k in t for k in ("降雨", "降水", "雨量"))))
        and not any(k in t for k in ("未来", "明天", "后天", "预报", "预计"))
    )


def _is_historical_same_period(text: str) -> bool:
    t = text or ""
    return (
        any(k in t for k in ("历史同期", "同期平均", "历年同期", "多年同期", "历史平均", "常年同期"))
        and any(k in t for k in ("雨量", "降雨", "降水", "降雨量", "降水量"))
        and not any(k in t for k in ("面雨量", "分区", "子流域", "未来", "预报", "预计", "会不会", "会有"))
    )


def _is_max_station(text: str) -> bool:
    t = text or ""
    # 如果查询明确提到"自动站/站点"等，优先按站点维度处理，不再因为"子流域"等词排除
    has_station_keyword = any(k in t for k in ("自动站", "站点", "气象站", "监测站", "雨量站"))
    if not has_station_keyword:
        return (
            any(k in t for k in ("雨量", "降雨", "降水"))
            and any(k in t for k in ("最大", "最多", "最高", "第一", "排第一", "最大的是", "哪个"))
            and not any(k in t for k in ("面雨量", "分区", "子流域", "流域平均", "去年最大日降雨", "最大日降雨量", "去年", "前年", "上年", "上一年", "上个月", "上月", "上周", "历史", "全年", "整年"))
        )
    return (
        any(k in t for k in ("雨量", "降雨", "降水"))
        and any(k in t for k in ("最大", "最多", "最高", "第一", "排第一", "最大的是", "哪个"))
        and not any(k in t for k in ("面雨量", "流域平均", "去年最大日降雨", "最大日降雨量", "去年", "前年", "上年", "上一年", "上个月", "上月", "上周", "历史", "全年", "整年"))
    )


def _parse_year_count(text: str) -> int:
    for n in (30, 20, 15, 10, 5):
        if f"近{n}年" in (text or "") or f"{n}年" in (text or ""):
            return n
    return 10


def _reference_window(user_text: str) -> tuple[str, str, str]:
    time_range, label = _today_like_window(user_text)
    start, end = time_range.strip("[]").split(",")
    return start, end, label


def _format_last_year(mo, data: Any) -> str:
    if not isinstance(data, dict):
        return "去年最大日降雨量查询结果格式异常，请稍后重试。"
    if data.get("status") == "no_data":
        return data.get("message") or "去年海河流域暂无有效日降雨量数据。"
    year = data.get("year") or "去年"
    time_label = data.get("time_range_readable") or f"{year}年全年"
    max_record = data.get("max_record") or (data.get("summary") or {}).get("max_record") or {}
    records = [r for r in (data.get("records") or []) if isinstance(r, dict)]
    if not isinstance(max_record, dict) or not max_record:
        return f"{year}年海河流域暂无有效日降雨量数据。"
    lines = [
        f"## {year}年海河流域最大日降雨量\n\n",
        f"**统计时段**：{_safe_cell(mo, time_label)}（北京时）  \n",
        f"**最大日降雨量**：{_pick_number(max_record, 'daily_rainfall_mm', 'MAX_PRE_Time_0808', 'PRE_Time_0808')} mm  \n",
        f"**出现日期**：{_safe_cell(mo, max_record.get('date') or '-')}  \n",
        f"**出现站点**：{_safe_cell(mo, max_record.get('station_name') or '未知站点')}（站号：{_safe_cell(mo, max_record.get('station_id') or '-')}）  \n",
        f"**站点位置**：{_safe_cell(mo, _station_location(max_record))}\n",
    ]
    if len(records) > 1:
        lines.append("\n### 去年日降雨量排名前列站点\n\n| 排名 | 日期 | 站点 | 站号 | 日降雨量(mm) | 位置 |\n| :--- | :--- | :--- | :--- | :--- | :--- |\n")
        for idx, item in enumerate(records[:10], 1):
            lines.append(f"| {idx} | {_safe_cell(mo, item.get('date') or '-')} | {_safe_cell(mo, item.get('station_name') or '未知站点')} | {_safe_cell(mo, item.get('station_id') or '-')} | {_pick_number(item, 'daily_rainfall_mm', 'MAX_PRE_Time_0808', 'PRE_Time_0808')} | {_safe_cell(mo, _station_location(item))} |\n")
    return "".join(lines)


def _format_historical(mo, data: Any, fallback_label: str) -> str:
    if not isinstance(data, dict):
        return "历史同期平均降雨量查询结果格式异常，请稍后重试。"
    if data.get("status") == "no_data":
        return data.get("message") or "历史同期暂无有效自动站降雨量数据。"
    summary = data.get("summary") or {}
    max_year = summary.get("max_year") if isinstance(summary, dict) else None
    min_year = summary.get("min_year") if isinstance(summary, dict) else None
    records = [r for r in (data.get("yearly_records") or []) if isinstance(r, dict)]
    lines = [
        "## 历史同期平均降雨量\n\n",
        f"**参考时段**：{_safe_cell(mo, data.get('reference_time_range_readable') or fallback_label)}（北京时）  \n",
        f"**历史年份范围**：{_safe_cell(mo, data.get('historical_year_range') or '-')}  \n",
        f"**有效年份数**：{data.get('valid_year_count') or 0} 年  \n",
        f"**历史同期平均降雨量**：{_pick_number(data, 'historical_average_rainfall_mm')} mm\n",
    ]
    if isinstance(max_year, dict):
        lines.append(f"\n**同期雨量最大年份**：{_safe_cell(mo, max_year.get('year'))}年，平均降雨量 {_pick_number(max_year, 'average_rainfall_mm')} mm。\n")
    if isinstance(min_year, dict):
        lines.append(f"**同期雨量最小年份**：{_safe_cell(mo, min_year.get('year'))}年，平均降雨量 {_pick_number(min_year, 'average_rainfall_mm')} mm。\n")
    if records:
        lines.append("\n### 各年份同期平均降雨量\n\n| 年份 | 平均降雨量(mm) | 参与站点数 | 最大站 | 最大站雨量(mm) | 位置 |\n| :--- | :--- | :--- | :--- | :--- | :--- |\n")
        for item in records[:30]:
            max_station = item.get("max_station") if isinstance(item.get("max_station"), dict) else {}
            lines.append(f"| {_safe_cell(mo, item.get('year'))} | {_pick_number(item, 'average_rainfall_mm')} | {_pick_number(item, 'station_count')} | {_safe_cell(mo, max_station.get('station_name') or max_station.get('station_id') or '-')} | {_pick_number(max_station, 'rainfall_mm')} | {_safe_cell(mo, _station_location(max_station))} |\n")
    return "".join(lines)


def _collect_top_stations(data: dict) -> list[dict]:
    stations: list[dict] = []
    if isinstance(data.get("max_station"), dict):
        stations.append(data["max_station"])
    for level in data.get("level_analysis") or []:
        if isinstance(level, dict):
            stations.extend([s for s in (level.get("stations") or []) if isinstance(s, dict)])
    unique, seen = [], set()
    for s in stations:
        key = (str(s.get("station_id") or s.get("Station_Id_C") or s.get("name") or ""), str(s.get("rainfall") or s.get("rain") or ""))
        if key not in seen:
            seen.add(key)
            unique.append(s)
    unique.sort(key=lambda s: _to_float(s.get("rainfall") or s.get("rain") or s.get("value"), 0.0), reverse=True)
    return unique


def _format_max_station(mo, data: Any, time_label: str) -> str:
    if not isinstance(data, dict) or not isinstance(data.get("max_station"), dict):
        return "当前未查询到有效自动站降雨数据。"
    s = data["max_station"]
    lines = [
        "## 自动站最大雨量\n\n",
        f"**统计时段**：{_safe_cell(mo, data.get('time_range_readable') or time_label)}（北京时）  \n",
        f"**雨量最大的自动站**：{_safe_cell(mo, s.get('name') or s.get('station_name') or '未知站点')}（站号：{_safe_cell(mo, s.get('station_id') or s.get('Station_Id_C') or '-')}）  \n",
        f"**累计雨量**：{_to_float(s.get('rainfall') or s.get('rain') or s.get('value'), 0.0):.1f} mm  \n",
        f"**站点位置**：{_safe_cell(mo, _station_location(s))}  \n",
    ]
    if data.get("max_level"):
        lines.append(f"**降雨等级**：{_safe_cell(mo, data.get('max_level'))}  \n")
    if data.get("total_stations"):
        lines.append(f"**参与统计站点数**：{data.get('total_stations')} 个\n")
    top = _collect_top_stations(data)
    if len(top) > 1:
        lines.append("\n### 雨量排名前列自动站\n\n| 排名 | 站点 | 站号 | 累计雨量(mm) | 位置 |\n| :--- | :--- | :--- | :--- | :--- |\n")
        for idx, item in enumerate(top[:10], 1):
            lines.append(f"| {idx} | {_safe_cell(mo, item.get('name') or item.get('station_name') or '未知站点')} | {_safe_cell(mo, item.get('station_id') or item.get('Station_Id_C') or '-')} | {_to_float(item.get('rainfall') or item.get('rain') or item.get('value'), 0.0):.1f} | {_safe_cell(mo, _station_location(item))} |\n")
    return "".join(lines)


def install_all_fast_paths() -> None:
    try:
        import message_orchestrator as mo
    except Exception as exc:
        print(f"[rainfall_fast_paths] message_orchestrator 导入失败：{exc}")
        return

    if not getattr(mo, _MARKER_AREAL, False):
        original_areal = getattr(mo, "_try_basin_areal_rainfall_fast_path", None)
        if callable(original_areal):
            async def patched_areal(user_text: str, thinking_chain, tools, messages, callbacks) -> bool:
                reasoning = None
                try:
                    if _is_last_month_areal(user_text):
                        time_range, label, month = _previous_month_range()
                        reasoning = await mo._show_business_reasoning(
                            "查询上个月海河9分区面雨量",
                            ["面雨量数据"],
                            "将给出各分区面雨量排名与对比"
                        )
                        thinking_text = await mo.generate_fast_path_thinking(
                            thinking_chain, user_text,
                            "查询上个月海河9分区面雨量",
                            ["面雨量数据"]
                        )
                        if thinking_text:
                            await reasoning.line(thinking_text)
                        msg = await mo._show_thinking("🔍 正在查询上个月海河9分区面雨量数据，请稍候...")
                        data = await _call_areal_tool(mo, tools, time_range, use_last_month=True)
                        await mo._emit_fast_path_result(_format_last_month(mo, data, label, month), msg, messages, user_text, reasoning=reasoning)
                        return True
                    if _is_subbasin_max(user_text):
                        time_range, label = _today_like_window(user_text)
                        reasoning = await mo._show_business_reasoning(
                            "查询子流域降雨量排名",
                            ["面雨量数据"],
                            "将给出降雨最多的子流域及分区排名"
                        )
                        thinking_text = await mo.generate_fast_path_thinking(
                            thinking_chain, user_text,
                            "查询子流域降雨量排名",
                            ["面雨量数据"]
                        )
                        if thinking_text:
                            await reasoning.line(thinking_text)
                        msg = await mo._show_thinking("🔍 正在查询子流域降雨量，请稍候...")
                        data = await _call_areal_tool(mo, tools, time_range)
                        await mo._emit_fast_path_result(_format_subbasin_max(mo, data, label), msg, messages, user_text, reasoning=reasoning)
                        return True
                    if _is_year_to_date(user_text):
                        time_range, label = _year_to_date_window()
                        reasoning = await mo._show_business_reasoning(
                            "查询今年以来海河9分区累计降雨量",
                            ["面雨量数据"],
                            "将给出今年以来各分区累计降雨量排名"
                        )
                        thinking_text = await mo.generate_fast_path_thinking(
                            thinking_chain, user_text,
                            "查询今年以来海河9分区累计降雨量",
                            ["面雨量数据"]
                        )
                        if thinking_text:
                            await reasoning.line(thinking_text)
                        msg = await mo._show_thinking("🔍 正在查询今年以来海河9分区累计降雨量，请稍候...")
                        data = await _call_year_to_date_tool(mo, tools, time_range)
                        await mo._emit_fast_path_result(_format_year_to_date(mo, data, label), msg, messages, user_text, reasoning=reasoning)
                        return True
                except asyncio.TimeoutError:
                    await mo._emit_fast_path_result("⏱️ 降雨量查询超时，请稍后重试。", locals().get("msg", None), messages, user_text, reasoning=reasoning)
                    return True
                except Exception as exc:
                    print(f"[rainfall_fast_paths] 面雨量快速路径失败：{exc}")
                    await mo._emit_fast_path_result("降雨量查询遇到异常，请稍后重试。", locals().get("msg", None), messages, user_text, reasoning=reasoning)
                    return True
                finally:
                    if reasoning is not None:
                        await reasoning.close()
                return await original_areal(user_text, thinking_chain, tools, messages, callbacks)
            mo._try_basin_areal_rainfall_fast_path = patched_areal
            setattr(mo, _MARKER_AREAL, True)
            print("[rainfall_fast_paths] 已安装：面雨量/分区类快速路径")

    if not getattr(mo, _MARKER_STATION, False):
        original_station = getattr(mo, "_try_rainfall_analysis_fast_path", None)
        if callable(original_station):
            async def patched_station(user_text: str, thinking_chain, tools, messages, callbacks) -> bool:
                reasoning = None
                try:
                    if _is_last_year_max_daily(user_text):
                        tool = mo._find_tool(tools, "query_last_year_max_daily_rainfall")
                        if tool:
                            reasoning = await mo._show_business_reasoning(
                                "查询去年最大日降雨量",
                                ["日降雨量数据"],
                                "将给出去年海河流域最大日降雨量及站点信息"
                            )
                            thinking_text = await mo.generate_fast_path_thinking(
                                thinking_chain, user_text,
                                "查询去年最大日降雨量",
                                ["日降雨量数据"]
                            )
                            if thinking_text:
                                await reasoning.line(thinking_text)
                            msg = await mo._show_thinking("🔍 正在查询去年最大日降雨量，请稍候...")
                            data = _unwrap_tool_result(await asyncio.wait_for(tool.ainvoke({"top_n": 10, "allow_slow_fallback": False}), timeout=75))
                            await mo._emit_fast_path_result(_format_last_year(mo, data), msg, messages, user_text, reasoning=reasoning)
                            return True
                    if _is_historical_same_period(user_text):
                        tool = mo._find_tool(tools, "query_historical_same_period_avg_rainfall")
                        if tool:
                            start, end, label = _reference_window(user_text)
                            reasoning = await mo._show_business_reasoning(
                                "查询历史同期平均降雨量",
                                ["历史降雨量数据"],
                                "将给出历史同期平均降雨量及年际对比"
                            )
                            thinking_text = await mo.generate_fast_path_thinking(
                                thinking_chain, user_text,
                                "查询历史同期平均降雨量",
                                ["历史降雨量数据"]
                            )
                            if thinking_text:
                                await reasoning.line(thinking_text)
                            msg = await mo._show_thinking("🔍 正在查询历史同期平均降雨量，请稍候...")
                            data = _unwrap_tool_result(await asyncio.wait_for(tool.ainvoke({"reference_start_time": start, "reference_end_time": end, "years": _parse_year_count(user_text)}), timeout=90))
                            await mo._emit_fast_path_result(_format_historical(mo, data, label), msg, messages, user_text, reasoning=reasoning)
                            return True
                    if _is_max_station(user_text):
                        tool = mo._find_tool(tools, "local_analyze_rainfall_by_time") or mo._find_tool(tools, "analyze_rainfall_by_time")
                        if tool:
                            time_range, label = _today_like_window(user_text)
                            start, end = time_range.strip("[]").split(",")
                            reasoning = await mo._show_business_reasoning(
                                "查询自动站最大雨量",
                                ["实况降雨站点数据"],
                                "将给出雨量最大的自动站及排名信息"
                            )
                            thinking_text = await mo.generate_fast_path_thinking(
                                thinking_chain, user_text,
                                "查询自动站最大雨量",
                                ["实况降雨站点数据"]
                            )
                            if thinking_text:
                                await reasoning.line(thinking_text)
                            msg = await mo._show_thinking("🔍 正在查询自动站最大雨量，请稍候...")
                            data = _unwrap_tool_result(await asyncio.wait_for(tool.ainvoke({"time_str": end, "start_time": start, "end_time": end}), timeout=45))
                            await mo._emit_fast_path_result(_format_max_station(mo, data, label), msg, messages, user_text, reasoning=reasoning)
                            return True
                except asyncio.TimeoutError:
                    await mo._emit_fast_path_result("⏱️ 降雨分析查询超时，请稍后重试。", locals().get("msg", None), messages, user_text, reasoning=reasoning)
                    return True
                except Exception as exc:
                    print(f"[rainfall_fast_paths] 自动站/历史统计快速路径失败：{exc}")
                    await mo._emit_fast_path_result("降雨分析查询遇到异常，请稍后重试。", locals().get("msg", None), messages, user_text, reasoning=reasoning)
                    return True
                finally:
                    if reasoning is not None:
                        await reasoning.close()
                return await original_station(user_text, thinking_chain, tools, messages, callbacks)
            mo._try_rainfall_analysis_fast_path = patched_station
            setattr(mo, _MARKER_STATION, True)
            print("[rainfall_fast_paths] 已安装：自动站/历史统计类快速路径")
