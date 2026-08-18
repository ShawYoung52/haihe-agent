"""14所降水专题组合长图工具。

业务场景：用户问「长图 / 组合长图 / 降水专题长图 / 出今天的长图」等时，调用多个
14所接口，把降水实况文字、swan3 组合反射率雷达图、降水实况图、实况面雨量图、
点雨量列表、预报面雨量图、面雨量预报**纵向拼成一张组合长图**（PNG）返回 base64。

口径（用户确认 2026-08-17）：
- 板块顺序（从上到下）：降水实况文字 → swan3 雷达图 → 降水实况图 → 实况面雨量图
  → 点雨量列表 → 预报面雨量图 → 面雨量预报（实况+预报都要）。
- 任意"长图"话术触发（如"请输出今天的长图"）。
- 每个子接口**独立容错**：失败板块显示中文占位，其余板块照常拼接，不整图失败。
- base 默认 http://10.226.107.35:8001，env RAINFALL_DESCRIBE_API_BASE 可覆盖。
- 中文字体自动探测（env RAINFALL_DESCRIBE_FONT 可指定）；缺字体时降级返回
  `base64:""` + `text`（各文字板块内容），由前端展示，不报错。
- **不改动** _IMAGE_URL_ALLOW_HOSTS 白名单：图片以本地落盘文件 URL 经 HTTP images 下发。
"""
from __future__ import annotations

import base64
import io
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
BASE = RAINFALL_DESCRIBE_API_BASE.rstrip("/")

# 海河 9 大分区区域 id。
DEFAULT_AREA_IDS = [6, 7, 8, 9, 10, 11, 12, 13, 14]

_TIMEOUT = 60
_FETCH_TIMEOUT = 30
_MAX_IMAGE_BYTES = 20 * 1024 * 1024

