"""水位类自然语言快速路径。

新增问题：哪些河流水位会不会上涨明显？

实现原则：
- 不新增后端接口；
- 复用已有 query_water_level；
- 只补充“泛问多条河流水位上涨趋势”的前端路由；
- 不影响原有“某条河水位情况”快速路径。
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any


_MODULE_MARKER = "_water_level_rise_fast_path_installed"

_DEFAULT_RIVERS = [
    "大清河", "子牙河", "永定河", "北三河", "漳卫南运河",
    "徒骇马颊河", "黑龙港", "滦河", "潮白河", "蓟运河", "海河干流",
]

_OBVIOUS_RISE_RATE_MPH = 0.05
_SLIGHT_RISE_RATE_MPH = 0.0


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


def _should_use_water_level_rise_path(text: str) -> bool:
    t = text or ""
    if "水位" not in t:
        return False
    river_scope_words = ("哪些河", "哪些河流", "哪几条河", "哪些站", "哪些水位站", "河流")
    rise_words = ("上涨", "涨水", "涨幅", "涨势", "上升", "升高", "明显")
    question_words = ("哪些", "哪几", "会不会", "是否", "有没有", "明显")
    return any(k in t for k in river_scope_words) and any(k in t for k in rise_words) and any(k in t for k in question_words)


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text in {"-", "—", "--", "None", "null"}:
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def _get_rate(record: dict) -> float | None:
    for key in (
        "涨率", "rate", "rise_rate", "rise_rate_m_per_h", "change_rate", "rate_m_per_h",
        "water_level_change_rate", "水位涨率", "涨幅", "change_m_per_h",
    ):
        val = _parse_float(record.get(key))
        if val is not None:
            return val
    return None


def _get_water_level(record: dict) -> Any:
    for key in ("water_level_m", "水位(m)", "水位", "Z", "z", "level", "current_level"):
        if record.get(key) not in (None, "", "None"):
            return record.get(key)
    return "-"


def _get_warning_level(record: dict) -> Any:
    for key in ("warning_level_m", "警戒水位(m)", "警戒水位", "warn_level", "warning_level"):
        if record.get(key) not in (None, "", "None"):
            return record.get(key)
    return "-"


def _get_station_name(record: dict) -> str:
    return str(record.get("station_name") or record.get("站点名称") or record.get("Station_Name") or record.get("name") or "-")


def _get_time(record: dict) -> str:
    return str(record.get("time") or record.get("更新时间") or record.get("Datetime") or record.get("update_time") or "-")


def _rise_judgment(rate: float | None) -> str:
    if rate is None:
        return "缺少涨率"
    if rate >= _OBVIOUS_RISE_RATE_MPH:
        return "明显上涨"
    if rate > _SLIGHT_RISE_RATE_MPH:
        return "小幅上涨"
    return "暂无上涨"


def _collect_records(river_name: str, data: Any) -> list[dict]:
    if not isinstance(data, dict) or data.get("error"):
        return []
    records = data.get("records") or data.get("data") or []
    if not isinstance(records, list):
        return []
    out = []
    for item in records:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["river_name"] = river_name
        row["rise_rate_m_per_h"] = _get_rate(row)
        row["rise_judgment"] = _rise_judgment(row["rise_rate_m_per_h"])
        out.append(row)
    return out


async def _query_one_river(mo, tool, river_name: str, sem: asyncio.Semaphore) -> tuple[str, list[dict], str | None]:
    async with sem:
        try:
            result = await asyncio.wait_for(
                tool.ainvoke({"river_name": river_name, "data_type": "river"}),
                timeout=20,
            )
            data = _unwrap_tool_result(result)
            return river_name, _collect_records(river_name, data), None
        except Exception as exc:
            print(f"[water_level_fast_paths] {river_name} 水位查询失败：{exc}")
            return river_name, [], str(exc)[:120]


def _format_water_level_rise_result(mo, river_rows: list[dict], failed_rivers: list[str]) -> str:
    from datetime import datetime as _dt

    now_label = _dt.now().strftime("%Y年%m月%d日%H:%M")
    all_rows = [r for r in river_rows if isinstance(r, dict)]
    if not all_rows:
        return "当前未查询到可用于判断河流水位上涨趋势的有效水位数据。"

    obvious = [r for r in all_rows if (r.get("rise_rate_m_per_h") is not None and r["rise_rate_m_per_h"] >= _OBVIOUS_RISE_RATE_MPH)]
    slight = [r for r in all_rows if (r.get("rise_rate_m_per_h") is not None and _SLIGHT_RISE_RATE_MPH < r["rise_rate_m_per_h"] < _OBVIOUS_RISE_RATE_MPH)]
    obvious.sort(key=lambda r: float(r.get("rise_rate_m_per_h") or 0.0), reverse=True)
    slight.sort(key=lambda r: float(r.get("rise_rate_m_per_h") or 0.0), reverse=True)

    obvious_rivers = sorted({str(r.get("river_name") or "") for r in obvious if str(r.get("river_name") or "")})
    slight_rivers = sorted({str(r.get("river_name") or "") for r in slight if str(r.get("river_name") or "")})

    lines = ["## 河流水位上涨趋势判断\n\n"]
    lines.append(f"**统计时间**：截至{now_label}（北京时）  \n")
    lines.append("**判断口径**：站点涨率 ≥ 0.05 m/h 记为“明显上涨”；0~0.05 m/h 记为“小幅上涨”。\n\n")

    if obvious_rivers:
        lines.append(f"**结论**：当前有 **{len(obvious_rivers)} 条河流** 的相关水位站出现明显上涨：{_safe_cell(mo, '、'.join(obvious_rivers))}。\n\n")
    elif slight_rivers:
        lines.append(f"**结论**：当前暂未发现明显上涨河流，但有 **{len(slight_rivers)} 条河流** 存在小幅上涨：{_safe_cell(mo, '、'.join(slight_rivers))}。\n\n")
    else:
        lines.append("**结论**：当前暂未发现相关河流水位明显上涨。\n\n")

    focus_rows = obvious if obvious else slight
    if focus_rows:
        title = "明显上涨站点" if obvious else "小幅上涨站点"
        lines.append(f"### {title}\n\n")
        lines.append("| 河流 | 站点 | 当前水位(m) | 警戒水位(m) | 涨率(m/h) | 判断 | 更新时间 |\n")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n")
        for r in focus_rows[:20]:
            rate = r.get("rise_rate_m_per_h")
            rate_text = f"{rate:.3f}" if isinstance(rate, (int, float)) else "-"
            lines.append(
                f"| {_safe_cell(mo, r.get('river_name') or '-')} | "
                f"{_safe_cell(mo, _get_station_name(r))} | "
                f"{_safe_cell(mo, _get_water_level(r))} | "
                f"{_safe_cell(mo, _get_warning_level(r))} | "
                f"{rate_text} | "
                f"{_safe_cell(mo, r.get('rise_judgment') or '-')} | "
                f"{_safe_cell(mo, _get_time(r))} |\n"
            )

    if failed_rivers:
        lines.append("\n**未获取到有效水位数据的河流**：")
        lines.append(_safe_cell(mo, "、".join(failed_rivers[:10])))
        if len(failed_rivers) > 10:
            lines.append(f" 等 {len(failed_rivers)} 条")
        lines.append("。\n")

    return "".join(lines)


def install_water_level_fast_paths() -> bool:
    try:
        import message_orchestrator as mo
    except Exception as exc:
        print(f"[water_level_fast_paths] message_orchestrator 导入失败：{exc}")
        return False

    if getattr(mo, _MODULE_MARKER, False):
        print("[water_level_fast_paths] 已安装过，无需重复安装")
        return True

    original = getattr(mo, "_try_water_level_fast_path", None)
    if not callable(original):
        print("[water_level_fast_paths] 未找到原水位快速路径，跳过")
        return False

    async def patched_water_level_fast_path(user_text: str, tools, messages, callbacks) -> bool:
        if not _should_use_water_level_rise_path(user_text):
            return await original(user_text, tools, messages, callbacks)

        tool = mo._find_tool(tools, "query_water_level")
        if not tool:
            return await original(user_text, tools, messages, callbacks)

        thinking_msg = await mo._show_thinking("🔍 正在查询主要河流水位上涨趋势，请稍候...")
        try:
            rivers = getattr(mo, "_KNOWN_WATER_LEVEL_RIVERS", None) or _DEFAULT_RIVERS
            sem = asyncio.Semaphore(4)
            results = await asyncio.gather(*[_query_one_river(mo, tool, river, sem) for river in rivers])
            all_rows: list[dict] = []
            failed: list[str] = []
            for river, rows, err in results:
                if rows:
                    all_rows.extend(rows)
                else:
                    failed.append(river)
            text = _format_water_level_rise_result(mo, all_rows, failed)
            await mo._emit_fast_path_result(text, thinking_msg, messages, user_text)
            return True
        except asyncio.TimeoutError:
            await mo._emit_fast_path_result("⏱️ 水位上涨趋势查询超时，请稍后重试。", thinking_msg, messages, user_text)
            return True
        except Exception as exc:
            print(f"[water_level_fast_paths] 水位上涨趋势快速路径失败：{exc}")
            await mo._emit_fast_path_result("水位上涨趋势查询遇到异常，请稍后重试。", thinking_msg, messages, user_text)
            return True

    mo._try_water_level_fast_path = patched_water_level_fast_path
    setattr(mo, _MODULE_MARKER, True)
    print("[water_level_fast_paths] 已安装：河流水位上涨趋势快速路径")
    return True
