"""风险预警查询 MCP 工具。

支持三类风险接口：
- 中小河流洪水：model=EC, type=1
- 山洪风险：model=EC, type=2
- 地质灾害/滑坡风险：model=SCMOC, type=3

接口来源：/hhfw/riskWarnNew/findDataListByConfig
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastmcp import FastMCP

logger = logging.getLogger(__name__)

RISK_ROUTE = "/hhfw/riskWarnNew/findDataListByConfig"

RISK_CONFIGS: dict[str, dict[str, Any]] = {
    "river": {
        "model": "EC",
        "type": 1,
        "label": "中小河流洪水风险",
        "question": "哪些区域需注意中小河流洪水？",
    },
    "mountain": {
        "model": "EC",
        "type": 2,
        "label": "山洪风险",
        "question": "有没有山洪风险？",
    },
    "geologic": {
        "model": "SCMOC",
        "type": 3,
        "label": "地质灾害风险",
        "question": "有没有地质灾害风险？",
    },
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
}

BASE_ENV_KEYS = (
    "RISK_WARN_BASE",
    "RISK_WARN_BASE_URL",
    "HHFW_API_BASE",
    "HHFW_BASE",
    "HAIHE_RISK_BASE",
    "HAIHE_RISK_WARN_BASE",
)


def _normalize_risk_kind(risk_kind: str) -> str:
    raw = str(risk_kind or "").strip()
    kind = RISK_ALIASES.get(raw) or raw
    if kind not in RISK_CONFIGS:
        raise ValueError(f"不支持的风险类型：{risk_kind}，支持 river/mountain/geologic")
    return kind


def _is_absolute_http_url(value: str) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith("http://") or text.startswith("https://")


def _risk_api_base_urls() -> list[str]:
    """返回风险预警服务候选根地址。

    注意：这里不再默认使用 EMERGENCY_HTTP_BASE。日志已经证明 8080 的应急服务下没有
    /hhfw/riskWarnNew/findDataListByConfig，继续默认打过去只会产生误导性的 404。
    """
    values: list[str] = []
    multi = os.environ.get("RISK_WARN_BASES") or ""
    for item in multi.split(","):
        item = item.strip()
        if item:
            values.append(item)
    for key in BASE_ENV_KEYS:
        val = (os.environ.get(key) or "").strip()
        if val:
            values.append(val)

    bases: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not _is_absolute_http_url(value):
            logger.warning("[risk_warning] ignore non-http base value=%s", value)
            continue
        base = value.rstrip("/")
        if base and base not in seen:
            seen.add(base)
            bases.append(base)
    return bases


def _load_json_response(url: str, timeout_sec: int) -> dict[str, Any]:
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except Exception:
        logger.warning("[risk_warning] non-json response url=%s raw=%s", url, raw[:500])
        return {"raw": raw}


def _fetch_risk_warning(kind: str, extra_params: dict[str, Any] | None = None, timeout_sec: int = 30) -> dict[str, Any]:
    cfg = RISK_CONFIGS[kind]
    bases = _risk_api_base_urls()
    if not bases:
        raise RuntimeError("风险预警服务地址未配置，请配置 RISK_WARN_BASE 或 HHFW_API_BASE。")

    params: dict[str, Any] = {k: v for k, v in (extra_params or {}).items() if v not in (None, "")}
    params.update({"model": cfg["model"], "type": cfg["type"]})
    query = urlencode(params, doseq=False)

    errors: list[str] = []
    for base in bases:
        url = f"{base}{RISK_ROUTE}?{query}"
        logger.warning("[risk_warning] request kind=%s url=%s", kind, url)
        try:
            return _load_json_response(url, timeout_sec)
        except HTTPError as exc:
            msg = f"{base}: HTTP {exc.code}"
            errors.append(msg)
            logger.warning("[risk_warning] %s", msg)
        except URLError as exc:
            msg = f"{base}: {exc.reason}"
            errors.append(msg)
            logger.warning("[risk_warning] %s", msg)
        except Exception as exc:
            msg = f"{base}: {str(exc)[:180]}"
            errors.append(msg)
            logger.warning("[risk_warning] %s", msg)

    raise RuntimeError("; ".join(errors) or "风险预警接口调用失败")


def _extract_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "rows", "list", "records", "result", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _extract_items(value)
            if nested:
                return nested
    return []


def _first_value(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row.get(key) not in (None, "", "null"):
            return row.get(key)
    lower_map = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        val = lower_map.get(key.lower())
        if val not in (None, "", "null"):
            return val
    return None


def _normalize_record(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {"raw": row}
    area = _first_value(row, (
        "areaName", "area_name", "regionName", "region_name", "name", "county", "cnty",
        "district", "city", "town", "xzqmc", "qxmc", "adnm", "address",
    ))
    level = _first_value(row, (
        "riskLevel", "risk_level", "level", "warnLevel", "warn_level", "grade", "riskGrade",
        "levelName", "warnLevelName", "等级", "风险等级",
    ))
    time_value = _first_value(row, (
        "time", "dataTime", "data_time", "publishTime", "publish_time", "forecastTime",
        "forecast_time", "startTime", "endTime", "validTime",
    ))
    desc = _first_value(row, (
        "desc", "description", "content", "remark", "message", "warnContent", "riskDesc", "summary",
    ))
    lon = _first_value(row, ("lon", "longitude", "lng", "x"))
    lat = _first_value(row, ("lat", "latitude", "y"))
    return {
        "area": area,
        "level": level,
        "time": time_value,
        "description": desc,
        "longitude": lon,
        "latitude": lat,
        "raw": row,
    }


def _is_risky_level(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if any(k in text for k in ("无", "暂无", "没有", "低风险", "较低", "0")):
        return False
    return True


def _summarize(kind: str, payload: Any) -> dict[str, Any]:
    cfg = RISK_CONFIGS[kind]
    items = _extract_items(payload)
    records = [_normalize_record(x) for x in items]
    risky = [r for r in records if _is_risky_level(r.get("level"))]
    risky_with_area = [r for r in risky if r.get("area")]
    levels = sorted({str(r.get("level")) for r in risky if r.get("level")})
    areas = []
    seen = set()
    for r in risky_with_area:
        area = str(r.get("area")).strip()
        if area and area not in seen:
            seen.add(area)
            areas.append(area)
    message = ""
    if not records:
        message = f"当前未查询到{cfg['label']}数据。"
    elif not risky:
        message = f"当前未发现明显{cfg['label']}。"
    elif areas:
        message = f"当前{cfg['label']}需关注区域：" + "、".join(areas[:20]) + "。"
    else:
        message = f"当前查询到{len(risky)}条{cfg['label']}记录，请关注详情。"
    return {
        "risk_kind": kind,
        "risk_label": cfg["label"],
        "model": cfg["model"],
        "type": cfg["type"],
        "status": "ok",
        "count": len(records),
        "risk_count": len(risky),
        "areas": areas[:50],
        "levels": levels,
        "records": records[:50],
        "message": message,
    }


def _error_payload(kind: str, message: str, debug_reason: str = "") -> dict[str, Any]:
    cfg = RISK_CONFIGS.get(kind, {})
    return {
        "status": "error",
        "risk_kind": kind,
        "risk_label": cfg.get("label") or kind,
        "message": message,
        "debug_reason": debug_reason[:500] if debug_reason else "",
    }


def register_risk_warning_tool(mcp: FastMCP) -> None:
    @mcp.tool()
    def query_risk_warning(
        risk_kind: str,
        region: str = "",
        start_time: str = "",
        end_time: str = "",
        extra_params_json: str = "",
    ) -> dict[str, Any]:
        """查询山洪、地质灾害或中小河流洪水风险预警。"""
        try:
            kind = _normalize_risk_kind(risk_kind)
        except Exception as exc:
            return _error_payload("unknown", "风险类型识别失败。", str(exc))

        extra: dict[str, Any] = {}
        if extra_params_json:
            try:
                obj = json.loads(extra_params_json)
                if isinstance(obj, dict):
                    extra.update(obj)
            except Exception as exc:
                logger.warning("[risk_warning] extra_params_json parse failed: %s", exc)
        if region:
            extra.setdefault("region", region)
        if start_time:
            extra.setdefault("startTime", start_time)
        if end_time:
            extra.setdefault("endTime", end_time)

        try:
            payload = _fetch_risk_warning(kind, extra)
            result = _summarize(kind, payload)
            result["query"] = {"region": region, "start_time": start_time, "end_time": end_time}
            return result
        except Exception as exc:
            logger.warning("[risk_warning] failed kind=%s error=%s", kind, exc)
            text = str(exc)
            if "服务地址未配置" in text:
                return _error_payload(kind, "风险预警服务地址未配置。", text)
            return _error_payload(kind, f"{RISK_CONFIGS[kind]['label']}查询失败。", text)