# 内网地址/路径脱敏（CLAUDE.md 约定）。
_SCRUB_PATTERNS = [
    (re.compile(r"https?://[^\s\"'<>|]+"), "[内网地址]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "[内网地址]"),
    (re.compile(r"[A-Za-z]:\\[^\s\"'<>|]+"), "[路径]"),
    (re.compile(r"(?<![\w.])/(?:home|usr|var|etc|opt|root|openapi|hhly|hhfw|img)/[^\s\"'<>|]*"), "[路径]"),
]

# 常见栅格图片魔数。
_IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF", b"BM", b"II*\x00", b"MM\x00*")

# 长图版式（严格对齐示范图 download.png）：
#   外层浅蓝背景(bg 色) + 顶部全幅风景头图(top-bg 上段,叠加白色大标题) +
#   内嵌白色内容卡片 + 3 大主题板块(居中蓝色标题+两侧装饰线) + 大幅带框地图 +
#   浅蓝表头表格(点雨量排名表 / 河系雨量矩阵预报表) + 底部发布单位。
_ASSETS_DIR = os.getenv(
    "LONGIMG_ASSETS_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "longimg"),
)
_BOARD_WIDTH = 1125          # 画布宽 = 模板宽
_BG = (131, 207, 251)        # 外层浅蓝背景（= bg.png 实测色）
_CARD_MARGIN = 38            # 白色内容卡片左右外边距（示范图实测 ~19px@561 → 38@1125）
_CARD_PAD = 42               # 卡片内边距
_CARD_TOP_OVERLAP = 0        # 卡片与头图衔接（0=对接）
_HEADER_H = 600              # 顶部风景头图高度（示范图 ~300px@561 → 600@1125）
_HEADER_UNIT = "发布单位：海河流域气象中心"
_HEADER_TITLE_1 = "海河流域水文气象"
_HEADER_TITLE_2 = "公报"
_UNIT_SIZE = 42
_TITLE_SIZE = 130            # 大标题字号（7字≈910px，两侧留 ~110px 边距，对齐示范图）
_SHADOW = (18, 74, 96)       # 头图白色标题的阴影色
_SEC_SIZE = 48               # 板块标题字号
_SEC_BLUE = (35, 118, 208)   # 板块标题蓝（实测示范图）
_RULE = (150, 196, 236)      # 板块标题两侧装饰线
_CAPTION_SIZE = 34           # 图/表小标题字号（深灰）
_BODY_SIZE = 31              # 正文（降水实况文字）
_TBL_SIZE = 29               # 表格字号
_TBL_HEAD = (206, 234, 251)  # 表头浅蓝（实测）
_TBL_LINE = (168, 198, 226)  # 表格线
_TBL_ROW_H = 58
_FG = (38, 38, 38)           # 正文深灰
_IMG_BORDER = (176, 190, 204)  # 地图外框
_IMG_MAX_H = 980             # 单张地图最大高度
_BOTTOM_PAD = 56
_PUB_W = 460                 # 底部发布单位缩放宽度
_FORECAST_HOURS = 72         # 公报口径：预报覆盖未来 72h（⑥图为 3 天累计）
_FORECAST_DAYS = 3           # ⑦按 24h 拆成 3 张"河系/雨量"预报表
_RANGE = range               # 工具函数签名有 range 参数会遮蔽内建，这里留别名

# 模板图懒加载缓存
_TEMPLATE_CACHE: dict[str, Any] = {}


def _load_template(name: str) -> Any | None:
    """加载长图模板（bg/top-bg/title/publish-depart），懒加载缓存；缺失返回 None。"""
    cached = _TEMPLATE_CACHE.get(name)
    if cached is not None:
        return cached
    path = os.path.join(_ASSETS_DIR, name)
    if not os.path.isfile(path):
        return None
    try:
        from PIL import Image
        img = Image.open(path)
        _TEMPLATE_CACHE[name] = img
        return img
    except Exception:
        return None


def _scrub_text(text: Any) -> str:
    out = str(text)
    for pattern, repl in _SCRUB_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def _safe_err(exc: Exception) -> str:
    return _scrub_text(str(exc))


def _find_cjk_font() -> str | None:
    """探测系统可用中文字体；env RAINFALL_DESCRIBE_FONT 可显式指定。

    不依赖联网安装：依次尝试 ①显式指定 ②常见路径 ③fc-list ④matplotlib 字体表。
    内网服务器可能没装 yum 中文字体，但 conda 环境 / matplotlib 常自带 CJK 字体。
    """
    override = os.getenv("RAINFALL_DESCRIBE_FONT", "").strip()
    candidates: list[str] = []
    if override:
        candidates.append(override)
    if os.name == "nt":
        candidates += [
            r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc",
            r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simsun.ttc",
            r"C:\Windows\Fonts\Deng.ttf",
        ]
    else:
        candidates += [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/simhei.ttf",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
            "/usr/share/fonts/truetype/arphic/uming.ttc",
            "/usr/share/fonts/truetype/arphic/ukai.ttc",
            "/opt/conda/fonts/NotoSansCJK-Regular.ttc",
            "/opt/anaconda3/fonts/NotoSansCJK-Regular.ttc",
            "/usr/local/share/fonts/NotoSansCJK-Regular.ttc",
            "/usr/lib/fonts/NotoSansCJK-Regular.ttc",
        ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p

    # fc-list :lang=zh（系统已装 fontconfig 时最可靠）
    try:
        import subprocess
        out = subprocess.check_output(
            ["fc-list", ":lang=zh", "file"], stderr=subprocess.DEVNULL, timeout=10
        )
        for line in out.decode("utf-8", "ignore").splitlines():
            path = line.split(":", 1)[0].strip()
            if path and os.path.isfile(path):
                return path
    except Exception:
        pass

    # matplotlib 已注册的中文字体（项目用 matplotlib 画图，若中文正常则此处必命中）
    try:
        import matplotlib.font_manager as fm
        for f in fm.fontManager.ttflist:
            if any(k in f.name for k in ("CJK", "Hei", "Song", "Wen", "Noto", "Droid", "AR PL", "Kai", "Ming")):
                if os.path.isfile(f.fname):
                    return f.fname
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- 子接口

def _resolve_image_bytes(payload: Any) -> bytes | None:
    """从 14所 接口响应里提取图片字节（兼容 base64 / URL / 相对路径）。失败返回 None。"""
    if isinstance(payload, str):
        raw: Any = payload
    elif isinstance(payload, dict):
        raw = payload.get("data") or payload.get("result") or payload.get("image") or payload.get("base64") or ""
    else:
        raw = ""
    while isinstance(raw, dict):
        raw = raw.get("base64") or raw.get("data") or raw.get("img") or raw.get("image") or raw.get("url") or ""
    if not isinstance(raw, str):
        raw = str(raw)
    raw = raw.strip()
    if not raw:
        return None
    try:
        if raw.lower().startswith("http"):
            return _fetch_bytes(raw)
        if raw.startswith("/"):
            return _fetch_bytes(f"{BASE}{raw}")
        if "," in raw:
            raw = raw.split(",", 1)[1]
        data = base64.b64decode(raw, validate=False)
        if _is_plausible_image(data):
            return data
    except Exception:
        return None
    return None


def _is_plausible_image(data: bytes) -> bool:
    return bool(data) and any(data.startswith(m) for m in _IMAGE_MAGIC)


def _fetch_bytes(url: str) -> bytes:
    resp = requests.get(url, timeout=_FETCH_TIMEOUT)
    resp.raise_for_status()
    content = resp.content
    if len(content) > _MAX_IMAGE_BYTES:
        raise ValueError("图片数据过大")
    if not _is_plausible_image(content):
        raise ValueError("响应不是有效图片")
    return content


def _post(url: str, payload: dict) -> Any:
    resp = requests.post(url, json=payload, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _fmt_hh(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:00:00")


def _window(interval_hours: int) -> tuple[str, str]:
    now = datetime.now(BEIJING_TIMEZONE)
    end = now.replace(minute=0, second=0, microsecond=0)
    begin = end - timedelta(hours=interval_hours)
    return _fmt_hh(begin), _fmt_hh(end)


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


def _fetch_describe_text(begin: str, end: str, area_ids: list[int], interval: int, range_: str, type_: str) -> str:
    """① 降水实况文字。"""
    body = _post(f"{BASE}/openapi/rainfall_describe/real", {
        "areaIds": area_ids, "beginTime": begin, "endTime": end,
        "interval": interval, "range": range_, "type": type_,
    })
    raw = body.get("data") if isinstance(body, dict) else body
    return str(raw).strip() if isinstance(raw, str) and raw.strip() else ""


def _fetch_swan3(query_time: str) -> bytes | None:
    """② swan3 组合反射率雷达图（GET）。"""
    resp = requests.get(
        f"{BASE}/openapi/tqStation/querySwan3Img",
        params={"areaCode": "tj", "queryTime": query_time},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return _resolve_image_bytes(resp.json())


def _fetch_station_rain_img(begin: str, end: str, area_ids: list[int], interval: int, range_: str, type_: str) -> bytes | None:
    """③ 降水实况图。"""
    return _resolve_image_bytes(_post(f"{BASE}/openapi/meteor_img/stationRainRealImg?forceCreate=1", {
        "areaIds": area_ids, "beginTime": begin, "endTime": end,
        "interval": interval, "range": range_, "type": type_, "isClimateImg": False,
    }))


def _fetch_area_rain_real_img(begin: str, end: str, area_ids: list[int], interval: int, range_: str, type_: str) -> bytes | None:
    """④ 实况面雨量图。"""
    return _resolve_image_bytes(_post(f"{BASE}/openapi/meteor_img/area_rain_real_img?forceCreate=1", {
        "areaIds": area_ids, "beginTime": begin, "endTime": end,
        "interval": interval, "range": range_, "type": type_, "isClimateImg": False,
    }))


def _fetch_station_list(begin: str, end: str, interval: int, type_: str) -> list[dict]:
    """⑤ 点雨量列表（站点数据）。"""
    body = _post(f"{BASE}/openapi/area_rain_station/list", {
        "areaIds": DEFAULT_AREA_IDS, "beginTime": begin, "endTime": end,
        "interval": interval, "sourceType": 2, "type": type_,
    })
    data = body.get("data") if isinstance(body, dict) else body
    return [d for d in (data or []) if isinstance(d, dict)]


def _fetch_area_rain_fore_img(fore_time: str, begin: str, end: str, area_ids: list[int], interval: int) -> bytes | None:
    """⑥ 预报面雨量图。"""
    return _resolve_image_bytes(_post(f"{BASE}/openapi/meteor_img/area_rain_fore_img?forceCreate=1", {
        "areaIds": area_ids, "foreTime": fore_time, "beginTime": begin, "endTime": end,
        "intval": interval, "modelTypes": ["ECMF"], "range": "9",
    }))


def _fetch_forecast(fore_time: str, begin: str, end: str, area_ids: list[int], interval: int) -> list[dict]:
    """⑦ 面雨量预报（分区数据）。"""
    body = _post(f"{BASE}/openapi/area_rainfall/forecast", {
        "areaIds": area_ids, "foreTime": fore_time, "beginTime": begin, "endTime": end,
        "intval": interval, "modelTypes": ["ECMF"], "range": "9",
    })
    data = body.get("data") if isinstance(body, dict) else body
    return [d for d in (data or []) if isinstance(d, dict)]


def _fore_cycle_candidates(max_days: int = 4) -> list[str]:
    """最近 N 天 08/20 起报时次（新→旧），用于预报接口时次回退。

    14所 的某一起报时次预报数据未就绪时出图接口会返回 500（实测今天 08 时次 500、
    昨天 08 时次 200），因此按序尝试最近时次，取第一个能出图的。
    """
    now = datetime.now(BEIJING_TIMEZONE)
    cycles: list[datetime] = []
    for offset in range(0, max_days):
        day = now.replace(hour=8, minute=0, second=0, microsecond=0) - timedelta(days=offset)
        cycles.append(day)
        cycles.append(day + timedelta(hours=12))  # 20 时
    seen: set[str] = set()
    out: list[str] = []
    for c in sorted(cycles, reverse=True):
        key = c.strftime("%Y-%m-%d %H:00:00")
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


# ---------------------------------------------------------------- 渲染

def _wrap_text(draw, font, text: str, max_width: int) -> list[str]:
    lines: list[str] = []
    for raw_line in str(text).split("\n"):
        if not raw_line:
            lines.append("")
            continue
        current = ""
        for ch in raw_line:
            trial = current + ch
            if draw.textlength(trial, font=font) <= max_width:
                current = trial
            else:
                if current:
                    lines.append(current)
                current = ch
        if current:
            lines.append(current)
    return lines


def _load_image(data: bytes) -> Any | None:
    """字节 → PIL Image（RGB），失败返回 None。"""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        if img.mode != "RGB":
            img = img.convert("RGB")
        return img
    except Exception:
        return None


def _fit_image(img: Any, usable_w: int) -> Any:
    """等比缩放子图到内容宽度（最大高度 _IMG_MAX_H）。"""
    w, h = img.size
    scale = min(1.0, usable_w / w if w else 1.0, _IMG_MAX_H / h if h else 1.0)
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    return img


def _header_band() -> Any | None:
    """裁出顶部风景头图（top-bg 上段，含山水库坝、避开底部水印），缩放到画布宽。"""
    top_bg = _load_template("top-bg.png")
    if top_bg is None:
        return None
    try:
        tw, th = top_bg.size
        src_h = min(th, int(tw * _HEADER_H / _BOARD_WIDTH))
        band = top_bg.crop((0, 0, tw, src_h)).convert("RGB")
        return band.resize((_BOARD_WIDTH, _HEADER_H))
    except Exception:
        return None


def _draw_centered(draw, cx: int, y: int, text: str, font, fill) -> None:
    w = draw.textlength(text, font=font)
    draw.text((cx - w / 2, y), text, font=font, fill=fill)


def _draw_header(img: Any, draw, fonts) -> None:
    """顶部全幅风景头图 + 白色大标题（发布单位 + 海河流域水文气象公报）。"""
    band = _header_band()
    if band is not None:
        img.paste(band, (0, 0))
    else:  # 模板缺失：渐变蓝条兜底
        draw.rectangle([0, 0, _BOARD_WIDTH, _HEADER_H], fill=(70, 150, 210))
    cx = _BOARD_WIDTH // 2

    def _white(cx_, y_, text, font):
        draw.text((cx_ - draw.textlength(text, font=font) / 2 + 4, y_ + 4), text, font=font, fill=_SHADOW)
        draw.text((cx_ - draw.textlength(text, font=font) / 2, y_), text, font=font, fill=(255, 255, 255))

    # 发布单位（左上）
    draw.text((44 + 3, 34 + 3), _HEADER_UNIT, font=fonts["unit"], fill=_SHADOW)
    draw.text((44, 34), _HEADER_UNIT, font=fonts["unit"], fill=(255, 255, 255))
    # 大标题（居中两行）
    _white(cx, 158, _HEADER_TITLE_1, fonts["title"])
    _white(cx, 340, _HEADER_TITLE_2, fonts["title"])


def _draw_sec_icon(draw, cx: float, cy: float, key: str) -> None:
    """板块标题左侧小图标（radar/cloud/cloudsun），~52px，矢量绘制。"""
    import math
    if key == "radar":
        # 支架 + 抛物面天线 + 馈源
        draw.polygon([(cx - 14, cy + 24), (cx + 14, cy + 24), (cx + 4, cy + 2), (cx - 4, cy + 2)],
                     fill=(120, 130, 145))
        draw.pieslice([cx - 24, cy - 22, cx + 24, cy + 26], start=200, end=340,
                      fill=(70, 130, 200), outline=(40, 90, 150), width=2)
        draw.line([cx, cy + 2, cx + 14, cy - 12], fill=(40, 90, 150), width=3)
        draw.ellipse([cx + 11, cy - 15, cx + 17, cy - 9], fill=(230, 90, 60))
    elif key in ("cloud", "cloudsun"):
        if key == "cloudsun":
            # 太阳（右上，光芒）
            scx, scy, sr = cx + 16, cy - 14, 12
            for ang in range(0, 360, 45):
                dx = math.cos(math.radians(ang)); dy = math.sin(math.radians(ang))
                draw.line([scx + dx * (sr + 2), scy + dy * (sr + 2), scx + dx * (sr + 8), scy + dy * (sr + 8)],
                          fill=(240, 170, 40), width=3)
            draw.ellipse([scx - sr, scy - sr, scx + sr, scy + sr], fill=(250, 190, 60), outline=(230, 150, 30))
        # 云（三圆 + 平底）
        cloud = (150, 165, 185) if key == "cloud" else (170, 180, 198)
        draw.ellipse([cx - 24, cy - 6, cx - 2, cy + 16], fill=cloud)
        draw.ellipse([cx - 8, cy - 14, cx + 16, cy + 14], fill=cloud)
        draw.ellipse([cx + 8, cy - 4, cx + 26, cy + 16], fill=cloud)
        draw.rectangle([cx - 22, cy + 4, cx + 24, cy + 16], fill=cloud)
        if key == "cloud":
            # 雨滴
            for rx in (-14, 0, 14):
                draw.line([cx + rx, cy + 20, cx + rx - 4, cy + 30], fill=(70, 130, 200), width=3)


def _sec_header_h() -> int:
    return int(_SEC_SIZE * 1.4) + 26


def _draw_sec_header(draw, y: int, title: str, font, icon: str = "") -> int:
    """板块标题：居中蓝色大字 + 左侧小图标 + 两侧装饰线（贴近示范图）。返回结束 y。"""
    cx = _BOARD_WIDTH // 2
    cy = y + int(_SEC_SIZE * 0.7)
    tw = draw.textlength(title, font=font)
    line_y = cy + _SEC_SIZE // 2
    # 图标在标题左侧（图标+标题整体仍近似居中，故把标题略右移）
    shift = 34 if icon else 0
    tcx = cx + shift
    if icon:
        _draw_sec_icon(draw, tcx - tw / 2 - 48, cy + _SEC_SIZE * 0.28, icon)
    for sign in (-1, 1):
        x_near = tcx + sign * (tw / 2 + 40)
        x_far = cx + sign * (_BOARD_WIDTH / 2 - _CARD_MARGIN - _CARD_PAD)
        draw.line([x_near, line_y, x_far, line_y], fill=_RULE, width=3)
        bx = tcx + sign * (tw / 2 + 18)
        draw.line([bx - 6, line_y, bx + 6, line_y], fill=_SEC_BLUE, width=5)
    _draw_centered(draw, tcx, cy, title, font, _SEC_BLUE)
    return y + _sec_header_h()


def _measure_text(draw, font, text: str, usable_w: int) -> int:
    lines = _wrap_text(draw, font, str(text or ""), usable_w)
    return max(len(lines), 1) * int(_BODY_SIZE * 1.6) + 8


def _render_text_block(draw, font, x: int, y: int, usable_w: int, text: str) -> int:
    line_h = int(_BODY_SIZE * 1.6)
    lines = _wrap_text(draw, font, str(text or ""), usable_w) or ["（暂无内容）"]
    for ln in lines:
        draw.text((x, y), ln, font=font, fill=_FG)
        y += line_h
    return y + 8


def _measure_caption(font) -> int:
    return int(_CAPTION_SIZE * 1.5) + 12


def _render_caption(draw, font, y: int, text: str) -> int:
    _draw_centered(draw, _BOARD_WIDTH // 2, y + 6, text, font, _FG)
    return y + _measure_caption(font)


def _measure_image(img: Any, usable_w: int) -> int:
    return _fit_image(img, usable_w).size[1] + 20


def _render_image_block(img: Any, draw, x: int, y: int, usable_w: int, sub: Any) -> int:
    """地图：白底 + 细框 + 居中（14所 图自带红色小标题）。"""
    sub = _fit_image(sub, usable_w)
    px = x + (usable_w - sub.size[0]) // 2
    draw.rectangle([px - 8, y + 2, px + sub.size[0] + 8, y + sub.size[1] + 12],
                   fill=(255, 255, 255), outline=_IMG_BORDER, width=2)
    img.paste(sub, (px, y + 7))
    return y + sub.size[1] + 20


_RANK_COLS = (0.12, 0.28, 0.20, 0.24, 0.16)  # 序号|站点|省|市|降水量


def _measure_rank(rows: list) -> int:
    return _TBL_ROW_H * (1 + min(len(rows), 15)) + 10


def _render_rank(draw, font, x: int, y: int, usable_w: int, headers: list[str], rows: list[list[str]]) -> int:
    """点雨量排名表：浅蓝表头 + 白行 + 横线，居中。"""
    widths = [usable_w * f for f in _RANK_COLS]
    xs = [x]
    for w in widths[:-1]:
        xs.append(xs[-1] + w)

    def _cell(cx, cw, text, bold=False, fg=_FG):
        t = str(text or "")
        while t and draw.textlength(t, font=font) > cw - 12:
            t = t[:-1]
        tw = draw.textlength(t, font=font)
        draw.text((cx + (cw - tw) / 2, y + (_TBL_ROW_H - font.size) // 2 - 2), t, font=font, fill=fg)

    # 表头
    draw.rectangle([x, y, x + usable_w, y + _TBL_ROW_H], fill=_TBL_HEAD)
    save_y = y
    for i, h in enumerate(headers):
        _cell(xs[i], widths[i], h)
    y += _TBL_ROW_H
    for row in rows[:15]:
        for i, c in enumerate(row):
            _cell(xs[i], widths[i], c)
        y += _TBL_ROW_H
        draw.line([x, y, x + usable_w, y], fill=_TBL_LINE, width=1)
    draw.rectangle([x, save_y, x + usable_w, y], outline=_TBL_LINE, width=2)
    return y + 10


def _measure_fore(values: list) -> int:
    groups = max(1, (len(values) + 2) // 3)
    return _TBL_ROW_H * groups * 2 + 8


def _render_fore(draw, font, x: int, y: int, usable_w: int, rivers: list[str], values: list[str]) -> int:
    """面雨量预报矩阵：河系(浅蓝)/雨量(白) 成对行，每行 3 个分区。"""
    label_w = usable_w * 0.16
    col_w = (usable_w - label_w) / 3
    pairs = list(zip(rivers, values))
    groups = [pairs[i:i + 3] for i in range(0, len(pairs), 3)] or [[]]

    def _row(y_, label, cells, head):
        bg = _TBL_HEAD if head else (255, 255, 255)
        draw.rectangle([x, y_, x + usable_w, y_ + _TBL_ROW_H], fill=bg)
        tw = draw.textlength(label, font=font)
        draw.text((x + (label_w - tw) / 2, y_ + (_TBL_ROW_H - font.size) // 2 - 2), label, font=font, fill=_FG)
        for i in range(3):
            cell = cells[i] if i < len(cells) else ""
            t = str(cell)
            while t and draw.textlength(t, font=font) > col_w - 10:
                t = t[:-1]
            tw = draw.textlength(t, font=font)
            cx = x + label_w + i * col_w
            draw.text((cx + (col_w - tw) / 2, y_ + (_TBL_ROW_H - font.size) // 2 - 2), t, font=font, fill=_FG)
        # 竖线
        for i in range(4):
            vx = x + label_w + i * col_w
            draw.line([vx, y_, vx, y_ + _TBL_ROW_H], fill=_TBL_LINE, width=1)
        draw.line([x, y_, x, y_ + _TBL_ROW_H], fill=_TBL_LINE, width=1)
        draw.line([x + usable_w, y_, x + usable_w, y_ + _TBL_ROW_H], fill=_TBL_LINE, width=1)

    top = y
    for g in groups:
        names = [p[0] for p in g]
        vals = [p[1] for p in g]
        _row(y, "河系", names, head=True)
        y += _TBL_ROW_H
        draw.line([x, y, x + usable_w, y], fill=_TBL_LINE, width=1)
        _row(y, "雨量", vals, head=False)
        y += _TBL_ROW_H
        draw.line([x, y, x + usable_w, y], fill=_TBL_LINE, width=1)
    draw.rectangle([x, top, x + usable_w, y], outline=_TBL_LINE, width=2)
    return y + 8


def _measure_block(draw, fonts, usable_w: int, kind: str, payload: Any) -> int:
    if kind == "text":
        return _measure_text(draw, fonts["body"], payload, usable_w)
    if kind == "caption":
        return _measure_caption(fonts["caption"])
    if kind == "image":
        return _measure_image(payload, usable_w)
    if kind == "rank":
        return _measure_rank(payload[1])
    if kind == "fore":
        return _measure_fore(payload[2])
    return 0


def _render_block(img, draw, fonts, x: int, y: int, usable_w: int, kind: str, payload: Any) -> int:
    if kind == "text":
        return _render_text_block(draw, fonts["body"], x, y, usable_w, payload)
    if kind == "caption":
        return _render_caption(draw, fonts["caption"], y, payload)
    if kind == "image":
        return _render_image_block(img, draw, x, y, usable_w, payload)
    if kind == "rank":
        return _render_rank(draw, fonts["tbl"], x, y, usable_w, payload[0], payload[1])
    if kind == "fore":
        return _render_fore(draw, fonts["tbl"], x, y, usable_w, payload[1], payload[2])
    return y


def _compose_longimg(sections: list[dict]) -> bytes | None:
    """把 3 大主题板块渲染成一张严格对齐示范图的模板化长图 PNG。

    sections: [{"header": 板块名, "blocks": [(kind, payload), ...]}]，kind ∈
    text/image/caption/rank/fore。缺 Pillow/缺中文字体返回 None（调用方降级文字）。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    font_path = _find_cjk_font()
    if font_path is None:
        return None

    def _font(size):
        return ImageFont.truetype(font_path, size)

    try:
        fonts = {
            "unit": _font(_UNIT_SIZE),
            "title": _font(_TITLE_SIZE),
            "sec": _font(_SEC_SIZE),
            "caption": _font(_CAPTION_SIZE),
            "body": _font(_BODY_SIZE),
            "tbl": _font(_TBL_SIZE),
        }
    except Exception:
        return None

    card_x = _CARD_MARGIN
    card_w = _BOARD_WIDTH - 2 * _CARD_MARGIN
    content_x = card_x + _CARD_PAD
    content_w = card_w - 2 * _CARD_PAD

    # 第一遍：量内容总高
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1), (255, 255, 255)))
    content_h = 26
    for sec in sections:
        content_h += _sec_header_h() + 6
        for kind, payload in sec["blocks"]:
            content_h += _measure_block(probe, fonts, content_w, kind, payload) + 14
    pub_img = _load_template("publish-depart.png")
    pub_h = int(pub_img.size[1] * _PUB_W / pub_img.size[0]) if pub_img else 0
    content_h += pub_h + _BOTTOM_PAD + 30

    header_h = _HEADER_H
    card_top = header_h - _CARD_TOP_OVERLAP
    total_h = card_top + content_h + 40

    img = Image.new("RGB", (_BOARD_WIDTH, total_h), _BG)
    draw = ImageDraw.Draw(img)

    # 顶部风景头图（全幅）
    _draw_header(img, draw, fonts)

    # 白色内容卡片（对接头图，圆角微凸）
    draw.rounded_rectangle(
        [card_x, card_top, card_x + card_w, card_top + content_h + 24],
        radius=28, fill=(255, 255, 255),
    )

    # 第二遍：渲染板块
    y = card_top + 26
    for sec in sections:
        y = _draw_sec_header(draw, y, sec["header"], fonts["sec"], sec.get("icon", "")) + 6
        for kind, payload in sec["blocks"]:
            y = _render_block(img, draw, fonts, content_x, y, content_w, kind, payload) + 14

    # 底部发布单位（卡片内居中）
    if pub_img:
        pi = pub_img.resize((_PUB_W, pub_h))
        img.paste(pi, ((_BOARD_WIDTH - _PUB_W) // 2, y + 6),
                  pi if pub_img.mode == "RGBA" else None)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- 主函数

def generate_haihe_composite_longimg_core(
    beginTime: str = "",
    endTime: str = "",
    areaIds: list | None = None,
    interval: int = 24,
    range: str = "9",
    type: str = "0",
) -> dict[str, Any]:
    """调用 14所 各子接口，拼装组合长图，返回 base64 与各板块文本。"""
    try:
        interval_hours = int(interval) if interval not in (None, "") else 24
    except (TypeError, ValueError):
        interval_hours = 24
    if interval_hours < 1:
        interval_hours = 1
    area_ids = DEFAULT_AREA_IDS
    if areaIds:
        area_ids = []
        for a in areaIds:
            try:
                area_ids.append(int(a))
            except (TypeError, ValueError):
                continue
        area_ids = area_ids or DEFAULT_AREA_IDS
    range_ = str(range or "9")
    type_ = str(type or "0")

    if beginTime and endTime:
        begin, end = beginTime, endTime
        # 显式窗口且 interval 保持默认 24 → 对齐窗口时长（超 24h 按累计语义）。
        if interval_hours == 24:
            interval_hours = _window_hours(begin, end)
    else:
        begin, end = _window(interval_hours)
    fore_cycles = _fore_cycle_candidates()
    query_time = datetime.now(BEIJING_TIMEZONE).strftime("%Y%m%d%H0000")

    texts: list[str] = []

    # ==================================================== 雷达拼图（② swan3）
    try:
        swan = _fetch_swan3(query_time)
    except Exception as exc:
        swan = None
        texts.append(f"雷达图获取失败：{_safe_err(exc)}")
    swan_img = _load_image(swan) if swan else None
    radar_blocks = [("image", swan_img)] if swan_img else [("text", "（雷达图获取失败）")]

    # ==================== 降水实况（①文字 + ③实况图 + ④面雨量实况图 + ⑤点雨量排名）
    real_blocks: list[tuple[str, Any]] = []

    # ① 降水实况文字
    try:
        describe = _fetch_describe_text(begin, end, area_ids, interval_hours, range_, type_)
    except Exception as exc:
        describe = ""
        texts.append(f"降水实况文字获取失败：{_safe_err(exc)}")
    if describe:
        texts.append(describe)
    real_blocks.append(("text", describe or "（暂无降水实况文字）"))

    # ③ 降水实况图
    try:
        station_img_bytes = _fetch_station_rain_img(begin, end, area_ids, interval_hours, range_, type_)
    except Exception as exc:
        station_img_bytes = None
        texts.append(f"降水实况图获取失败：{_safe_err(exc)}")
    station_img = _load_image(station_img_bytes) if station_img_bytes else None
    real_blocks.append(("image", station_img) if station_img else ("text", "（降水实况图获取失败）"))

    # ④ 实况面雨量图
    try:
        area_real = _fetch_area_rain_real_img(begin, end, area_ids, interval_hours, range_, type_)
    except Exception as exc:
        area_real = None
        texts.append(f"实况面雨量图获取失败：{_safe_err(exc)}")
    area_real_img = _load_image(area_real) if area_real else None
    real_blocks.append(("image", area_real_img) if area_real_img else ("text", "（实况面雨量图获取失败）"))

    # ⑤ 点雨量排名表（序号|站点|省|市|降水量，前 15 站）
    try:
        stations = _fetch_station_list(begin, end, interval_hours, type_)
    except Exception as exc:
        stations = []
        texts.append(f"点雨量列表获取失败：{_safe_err(exc)}")
    station_rows: list[list[str]] = []
    try:
        valid = [s for s in stations if s.get("val") not in (None, "", "-")]
        valid.sort(key=lambda s: float(s.get("val") or 0), reverse=True)
        station_rows = [
            [str(i),
             str(s.get("siteName") or s.get("siteCode") or "-"),
             str(s.get("provence") or "-"),
             str(s.get("cnty") or "-"),
             f"{float(s.get('val') or 0):.1f}"]
            for i, s in enumerate(valid[:15], 1)
        ]
    except Exception:
        station_rows = []
    if station_rows:
        real_blocks.append(("caption", "自动站累计降水量排名"))
        real_blocks.append(("rank", (["序号", "站点", "省", "市", "降水量(毫米)"], station_rows)))
    else:
        real_blocks.append(("text", "（暂无点雨量数据）"))

    # ============ 降水预报（⑥面雨量预报图 + ⑦每日河系雨量预报表，公报口径=未来3天）
    fore_blocks: list[tuple[str, Any]] = []

    # 起报时次自动回退（14所 某时次数据未就绪时出图接口 500，实测今天08时500/昨天08时200）
    used_fore = ""
    fore_img_bytes = None
    for fc in fore_cycles:
        try:
            fdt = datetime.strptime(fc, "%Y-%m-%d %H:00:00")
            fe = (fdt + timedelta(hours=_FORECAST_HOURS)).strftime("%Y-%m-%d %H:00:00")
            img = _fetch_area_rain_fore_img(fc, fc, fe, area_ids, _FORECAST_HOURS)
            if img:
                used_fore, fore_img_bytes = fc, img
                break
        except Exception:
            continue
    if not fore_img_bytes:
        texts.append("预报面雨量图获取失败：最近起报时次均未就绪")
    fore_img = _load_image(fore_img_bytes) if fore_img_bytes else None
    fore_blocks.append(("image", fore_img) if fore_img else ("text", "（预报面雨量图获取失败）"))

    # ⑦ 每日河系雨量预报表（与 ⑥ 同一起报时次，按 24h 拆 3 天）
    if used_fore:
        fdt = datetime.strptime(used_fore, "%Y-%m-%d %H:00:00")
        for day in _RANGE(_FORECAST_DAYS):
            d0 = fdt + timedelta(hours=24 * day)
            d1 = d0 + timedelta(hours=24)
            b, e = d0.strftime("%Y-%m-%d %H:00:00"), d1.strftime("%Y-%m-%d %H:00:00")
            try:
                fc_data = _fetch_forecast(used_fore, b, e, area_ids, 24)
            except Exception as exc:
                fc_data = []
                texts.append(f"面雨量预报获取失败：{_safe_err(exc)}")
            pairs = []
            try:
                pairs = [
                    (str(f.get("areaName") or f.get("areaId") or "-"), f"{float(f.get('sum') or 0):.1f}")
                    for f in fc_data
                ]
            except Exception:
                pairs = []
            if not pairs:
                continue
            label = f"{d0.month:02d}月{d0.day:02d}日{d0.hour:02d}时 - {d1.month:02d}月{d1.day:02d}日{d1.hour:02d}时，降水量预报表"
            fore_blocks.append(("caption", label))
            fore_blocks.append(("fore", (label, [p[0] for p in pairs], [p[1] for p in pairs])))
    if len(fore_blocks) == 1:  # 只有图、没有任何预报表
        fore_blocks.append(("text", "（暂无面雨量预报数据）"))

    sections = [
        {"header": "雷达拼图", "icon": "radar", "blocks": radar_blocks},
        {"header": "降水实况", "icon": "cloud", "blocks": real_blocks},
        {"header": "降水预报", "icon": "cloudsun", "blocks": fore_blocks},
    ]

    render_warning = ""
    try:
        png = _compose_longimg(sections)
    except Exception as exc:
        render_warning = _safe_err(exc)
        print(f"[composite_longimg] 组合长图渲染失败（降级文字）：{render_warning}")
        # 把渲染失败原因写进降级文字，便于联调直接看到（脱敏后）
        texts.append(f"组合长图渲染失败：{render_warning}")
        png = None
    if png is None and not render_warning:
        render_warning = "服务器缺少中文字体或 Pillow，本次以文字展示"

    b64 = base64.b64encode(png).decode("ascii") if png else ""
    joined = "\n".join(t for t in texts if t)
    return {
        "status": "ok",
        "base64": b64,
        "text": joined or "（各板块文字见图片）",
        "render_warning": render_warning,
        "beginTime": begin,
        "endTime": end,
        "range": range_,
        "type": type_,
        "message": "已生成海河流域降水专题组合长图。",
    }


def register_composite_longimg_tool(mcp: FastMCP) -> None:
    @mcp.tool()
    def generate_haihe_composite_longimg(
        beginTime: str = "",
        endTime: str = "",
        areaIds: list = None,
        interval: int = 24,
        range: str = "9",
        type: str = "0",
    ) -> dict:
        """
        生成海河流域 14所 降水专题**组合长图**。

        把降水实况文字、swan3 组合反射率雷达图、降水实况图、实况面雨量图、
        点雨量列表、预报面雨量图、面雨量预报从上到下拼成一张长图（PNG），
        base64 渲染后由前端自动展示。只出图、不返回数值，不能用于回答
        "下了多少雨 / 天气怎么样 / 面雨量多少" 等数值查询。

        触发：用户说"长图 / 组合长图 / 降水专题长图 / 出今天的长图 / 出一张长图"
        等，未指明具体图类型时默认走本工具（本智能体的"长图"即降水专题组合长图）。

        与 get_station_rainfall_real_img（各子流域降雨分布图）区分：那是单一分区
        面雨量空间分布图；本工具是多板块组合长图。

        Args:
            beginTime: 开始时间 "YYYY-MM-DD HH:mm:ss"（北京时），不传取当前前推 interval 小时
            endTime: 结束时间，不传取当前整点
            areaIds: 区域 id 列表，默认海河9大分区
            interval: 间隔(小时)，默认 24
            range: 分区，默认 "9"，可传 "9" 或 "11"
            type: 站点类型，"0"=国家站，"1"=区域站，默认 "0"
        """
        return generate_haihe_composite_longimg_core(
            beginTime=beginTime,
            endTime=endTime,
            areaIds=areaIds,
            interval=interval,
            range=range,
            type=type,
        )
