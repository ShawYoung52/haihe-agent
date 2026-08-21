"""山洪、地质灾害、中小河流洪水风险快速路径。"""
from __future__ import annotations

import asyncio
import json
from typing import Any

_MARKER = "_risk_warning_fast_path_installed"


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


def _detect_risk_kind(text: str) -> str | None:
    t = text or ""
    if "中小河流" in t or "河流洪水" in t:
        return "river"
    if "山洪" in t:
        return "mountain"
    if "地质灾害" in t or "滑坡" in t or "崩塌" in t or "泥石流" in t:
        return "geologic"
    return None


def _is_risk_question(text: str) -> bool:
    return _detect_risk_kind(text) is not None and any(k in text for k in ("风险", "注意", "需注意", "有没有", "哪些", "区域", "滑坡", "洪水"))


def _risk_title(kind: str, user_text: str) -> str:
    if kind == "river":
        return "中小河流洪水风险"
    if kind == "mountain":
        return "山洪风险"
    if "滑坡" in (user_text or ""):
        return "山区滑坡风险"
    return "地质灾害风险"


def _risk_action(kind: str, user_text: str) -> str:
    if kind == "river":
        return "关注中小河流水位上涨、沟道来水和沿河低洼区域。"
    if kind == "mountain":
        return "关注山洪沟道、山区村镇、临水道路和低洼地带。"
    if "滑坡" in (user_text or ""):
        return "关注山区边坡、道路切坡、沟谷两侧和地质灾害隐患点。"
    return "关注地质灾害隐患点、山区道路、边坡和沟谷地带。"


def _val(v: Any) -> str:
    if v in (None, "", "None", "null"):
        return "-"
    return str(v)


def _count_key(kv: tuple[Any, Any]) -> int:
    """隐患点总数排序键；值非法时按 0 处理（跨进程数据，单个脏值不能击穿整个回答）。"""
    try:
        return -int(kv[1])
    except (TypeError, ValueError):
        return 0


def _format_records(mo, records: list[dict]) -> str:
    if not records:
        return ""
    lines = ["\n| 区域 | 风险等级 | 时间/说明 |\n| :--- | :--- | :--- |\n"]
    shown = 0
    for row in records:
        if not isinstance(row, dict):
            continue
        area = _val(row.get("area"))
        # 优先用 MCP 端归一好的 level_norm（"5"→"一级"），与本次风险等级统计表一致
        level = _val(row.get("level_norm") or row.get("level"))
        desc = _val(row.get("description") or row.get("time"))
        if area == "-" and level == "-" and desc == "-":
            continue
        lines.append(f"| {_clean(mo, area)} | {_clean(mo, level)} | {_clean(mo, desc)} |\n")
        shown += 1
        if shown >= 10:
            break
    return "".join(lines) if shown else ""


