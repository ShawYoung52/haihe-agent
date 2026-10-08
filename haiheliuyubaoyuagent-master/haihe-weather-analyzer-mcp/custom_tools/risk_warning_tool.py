"""风险预警查询 MCP 工具（天津灾害风险清单版，2026-10-08）。

数据源（甲方 2026-09-29《天津灾害风险清单接口说明》）：
    GET http://10.226.107.130:8001/conclusion/risk-lists?start_time=YYYYMMDDHHMMSS

返回聚合清单（只覆盖天津）：
- township_risks：按乡镇聚合（乡镇名 / 最高风险等级 / 主灾种×数量）；
- industry_risks：按行业聚合；
- key_objects：按重点对象类别聚合；
- call_advice：固定叫应建议（无风险时也返回，不能作"是否有风险"判据）。

三态：HTTP 层失败 → error/None；可达且三清单全空 → 本次无风险（ok，空记录）；
可达且有记录 → ok。旧 findDataListByConfig（逐隐患点 id/经纬度）接口 2026-10-08 下线，
id 直连/haversine 隐患点匹配随之移除——新接口无 id 无坐标，区县由 township 名解析。
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from typing import Any

import requests
from fastmcp import FastMCP

from custom_tools._ttl_cache import make_ttl_cache
import time_source

logger = logging.getLogger(__name__)

RISK_LISTS_ROUTE = "/conclusion/risk-lists"
DEFAULT_RISK_LISTS_BASE = "http://10.226.107.130:8001"

BASE_ENV_KEYS = (
    "RISK_LISTS_API_BASE",
    "RISK_LISTS_BASE",
    "RISK_WARN_BASE",
    "RISK_WARN_BASE_URL",
    "HHFW_API_BASE",
    "HHFW_BASE",
    "HAIHE_RISK_BASE",
    "HAIHE_RISK_WARN_BASE",
)

RISK_CONFIGS: dict[str, dict[str, Any]] = {
    "river": {"label": "中小河流洪水风险", "question": "哪些区域需注意中小河流洪水？"},
    "mountain": {"label": "山洪风险", "question": "有没有山洪风险？"},
    "geologic": {"label": "地质灾害风险", "question": "有没有地质灾害风险？"},
}

RISK_ALIASES = {
    "river": "river",
    "middle_small_river": "river",
    "中小河流": "river",
    "中小河流洪水": "river",
    "河流洪水": "river",
    "mountain": "mountain",
    "flash_flood": "mountain",
    "山洪": "mountain",
    "山洪风险": "mountain",
    "geologic": "geologic",
    "geology": "geologic",
    "landslide": "geologic",
    "地质灾害": "geologic",
    "滑坡": "geologic",
    "崩塌": "geologic",
    "泥石流": "geologic",
}

# 风险清单接口灾种标签 ↔ 工具内部 kind/key（渲染层 categories key 保持 dzzh/sh/zxhl 不变）。
_RISK_LIST_HAZARD_TO_KEY = {"地灾": "dzzh", "山洪": "sh", "中小河流洪水": "zxhl"}
_RISK_LIST_KIND_LABELS: dict[str, tuple[str, ...]] = {
    "geologic": ("地灾",),
    "mountain": ("山洪",),
    "river": ("中小河流洪水",),
}
HAZARD_KIND_TO_KEY = {"geologic": "dzzh", "mountain": "sh", "river": "zxhl"}

# 等级归一（接口返回 低/中/高/极高）：极高=一级（最重）…低=四级（最轻），
# 与渲染层 _LEVEL_SEVERITY_ORDER 口径一致。
_RISK_LIST_LEVEL_MAP = {"极高": "一级", "高": "二级", "中": "三级", "低": "四级"}

# query_region_risk_levels 返回的 risk_levels 字典里，该灾种"该时次暂无数据"的哨兵值
# （新接口下不再出现：清单三数组全空即"本次无风险"；保留常量供渲染层兼容）。
RISK_LEVELS_NO_DATA = "no_data"
# 风险清单接口仅覆盖天津：所问区域不在天津时的哨兵值（渲染层隐藏"本次风险等级"列）。
RISK_LEVELS_NO_COVERAGE = "no_coverage"

# township 前缀（区县，含新旧名）→ 规范区县名。新接口 township 形如
# "蓟县官庄镇"（旧县名）/"宝坻区牛道口镇"。覆盖天津 16 区。
_TOWNSHIP_COUNTY_ALIASES: dict[str, str] = {
    "蓟县": "蓟州区", "蓟州": "蓟州区", "蓟州区": "蓟州区",
    "宝坻": "宝坻区", "宝坻区": "宝坻区",
    "宁河": "宁河区", "宁河区": "宁河区",
    "静海": "静海区", "静海区": "静海区",
    "武清": "武清区", "武清区": "武清区",
    "北辰": "北辰区", "北辰区": "北辰区",
    "西青": "西青区", "西青区": "西青区",
    "津南": "津南区", "津南区": "津南区",
    "东丽": "东丽区", "东丽区": "东丽区",
    "滨海新区": "滨海新区", "滨海": "滨海新区",
    "和平": "和平区", "和平区": "和平区",
    "河东": "河东区", "河东区": "河东区",
    "河西": "河西区", "河西区": "河西区",
    "南开": "南开区", "南开区": "南开区",
    "河北区": "河北区",
    "红桥": "红桥区", "红桥区": "红桥区",
}
# 长前缀优先（"滨海新区"先于"滨海"，"蓟州区"先于"蓟州"），防短名截胡。
_TOWNSHIP_PREFIXES = sorted(_TOWNSHIP_COUNTY_ALIASES, key=len, reverse=True)

# 天津区域别名 → 规范区县名（用于 query_region_risk_levels 的 region 判定与匹配）。
# 仅天津在风险清单覆盖范围内；不在此表 = 非天津（no_coverage）。
_TIANJIN_REGION_ALIASES: dict[str, str] = {
    "天津": "天津市", "天津市": "天津市", "天津市区": "天津市", "全市": "天津市",
    **{alias: county for alias, county in _TOWNSHIP_COUNTY_ALIASES.items()},
}


class RiskInterfaceNoDataError(RuntimeError):
    """旧接口"该起报时次无资料"异常。新 risk-lists 接口无此语义（空清单=本次无风险），
    类保留以免历史引用/文档断链。"""


def _risk_lists_base_urls() -> list[str]:
    bases: list[str] = []
    for key in BASE_ENV_KEYS:
        raw = os.getenv(key, "").strip()
        if raw:
            bases.append(raw.rstrip("/"))
    if not bases:
        bases.append(DEFAULT_RISK_LISTS_BASE)
    # 去重保持顺序
    seen: set[str] = set()
    out: list[str] = []
    for base in bases:
        if base not in seen:
            seen.add(base)
            out.append(base)
    return out


def _default_start_time() -> str:
    """start_time 默认取系统时间（time_source，验收切换系统时间时随"当时"走）。"""
    return time_source.now().strftime("%Y%m%d%H%M%S")


def _fetch_risk_lists(start_time: str = "", timeout_sec: int = 30) -> dict[str, Any]:
    """GET /conclusion/risk-lists。返回原始 JSON dict；HTTP/连接失败抛 RuntimeError。"""
    st = str(start_time or "").strip() or _default_start_time()
    headers = {"Accept": "application/json", "User-Agent": "haihe-weather-analyzer/1.0"}
    errors: list[str] = []
    for base in _risk_lists_base_urls():
        url = f"{base}{RISK_LISTS_ROUTE}"
        try:
            resp = requests.get(url, params={"start_time": st}, headers=headers, timeout=timeout_sec)
            if resp.ok:
                try:
                    payload = resp.json()
                except Exception:
                    logger.warning("[risk_lists] non-json response url=%s body=%s", url, resp.text[:300])
                    errors.append(f"{base}: 响应非 JSON")
                    continue
                if not isinstance(payload, dict):
                    errors.append(f"{base}: 响应结构异常")
                    continue
                return payload
            errors.append(f"{base}: HTTP {resp.status_code}")
        except requests.RequestException as exc:
            errors.append(f"{base}: {exc}")
            logger.warning("[risk_lists] %s: %s", base, exc)
    raise RuntimeError("; ".join(errors) or "风险清单接口调用失败")


def _risk_list_level(value: Any) -> str:
    """接口等级（低/中/高/极高）→ 一~四级；未知值原样透传，空→空串。"""
    text = str(value or "").strip()
    if not text:
        return ""
    return _RISK_LIST_LEVEL_MAP.get(text, text)


def _township_county(township: str) -> str:
    """从 township 名（"蓟县官庄镇"）解析规范区县名（"蓟州区"）；解析不出返回空串。"""
    text = str(township or "").strip()
    if not text:
        return ""
    for prefix in _TOWNSHIP_PREFIXES:
        if text.startswith(prefix):
            return _TOWNSHIP_COUNTY_ALIASES[prefix]
    return ""


def _normalize_tianjin_region(region: str) -> str:
    """区域问法 → 规范名。"天津市/全市/市区"归一到"天津市"；区县归一到"XX区"。"""
    text = str(region or "").strip()
    if not text:
        return ""
    if text in _TIANJIN_REGION_ALIASES:
        return _TIANJIN_REGION_ALIASES[text]
    with_suffix = text + "区"
    if with_suffix in _TIANJIN_REGION_ALIASES:
        return _TIANJIN_REGION_ALIASES[with_suffix]
    return ""


def _township_matches_region(township: str, region: str) -> bool:
    """township 是否属于所问区域。天津全市恒真；区县按前缀别名匹配（蓟州≈蓟县）。"""
    text = str(township or "").strip()
    if not text:
        return False
    normalized = _normalize_tianjin_region(region)
    if not normalized:
        return False
    if normalized == "天津市":
        return True
    return _township_county(text) == normalized


def _extract_risk_list_records(
    payload: Any, kind: str = "", region: str = ""
) -> list[dict[str, Any]]:
    """从 risk-lists payload 提取乡镇级风险记录（可选灾种/区域过滤）。

    记录结构：township/county/level/level_norm/main_risks/count（主灾种合计）。
    kind 过滤 = township 的 main_risks 含该灾种标签；region 过滤 = township 属于该区域。
    """
    if not isinstance(payload, dict):
        return []
    townships = payload.get("township_risks")
    if not isinstance(townships, list):
        return []
    labels = _RISK_LIST_KIND_LABELS.get(kind, ()) if kind else ()
    records: list[dict[str, Any]] = []
    for item in townships:
        if not isinstance(item, dict):
            continue
        township = str(item.get("township") or "").strip()
        if not township:
            continue
        main = item.get("main_risks")
        if not isinstance(main, list):
            main = []
        if labels and not any(str(m.get("hazard_type") or "") in labels for m in main if isinstance(m, dict)):
            continue
        if region and not _township_matches_region(township, region):
            continue
        count = 0
        for m in main:
            if not isinstance(m, dict):
                continue
            if labels and str(m.get("hazard_type") or "") not in labels:
                continue
            try:
                count += int(m.get("count") or 0)
            except (TypeError, ValueError):
                continue
        records.append({
            "township": township,
            "county": _township_county(township),
            "level": str(item.get("risk_level") or ""),
            "level_norm": _risk_list_level(item.get("risk_level")),
            "main_risks": main,
            "count": count,
        })
    return records


def _aggregate_risk_list_levels(payload: Any, region: str) -> dict | str | None:
    """按区域聚合各灾种风险等级分布（结构对齐旧 findDataListByConfig 版）。

    返回 {hazard_key: {"label","kind","levels","total","level_advice"}}（仅含本次有记录
    的灾种）；区域无记录 → {}（本次无风险）；非天津区域 → RISK_LEVELS_NO_COVERAGE 哨兵。
    level_advice 直接用接口返回的 call_advice（官方叫应建议，零编造）。
    """
    normalized = _normalize_tianjin_region(region)
    if not normalized:
        return RISK_LEVELS_NO_COVERAGE
    call_advice = payload.get("call_advice") if isinstance(payload, dict) else None
    if not isinstance(call_advice, list):
        call_advice = []
    out: dict[str, Any] = {}
    for kind, key in HAZARD_KIND_TO_KEY.items():
        records = _extract_risk_list_records(payload, kind=kind, region=normalized)
        levels: dict[str, int] = {}
        for rec in records:
            lv = rec.get("level_norm") or "未知等级"
            n = rec.get("count") or 0
            levels[lv] = levels.get(lv, 0) + n
        if not levels:
            continue
        out[key] = {
            "label": RISK_CONFIGS[kind]["label"],
            "kind": kind,
            "levels": levels,
            "total": sum(levels.values()),
            "level_advice": list(call_advice),
        }
    return out


def _normalize_risk_kind(risk_kind: str) -> str:
    raw = str(risk_kind or "").strip()
    if not raw:
        return ""
    kind = RISK_ALIASES.get(raw) or raw
    if kind not in RISK_CONFIGS:
        raise ValueError(f"不支持的风险类型：{risk_kind}，支持 river/mountain/geologic")
    return kind


def _safe_err(exc: Exception) -> str:
    text = str(exc)
    # 内网地址脱敏（CLAUDE.md 约定）
    import re
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b", "[内网地址]", text)
    return text[:500]


def _error_payload(kind: str, message: str, debug_reason: str = "") -> dict[str, Any]:
    cfg = RISK_CONFIGS.get(kind, {})
    return {
        "status": "error",
        "risk_kind": kind,
        "risk_label": cfg.get("label") or kind,
        "message": message,
        "debug_reason": _safe_err(RuntimeError(debug_reason)) if debug_reason else "",
    }


# ---------------------------------------------------------------- 区域风险等级
# 结构同旧版（渲染层零改动）：{hazard_key: {"label","kind","levels","total","level_advice"}}
# 值语义：dict=本次各等级数量；RISK_LEVELS_NO_COVERAGE=接口仅覆盖天津、该区域无清单；
# 整体 None=接口调用失败（渲染"接口暂不可用"）；{}=可达但本次无风险。
# =====================================================================
REGION_RISK_LEVELS_CACHE_TTL = int(os.getenv("REGION_RISK_LEVELS_CACHE_TTL", "120"))
REGION_RISK_LEVELS_TIMEOUT_SEC = int(os.getenv("REGION_RISK_LEVELS_TIMEOUT_SEC", "8"))

_region_levels_decorator, _region_levels_cache, _region_levels_lock = make_ttl_cache(
    REGION_RISK_LEVELS_CACHE_TTL,
    lambda lon, lat, radius_km, fcst_times=None, *, region="", include_coverage=False: (
        f"{round(float(lon), 3)}|{round(float(lat), 3)}|{round(float(radius_km), 1)}|"
        f"{region}|{'|'.join(fcst_times) if fcst_times else ''}|coverage={bool(include_coverage)}"
    ),
    # 接口不可达(None)不缓存以便重试；可达（{} 无风险 / {...} 有等级 / 哨兵）缓存。
    should_cache=lambda v: v is not None,
)


@_region_levels_decorator
def query_region_risk_levels(
    lon: float, lat: float, radius_km: float, fcst_times: list[str] | None = None,
    *, region: str = "", include_coverage: bool = False,
) -> dict | str | None:
    """按区域名查天津灾害风险清单，聚合各灾种风险等级分布。

    - region 为空或无法归一到天津区域 → 按"天津市"全市口径（点位路径靠坐标在天津域内，
      全市清单即该点位的风险背景）；
    - 归一到非天津（如"唐山"）→ RISK_LEVELS_NO_COVERAGE 哨兵（清单仅覆盖天津）；
    - 接口失败 → None；可达无风险 → {}。
    lon/lat/radius_km/fcst_times 仅作缓存键与兼容旧签名，不参与过滤（清单无坐标）。
    """
    st = (fcst_times[0] if fcst_times else "") or _default_start_time()
    try:
        payload = _fetch_risk_lists(st, timeout_sec=REGION_RISK_LEVELS_TIMEOUT_SEC)
    except Exception as exc:
        logger.warning("[risk_lists] region levels fetch failed: %s", exc)
        return None
    if region and str(region).strip():
        # 显式区域：非天津直接 no_coverage 哨兵，绝不回落全市口径冒充。
        return _aggregate_risk_list_levels(payload, region)
    # 未给区域（点位路径靠坐标）：清单仅覆盖天津，全市清单即风险背景。
    return _aggregate_risk_list_levels(payload, "天津市")


def _query_risk_warning_core(
    risk_kind: str = "",
    region: str = "",
    start_time: str = "",
    end_time: str = "",
    fcst_time: str = "",
    extra_params_json: str = "",
) -> dict[str, Any]:
    """查询天津灾害风险清单（乡镇/行业/重点对象聚合），可指定灾种。

    risk_kind：geologic 地质灾害 / mountain 山洪 / river 中小河流洪水；空=全部灾种。
    fcst_time 格式 YYYYMMDDHHmmss（北京时间），映射为接口 start_time；未传取系统时间
    （time_source，验收切换系统时间时随"当时"走）。接口为"24小时起报窗口"语义：
    三清单全空=本次无风险；start_time/end_time 入参仅兼容保留。清单只覆盖天津。
    """
    try:
        kind = _normalize_risk_kind(risk_kind)
    except Exception as exc:
        return _error_payload("unknown", "风险类型识别失败。", str(exc))
    st = str(fcst_time or "").strip() or _default_start_time()
    try:
        payload = _fetch_risk_lists(st)
    except Exception as exc:
        logger.warning("[risk_lists] query failed kind=%s error=%s", kind, exc)
        return _error_payload(kind, "天津灾害风险清单查询失败。", str(exc))

    records = _extract_risk_list_records(payload, kind=kind, region=region)
    label = RISK_CONFIGS[kind]["label"] if kind else "灾害风险"
    townships = [r["township"] for r in records]
    county_totals: dict[str, int] = {}
    for r in records:
        county = r.get("county") or "未知区域"
        county_totals[county] = county_totals.get(county, 0) + 1
    order = {"一级": 0, "二级": 1, "三级": 2, "四级": 3}
    groups: dict[tuple[str, str], int] = {}
    for r in records:
        county = r.get("county") or "未知区域"
        lv = r.get("level_norm") or "未知等级"
        groups[(county, lv)] = groups.get((county, lv), 0) + 1
    summary = [
        {"county": c, "level": lv, "count": n} for (c, lv), n in groups.items()
    ]
    summary.sort(key=lambda x: (order.get(x["level"], 99), -int(x["count"])))
    call_advice = payload.get("call_advice")
    if not isinstance(call_advice, list):
        call_advice = []
    industry_risks = payload.get("industry_risks") if isinstance(payload.get("industry_risks"), list) else []
    key_objects = payload.get("key_objects") if isinstance(payload.get("key_objects"), list) else []
    if not records:
        message = f"当前未发现明显{label}（清单无风险记录）。"
    else:
        message = f"当前{label}需关注乡镇：" + "、".join(townships[:20]) + "。"
    return {
        "status": "ok",
        "risk_kind": kind,
        "risk_label": label,
        "count": len(records),
        "risk_count": len(records),
        "areas": townships[:50],
        "levels": sorted({r["level_norm"] for r in records if r.get("level_norm")}),
        "records": records[:50],
        "county_totals": county_totals,
        "county_risk_summary": summary,
        "level_advice": list(call_advice),
        "industry_risks": industry_risks[:50],
        "key_objects": key_objects[:50],
        "message": message,
        "query": {"region": region, "start_time": st},
    }


def register_risk_warning_tool(mcp: FastMCP) -> None:
    # 风险清单随 start_time 更新，同「类型|fcst_time」120s 内命中；region 不上接口不进键。
    _decorator, _risk_warning_cache, _risk_warning_lock = make_ttl_cache(
        int(os.getenv("RISK_WARNING_CACHE_TTL", "120")),
        lambda risk_kind="", region="", start_time="", end_time="", fcst_time="",
               extra_params_json="": (
            f"{risk_kind}|{fcst_time}|{extra_params_json}"
        ),
    )

    @mcp.tool()
    @_decorator
    def query_risk_warning(
        risk_kind: str = "",
        region: str = "",
        start_time: str = "",
        end_time: str = "",
        fcst_time: str = "",
        extra_params_json: str = "",
    ) -> dict[str, Any]:
        """查询天津灾害风险清单（乡镇/行业/重点对象聚合），可指定灾种。

        risk_kind：geologic 地质灾害 / mountain 山洪 / river 中小河流洪水；空=全部灾种。
        fcst_time 格式 YYYYMMDDHHmmss（北京时间），映射为接口 start_time；未传取系统时间
        （time_source，验收切换系统时间时随"当时"走）。接口为"24小时起报窗口"语义：
        三清单全空=本次无风险；start_time/end_time 入参仅兼容保留。清单只覆盖天津。
        """
        return _query_risk_warning_core(
            risk_kind=risk_kind, region=region, start_time=start_time,
            end_time=end_time, fcst_time=fcst_time, extra_params_json=extra_params_json,
        )
