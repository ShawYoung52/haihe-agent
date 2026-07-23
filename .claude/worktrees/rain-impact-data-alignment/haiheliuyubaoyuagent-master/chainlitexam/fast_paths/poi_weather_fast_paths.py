"""POI 点最近观测站实况快速路径。"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

_MARKER = "_poi_weather_fast_path_installed"


def _unwrap(result: Any) -> Any:
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


def _clean(mo, value: Any) -> str:
    fn = getattr(mo, "_clean_table_cell", None)
    if callable(fn):
        return fn(value)
    return "" if value is None else str(value).replace("|", "｜").strip()


def _is_poi_weather_question(text: str) -> bool:
    t = text or ""
    if any(k in t for k in ("未来", "预报", "明天", "后天", "一周")):
        return False
    # 必须有明确的 POI/观测站意图，避免"今天天气怎么样"这类普通区域预报被误判
    poi_indicators = ("观测站", "观测值", "最近站", "附近", "周边", "实况")
    return any(k in t for k in poi_indicators) and len(t.strip()) >= 4


def _keyword(text: str) -> str:
    t = (text or "").strip()
    for word in ("最近的观测站", "最近观测站", "观测站的值", "观测值", "天气", "实况", "附近", "周边", "是多少", "怎么样"):
        t = t.replace(word, "")
    t = t.replace("的", "").replace("？", "").replace("?", "").replace("。", "").strip()
    return t


def _val(v: Any) -> str:
    if v in (None, "", "None"):
        return "-"
    if isinstance(v, (int, float)):
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return str(v)


def _display_time(data: dict) -> str:
    # 后端优先返回北京时间；若旧后端未返回，则前端兜成北京时间展示。
    bjt = data.get("observation_time_beijing")
    if bjt:
        return str(bjt)
    raw = str(data.get("observation_time") or data.get("query_time") or "").strip()
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S"):
        try:
            return (datetime.strptime(raw, pattern) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
    return raw or "-"


def _format(mo, data: Any) -> str:
    if not isinstance(data, dict):
        return "POI 最近观测站实况查询结果格式异常。"
    if data.get("status") != "ok":
        # 正常用户界面不暴露后端调试信息；详细原因看 MCP 后端日志。
        return data.get("message") or "未查询到该点最近观测站实况。"

    poi = data.get("poi") if isinstance(data.get("poi"), dict) else {}
    sta = data.get("nearest_station") if isinstance(data.get("nearest_station"), dict) else {}
    obs = data.get("observation") if isinstance(data.get("observation"), dict) else {}
    title = data.get("keyword") or "该点"

    lines = [f"## {_clean(mo, title)}天气实况\n\n"]
    if poi.get("longitude") is not None and poi.get("latitude") is not None:
        lines.append(f"**定位点经纬度**：{_val(poi.get('longitude'))}，{_val(poi.get('latitude'))}  \n")
    lines.append(
        f"**最近观测站**：{_clean(mo, sta.get('station_name') or '-')}"
        f"（站号：{_clean(mo, sta.get('station_id') or '-')}，距离约 {_val(sta.get('distance_km'))} km）  \n"
    )
    lines.append(f"**观测时间**：{_clean(mo, _display_time(data))}（北京时间）\n\n")

    if obs:
        lines.append("| 要素 | 最近观测站实况值 |\n| :--- | :--- |\n")
        for key, value in obs.items():
            lines.append(f"| {_clean(mo, key)} | {_clean(mo, _val(value))} |\n")
    else:
        lines.append("当前最近观测站未返回可展示的实况要素值。\n")
    lines.append("\n**说明**：以上为该点附近最近观测站实况，可代表该位置附近天气情况。")
    return "".join(lines)


def install_poi_weather_fast_paths() -> bool:
    try:
        import message_orchestrator as mo
    except Exception as exc:
        print(f"[poi_weather_fast_paths] 导入失败：{exc}")
        return False
    if getattr(mo, _MARKER, False):
        return True
    original = getattr(mo, "_try_general_weather_fast_path", None)
    if not callable(original):
        return False

    async def patched(user_text: str, thinking_chain, tools, messages, callbacks) -> bool:
        if not _is_poi_weather_question(user_text):
            return await original(user_text, thinking_chain, tools, messages, callbacks)
        kw = _keyword(user_text)
        if not kw or kw in {"天气", "实况", "观测站"}:
            return await original(user_text, thinking_chain, tools, messages, callbacks)
        tool = mo._find_tool(tools, "query_poi_nearest_observation")
        if not tool:
            return await original(user_text, thinking_chain, tools, messages, callbacks)

        reasoning = await mo._show_business_reasoning(
            "查询 POI 最近观测站实况",
            ["POI 最近观测站实况数据"],
            "将给出该点最近观测站的实况天气信息"
        )
        thinking_text = await mo.generate_fast_path_thinking(
            thinking_chain, user_text,
            "查询 POI 最近观测站实况",
            ["POI 最近观测站实况数据"]
        )
        if thinking_text:
            await reasoning.line(thinking_text)
        await reasoning.stage("📡 查询数据", f"正在查询{kw}附近最近观测站实况...")

        try:
            data = _unwrap(await asyncio.wait_for(tool.ainvoke({"keyword": kw}), timeout=60))
            await mo._emit_fast_path_result(_format(mo, data), messages, user_text, reasoning=reasoning)
            return True
        except asyncio.TimeoutError:
            await mo._emit_fast_path_result("⏱️ POI 最近观测站实况查询超时，请稍后重试。", messages, user_text, reasoning=reasoning)
            return True
        except Exception as exc:
            print(f"[poi_weather_fast_paths] 查询失败：{exc}")
            await mo._emit_fast_path_result("POI 最近观测站实况查询遇到异常，请稍后重试。", messages, user_text, reasoning=reasoning)
            return True
        finally:
            if reasoning is not None:
                await reasoning.close()

    mo._try_general_weather_fast_path = patched
    setattr(mo, _MARKER, True)
    print("[poi_weather_fast_paths] 已安装：POI最近观测站实况快速路径")
    return True
