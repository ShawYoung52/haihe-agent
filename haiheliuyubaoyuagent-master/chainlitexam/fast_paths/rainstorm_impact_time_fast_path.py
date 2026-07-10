"""暴雨影响河系日期解析修正。

修正问题：用户明确说“2026年6月30日暴雨会影响哪些河系”时，原快速路径没有正确解析绝对日期，
导致按最近24小时查询。
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from typing import Any

_MARKER = "_rainstorm_impact_time_fast_path_installed"


def _need_affected_river_network_by_rainfall(user_text: str) -> bool:
    if not user_text:
        return False
    text = user_text.strip()
    core_keywords = ("暴雨影响", "暴雨会影", "暴雨波及", "暴雨涉及", "暴雨下")
    if not any(k in text for k in core_keywords):
        return False
    river_keywords = ("河系", "河流", "河网", "水系", "河道", "哪些河")
    return any(k in text for k in river_keywords)


def _parse_absolute_day_window(text: str) -> tuple[str, str, str] | None:
    """解析明确日期，返回 time_str/start_time/end_time。

    对“2026年6月30日”按北京时自然日 00:00~24:00 统计，time_str 使用次日08时，
    与后端原有 -32h/-8h 口径兼容，但这里显式传 start/end，避免跑到默认最近24小时。
    """
    t = text or ""
    patterns = [
        r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日",
        r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})",
    ]
    for pat in patterns:
        m = re.search(pat, t)
        if not m:
            continue
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            start_dt = datetime(y, mo, d, 0, 0, 0)
            end_dt = start_dt + timedelta(days=1)
            ref_dt = end_dt + timedelta(hours=8)
            if start_dt > datetime.now():
                return None
            return (
                ref_dt.strftime("%Y%m%d%H%M%S"),
                start_dt.strftime("%Y%m%d%H%M%S"),
                end_dt.strftime("%Y%m%d%H%M%S"),
            )
        except Exception:
            continue
    return None


def _parse_relative_day_window(text: str) -> tuple[str, str, str] | None:
    t = text or ""
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if "前天" in t:
        start_dt = today - timedelta(days=2)
    elif "昨天" in t or "昨日" in t:
        start_dt = today - timedelta(days=1)
    else:
        return None
    end_dt = start_dt + timedelta(days=1)
    ref_dt = end_dt + timedelta(hours=8)
    return (
        ref_dt.strftime("%Y%m%d%H%M%S"),
        start_dt.strftime("%Y%m%d%H%M%S"),
        end_dt.strftime("%Y%m%d%H%M%S"),
    )


def _parse_rainstorm_impact_window(text: str) -> tuple[str, str, str] | None:
    return _parse_absolute_day_window(text) or _parse_relative_day_window(text)


def _unwrap_tool_result(result: Any) -> Any:
    data = result
    if data is None:
        return {}
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


def _build_brief(mo, result_data: dict, user_text: str) -> str:
    build = getattr(mo, "_build_affected_river_network_brief", None)
    if callable(build):
        return build(result_data, user_text)
    affected_rivers = result_data.get("affected_rivers", []) or []
    time_range = result_data.get("time_range_readable", "")
    threshold = result_data.get("rainfall_threshold_mm", 50.0)
    if not affected_rivers:
        return f"统计时段 {time_range} 内，降雨量未达到 {threshold}mm 暴雨阈值，未识别到受暴雨显著影响的河系。"
    lines = [
        f"统计时段 {time_range} 内，降雨量≥{threshold}mm 的暴雨区域共影响 **{len(affected_rivers)} 条河流**。",
        "",
        "**受影响河系列表（已全部列出）**",
        "",
        "| 序号 | 河流名称 |",
        "| :--- | :--- |",
    ]
    for idx, name in enumerate(sorted(affected_rivers), 1):
        lines.append(f"| {idx} | {name} |")
    return "\n".join(lines)


def install_rainstorm_impact_time_fast_path() -> bool:
    try:
        import chainlit as cl
        from langchain_core.messages import AIMessage, HumanMessage
        import message_orchestrator as mo
    except Exception as exc:
        print(f"[rainstorm_impact_time_fast_path] 导入失败：{exc}")
        return False

    if getattr(mo, _MARKER, False):
        return True

    async def patched(user_text: str, thinking_chain, tools, messages, callbacks) -> bool:
        if not _need_affected_river_network_by_rainfall(user_text):
            return False
        tool = mo._find_tool(tools, "get_affected_river_network_by_rainfall")
        if not tool:
            return False

        window = _parse_rainstorm_impact_window(user_text)
        if window:
            time_str, start_time, end_time = window
        else:
            # 没有明确日期时保留原行为：最近24小时；未来词交给通用流程。
            if any(k in user_text for k in ("明天", "明日", "后天", "未来", "今后", "接下来")):
                return False
            now = datetime.now()
            end_time = now.strftime("%Y%m%d%H%M%S")
            start_time = (now - timedelta(hours=24)).strftime("%Y%m%d%H%M%S")
            time_str = end_time

        reasoning = await mo._show_business_reasoning(
            "分析暴雨影响河系并绘制专题图",
            ["降雨实况数据", "河网水系数据"],
            "将绘制暴雨影响河系专题图并给出文字分析"
        )
        thinking_text = await mo.generate_fast_path_thinking(
            thinking_chain, user_text,
            "分析暴雨影响河系并绘制专题图",
            ["降雨实况数据", "河网水系数据"]
        )
        if thinking_text:
            await reasoning.line(thinking_text)
        await reasoning.stage("📡 查询数据", "正在分析暴雨影响河系并绘制专题图...")

        try:
            result = await tool.ainvoke({
                "time_str": time_str,
                "start_time": start_time,
                "end_time": end_time,
                "rainfall_threshold_mm": 50.0,
                "include_background": True,
            })
            result_data = _unwrap_tool_result(result)
            if not isinstance(result_data, dict):
                raise ValueError(f"工具返回格式异常：{type(result_data)}")

            affected_rivers = result_data.get("affected_rivers", []) or []
            segments = result_data.get("segments", []) or []
            stations = []
            heavy_rain_levels = {"暴雨", "大暴雨", "特大暴雨"}
            raw_stations = result_data.get("stations", [])
            if isinstance(raw_stations, list):
                for s in raw_stations:
                    if isinstance(s, dict):
                        level = str(s.get("level", "暴雨")).strip()
                        if level in heavy_rain_levels:
                            stations.append({
                                "lon": s.get("lon"),
                                "lat": s.get("lat"),
                                "rainfall": s.get("rainfall"),
                                "level": level,
                                "name": s.get("name", ""),
                            })

            print(
                f"[暴雨影响河系快路径] time={time_str}, window={start_time}~{end_time}, "
                f"受影响河流={len(affected_rivers)}, 河段={len(segments)}, 暴雨站点={len(stations)}"
            )

            if segments or stations:
                admin_observation = None
                if segments:
                    admin_observation = await callbacks["build_admin_overlay_for_plot"](tools, segments)
                await callbacks["render_and_send_plot"](
                    segments,
                    title_suffix=result_data.get("time_range_readable", f"{start_time}~{end_time}"),
                    admin_raw_result=admin_observation,
                    highlight_rivers=affected_rivers,
                    stations=stations,
                )

            brief = _build_brief(mo, result_data, user_text)
            brief = callbacks["append_followup_if_needed"](brief, user_text)
            await mo._maybe_close_reasoning(reasoning)
            await callbacks["stream_text_to_message"](brief)

            messages.append(HumanMessage(content=user_text))
            messages.append(AIMessage(content=brief))
            cl.user_session.set("messages", messages)
            return True
        except Exception as exc:
            print(f"[rainstorm_impact_time_fast_path] 失败：{exc}")
            return False
        finally:
            if reasoning is not None:
                await reasoning.close()

    mo._try_affected_river_network_by_rainfall_fast_path = patched
    setattr(mo, _MARKER, True)
    print("[rainstorm_impact_time_fast_path] 已安装：暴雨影响河系日期解析修正")
    return True
