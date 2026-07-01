"""
将「LangChain Skills」目录（skills/*/SKILL.md）对应的合作方能力，暴露为 LangChain Tool，
供主模型按需调用。无厂商接口时使用 mock_vendor_agents。
"""
from __future__ import annotations

import json

from langchain_core.tools import tool

from mock_vendor_agents import call_vendor_alpha_hydro_api, call_vendor_beta_emergency_api


def _auto_route_vendor(query: str) -> str:
    """
    轻量路由（demo）：尽量稳定、可解释。
    返回值固定为：alpha / beta
    """
    q = (query or "").strip().lower()

    # 显式点名优先
    if any(k in q for k in ["alpha", "阿尔法", "合作方a", "a方"]):
        return "alpha"
    if any(k in q for k in ["beta", "贝塔", "合作方b", "b方"]):
        return "beta"

    # 语义关键词兜底：水文 vs 应急
    hydro_kw = ["水文", "径流", "汇流", "水位", "洪水", "调度", "来水", "行洪"]
    emer_kw = ["应急", "联动", "处置", "预案", "会商", "预警", "响应", "转移", "物资", "值守"]
    if any(k in q for k in hydro_kw) and not any(k in q for k in emer_kw):
        return "alpha"
    if any(k in q for k in emer_kw) and not any(k in q for k in hydro_kw):
        return "beta"

    # 默认：水文（可按你业务偏好调整）
    return "alpha"


@tool
async def route_partner_skill(vendor: str, query: str) -> str:
    """
    通用合作方路由工具（demo）。

    参数：
    - vendor: "auto" | "alpha" | "beta"
    - query: 用户原问或提炼后的任务描述（中文）

    返回：JSON 字符串，包含：
    - routed_vendor: "alpha" | "beta"
    - skill_id: 对应 skill id
    - vendor: 对方智能体名称
    - mock/disclaimer 等字段（demo）
    """
    v = (vendor or "").strip().lower()
    if v in {"auto", "自动", "智能"}:
        v = _auto_route_vendor(query)

    if v in {"alpha", "a"}:
        data = await call_vendor_alpha_hydro_api(query)
        data["routed_vendor"] = "alpha"
        return json.dumps(data, ensure_ascii=False)

    if v in {"beta", "b"}:
        data = await call_vendor_beta_emergency_api(query)
        data["routed_vendor"] = "beta"
        return json.dumps(data, ensure_ascii=False)

    return json.dumps(
        {
            "error": "unknown_vendor",
            "message": "vendor 仅支持 auto/alpha/beta",
            "routed_vendor": None,
            "mock": True,
        },
        ensure_ascii=False,
    )


@tool
async def invoke_partner_skill_alpha_hydro(query: str) -> str:
    """
    调用合作方 Alpha 的水文分析智能体（Skill：vendor_alpha_water，见 skills/vendor_alpha_water/SKILL.md）。
    当用户提到 Alpha/阿尔法/合作方A水文、或明确要求第三方水文专项分析时使用。
    参数 query：用户问题原文或你提炼后的任务描述（中文）。
    返回：JSON 字符串（含 mock 标记时须向用户说明为演示数据）。
    """
    data = await call_vendor_alpha_hydro_api(query)
    return json.dumps(data, ensure_ascii=False)


@tool
async def invoke_partner_skill_beta_emergency(query: str) -> str:
    """
    调用合作方 Beta 的应急联动辅助智能体（Skill：vendor_beta_emergency，见 skills/vendor_beta_emergency/SKILL.md）。
    当用户提到 Beta/贝塔/合作方B应急、或明确要求第三方应急处置/联动分析时使用。
    参数 query：用户问题原文或你提炼后的任务描述（中文）。
    返回：JSON 字符串（含 mock 标记时须向用户说明为演示数据）。
    """
    data = await call_vendor_beta_emergency_api(query)
    return json.dumps(data, ensure_ascii=False)


def build_external_skill_tools():
    """与 MCP 工具列表合并：bind_tools(mcp_tools + build_external_skill_tools())"""
    return [route_partner_skill, invoke_partner_skill_alpha_hydro, invoke_partner_skill_beta_emergency]
