"""山洪风险自然语言快速路径。

问题：有没有山洪风险？
后端：query_flash_flood_risk，基于 TJ_MDRWTFLD_REGI_DSR 海河区域山洪动态阈值计算产品。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any


_MODULE_MARKER = "_flash_flood_risk_fast_path_installed"


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


def _is_flash_flood_risk_question(text: str) -> bool:
    t = text or ""
    if not any(k in t for k in ("山洪", "山洪灾害", "山洪风险", "山洪预警")):
        return False
    return any(k in t for k in ("有没有", "是否", "会不会", "风险", "危险", "预警", "大不大", "明显", "可能"))


def _format_file_name(file_info: Any) -> str:
    if not isinstance(file_info, dict):
        return "-"
    return str(file_info.get("file_name") or file_info.get("file_path") or "-")


def _format_flash_flood_result(mo, data: Any) -> str:
    if not isinstance(data, dict):
        return "山洪风险查询结果格式异常，无法生成回答。"

    if data.get("status") == "no_data":
        return data.get("message") or "当前未查询到山洪动态阈值产品，暂无法判断山洪风险。"

    risk_status = str(data.get("risk_status") or "unknown")
    risk_level = str(data.get("risk_level") or "无法判定")
    risk_count = data.get("risk_count") or 0
    risk_areas = data.get("risk_areas") or []
    file_count = data.get("file_count") or len(data.get("files") or [])
    latest = data.get("latest_file") if isinstance(data.get("latest_file"), dict) else {}
    product_name = data.get("product_name") or "海河区域山洪动态阈值计算产品"
    time_label = data.get("time_range_readable") or "最近时段"

    if risk_status == "risk_found":
        conclusion = f"**结论**：当前查询到山洪风险信号，风险等级/类型为 **{_safe_cell(mo, risk_level)}**。"
    elif risk_status == "no_risk_found":
        conclusion = "**结论**：当前产品内容未识别到明显山洪风险信号。"
    elif risk_status == "product_found_unparsed":
        conclusion = "**结论**：已查询到山洪动态阈值产品，但当前仅能获取产品文件元数据，无法直接判定是否有山洪风险。"
    else:
        conclusion = "**结论**：当前山洪风险状态无法判定。"

    lines = ["## 山洪风险查询\n\n"]
    lines.append(f"**查询时段**：{_safe_cell(mo, time_label)}（北京时）  \n")
    lines.append(f"**产品名称**：{_safe_cell(mo, product_name)}  \n")
    lines.append(f"**产品数量**：{file_count} 个  \n")
    lines.append(conclusion + "\n")

    if risk_count:
        lines.append(f"\n**识别到的风险记录数**：{risk_count} 条\n")
    if isinstance(risk_areas, list) and risk_areas:
        lines.append(f"\n**涉及区域**：{_safe_cell(mo, '、'.join(str(x) for x in risk_areas[:20]))}\n")

    lines.append("\n### 最新产品\n\n")
    lines.append("| 项目 | 内容 |\n| :--- | :--- |\n")
    lines.append(f"| 文件名 | {_safe_cell(mo, _format_file_name(latest))} |\n")
    if latest.get("data_time"):
        lines.append(f"| 产品时次 | {_safe_cell(mo, latest.get('data_time'))} |\n")
    if latest.get("file_size") not in (None, ""):
        lines.append(f"| 文件大小 | {_safe_cell(mo, latest.get('file_size'))} |\n")

    if risk_status == "product_found_unparsed":
        lines.append("\n**说明**：当前接口返回的是服务产品文件信息。若需要直接给出“有/无风险”，需要确认产品文件在服务器上的可读路径或产品内容字段格式。\n")
    return "".join(lines)


def install_flash_flood_fast_paths() -> bool:
    try:
        import message_orchestrator as mo
    except Exception as exc:
        print(f"[flash_flood_fast_paths] message_orchestrator 导入失败：{exc}")
        return False

    if getattr(mo, _MODULE_MARKER, False):
        print("[flash_flood_fast_paths] 已安装过，无需重复安装")
        return True

    original_general = getattr(mo, "_try_general_weather_fast_path", None)
    if not callable(original_general):
        print("[flash_flood_fast_paths] 未找到通用天气快速路径，跳过")
        return False

    async def patched_general_weather_fast_path(user_text: str, tools, messages, callbacks) -> bool:
        if not _is_flash_flood_risk_question(user_text):
            return await original_general(user_text, tools, messages, callbacks)

        tool = mo._find_tool(tools, "query_flash_flood_risk")
        if not tool:
            return await original_general(user_text, tools, messages, callbacks)

        thinking_msg = await mo._show_thinking("🔍 正在查询山洪动态阈值产品，请稍候...")
        try:
            data = _unwrap_tool_result(await asyncio.wait_for(tool.ainvoke({"hours": 6}), timeout=45))
            text = _format_flash_flood_result(mo, data)
            await mo._emit_fast_path_result(text, thinking_msg, messages, user_text)
            return True
        except asyncio.TimeoutError:
            await mo._emit_fast_path_result("⏱️ 山洪风险查询超时，请稍后重试。", thinking_msg, messages, user_text)
            return True
        except Exception as exc:
            print(f"[flash_flood_fast_paths] 山洪风险快速路径失败：{exc}")
            await mo._emit_fast_path_result("山洪风险查询遇到异常，请稍后重试。", thinking_msg, messages, user_text)
            return True

    mo._try_general_weather_fast_path = patched_general_weather_fast_path
    setattr(mo, _MODULE_MARKER, True)
    print("[flash_flood_fast_paths] 已安装：山洪风险快速路径")
    return True
