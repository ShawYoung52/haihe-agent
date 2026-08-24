"""
将「LangChain Skills」目录（skills/*/SKILL.md）对应的合作方能力，暴露为 LangChain Tool，
供主模型按需调用。无厂商接口时使用 mock_vendor_agents。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import httpx

from langchain_core.tools import tool

from mock_vendor_agents import (
    call_vendor_alpha_hydro_api,
    call_vendor_beta_emergency_api,
    call_vendor_shortterm_api,
)

logger = logging.getLogger(__name__)


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


@tool
async def invoke_partner_skill_shortterm(query: str, history: str = "[]") -> str:
    """
    调用短临预报智能体（Skill：vendor_shortterm，见 skills/vendor_shortterm/SKILL.md）。
    当用户询问风廓线、低空急流、短时强降水、雷暴、未来0-6小时天气等短临问题时使用。
    参数 query：用户问题原文（中文）。
    参数 history：可选历史消息，JSON 字符串格式 [{"role":"user"/"assistant","content":"..."}]。
    返回：JSON 字符串，含 full_text（完整回答文本）、content_blocks（结构化块，含chart/table/image）、charts/tables/images 等。
    """
    hist = json.loads(history) if isinstance(history, str) and history.strip() else []
    data = await call_vendor_shortterm_api(query, hist)
    return json.dumps(data, ensure_ascii=False)


TIANHE_QA_API_URL = os.getenv("TIANHE_QA_API_URL", "http://10.226.188.156:8001/api/qa")
# 连接 5s / 响应 120s（对接文档建议）。固定 QA 是整句固定问题，跨用户重复提问，
# 加 300s TTL 缓存减少重复打远程接口（项目已有 rolling_forecast/POI 缓存先例）。
TIANHE_QA_TIMEOUT = (5, 120)
TIANHE_QA_CACHE_TTL = int(os.getenv("TIANHE_QA_CACHE_TTL", "300"))
# 对接文档允许连接失败或明确临时服务错误最多重试 2 次。这里是两次重试前的
# 递增等待；测试可改成 (0, 0)，生产不做无等待重放。
TIANHE_QA_RETRY_DELAYS = (0.2, 0.5)

_TIANHE_ERR_EMPTY = "问题不能为空。"
_TIANHE_ERR_CONNECT = "天河问答服务连接超时，请稍后重试或换一种问法。"
_TIANHE_ERR_UNAVAILABLE = "天河问答服务暂时不可用，请稍后重试。"
_TIANHE_ERR_FORMAT = "天河问答服务返回格式异常，请稍后重试。"
TIANHE_UNAVAILABLE_TEXT = _TIANHE_ERR_UNAVAILABLE

# 工具级失败文案集合（单一事实源）：供观测过滤、测试和日志识别。天河目录问题无论成功、
# HTTP 200 降级还是工具级失败都由 orchestrator 强制收口，不回退本地智能体。
# 200 降级文案（对接文档 9.4，如“智能体服务暂时不可用”）由天河 API 作为 answer 返回，
# 不在此集合并保持原样透传。
TIANHE_ERROR_TEXTS = frozenset({
    _TIANHE_ERR_EMPTY,
    _TIANHE_ERR_CONNECT,
    _TIANHE_ERR_UNAVAILABLE,
    _TIANHE_ERR_FORMAT,
})

_tianhe_cache: dict[str, tuple[float, str]] = {}
_tianhe_client: httpx.AsyncClient | None = None


def _get_tianhe_client() -> httpx.AsyncClient:
    """懒加载共享 AsyncClient。httpx 顶层 post 是同步函数，await 会抛 TypeError，
    必须用 AsyncClient 的异步 post；共享 client 也复用连接避免每次握手。"""
    global _tianhe_client
    if _tianhe_client is None:
        _tianhe_client = httpx.AsyncClient(timeout=TIANHE_QA_TIMEOUT)
    return _tianhe_client


async def call_tianhe_qa_api(query: str) -> str:
    """真实调用天河平台问答接口（POST /api/qa），返回 answer 字符串。

    天河 Fixed QA 是整句精确匹配；本函数只做 HTTP 调用与解析，不判断命中。
    失败不抛异常，返回中文提示；上层对天河目录问题直接展示，不交给本地智能体代答。
    """
    q = (query or "").strip()
    if not q:
        return _TIANHE_ERR_EMPTY

    hit = _tianhe_cache.get(q)
    if hit and (time.time() - hit[0]) < TIANHE_QA_CACHE_TTL:
        return hit[1]

    resp = None
    last_request_error: httpx.RequestError | None = None
    for attempt in range(len(TIANHE_QA_RETRY_DELAYS) + 1):
        try:
            resp = await _get_tianhe_client().post(
                TIANHE_QA_API_URL,
                json={"question": q, "history": [], "stream": False},
            )
            last_request_error = None
        except httpx.RequestError as exc:
            last_request_error = exc
            resp = None

        retryable_status = resp is not None and resp.status_code in {502, 503, 504}
        # 只重试建立连接阶段的临时失败。ReadTimeout/WriteError 等异常可能发生在
        # 服务端已经收到请求之后，重复提交既会放大等待时间，也可能产生重复任务。
        retryable_error = isinstance(
            last_request_error, (httpx.ConnectError, httpx.ConnectTimeout)
        )
        if (retryable_error or retryable_status) and attempt < len(TIANHE_QA_RETRY_DELAYS):
            delay = TIANHE_QA_RETRY_DELAYS[attempt]
            logger.warning(
                "天河问答接口临时失败，准备第 %s 次重试（原因=%s）",
                attempt + 1,
                type(last_request_error).__name__ if last_request_error else resp.status_code,
            )
            if delay > 0:
                await asyncio.sleep(delay)
            continue
        break

    if last_request_error is not None:
        if isinstance(last_request_error, httpx.ConnectTimeout):
            logger.warning("天河问答接口连接超时（query=%s）", q[:50])
            return _TIANHE_ERR_CONNECT
        logger.warning("天河问答接口调用失败：%s", type(last_request_error).__name__)
        return _TIANHE_ERR_UNAVAILABLE

    if resp is None or resp.status_code >= 400:
        logger.warning("天河问答接口返回非成功状态码：%s", getattr(resp, "status_code", "unknown"))
        return _TIANHE_ERR_UNAVAILABLE

    try:
        data = resp.json()
    except ValueError:
        logger.warning("天河问答接口响应非 JSON")
        return _TIANHE_ERR_FORMAT

    answer = data.get("answer") if isinstance(data, dict) else None
    if not isinstance(answer, str) or not answer.strip():
        logger.warning("天河问答接口响应缺 answer 字段")
        return _TIANHE_ERR_FORMAT

    # 200 但降级正文（如"智能体服务暂时不可用"）原样透传，不自动重试
    _tianhe_cache[q] = (time.time(), answer)
    return answer


@tool
async def query_tianhe_fixed_qa(query: str) -> str:
    """调用天河平台 Fixed QA 固定问答接口，获取模板化回答。

    适用于天河已配置固定问答目录的问题（如：
    "今天雨下了多长时间"
    "全市现在下了多少雨"
    "市区现在气温和风的实况"
    "暴雨天气的防范建议"
    等），命中后由天河返回标准回答。Fixed QA 是整句精确匹配
    （去空白/去句末标点规范化），不要自行改写或提炼 query。
    参数 query：用户问题原文（中文）。
    返回：天河生成的完整回答正文（可能含 Markdown 表格）；失败返回中文提示，
    上层应直接展示且不得改用本地智能体代答。
    """
    return await call_tianhe_qa_api(query)


def build_external_skill_tools():
    """与 MCP 工具列表合并：bind_tools(mcp_tools + build_external_skill_tools())"""
    return [
        route_partner_skill,
        invoke_partner_skill_alpha_hydro,
        invoke_partner_skill_beta_emergency,
        invoke_partner_skill_shortterm,
        query_tianhe_fixed_qa,
    ]
