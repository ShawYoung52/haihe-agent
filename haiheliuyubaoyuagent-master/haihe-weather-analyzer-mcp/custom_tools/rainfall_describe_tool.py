"""14所降水实况文字长图工具（/openapi/rainfall_describe/real）。

业务场景：用户问「降水实况文字 / 生成降水实况 / 降水实况长图」时，调用 14所接口生成
实况降水的文字长图并展示。只出图，不返回雨量数值。

口径（遵循同端口既有 get_station_rainfall_real_img 类比）：
- 接口返回图片 base64 字符串（响应 `data` 字段，防御性取 data/result/image/base64 多键）；
  走 chainlitexam `_run_tool_round` 特判的 base64 → cl.Image 展示路径。
- **不改动** chainlitexam/qa_http_api.py 的 `_IMAGE_URL_ALLOW_HOSTS` 网络安全白名单：
  图片以本地落盘文件 URL 经 HTTP images 字段下发，不暴露内网出图代理 URL。
- 兼容上游三种返回：裸 base64（剥 `data:...;base64,` 前缀）、图片 URL（含相对路径，
  自动拼 base 后拉取）、均做**图片魔数校验**，无效内容按 no_data 处理不报成功。
- base 默认 http://10.226.107.35:8001，env RAINFALL_DESCRIBE_API_BASE 可覆盖。
- 实时出图，不做 TTL 缓存（与 basin_drawing 的静态分区清单不同）。
"""
from __future__ import annotations

import base64
import os
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests
from fastmcp import FastMCP

BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")

RAINFALL_DESCRIBE_API_BASE = os.getenv(
    "RAINFALL_DESCRIBE_API_BASE", "http://10.226.107.35:8001"
)
DESCRIBE_URL = f"{RAINFALL_DESCRIBE_API_BASE}/openapi/rainfall_describe/real"

# 海河 9 大分区区域 id（与 get_station_rainfall_real_img 默认一致）。
DEFAULT_AREA_IDS = [6, 7, 8, 9, 10, 11, 12, 13, 14]

# 出图超时：承接上游长图生成耗时，留足余量（与 basin_drawing 出图一致 60s）。
_DESCRIBE_TIMEOUT = 60
_URL_FETCH_TIMEOUT = 30
# URL 口径拉取字节上限：防超大图撑爆内存（base64 会再放大 ~1.33x）。
_MAX_IMAGE_BYTES = 20 * 1024 * 1024

# 内网地址/路径脱敏（CLAUDE.md 约定：错误文本返给 LLM/用户前去掉 IP/路径）。
_SCRUB_PATTERNS = [
    (re.compile(r"https?://[^\s\"'<>|]+"), "[内网地址]"),  # 完整 URL（含 host+path）
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "[内网地址]"),
    (re.compile(r"[A-Za-z]:\\[^\s\"'<>|]+"), "[路径]"),  # Windows 绝对路径
    (re.compile(r"(?<![\w.])/(?:home|usr|var|etc|opt|root|openapi|hhly|hhfw|img)/[^\s\"'<>|]*"), "[路径]"),
]

# 常见栅格图片魔数，用于校验 base64/URL 响应确实是图片，而非错误文案或垃圾串。
_IMAGE_MAGIC = (
    b"\x89PNG",      # PNG
    b"\xff\xd8\xff",  # JPEG
    b"GIF8",         # GIF
    b"RIFF",         # WebP（RIFF....WEBP）
    b"BM",           # BMP
    b"II*\x00",      # TIFF（小端）
    b"MM\x00*",      # TIFF（大端）
)


def _scrub_text(text: Any) -> str:
    """去掉内网 URL/IP/路径，防止把出图代理地址带进 LLM/用户输出。"""
    out = str(text)
    for pattern, repl in _SCRUB_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def _safe_err(exc: Exception) -> str:
    """异常文本脱敏（含 URL host 与接口路径）。"""
    return _scrub_text(str(exc))


def _is_plausible_image(data: bytes) -> bool:
    return bool(data) and any(data.startswith(m) for m in _IMAGE_MAGIC)


