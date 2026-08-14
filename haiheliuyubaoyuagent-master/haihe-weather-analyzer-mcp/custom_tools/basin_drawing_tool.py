"""14所 basin_drawing 出图工具（可出图分区列表 + 实况/预报格点雨量或面雨量出图）。

业务场景：用户问「XX分区降水图 / 面雨量分布图 / 格点预报降水图」时出图。
口径（用户确认）：单工具参数化（sceneType/productType 由 planner 按 docstring 路由）；
图片返回代理 URL；base 默认 http://10.226.107.35:8080，env BASIN_DRAWING_API_BASE 可覆盖；
时间自动规整到 10 分钟刻度；只有带 children 的一级分区可出图。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests
from fastmcp import FastMCP

from custom_tools._ttl_cache import make_ttl_cache


BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
# ⚠️ 与 chainlitexam/qa_http_api.py 的 IMAGE_URL_ALLOW_HOSTS 默认同值但独立配置：
# 部署改 BASIN_DRAWING_API_BASE 时必须同步扩展 IMAGE_URL_ALLOW_HOSTS，否则图链会被
# _scrub 脱敏成 [内网地址]、images 字段也不收录。
BASIN_DRAWING_API_BASE = os.getenv("BASIN_DRAWING_API_BASE", "http://10.226.107.35:8080")
AREAS_URL = f"{BASIN_DRAWING_API_BASE}/openapi/basin_drawing/areas"
IMAGE_URL = f"{BASIN_DRAWING_API_BASE}/openapi/basin_drawing/image"
BASIN_DRAWING_AREAS_CACHE_TTL = int(os.getenv("BASIN_DRAWING_AREAS_CACHE_TTL", "3600"))

VALID_SCENE_TYPES = {"REALTIME", "FORECAST"}
VALID_PRODUCT_TYPES = {"STATION_RAIN", "GRID_RAIN", "AREA_RAIN"}
# 产品与场景兼容性：站点雨量仅实况、格点雨量仅预报、面雨量两者皆可。
PRODUCT_SCENE_RESTRICTION = {
    "STATION_RAIN": {"REALTIME"},
    "GRID_RAIN": {"FORECAST"},
    "AREA_RAIN": {"REALTIME", "FORECAST"},
}


def _latest_synoptic_cycle(now: datetime | None = None) -> datetime:
    """最近一个北京时 08/20 起报时次（与 risk_warning 同口径）。"""
    current = now or datetime.now(BEIJING_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING_TIMEZONE)
    if current.hour >= 20:
        return current.replace(hour=20, minute=0, second=0, microsecond=0)
    if current.hour >= 8:
        return current.replace(hour=8, minute=0, second=0, microsecond=0)
    prev = current - timedelta(days=1)
    return prev.replace(hour=20, minute=0, second=0, microsecond=0)


def _parse_ten_minute(value: str) -> datetime | None:
    """解析北京时时间并规整到 10 分钟刻度（向下取整）。"""
    text = str(value or "").strip()
    if not text:
        return None
    parsed: datetime | None = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H"):
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    return parsed.replace(minute=(parsed.minute // 10) * 10, second=0, microsecond=0)


def _default_window() -> tuple[datetime, datetime]:
    """默认时间窗：北京时当前往前 24 小时（10 分钟规整）。"""
    now = datetime.now(BEIJING_TIMEZONE)
    end = now.replace(minute=(now.minute // 10) * 10, second=0, microsecond=0)
    return end - timedelta(hours=24), end


def _fetch_basin_drawing_areas() -> dict[str, Any]:
    """GET 可出图分区列表并归一化。"""
    try:
        resp = requests.get(AREAS_URL, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        return {
            "status": "error",
            "message": f"分区列表查询失败：{exc}",
            "areas": [],
            "supported_count": 0,
        }

    raw = payload.get("data") if isinstance(payload, dict) else payload
    areas: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        children = []
        for ch in item.get("children") or []:
            if isinstance(ch, dict):
                children.append({
                    "code": str(ch.get("code") or "").strip(),
                    "name": str(ch.get("name") or "").strip(),
                })
        areas.append({
            "areaId": item.get("areaId"),
            "areaName": str(item.get("areaName") or "").strip(),
            "children": children,
        })
    supported = [a for a in areas if a["children"]]
    if not areas:
        # 上游 HTTP 200 但无分区数据（可能未配置/鉴权失败）：按 no_data 处理，
        # 不写 3600s 缓存（与共享缓存「无数据不缓存」契约一致）。
        return {
            "status": "no_data",
            "message": "分区列表为空（可能未配置或接口鉴权失败）",
            "areas": [],
            "supported_count": 0,
        }
    return {
        "status": "ok",
        "areas": areas,
        "supported_count": len(supported),
    }


# 分区列表静态：3600s 内命中；只有 status=="ok" 才写缓存（make_ttl_cache 规则）。
_basin_areas_decorator, _basin_areas_cache, _basin_areas_lock = make_ttl_cache(
    BASIN_DRAWING_AREAS_CACHE_TTL,
    lambda: "areas",
)
query_basin_drawing_areas_core = _basin_areas_decorator(_fetch_basin_drawing_areas)


def generate_basin_rainfall_image_core(
    scene_type: str = "REALTIME",
    product_type: str = "AREA_RAIN",
    parent_area_id: int = 1,
    area_codes: str = "ALL",
    begin_time: str = "",
    end_time: str = "",
    main_title: str = "",
    sub_title: str = "",
    show_rain_value: bool = True,
    show_area_name: bool = True,
    forecast_time: str = "",
    force_create: int = 0,
) -> dict[str, Any]:
    """POST 生成实况/预报格点雨量或面雨量图，返回代理图片 URL。"""
    scene = (scene_type or "").strip().upper()
    product = (product_type or "").strip().upper()

    if scene not in VALID_SCENE_TYPES:
        return {"status": "error", "message": f"非法场景 sceneType={scene_type!r}，可选 REALTIME/FORECAST"}
    if product not in VALID_PRODUCT_TYPES:
        return {"status": "error", "message": f"非法产品 productType={product_type!r}，可选 STATION_RAIN/GRID_RAIN/AREA_RAIN"}
    if scene not in PRODUCT_SCENE_RESTRICTION[product]:
        return {"status": "error", "message": f"产品 {product} 不支持场景 {scene}（站点雨量仅实况、格点雨量仅预报）"}

    try:
        parent_id = int(parent_area_id)
    except (TypeError, ValueError):
        return {"status": "error", "message": f"parent_area_id 必须是整数，当前 {parent_area_id!r}"}

    codes = [c.strip() for c in str(area_codes or "ALL").split(",") if c.strip()] or ["ALL"]

    if begin_time and end_time:
        b_dt = _parse_ten_minute(begin_time)
        e_dt = _parse_ten_minute(end_time)
    else:
        b_dt, e_dt = _default_window()
    if b_dt is None or e_dt is None:
        return {"status": "error", "message": f"时间格式非法，须为 yyyy-MM-dd HH:mm，收到 begin={begin_time!r} end={end_time!r}"}
    if e_dt <= b_dt:
        return {"status": "error", "message": "结束时间必须晚于开始时间"}
    if e_dt - b_dt > timedelta(days=10):
        return {"status": "error", "message": "时间跨度不能超过 10 天"}

    if scene == "FORECAST":
        ft_dt = _parse_ten_minute(forecast_time) if forecast_time else _latest_synoptic_cycle()
        if ft_dt is None:
            return {"status": "error", "message": f"预报起报时间格式非法：{forecast_time!r}"}

    body: dict[str, Any] = {
        "sceneType": scene,
        "productType": product,
        "parentAreaId": parent_id,
        "areaCodes": codes,
        "beginTime": b_dt.strftime("%Y-%m-%d %H:%M"),
        "endTime": e_dt.strftime("%Y-%m-%d %H:%M"),
        "mainTitle": str(main_title or "").strip(),
        "subTitle": str(sub_title or "").strip(),
        "showRainValue": bool(show_rain_value),
        "showAreaName": bool(show_area_name),
    }
    if scene == "FORECAST":
        body["forecastTime"] = ft_dt.strftime("%Y-%m-%d %H:%M")

    try:
        force = int(force_create or 0)
    except (TypeError, ValueError):
        return {"status": "error", "message": f"force_create 必须是 0/1，当前 {force_create!r}"}

    url = f"{IMAGE_URL}?forceCreate={force}"
    try:
        resp = requests.post(url, json=body, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        return {"status": "error", "message": f"出图失败：{exc}", "image_url": ""}

    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, str) or not data.strip():
        return {"status": "error", "message": "出图接口未返回图片地址", "image_url": ""}

    image_url = data if data.startswith("http") else f"{BASIN_DRAWING_API_BASE.rstrip('/')}/{data.lstrip('/')}"
    return {
        "status": "ok",
        "image_url": image_url,
        "scene_type": scene,
        "product_type": product,
        "parent_area_id": parent_id,
        "area_codes": codes,
        "begin_time": b_dt.strftime("%Y-%m-%d %H:%M"),
        "end_time": e_dt.strftime("%Y-%m-%d %H:%M"),
        "main_title": str(main_title or "").strip(),
        "message": "已生成雨量图。",
    }


def register_basin_drawing_tool(mcp: FastMCP) -> None:
    @mcp.tool()
    def query_basin_drawing_areas() -> dict:
        """
        查询 14所可出图的一级分区及 Shapefile 二级分区清单。

        用于出图前确定 parent_area_id 与 areaCodes：用户要求「XX分区降水图/
        面雨量分布图/格点预报降水图」时，先调用本工具拿到分区 ID 与二级分区编码，
        再调用 generate_basin_rainfall_image 出图。只有带 children 的一级分区
        才可出图（supported_count 为可出图分区数）。分区数据静态，3600s 缓存。
        """
        return query_basin_drawing_areas_core()

    @mcp.tool()
    def generate_basin_rainfall_image(
        scene_type: str = "REALTIME",
        product_type: str = "AREA_RAIN",
        parent_area_id: int = 1,
        area_codes: str = "ALL",
        begin_time: str = "",
        end_time: str = "",
        main_title: str = "",
        sub_title: str = "",
        show_rain_value: bool = True,
        show_area_name: bool = True,
        forecast_time: str = "",
        force_create: int = 0,
    ) -> dict:
        """
        生成实况/预报格点雨量或面雨量图（14所 basin_drawing 出图），返回图片代理 URL。

        仅当用户明确要「XX分区降水图 / 面雨量分布图 / 格点预报降水图 / 雨量分布图」时调用，
        图片用 markdown 图链展示。不要用于回答"下雨吗/雨量多少/天气怎么样"等数值查询。

        路由口径（用户确认）：
        - 分区面雨量 / 雨量分布图 → product_type=AREA_RAIN（实况/预报均可）
        - 实况站点雨量图 → product_type=STATION_RAIN（仅 scene_type=REALTIME）
        - 格点预报降水图 → product_type=GRID_RAIN（仅 scene_type=FORECAST，自动取最近 08/20 起报时次）
        - 出图前先调 query_basin_drawing_areas 拿 parent_area_id 与 areaCodes（二级编码，ALL=全部）

        边界：海河 9 大分区/子流域的「9分区降雨分布图」走既有工具
        get_station_rainfall_real_img（8001 端口 base64 出图）；本工具为 14所
        一级分区+Shapefile 二级分区的面雨量/格点/站点雨量出图，支持自定义标题。

        Args:
            scene_type: REALTIME 实况 / FORECAST 预报
            product_type: STATION_RAIN 站点雨量(仅实况) / GRID_RAIN 格点雨量(仅预报) / AREA_RAIN 面雨量
            parent_area_id: 一级分区 ID（来自 query_basin_drawing_areas）
            area_codes: 二级分区编码，逗号分隔；ALL 表示该一级分区全部二级分区
            begin_time / end_time: 北京时 "YYYY-MM-DD HH:mm"，自动规整到 10 分钟刻度，跨度 ≤10 天
            main_title / sub_title: 图标题
            show_rain_value: 是否显示雨量数值，默认 true
            show_area_name: 是否显示分区名称，默认 true
            forecast_time: 预报起报时间（FORECAST 自动取最近 08/20 时次，一般无需传）
            force_create: 是否强制重新出图，1=强制
        """
        return generate_basin_rainfall_image_core(
            scene_type=scene_type,
            product_type=product_type,
            parent_area_id=parent_area_id,
            area_codes=area_codes,
            begin_time=begin_time,
            end_time=end_time,
            main_title=main_title,
            sub_title=sub_title,
            show_rain_value=show_rain_value,
            show_area_name=show_area_name,
            forecast_time=forecast_time,
            force_create=force_create,
        )
