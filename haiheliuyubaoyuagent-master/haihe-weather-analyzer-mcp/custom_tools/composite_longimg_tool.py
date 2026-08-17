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

# 长图版式
_BOARD_WIDTH = 1080
_MARGIN = 48
_SEC_TITLE_SIZE = 34
_BODY_SIZE = 28
_LINE_RATIO = 1.65
_SEC_GAP = 18
_IMG_MAX_HEIGHT = 560          # 单张子图最大高度（超过则等比缩小）
_TABLE_ROW_H = 40
_TABLE_HEAD_BG = (235, 240, 248)
_BG = (255, 255, 255)
_FG = (20, 20, 20)
_SEC_FG = (30, 90, 170)        # 板块标题颜色


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
        "interval": interval, "range": range_, "type": type_,
    }))


def _fetch_area_rain_real_img(begin: str, end: str, area_ids: list[int], interval: int, range_: str, type_: str) -> bytes | None:
    """④ 实况面雨量图。"""
    return _resolve_image_bytes(_post(f"{BASE}/openapi/meteor_img/area_rain_real_img?forceCreate=1", {
        "areaIds": area_ids, "beginTime": begin, "endTime": end,
        "interval": interval, "range": range_, "type": type_,
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


def _latest_fore_cycle() -> str:
    now = datetime.now(BEIJING_TIMEZONE)
    if now.hour >= 20:
        return now.replace(hour=20, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:00:00")
    if now.hour >= 8:
        return now.replace(hour=8, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:00:00")
    prev = now - timedelta(days=1)
    return prev.replace(hour=20, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:00:00")


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
    """等比缩放子图到可用宽度内（最大高度 _IMG_MAX_HEIGHT）。"""
    w, h = img.size
    scale = min(1.0, usable_w / w if w else 1.0, _IMG_MAX_HEIGHT / h if h else 1.0)
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    return img


def _render_sections(draw, fonts, usable_w: int, x: int, y: int, sections: list[tuple[str, str, Any]], body_lines: list[str]) -> int:
    """绘制各板块，返回结束 y 坐标。sections: (标题, 类型, 内容)。"""
    from PIL import Image

    sec_font, body_font = fonts
    line_h = int(_BODY_SIZE * _LINE_RATIO)
    sec_h = int(_SEC_TITLE_SIZE * _LINE_RATIO)
    for title, kind, content in sections:
        # 板块标题
        draw.text((x, y), title, font=sec_font, fill=_SEC_FG)
        y += sec_h + 6
        if kind == "text":
            text = str(content or "")
            lines = _wrap_text(draw, body_font, text, usable_w)
            for ln in lines:
                body_lines.append(ln)
                draw.text((x, y), ln, font=body_font, fill=_FG)
                y += line_h
            if not lines:
                draw.text((x, y), "暂无内容", font=body_font, fill=_FG)
                y += line_h
        elif kind == "image":
            img = _fit_image(content, usable_w)
            paste_x = x + (usable_w - img.size[0]) // 2
            draw._image.paste(img, (paste_x, y))
            y += img.size[1] + 8
        elif kind == "table":
            headers, rows = content
            y = _render_table(draw, body_font, x, y, usable_w, headers, rows)
        y += _SEC_GAP
    return y


def _render_table(draw, font, x: int, y: int, usable_w: int, headers: list[str], rows: list[list[str]]) -> int:
    """绘制简单表格，返回结束 y 坐标。"""
    n = len(headers) or 1
    col_w = usable_w / n
    row_h = _TABLE_ROW_H

    def _cell(text: str, max_w: float) -> str:
        t = str(text or "")
        while t and draw.textlength(t, font=font) > max_w - 8:
            t = t[:-1]
        return t + ("…" if len(str(text or "")) > len(t) else "")

    # 表头
    draw.rectangle([x, y, x + usable_w, y + row_h], fill=_TABLE_HEAD_BG)
    for i, h in enumerate(headers):
        draw.text((x + i * col_w + 4, y + (row_h - font.size) // 2 - 2), str(h), font=font, fill=_FG)
    y += row_h
    for row in rows[:15]:  # 表格最多 15 行
        for i, cell in enumerate(row):
            draw.text((x + i * col_w + 4, y + (row_h - font.size) // 2 - 2),
                      _cell(cell, col_w), font=font, fill=_FG)
        y += row_h
    # 底线
    draw.line([x, y, x + usable_w, y], fill=(200, 200, 200))
    y += 6
    return y


def _compose_longimg(sections: list[tuple[str, str, Any]]) -> bytes | None:
    """把板块列表渲染成一张长图 PNG。缺 Pillow/缺中文字体返回 None。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    font_path = _find_cjk_font()
    if font_path is None:
        return None
    try:
        sec_font = ImageFont.truetype(font_path, _SEC_TITLE_SIZE)
        body_font = ImageFont.truetype(font_path, _BODY_SIZE)
    except Exception:
        return None

    usable_w = _BOARD_WIDTH - 2 * _MARGIN
    line_h = int(_BODY_SIZE * _LINE_RATIO)
    sec_h = int(_SEC_TITLE_SIZE * _LINE_RATIO)

    # 先估算总高度
    probe_img = Image.new("RGB", (1, 1), _BG)
    probe = ImageDraw.Draw(probe_img)
    height = _MARGIN
    for title, kind, content in sections:
        height += sec_h + 6 + _SEC_GAP
        if kind == "text":
            lines = _wrap_text(probe, body_font, str(content or ""), usable_w)
            height += max(len(lines), 1) * line_h
        elif kind == "image":
            img = _fit_image(content, usable_w)
            height += img.size[1] + 8
        elif kind == "table":
            headers, rows = content
            height += _TABLE_ROW_H * (1 + min(len(rows), 15)) + 6
    height += _MARGIN

    img = Image.new("RGB", (_BOARD_WIDTH, max(height, 200)), _BG)
    draw = ImageDraw.Draw(img)
    body_lines: list[str] = []
    _render_sections(draw, (sec_font, body_font), usable_w, _MARGIN, _MARGIN, sections, body_lines)
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
    fore_time = _latest_fore_cycle()
    query_time = datetime.now(BEIJING_TIMEZONE).strftime("%Y%m%d%H0000")

    # 各板块独立取数，失败各自占位
    sections: list[tuple[str, str, Any]] = []
    texts: list[str] = []

    # ① 降水实况文字
    try:
        describe = _fetch_describe_text(begin, end, area_ids, interval_hours, range_, type_)
    except Exception as exc:
        describe = ""
        texts.append(f"降水实况文字获取失败：{_safe_err(exc)}")
    if describe:
        texts.append(describe)
        sections.append((f"① 降水实况文字（{begin} ~ {end}）", "text", describe))
    else:
        sections.append((f"① 降水实况文字（{begin} ~ {end}）", "text", "（暂无降水实况文字）"))

    # ② swan3 雷达图
    try:
        swan = _fetch_swan3(query_time)
    except Exception as exc:
        swan = None
        texts.append(f"雷达图获取失败：{_safe_err(exc)}")
    swan_img = _load_image(swan) if swan else None
    sections.append(("② swan3 组合反射率雷达图", "image", swan_img) if swan_img else ("② swan3 组合反射率雷达图", "text", "（雷达图获取失败）"))

    # ③ 降水实况图
    try:
        station_img_bytes = _fetch_station_rain_img(begin, end, area_ids, interval_hours, range_, type_)
    except Exception as exc:
        station_img_bytes = None
        texts.append(f"降水实况图获取失败：{_safe_err(exc)}")
    station_img = _load_image(station_img_bytes) if station_img_bytes else None
    sections.append(("③ 降水实况图", "image", station_img) if station_img else ("③ 降水实况图", "text", "（降水实况图获取失败）"))

    # ④ 实况面雨量图
    try:
        area_real = _fetch_area_rain_real_img(begin, end, area_ids, interval_hours, range_, type_)
    except Exception as exc:
        area_real = None
        texts.append(f"实况面雨量图获取失败：{_safe_err(exc)}")
    area_real_img = _load_image(area_real) if area_real else None
    sections.append(("④ 实况面雨量图", "image", area_real_img) if area_real_img else ("④ 实况面雨量图", "text", "（实况面雨量图获取失败）"))

    # ⑤ 点雨量列表（站点 → 表格）
    try:
        stations = _fetch_station_list(begin, end, interval_hours, type_)
    except Exception as exc:
        stations = []
        texts.append(f"点雨量列表获取失败：{_safe_err(exc)}")
    station_rows = []
    try:
        station_rows = sorted(
            [
                [str(s.get("siteName") or s.get("siteCode") or "-"),
                 str(s.get("areaName") or "-"),
                 f"{float(s.get('val') or 0):.1f}",
                 f"{str(s.get('provence') or '')} {str(s.get('cnty') or '')}".strip() or "-"]
                for s in stations
                if s.get("val") not in (None, "", "-")
            ],
            key=lambda r: float(r[2]), reverse=True,
        )[:15]
    except Exception:
        station_rows = []
    if station_rows:
        sections.append(("⑤ 点雨量列表（前 15 站）", "table", (["站点", "区域", "雨量(mm)", "位置"], station_rows)))
    else:
        sections.append(("⑤ 点雨量列表", "text", "（暂无点雨量数据）"))

    # ⑥ 预报面雨量图
    try:
        fore_img_bytes = _fetch_area_rain_fore_img(fore_time, begin, end, area_ids, interval_hours)
    except Exception as exc:
        fore_img_bytes = None
        texts.append(f"预报面雨量图获取失败：{_safe_err(exc)}")
    fore_img = _load_image(fore_img_bytes) if fore_img_bytes else None
    sections.append((f"⑥ 预报面雨量图（起报 {fore_time}）", "image", fore_img) if fore_img else ("⑥ 预报面雨量图", "text", "（预报面雨量图获取失败）"))

    # ⑦ 面雨量预报（分区 → 表格）
    try:
        forecasts = _fetch_forecast(fore_time, begin, end, area_ids, interval_hours)
    except Exception as exc:
        forecasts = []
        texts.append(f"面雨量预报获取失败：{_safe_err(exc)}")
    fore_rows = []
    try:
        fore_rows = [
            [str(f.get("areaName") or f.get("areaId") or "-"), f"{float(f.get('sum') or 0):.1f}"]
            for f in forecasts
        ]
    except Exception:
        fore_rows = []
    if fore_rows:
        sections.append(("⑦ 面雨量预报（各分区累计）", "table", (["分区", "预报面雨量(mm)"], fore_rows)))
    else:
        sections.append(("⑦ 面雨量预报", "text", "（暂无面雨量预报数据）"))

    try:
        png = _compose_longimg(sections)
    except Exception as exc:
        print(f"[composite_longimg] 组合长图渲染失败（降级文字）：{_safe_err(exc)}")
        png = None

    b64 = base64.b64encode(png).decode("ascii") if png else ""
    joined = "\n".join(t for t in texts if t)
    return {
        "status": "ok",
        "base64": b64,
        "text": joined or "（各板块文字见图片）",
        "render_warning": "" if b64 else "服务器缺少中文字体或 Pillow，本次以文字展示",
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