def _fetch_url_bytes(url: str) -> bytes:
    """拉取图片字节并做大小/魔数校验，失败抛异常。"""
    resp = requests.get(url, timeout=_URL_FETCH_TIMEOUT)
    resp.raise_for_status()
    content = resp.content
    if len(content) > _MAX_IMAGE_BYTES:
        raise ValueError("图片数据过大")
    if not _is_plausible_image(content):
        raise ValueError("响应不是有效图片")
    return content


def _resolve_image_base64(payload: Any) -> str:
    """从上游响应里稳健地提取图片内容，统一归一化为 base64 字符串。

    兼容三种口径：
    1. 裸 base64（含 `data:image/png;base64,` 前缀，已剥除）→ 解码后校验魔数再返回；
    2. 图片绝对 URL（http/https）→ 拉取字节校验后转 base64；
    3. 图片相对路径（/hhly/...）→ 拼 base 后拉取转 base64（与 basin_drawing 同族）。

    无效内容（错误文案/垃圾串/非图片）返回空串，由调用方按 no_data 处理，绝不报成功。
    """
    if isinstance(payload, str):
        raw: Any = payload
    elif isinstance(payload, dict):
        raw = (
            payload.get("data")
            or payload.get("result")
            or payload.get("image")
            or payload.get("base64")
            or ""
        )
    else:
        raw = ""

    # 上游 data 可能再包一层 dict（{base64:.../img:.../url:...}），继续下钻。
    while isinstance(raw, dict):
        raw = (
            raw.get("base64")
            or raw.get("data")
            or raw.get("img")
            or raw.get("image")
            or raw.get("url")
            or ""
        )

    if not isinstance(raw, str):
        raw = str(raw)
    raw = raw.strip()
    if not raw:
        return ""

    if raw.lower().startswith("http"):
        return base64.b64encode(_fetch_url_bytes(raw)).decode("ascii")
    if raw.startswith("/"):
        url = f"{RAINFALL_DESCRIBE_API_BASE.rstrip('/')}{raw}"
        return base64.b64encode(_fetch_url_bytes(url)).decode("ascii")

    # base64 口径：剥离 data:<mime>;base64, 前缀，解码后校验魔数。
    if "," in raw:
        raw = raw.split(",", 1)[1]
    raw = raw.strip()
    if not raw:
        return ""
    try:
        decoded = base64.b64decode(raw, validate=False)
    except Exception:
        return ""
    if not _is_plausible_image(decoded):
        return ""
    return raw


