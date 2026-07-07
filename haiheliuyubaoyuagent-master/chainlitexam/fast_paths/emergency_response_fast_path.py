"""防汛应急响应判定快速路径修正。"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime as _dt
from pathlib import Path
from typing import Any

_MARKER = "_safe_emergency_response_fast_path_installed"
EMERGENCY_RESPONSE_TIMEOUT_SEC = 300
_DAYPART_HOURS = (
    ("凌晨", 2),
    ("早晨", 8),
    ("上午", 8),
    ("中午", 12),
    ("午后", 14),
    ("下午", 14),
    ("傍晚", 20),
    ("晚上", 20),
    ("夜间", 20),
)


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


def _time_label(times: str) -> str:
    try:
        return _dt.strptime(times, "%Y%m%d%H%M%S").strftime("%Y年%m月%d日%H时%M分")
    except Exception:
        return str(times or "所选时次")


def _apply_daypart_hour(user_text: str, times: str) -> str:
    if not times or len(times) != 14 or not times.isdigit():
        return times
    for keyword, hour in _DAYPART_HOURS:
        if keyword in (user_text or ""):
            return f"{times[:8]}{hour:02d}0000"
    return times


def _friendly_error(data: dict[str, Any], times: str) -> str:
    raw_err = str(data.get("error") or data.get("debug_reason") or "")
    message = str(data.get("message") or "").strip()
    if "no record" in raw_err.lower() or "无记录" in raw_err or "暂无数据" in raw_err or "未查询到" in message:
        return f"未查询到 {_time_label(times)} 的应急响应判定数据，可能该时段无有效分钟降水资料。"
    return message or "当前无法获取应急响应判定数据，请稍后重试。"


def _format_response(data: dict[str, Any], times: str) -> str:
    if data.get("status") == "error" or data.get("error"):
        return _friendly_error(data, times)

    triggered = data.get("triggered") or data.get("reached")
    level = data.get("level")
    msg = data.get("message", "")
    evidence = data.get("evidence", {}) if isinstance(data.get("evidence"), dict) else {}
    time_label = _time_label(times)

    if triggered:
        lines = [f"## 防汛应急响应判定：已触发 {level} 级响应\n"]
        lines.append(f"截至 **{time_label}**，海河流域实况雨量已达到 **{level} 级**防汛应急响应启动条件。\n")
    else:
        lines = ["## 防汛应急响应判定：未触发\n"]
        lines.append(f"截至 **{time_label}**，海河流域实况雨量**未达到**防汛应急响应启动条件。\n")

    if msg:
        lines.append(f"**判定结论**：{msg}\n")

    total = evidence.get("total_station_count")
    qualified = evidence.get("qualified_station_count") or evidence.get("qualified_adjacent_station_count")
    ratio = evidence.get("ratio")
    threshold = evidence.get("threshold_mm")
    window = evidence.get("window_hours")

    if total is not None:
        lines.append("### 判定依据\n")
        lines.append(f"- 参与判定国家站数：{total}")
        if qualified is not None:
            lines.append(f"- 达标站点数：{qualified}")
        if ratio is not None:
            try:
                lines.append(f"- 达标站点占比：{float(ratio):.1%}")
            except Exception:
                lines.append(f"- 达标站点占比：{ratio}")
        if threshold is not None and window is not None:
            lines.append(f"- 触发阈值：最近 {window} 小时累计降水 ≥ {threshold} mm")
        lines.append("")

    lines.append("\n数据来源：天擎分钟降水实况")
    return "\n".join(lines)


def _ensure_mcp_module_path() -> None:
    root = Path(__file__).resolve().parents[2]
    mcp_dir = root / "haihe-weather-analyzer-mcp"
    if mcp_dir.is_dir():
        path = str(mcp_dir)
        if path not in sys.path:
            sys.path.insert(0, path)


def _call_local_emergency_response(times: str) -> dict[str, Any]:
    _ensure_mcp_module_path()
    from haihe_mcp_tools import evaluate_emergency_response_core

    return evaluate_emergency_response_core(
        basin_codes="HHLY",
        times=times,
    )


async def _query_emergency_response_locally(times: str) -> dict[str, Any]:
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_call_local_emergency_response, times),
            timeout=EMERGENCY_RESPONSE_TIMEOUT_SEC,
        )
        if isinstance(result, dict):
            result.setdefault("status", "ok")
            return result
        return {"status": "error", "message": "应急响应判定结果格式异常。", "raw": str(result)[:500]}
    except asyncio.TimeoutError:
        return {"status": "error", "message": "应急响应判定查询超时，请稍后重试。"}
    except Exception as exc:
        text = str(exc)
        print(f"[emergency_response_fast_path] 本地判定失败：{text}")
        return {"status": "error", "error": text[:500], "message": "当前无法获取应急响应判定数据，请稍后重试。"}


def install_emergency_response_fast_path() -> bool:
    try:
        import message_orchestrator as mo
    except Exception as exc:
        print(f"[emergency_response_fast_path] 导入失败：{exc}")
        return False
    if getattr(mo, _MARKER, False):
        return True

    async def patched(user_text: str, tools, messages, callbacks) -> bool:
        matched, raw_times = mo._extract_emergency_response_time(user_text)
        if not matched:
            return False
        times = _apply_daypart_hour(user_text, raw_times)

        print(f"\n=== 本地防汛应急响应快速路径：raw_times={raw_times}, times={times} ===")
        thinking_msg = await mo._show_thinking("🔍 正在查询防汛应急响应判定结果，请稍候...")
        data = await _query_emergency_response_locally(times)
        await mo._emit_fast_path_result(_format_response(data, times), thinking_msg, messages, user_text)
        return True

    mo._try_emergency_response_fast_path = patched
    setattr(mo, _MARKER, True)
    print("[emergency_response_fast_path] 已安装：本地应急响应快速路径")
    return True