def _format(mo, data: Any, user_text: str, kind: str) -> str:
    title = _risk_title(kind, user_text)
    if not isinstance(data, dict):
        return f"{title}查询结果格式异常。"
    if data.get("status") != "ok":
        return data.get("message") or f"{title}查询失败。"

    areas = [str(x).strip() for x in data.get("areas") or [] if str(x).strip()]
    records = data.get("records") if isinstance(data.get("records"), list) else []
    risk_count = int(data.get("risk_count") or 0)
    count = int(data.get("count") or 0)
    message = data.get("message") or ""

    lines = [f"## {title}\n\n"]
    if risk_count <= 0:
        if count <= 0:
            lines.append(f"当前未查询到{title}数据。\n")
        else:
            lines.append(f"当前未发现明显{title}。\n")
    else:
        if areas:
            lines.append("**需关注区域**：" + "、".join(_clean(mo, x) for x in areas[:20]) + "。\n")
        else:
            lines.append(_clean(mo, message or f"当前查询到 {risk_count} 条{title}记录，请关注详情。") + "\n")
        detail = _format_records(mo, records)
        if detail:
            lines.append(detail)

    # 逐区县×逐等级风险统计（MCP 端 2026-08-21 灾害点匹配产出）
    county_risk = data.get("county_risk_summary") if isinstance(data.get("county_risk_summary"), list) else []
    if county_risk:
        lines.append("\n**本次风险等级统计**\n")
        rows = ["| 区县 | 风险等级 | 数量 |", "| :--- | :--- | ---: |"]
        for item in county_risk:
            if not isinstance(item, dict):
                continue
            rows.append(
                f"| {_clean(mo, item.get('county'))} | {_clean(mo, item.get('level'))} "
                f"| {_clean(mo, item.get('count'))} |"
            )
        lines.append("\n".join(rows) + "\n")
    # 各区县隐患点总数（静态表全量，如"冀州区 257 个"）
    county_totals = data.get("county_totals") if isinstance(data.get("county_totals"), dict) else {}
    if county_totals:
        ordered = sorted(county_totals.items(), key=_count_key)
        total_line = "、".join(f"{_clean(mo, c)} {n} 个" for c, n in ordered[:10])
        lines.append(f"\n**隐患点总数**：{total_line}。\n")

    # 逐级防范建议优先（代码确定性生成，逐字采用）；仅当本次确有风险记录时展示，
    # 避免"本次无风险"的回答被四级文案刷屏；缺省时退回按风险类型的笼统建议。
    level_advice = data.get("level_advice") if isinstance(data.get("level_advice"), list) else []
    if level_advice and county_risk:
        lines.append("\n**防范建议（按风险等级）**\n")
        for item in level_advice:
            if not isinstance(item, dict):
                continue
            lines.append(f"- **{_clean(mo, item.get('level'))}**：{_clean(mo, item.get('advice'))}")
        lines.append("")
    else:
        lines.append(f"\n**建议**：{_risk_action(kind, user_text)}")
    return "".join(lines)


def install_risk_warning_fast_paths() -> bool:
    try:
        import message_orchestrator as mo
    except Exception as exc:
        print(f"[risk_warning_fast_paths] 导入失败：{exc}")
        return False
    if getattr(mo, _MARKER, False):
        return True
    original = getattr(mo, "_try_general_weather_fast_path", None)
    if not callable(original):
        return False

    async def patched(user_text: str, thinking_chain, tools, messages, callbacks) -> bool:
        if not _is_risk_question(user_text):
            return await original(user_text, thinking_chain, tools, messages, callbacks)
        kind = _detect_risk_kind(user_text)
        if not kind:
            return await original(user_text, thinking_chain, tools, messages, callbacks)
        tool = mo._find_tool(tools, "query_risk_warning")
        if not tool:
            return await original(user_text, thinking_chain, tools, messages, callbacks)
        title = _risk_title(kind, user_text)

        reasoning = await mo._show_business_reasoning(
            f"查询{title}",
            ["风险预警数据"],
            "将给出风险区域、等级与防范建议"
        )
        thinking_text = await mo.generate_fast_path_thinking(
            thinking_chain, user_text,
            f"查询{title}",
            ["风险预警数据"]
        )
        if thinking_text:
            await reasoning.line(thinking_text)
        await reasoning.stage("📡 查询数据", f"正在查询{title}...")

        try:
            data = _unwrap(await asyncio.wait_for(tool.ainvoke({"risk_kind": kind}), timeout=60))
            await mo._emit_fast_path_result(_format(mo, data, user_text, kind), messages, user_text, reasoning=reasoning)
            return True
        except asyncio.TimeoutError:
            await mo._emit_fast_path_result(f"⏱️ {title}查询超时，请稍后重试。", messages, user_text, reasoning=reasoning)
            return True
        except Exception as exc:
            print(f"[risk_warning_fast_paths] 查询失败：{exc}")
            await mo._emit_fast_path_result(f"{title}查询遇到异常，请稍后重试。", messages, user_text, reasoning=reasoning)
            return True
        finally:
            if reasoning is not None:
                await reasoning.close()

    mo._try_general_weather_fast_path = patched
    setattr(mo, _MARKER, True)
    print("[risk_warning_fast_paths] 已安装：风险预警快速路径")
    return True