def _window_hours(begin: str, end: str) -> int:
    """由显式时间窗计算小时数；解析失败或窗口非法回落 24。"""

    def _parse(s: str) -> datetime | None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H"):
            try:
                return datetime.strptime(s.strip(), fmt)
            except ValueError:
                continue
        return None

    b, e = _parse(begin), _parse(end)
    if b is None or e is None or e <= b:
        return 24
    return max(1, int((e - b).total_seconds() // 3600))


def generate_rainfall_describe_longimg_core(
    beginTime: str = "",
    endTime: str = "",
    areaIds: list | None = None,
    interval: int = 24,
    range: str = "9",
    type: str = "0",
    isClimateImg: bool = False,
) -> dict[str, Any]:
    """POST 生成降水实况文字长图，返回 base64 图片数据与查询窗口。"""
    try:
        interval_hours = int(interval) if interval not in (None, "") else 24
    except (TypeError, ValueError):
        interval_hours = 24
    if interval_hours < 1:
        interval_hours = 1

    now = datetime.now(BEIJING_TIMEZONE)
    explicit_window = bool(beginTime and endTime)
    if not endTime:
        endTime = now.strftime("%Y-%m-%d %H:00:00")
    if not beginTime:
        beginTime = (now - timedelta(hours=interval_hours)).strftime("%Y-%m-%d %H:00:00")

    # begin/end 均显式给出且未指定 interval（保持默认 24）时，interval 自动对齐窗口
    # 时长——符合接口「超过24h使用累计」语义（如 48h 窗口应传 interval=48）。
    # planner 显式传了其它 interval 则保留其值。
    if explicit_window and interval_hours == 24:
        interval_hours = _window_hours(beginTime, endTime)

    if not areaIds:
        area_ids = DEFAULT_AREA_IDS
    else:
        area_ids = []
        for a in areaIds:
            try:
                area_ids.append(int(a))
            except (TypeError, ValueError):
                continue
        if not area_ids:
            area_ids = DEFAULT_AREA_IDS

    payload: dict[str, Any] = {
        "areaIds": area_ids,
        "beginTime": beginTime,
        "endTime": endTime,
        "interval": interval_hours,
        "range": str(range or "9"),
        "type": str(type or "0"),
        "isClimateImg": bool(isClimateImg),
    }

    try:
        resp = requests.post(DESCRIBE_URL, json=payload, timeout=_DESCRIBE_TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        return {"status": "error", "message": f"获取降水实况文字失败：{_safe_err(exc)}", "base64": ""}

    try:
        b64 = _resolve_image_base64(body)
    except Exception as exc:
        return {"status": "error", "message": f"降水实况文字图片获取失败：{_safe_err(exc)}", "base64": ""}

    if not b64:
        msg = _scrub_text(body.get("msg")) if isinstance(body, dict) else ""
        return {
            "status": "no_data",
            "message": msg or "接口未返回降水实况文字图片，请确认时段内是否有有效降水实况数据。",
            "base64": "",
            "beginTime": beginTime,
            "endTime": endTime,
        }

    return {
        "status": "ok",
        "base64": b64,
        "beginTime": beginTime,
        "endTime": endTime,
        "interval": interval_hours,
        "range": str(range or "9"),
        "type": str(type or "0"),
        "message": "已生成降水实况文字长图。",
    }


def register_rainfall_describe_tool(mcp: FastMCP) -> None:
    @mcp.tool()
    def generate_rainfall_describe_longimg(
        beginTime: str = "",
        endTime: str = "",
        areaIds: list = None,
        interval: int = 24,
        range: str = "9",
        type: str = "0",
        isClimateImg: bool = False,
    ) -> dict:
        """
        生成海河流域降水实况文字长图（14所）/openapi/rainfall_describe/real。

        仅当用户要「降水实况文字 / 降水实况文字长图 / 生成降水实况文字」，或**只说
        "长图 / 生成长图 / 出一张长图"而无具体图类型**（本智能体的"长图"即降水实况
        文字长图）时调用，图片用 base64 渲染后由前端自动展示。此工具只出图、不返回
        雨量数值，不能用于回答 "下了多少雨 / 天气怎么样 / 面雨量多少" 等数值查询。

        与「各子流域降雨分布图」get_station_rainfall_real_img 区分：那是分区面雨量的
        **空间分布图**（问句含"降水实况图/降雨分布图/面雨量分布图"）；本工具是降水
        实况的**文字**长图（含分区雨情综述文字）。"长图/文字长图"不属空间分布图，
        默认走本工具。

        Args:
            beginTime: 开始时间，格式 "YYYY-MM-DD HH:mm:ss"（北京时），不传取当前
                前推 interval 小时
            endTime: 结束时间，格式同上，不传取当前整点
            areaIds: 区域 id 列表，默认海河9大分区 [6,7,8,9,10,11,12,13,14]
            interval: 间隔(小时)，默认 24；begin/end 均给出且未指定 interval 时自动
                对齐窗口时长（超 24h 按累计，如 48h 窗口 interval=48）
            range: 分区，默认 "9"，可传 "9" 或 "11"
            type: 站点类型，"0"=国家站，"1"=区域站，默认 "0"
            isClimateImg: 出图文字颜色是否黑色，默认 False
        """
        return generate_rainfall_describe_longimg_core(
            beginTime=beginTime,
            endTime=endTime,
            areaIds=areaIds,
            interval=interval,
            range=range,
            type=type,
            isClimateImg=isClimateImg,
        )
