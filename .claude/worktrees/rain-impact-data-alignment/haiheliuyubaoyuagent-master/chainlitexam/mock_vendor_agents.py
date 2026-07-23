"""
外部合作方智能体调用层：当前全部为 mock，后续把对应函数改为 httpx 请求即可。
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx


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


async def call_vendor_shortterm_api(query: str, history: list | None = None) -> dict[str, Any]:
    """
    短临预报智能体（真实 HTTP 接口，非 mock）。
    调用 10.226.107.134:12582/chat_completions，聚合 SSE 流式结果。
    """
    import uuid

    api_url = os.getenv("SHORTTERM_API_URL", "http://10.226.107.134:12582/chat_completions")
    sid = str(uuid.uuid4())
    payload = {
        "message_id": str(uuid.uuid4()),
        "session_id": sid,
        "user_id": "user_001",
        "content": query,
        "history": history or [],
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", api_url, json=payload) as resp:
                resp.raise_for_status()

                full_text_parts = []
                content_blocks = []
                chart_blocks = []
                table_blocks = []
                image_blocks = []
                error_msg = None
                total_tokens = 0

                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type", "")
                    if event_type == "delta":
                        block = event.get("block", {})
                        block_type = block.get("blockType", "")
                        data = block.get("data", "")
                        if block_type == "text" and isinstance(data, str):
                            full_text_parts.append(data)
                            content_blocks.append({"blockType": "text", "data": data})
                        elif block_type == "chart" and isinstance(data, dict):
                            chart_blocks.append(data)
                            content_blocks.append({"blockType": "chart", "data": data})
                        elif block_type == "table" and isinstance(data, dict):
                            table_blocks.append(data)
                            content_blocks.append({"blockType": "table", "data": data})
                        elif block_type == "image" and isinstance(data, list):
                            image_blocks.extend(data)
                            content_blocks.append({"blockType": "image", "data": data})
                    elif event_type == "end":
                        full_data = event.get("full", {})
                        total_tokens = full_data.get("totalTokens", 0)
                        if full_data.get("contentBlocks"):
                            content_blocks = full_data["contentBlocks"]
                        break
                    elif event_type == "error":
                        error_msg = event.get("error", "短临服务返回错误")
                        break

                if error_msg:
                    return {"error": error_msg, "session_id": sid, "vendor": "短临智能体", "skill_id": "vendor_shortterm"}
                return {
                    "vendor": "短临智能体",
                    "skill_id": "vendor_shortterm",
                    "session_id": sid,
                    "full_text": "".join(full_text_parts),
                    "content_blocks": content_blocks,
                    "charts": chart_blocks,
                    "tables": table_blocks,
                    "images": image_blocks,
                    "total_tokens": total_tokens,
                }

    except httpx.TimeoutException:
        return {"error": "短临服务请求超时，请稍后重试", "session_id": sid, "vendor": "短临智能体", "skill_id": "vendor_shortterm"}
    except httpx.ConnectError:
        return {"error": "无法连接短临服务，请检查网络", "session_id": sid, "vendor": "短临智能体", "skill_id": "vendor_shortterm"}
    except Exception as e:
        return {"error": f"短临服务调用失败: {str(e)}", "session_id": sid, "vendor": "短临智能体", "skill_id": "vendor_shortterm"}
