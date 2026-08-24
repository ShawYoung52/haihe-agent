"""风险预警查询 MCP 工具。

支持三类风险接口：
- 中小河流洪水：model=EC, type=1
- 山洪风险：model=EC, type=2
- 地质灾害/滑坡风险：model=SCMOC, type=3

接口来源：/hhfw/riskWarnNew/findDataListByConfig
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import os
from typing import Any

import requests
from fastmcp import FastMCP

from custom_tools._ttl_cache import make_ttl_cache

logger = logging.getLogger(__name__)

DEFAULT_RISK_WARN_BASE = "http://10.226.107.35:8070"
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
        # SCMOC 地灾接口除 fcstTime 外还要求 startTime/endTime 时间段（缺任一 →
        # HTTP 500，2026-08-24 接口开发给的可用调法证实）；EC 两类只认 fcstTime。
        "needs_time_range": True,
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
    "崩塌": "geologic",
    "泥石流": "geologic",
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

    默认使用已确认的风险预警服务地址；环境变量可覆盖或追加候选地址。
    这里不再默认使用 EMERGENCY_HTTP_BASE。日志已经证明 8080 的应急服务下没有
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
    values.append(DEFAULT_RISK_WARN_BASE)

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


def _latest_fcst_cycle(now: _dt.datetime) -> str:
    """把北京时间折到最近一个起报时次（08/20），格式 yyyyMMddHHmmss。"""
    if now.hour >= 20:
        fcst = now.replace(hour=20, minute=0, second=0, microsecond=0)
    elif now.hour >= 8:
        fcst = now.replace(hour=8, minute=0, second=0, microsecond=0)
    else:
        fcst = (now - _dt.timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
    return fcst.strftime("%Y%m%d%H%M%S")


def _default_fcst_time() -> str:
    """默认 fcstTime：真实北京时间的最近起报时次。

    后端 findDataListByConfig 必传 fcstTime（yyyyMMddHHmmss）：不传 HTTP 500、
    格式错误 400（2026-08-24 服务器 curl 三连证实）。风险预警是实时产品、
    后端只有当前起报周期，所以用真实系统时间，不跟 time_source 模拟时间走
    （模拟的历史日期在后端没有对应周期，同样 500——8-21 验收模拟 2026-07-10
    时接口开发看到的"那个日期的报错"即此）。
    """
    beijing = _dt.timezone(_dt.timedelta(hours=8))
    return _latest_fcst_cycle(_dt.datetime.now(beijing))


def _fetch_risk_warning(kind: str, extra_params: dict[str, Any] | None = None, timeout_sec: int = 30) -> dict[str, Any]:
    cfg = RISK_CONFIGS[kind]
    bases = _risk_api_base_urls()
    if not bases:
        raise RuntimeError("风险预警服务地址未配置，请配置 RISK_WARN_BASE 或 HHFW_API_BASE。")

    params: dict[str, Any] = {k: v for k, v in (extra_params or {}).items() if v not in (None, "")}
    # 调用方提供的 startTime/endTime 一律剥离——时间段只允许由 fcstTime 推导（见下），
    # 避免 planner/extra_params_json 传入与 fcstTime 不一致的窗口。
    params.pop("startTime", None)
    params.pop("endTime", None)
    params["model"] = cfg["model"]
    params["type"] = cfg["type"]
    # fcstTime 必填（缺省补最近起报时次），否则后端 HTTP 500。
    params.setdefault("fcstTime", _default_fcst_time())
    if cfg.get("needs_time_range"):
        # SCMOC 地灾：fcstTime + startTime + endTime 三者缺一不可（缺 → 500），
        # 接口开发确认的口径：startTime=fcstTime、endTime=fcstTime+24h。
        try:
            fcst_dt = _dt.datetime.strptime(str(params["fcstTime"]), "%Y%m%d%H%M%S")
            params["startTime"] = fcst_dt.strftime("%Y%m%d%H%M%S")
            params["endTime"] = (fcst_dt + _dt.timedelta(hours=24)).strftime("%Y%m%d%H%M%S")
        except ValueError:
            logger.warning("[risk_warning] fcstTime %r 无法解析，跳过 startTime/endTime 推导", params["fcstTime"])
    headers = {"Accept": "application/json", "User-Agent": "haihe-weather-analyzer/1.0"}

    errors: list[str] = []
    for base in bases:
        url = f"{base}{RISK_ROUTE}"
        logger.warning("[risk_warning] request kind=%s url=%s params=%s", kind, url, params)
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout_sec)
            if resp.ok:
                try:
                    return resp.json()
                except Exception:
                    logger.warning("[risk_warning] non-json response url=%s status=%s body=%s", url, resp.status_code, resp.text[:500])
                    return {"raw": resp.text}
            msg = f"{base}: HTTP {resp.status_code}"
            body = resp.text[:500]
            if body:
                msg = f"{msg}, body={body}"
            errors.append(msg)
            logger.warning("[risk_warning] %s", msg)
        except requests.RequestException as exc:
            msg = f"{base}: {exc}"
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
    # 接口 2026-08-21 确认返回隐患点 id/name（样本：{"name":"石界滑坡","lon":113.75,
    # "id":68,"lat":36.25,"level":5,"area_246":"474"}）——id 即静态隐患点表主键，
    # 直接据此联表取 county/city（见 _match_hazard_points）。
    id_value = _first_value(row, (
        "id", "pointId", "point_id", "hazardId", "hazard_id", "riskId", "risk_id",
        "隐患点id", "隐患点ID",
    ))
    name = _first_value(row, (
        "name", "hazardName", "hazard_name", "pointName", "point_name",
        "隐患点名称", "隐患点",
    ))
    # 注意：区域候选不能放 "name"——本接口的 name 是隐患点名（样本"石界滑坡"），
    # 若仍作区域候选，无真实区县字段的记录会把隐患点名当区县名混入 county_risk_summary
    # （2026-08-21 review 修正：县级汇总里出现"石界滑坡"伪县名）。
    area = _first_value(row, (
        "areaName", "area_name", "regionName", "region_name", "county", "cnty",
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
        "id": id_value,
        "name": name,
        "area": area,
        "level": level,
        "level_norm": _normalize_risk_level(level),  # 展示用：原始"5"→"一级"，各路径展示一致
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
    # 数字等级按"数字越大风险越高"（样本 level=5）：1=无/极低风险，不列为本次风险。
    # 方向假设见 _NUMERIC_LEVEL_MAP 注释；服务器诊断脚本打印真实等级分布后按需改。
    if text.isdigit() and int(text) <= 1:
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


# =====================================================================
# 风险记录 → 灾害点匹配（2026-08-21 需求）
#
# 风险接口 /hhfw/riskWarnNew/findDataListByConfig 现已确认返回隐患点 id/name
# （用户提供样本：{"name":"石界滑坡","lon":113.75,"id":68,"lat":36.25,"level":5}）。
# 因此：
#   - 有 id 的记录直接按 id 关联静态隐患点表（dzzh/sh/zxhl）取 county/city，
#     不再靠经纬度就近猜 id（match_method="id"）；
#   - 记录本身无 id 时才按经纬度就近匹配兜底（match_method="haversine"，
#     RISK_WARNING_MATCH_RADIUS_KM 默认 1km，超半径不匹配保留原样）；
#   - id 在静态表查不到时不就近猜测——避免挂到错误的隐患点。
# 回答"某灾害点 = 几级风险"，并汇总出「各区县隐患点总数」与「本次各区县各级
# 风险数量」供司南问答按等级分层。数据库不可用/匹配失败时静默降级。
# =====================================================================
HAZARD_KIND_TO_KEY = {"geologic": "dzzh", "mountain": "sh", "river": "zxhl"}

def _parse_radius_km(raw: str | None) -> float:
    """解析 RISK_WARNING_MATCH_RADIUS_KM；非法/越界一律回退默认 1.0（导入期绝不抛错，
    单个环境变量配错不能击穿整个工具，2026-08-21 review 修正）。"""
    try:
        value = float(raw or "")
    except (TypeError, ValueError):
        return 1.0
    return value if 0 < value <= 10 else 1.0


RISK_WARNING_MATCH_RADIUS_KM = _parse_radius_km(os.getenv("RISK_WARNING_MATCH_RADIUS_KM"))

# 等级规范化：地质灾害气象风险预警 一级(红)最高 → 四级(蓝)最低。
_LEVEL_COLOR_MAP = {
    "红": "一级", "红色": "一级",
    "橙": "二级", "橙色": "二级",
    "黄": "三级", "黄色": "三级",
    "蓝": "四级", "蓝色": "四级",
}
# 数字等级：接口样本实测返回 level=5，按"数字越大风险越高"（5=最高/红色/极高）
# 映射到司南"一~四级"话术：5→一级(最重)…2→四级；1≈无/极低风险（_is_risky_level 排除）。
# 方向为假设——服务器用 diagnose_risk_api.py 打印真实等级分布后按需改（若方向相反
# 则把 1 与 5 互换即可）。
_NUMERIC_LEVEL_MAP = {"5": "一级", "4": "二级", "3": "三级", "2": "四级"}

# 逐级防范建议（按国家地质灾害气象风险预警标准起草，2026-08-21，文案待业务确认）。
_LEVEL_ADVICE: dict[str, dict[str, str]] = {
    "geologic": {
        "一级": "停止露天作业与旅游等活动，组织受威胁人员转移避险，安排专人加密巡查监测。",
        "二级": "暂停户外作业，山区、沟谷、边坡区域人员及时撤离，安排专人值守监测。",
        "三级": "加强地质灾害隐患点巡查监测，做好转移避险准备，降雨期间避免进入山区沟谷。",
        "四级": "关注雨情变化与预警信息，提高警惕，注意防范突发地质灾害。",
    },
    "mountain": {
        "一级": "立即转移山洪沟道危险区人员，停止一切涉水与山区活动。",
        "二级": "危险区人员提前转移，停止山区野外作业与旅游活动。",
        "三级": "加强山洪沟道巡查监测，做好转移避险准备。",
        "四级": "关注预警信息，远离沟道、低洼等山洪易发地带。",
    },
    "river": {
        "一级": "立即撤离沿河低洼区域人员，封控危险河段。",
        "二级": "沿河低洼区域人员提前转移，停止水上与沿河作业。",
        "三级": "加强沿河巡查监测，关注水位上涨与行洪安全。",
        "四级": "关注水位与预警信息，远离沿河低洼地带。",
    },
}


def _safe_float(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        number = float(value)
    except Exception:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius_km = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(a))


def _normalize_risk_level(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for cn in ("一级", "二级", "三级", "四级"):
        if cn in text:
            return cn
    for color, cn in _LEVEL_COLOR_MAP.items():
        if color in text:
            return cn
    key = text.strip().lstrip("0")
    if key in _NUMERIC_LEVEL_MAP:
        return _NUMERIC_LEVEL_MAP[key]
    return text


# 懒加载 poi_hazard_reminder_tool（绕开 custom_tools/__init__.py 的重依赖链，
# 与 rolling_forecast_service._load_region_hazard_queryer 同套路）。
_hazard_module = None


def _load_hazard_module() -> Any:
    global _hazard_module
    if _hazard_module is None:
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "poi_hazard_reminder_tool",
            # risk_warning_tool.py 与 poi_hazard_reminder_tool.py 同目录（custom_tools/），
            # 这里直接取同级文件，不能再拼一层 custom_tools/。
            Path(__file__).resolve().parent / "poi_hazard_reminder_tool.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _hazard_module = mod
    return _hazard_module


def _get_hazard_rows_for_kind(kind: str) -> list[dict] | None:
    """返回指定风险类型对应的隐患点表行；失败/不可用返回 None（静默降级）。"""
    key = HAZARD_KIND_TO_KEY.get(kind)
    if not key:
        return None
    try:
        mod = _load_hazard_module()
        pg_conf = mod._get_postgres_conf()
        if not pg_conf:
            logger.warning("[risk_warning] postgres 未配置，跳过灾害点匹配")
            return None
        schema = mod._resolve_schema(pg_conf)
        rows_by_key, errors = mod._get_cached_hazard_rows(pg_conf, schema)
        rows = rows_by_key.get(key) or []
        if not rows and errors:
            logger.warning("[risk_warning] 隐患点表 %s 无数据：%s", key, "; ".join(errors))
        return rows or None
    except Exception as exc:
        logger.warning("[risk_warning] 灾害点匹配数据不可用：%s", exc)
        return None


def _nearest_hazard_row(rec: dict, rows: list[dict]) -> tuple[dict | None, float | None]:
    """按经纬度在隐患点表内就近查找（超半径返回 (None, None)）。"""
    lon = _safe_float(rec.get("longitude"))
    lat = _safe_float(rec.get("latitude"))
    if lon is None or lat is None:
        return None, None
    best = None
    best_d: float | None = None
    for row in rows:
        d = _haversine_km(lon, lat, float(row["lon"]), float(row["lat"]))
        if best_d is None or d < best_d:
            best_d = d
            best = row
    if best_d is not None and best_d <= RISK_WARNING_MATCH_RADIUS_KM:
        return best, best_d
    return None, None


def _id_key(value: Any) -> str:
    """隐患点 id → 查询主键；int 68 / float 68.0 / str "68" / str "68.0" 归一到 "68"。

    None 与 0（常见"无 id"哨兵，静态表主键从 1 起不会有真 id=0）视为无 id，返回空串，
    走经纬度兜底。不做 int() 强转，保留前导零（"068" 不误配 "68"）。
    2026-08-21 review 修正：JSON 序列化会把数字 id 打成 68.0，直接 str() 会对不上。
    """
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    if text == "0":
        return ""
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def _match_hazard_points(records: list[dict], kind: str, rows: list[dict]) -> tuple[list[dict], int]:
    """把风险记录关联到对应隐患点表；返回 (enriched, matched_count)。

    records 为已规范化的记录（含 id/name/longitude/latitude）；rows 为对应隐患点
    表行（id/name/lon/lat/county_name/city_name）。

    - 记录**有 id** 且命中静态表 → 按 id 直连（match_method="id"），county/city 取表值；
    - 记录**无 id** → 经纬度就近匹配兜底（match_method="haversine"，超半径不匹配）；
    - 记录**有 id 但静态表查不到** → 不就近猜测，保留原样（可继续按 area 兜底分组）。
    """
    rows_by_id: dict[str, dict] = {}
    for row in rows:
        rid_key = _id_key(row.get("id"))
        if rid_key:
            rows_by_id[rid_key] = row

    matched_count = 0
    enriched: list[dict] = []
    for rec in records:
        out = dict(rec)
        rid_key = _id_key(rec.get("id"))
        row = None
        distance_km: float | None = None
        if rid_key in rows_by_id:
            row = rows_by_id[rid_key]
        elif not rid_key:
            row, distance_km = _nearest_hazard_row(rec, rows)
        if row is not None:
            method = "id" if rid_key in rows_by_id else "haversine"
            out["hazard_id"] = row.get("id")
            out["hazard_name"] = row.get("name") or out.get("name")
            out["county_name"] = row.get("county_name")
            out["city_name"] = row.get("city_name")
            out["match_method"] = method
            if method == "id":
                out.pop("match_distance_km", None)
            elif distance_km is not None:
                out["match_distance_km"] = round(distance_km, 2)
            matched_count += 1
        enriched.append(out)
    return enriched, matched_count


def _county_totals(rows: list[dict]) -> dict[str, int]:
    """各区县隐患点总数（静态表全量，如"冀州区 257 个"）。"""
    totals: dict[str, int] = {}
    for row in rows:
        county = str(row.get("county_name") or "").strip()
        if not county:
            continue
        totals[county] = totals.get(county, 0) + 1
    return totals


def _summarize_by_county_level(records: list[dict], county_key: str = "county_name") -> list[dict]:
    """按（区县, 规范化风险等级）统计本次风险数量；按严重度降序返回。"""
    order = {"一级": 0, "二级": 1, "三级": 2, "四级": 3}
    groups: dict[tuple[str, str], int] = {}
    for rec in records:
        if not _is_risky_level(rec.get("level")):
            continue
        county = str(rec.get(county_key) or rec.get("area") or "").strip()
        if not county:
            county = "未知区域"
        lv = _normalize_risk_level(rec.get("level")) or "未知等级"
        key = (county, lv)
        groups[key] = groups.get(key, 0) + 1
    items = [{"county": c, "level": lv, "count": n} for (c, lv), n in groups.items()]
    items.sort(key=lambda x: (order.get(x["level"], 99), -int(x["count"])))
    return items


def _level_advice_for(kind: str, present_levels: set[str] | None = None) -> list[dict]:
    """逐级防范建议；present_levels 传本次实际出现的等级时只返回这些等级（仍按严重度
    排序），避免"本次仅四级"也刷出一级"立即转移/封控"的最高级文案（2026-08-21 review）。
    不传则返回全四级（原行为，供独立调用）。"""
    table = _LEVEL_ADVICE.get(kind) or _LEVEL_ADVICE.get("geologic") or {}
    if present_levels is None:
        candidates = ("一级", "二级", "三级", "四级")
    else:
        candidates = [lv for lv in ("一级", "二级", "三级", "四级") if lv in present_levels]
    return [{"level": lv, "advice": table[lv]} for lv in candidates if lv in table]


def _enrich_risk_result(result: dict, kind: str, all_records: list[dict]) -> dict:
    """在 _summarize 基础上追加灾害点匹配与逐级汇总（2026-08-21 需求）。

    - 每条带经纬度的风险记录就近关联隐患点 id/名称/区县；
    - county_totals：各区县隐患点总数（静态表全量）；
    - county_risk_summary：本次各区县各级风险数量（供"冀州共X个，本次A处一级…"作答）；
    - level_advice：逐级防范建议（代码确定性生成，LLM 逐字采用，防编造）。
    数据库不可用/无匹配时静默降级：summary 退回按记录 area 分组，不阻断回答。
    """
    rows = _get_hazard_rows_for_kind(kind)
    match_info: dict[str, Any] = {
        "enabled": rows is not None,
        "matched_count": 0,
        "id_matched_count": 0,
        "haversine_matched_count": 0,
        "unmatched_count": len(all_records),
        "total_records": len(all_records),
        "radius_km": RISK_WARNING_MATCH_RADIUS_KM,
    }
    if rows:
        matched_records, matched = _match_hazard_points(all_records, kind, rows)
        id_matched = sum(1 for r in matched_records if r.get("match_method") == "id")
        match_info["matched_count"] = matched
        match_info["id_matched_count"] = id_matched
        match_info["haversine_matched_count"] = matched - id_matched
        match_info["unmatched_count"] = len(all_records) - matched
        result["records"] = matched_records[:50]
        result["county_totals"] = _county_totals(rows)
        summary = _summarize_by_county_level(matched_records, "county_name")
    else:
        result["county_totals"] = {}
        summary = _summarize_by_county_level(all_records, "area")
    result["county_risk_summary"] = summary
    # 防范建议只覆盖本次实际出现的等级（summary 有记录才有对应等级文案；无风险→空列表，
    # 快速路径/提示词自然回退笼统建议）。
    present = {item.get("level") for item in summary if isinstance(item, dict) and item.get("level")}
    result["level_advice"] = _level_advice_for(kind, present)
    result["hazard_match"] = match_info
    return result


# =====================================================================
# 区域天气「风险等级」查询（#8，2026-08-21，针对国家局）
#
# 领导需求：区域天气（如"明天蓟州天气"）除隐患点底数外还要给"风险等级"。
# 等级数据源 = 风险接口 findDataListByConfig 的 level（与"司南分层回答"同一来源，
# _normalize_risk_level 归一到 一级(红)~四级(蓝)）。滚动预报区域模式按代表坐标半径
# 调用本函数，把"本次各灾种风险等级分布"叠加到【区域灾害风险】表上。
# 每次查询打 3 次风险接口（geologic/mountain/river），故加 120s TTL 缓存；
# 接口不可达返回 None 不缓存（下次重试），可达（{} 无风险 / {...} 有等级）才缓存。
# =====================================================================
REGION_RISK_LEVELS_CACHE_TTL = int(os.getenv("REGION_RISK_LEVELS_CACHE_TTL", "120"))
REGION_RISK_LEVELS_TIMEOUT_SEC = int(os.getenv("REGION_RISK_LEVELS_TIMEOUT_SEC", "8"))

_region_levels_decorator, _region_levels_cache, _region_levels_lock = make_ttl_cache(
    REGION_RISK_LEVELS_CACHE_TTL,
    lambda lon, lat, radius_km: (
        f"{round(float(lon), 3)}|{round(float(lat), 3)}|{round(float(radius_km), 1)}"
    ),
    # 接口不可达(None)不缓存以便重试；可达（{} 无风险 / {...} 有等级）缓存。
    should_cache=lambda v: v is not None,
)


@_region_levels_decorator
def query_region_risk_levels(lon: float, lat: float, radius_km: float) -> dict | None:
    """按区域代表坐标半径查风险接口当前各灾种风险等级分布。

    返回 ``{hazard_key: {"label", "kind", "levels", "total", "level_advice"} | None}``：
    - hazard_key 与区域隐患表 categories 的 key 一致（dzzh/sh/zxhl），便于按灾种对齐；
    - 值为 None 表示该灾种接口调用失败（渲染层显示"接口暂不可用"，不得误报
      "本次无风险"——2026-08-24 SCMOC 地灾接口单独 500 时被静默吞掉的教训）；
    - levels = {一级: n, ...}（只含本次实际出现的等级，_normalize_risk_level 归一）；
    - level_advice = 逐级防范建议（仅本次出现的等级，代码确定性生成）。

    口径：只统计"有风险"（_is_risky_level）且落在 radius_km 内的记录。接口可达但
    全域无风险 → {}；全部灾种接口失败/异常 → None（静默降级，绝不阻断天气回答）。
    """
    kinds: dict[str, dict | None] = {}
    reachable = False
    for kind, key in HAZARD_KIND_TO_KEY.items():
        try:
            payload = _fetch_risk_warning(kind, timeout_sec=REGION_RISK_LEVELS_TIMEOUT_SEC)
            reachable = True
        except Exception:
            kinds[key] = None  # 单灾种失败打标，交给渲染层显示"接口暂不可用"
            continue  # 单灾种接口失败静默跳过
        records = [_normalize_record(x) for x in _extract_items(payload)]
        level_counts: dict[str, int] = {}
        for rec in records:
            if not _is_risky_level(rec.get("level")):
                continue
            rlon = _safe_float(rec.get("longitude"))
            rlat = _safe_float(rec.get("latitude"))
            if rlon is None or rlat is None:
                continue
            if _haversine_km(lon, lat, rlon, rlat) > radius_km:
                continue
            level = _normalize_risk_level(rec.get("level"))
            if not level:
                continue
            level_counts[level] = level_counts.get(level, 0) + 1
        if not level_counts:
            continue
        kinds[key] = {
            "label": RISK_CONFIGS[kind]["label"],
            "kind": kind,
            "levels": level_counts,
            "total": sum(level_counts.values()),
            "level_advice": _level_advice_for(kind, set(level_counts)),
        }
    if not reachable:
        # 全部灾种接口失败 → None（不缓存以便重试；前端整列显示"接口暂不可用"）。
        return None
    # 可达：无风险且无失败 → {}（前端渲染"本次无风险"）；含 None 值 = 对应灾种失败。
    return kinds


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
    # 风险预警按起报时次刷新，同「类型|时间窗|extra」120s 内命中；region 不上接口故不进键。
    _decorator, _risk_warning_cache, _risk_warning_lock = make_ttl_cache(
        int(os.getenv("RISK_WARNING_CACHE_TTL", "120")),
        lambda risk_kind="", region="", start_time="", end_time="", fcst_time="",
               extra_params_json="": (
            f"{risk_kind}|{start_time}|{end_time}|{fcst_time}|{extra_params_json}"
        ),
    )

    @mcp.tool()
    @_decorator
    def query_risk_warning(
        risk_kind: str,
        region: str = "",
        start_time: str = "",
        end_time: str = "",
        fcst_time: str = "",
        extra_params_json: str = "",
    ) -> dict[str, Any]:
        """查询山洪、地质灾害或中小河流洪水风险预警。

        fcst_time 格式为 YYYYMMDDHHmmss（北京时间），未传时自动取真实北京时间
        最近一个起报时次（08:00 或 20:00）。start_time/end_time 入参仅兼容保留、
        不直接使用：SCMOC 地质灾害需要的时间段由 fcstTime 自动推导
        （startTime=fcstTime、endTime=+24h），EC 两类只发 fcstTime。
        """
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
        # 后端 /hhfw/riskWarnNew/findDataListByConfig 口径（2026-08-24 服务器 curl
        # + 接口开发确认）：fcstTime 必填（缺 → 500，格式非 yyyyMMddHHmmss → 400）；
        # SCMOC 地灾另需 startTime/endTime（由 fetch 层按 fcstTime 推导）；region
        # 不上接口，区域过滤由 LLM 侧基于返回结果筛选；fcstTime 默认取真实北京时间
        # 的最近起报时次，不跟随 time_source 模拟时间。
        explicit_fcst = str(fcst_time or "").strip()
        if explicit_fcst:
            extra["fcstTime"] = explicit_fcst
        effective_fcst = str(extra.get("fcstTime") or "").strip() or _default_fcst_time()
        extra["fcstTime"] = effective_fcst

        try:
            payload = _fetch_risk_warning(kind, extra)
            all_records = [_normalize_record(x) for x in _extract_items(payload)]
            result = _summarize(kind, payload)
            # 灾害点就近匹配 + 逐级汇总（2026-08-21 需求）：给每条风险记录挂上
            # 隐患点 id/名称/区县，并产出 county_totals/county_risk_summary/level_advice。
            result = _enrich_risk_result(result, kind, all_records)
            result["query"] = {"region": region, "start_time": start_time, "end_time": end_time, "fcst_time": effective_fcst}
            return result
        except Exception as exc:
            logger.warning("[risk_warning] failed kind=%s error=%s", kind, exc)
            text = str(exc)
            if "服务地址未配置" in text:
                return _error_payload(kind, "风险预警服务地址未配置。", text)
            return _error_payload(kind, f"{RISK_CONFIGS[kind]['label']}查询失败。", text)
