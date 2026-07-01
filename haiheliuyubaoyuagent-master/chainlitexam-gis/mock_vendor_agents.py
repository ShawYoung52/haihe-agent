"""
外部合作方智能体调用层：当前全部为 mock，后续把对应函数改为 httpx 请求即可。
"""
from __future__ import annotations

import asyncio
from typing import Any


async def call_vendor_alpha_hydro_api(query: str) -> dict[str, Any]:
    """
    合作方 Alpha 水文智能体。对接真实接口时在此组装 URL/Headers/Body 并解析响应。
    """
    await asyncio.sleep(0.05)
    q = (query or "").strip()
    return {
        "vendor": "Alpha水文智能体",
        "skill_id": "vendor_alpha_water",
        "summary": "（demo）已对问题进行流域尺度水文形势粗判：关注降雨—径流响应与干支流汇流风险；接入真实接口后将给出对方模型输出与指标。",
        "indicators": {
            "basin_focus": "海河流域（示例）",
            "confidence": 0.82,
        },
        "disclaimer": "当前为本地模拟数据，非厂商生产结果。",
        "mock": True,
        "echo_query": q[:500],
    }


async def call_vendor_beta_emergency_api(query: str) -> dict[str, Any]:
    """
    合作方 Beta 应急智能体。对接真实接口时在此组装 HTTP 调用。
    """
    await asyncio.sleep(0.05)
    q = (query or "").strip()
    return {
        "vendor": "Beta应急智能体",
        "skill_id": "vendor_beta_emergency",
        "alerts": [
            {"level": "提示", "text": "（demo）建议复核重点河段巡查与物资预置。"},
        ],
        "actions": [
            "（demo）先确认受影响行政区划与分区清单是否完整",
            "（demo）再对接实时雨情滚动更新",
        ],
        "disclaimer": "当前为本地模拟数据，非厂商生产结果。",
        "mock": True,
        "echo_query": q[:500],
    }
