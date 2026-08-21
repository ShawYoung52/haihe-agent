"""14所降水实况文字长图工具（/openapi/rainfall_describe/real）。

业务场景：用户问「降水实况文字 / 生成降水实况 / 只提长图」时，调用 14所接口获取
降水实况**文字**（data 字段为中文描述），再由本工具用 Pillow 渲染成**长图**（PNG）
返回 base64，走 chainlitexam `_run_tool_round` 特判的 base64 → cl.Image 展示路径。

口径（联调确认 2026-08-17）：
- 接口 `data` 是纯文本（如"8月16日15时-17日15时，海河流域出现小雨…"），**不是图片**。
- `isClimateImg` 不影响返回文本。
- base 默认 http://10.226.107.35:8001，env RAINFALL_DESCRIBE_API_BASE 可覆盖。
- 长图渲染：白底黑字、固定宽度、自动换行、高度随文本自适应（长图）。
  中文字体自动探测（env RAINFALL_DESCRIBE_FONT 可显式指定）；缺字体/缺 Pillow 时
  降级返回 `base64:""` + `text`，由前端直接展示文字，不报错。
- **不改动** chainlitexam/qa_http_api.py 的 `_IMAGE_URL_ALLOW_HOSTS` 白名单：
  图片以本地落盘文件 URL 经 HTTP images 字段下发，不暴露内网出图代理地址。
- 实时出图，不做 TTL 缓存。
"""
from __future__ import annotations

import base64
import os
import re
from datetime import datetime, timedelta
import time_source
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

# 出图超时：承接上游文字生成耗时，留足余量。
_DESCRIBE_TIMEOUT = 60

# 内网地址/路径脱敏（CLAUDE.md 约定：错误文本返给 LLM/用户前去掉 IP/路径）。
_SCRUB_PATTERNS = [
    (re.compile(r"https?://[^\s\"'<>|]+"), "[内网地址]"),  # 完整 URL（含 host+path）
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "[内网地址]"),
    (re.compile(r"[A-Za-z]:\\[^\s\"'<>|]+"), "[路径]"),  # Windows 绝对路径
    (re.compile(r"(?<![\w.])/(?:home|usr|var|etc|opt|root|openapi|hhly|hhfw|img)/[^\s\"'<>|]*"), "[路径]"),
]

# 长图渲染参数
_LONGIMG_WIDTH = 1080          # 画布宽度（px）
_LONGIMG_MARGIN = 48           # 左右边距
_LONGIMG_PAD_TOP = 56          # 顶部内边距
_LONGIMG_TITLE_SIZE = 40       # 标题字号
_LONGIMG_BODY_SIZE = 30        # 正文字号
_LONGIMG_LINE_RATIO = 1.7      # 行高 = 字号 * 比例
_LONGIMG_HEADER_GAP = 28       # 标题与正文间距
_LONGIMG_BG = (255, 255, 255)  # 白底
_LONGIMG_FG = (20, 20, 20)     # 深色文字


def _scrub_text(text: Any) -> str:
    """去掉内网 URL/IP/路径，防止把出图代理地址带进 LLM/用户输出。"""
    out = str(text)
    for pattern, repl in _SCRUB_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def _safe_err(exc: Exception) -> str:
    """异常文本脱敏（含 URL host 与接口路径）。"""
    return _scrub_text(str(exc))


def _find_cjk_font() -> str | None:
    """探测系统可用的中文字体；env RAINFALL_DESCRIBE_FONT 可显式指定路径。

    不依赖联网安装：依次尝试 ①显式指定 ②常见路径 ③fc-list ④matplotlib 字体表。
    内网服务器可能没装 yum 中文字体，但 conda 环境 / matplotlib 常自带 CJK 字体。
    """
    override = os.getenv("RAINFALL_DESCRIBE_FONT", "").strip()
    candidates: list[str] = []
    if override:
        candidates.append(override)
    if os.name == "nt":
        candidates += [
            r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
            r"C:\Windows\Fonts\msyhbd.ttc",
            r"C:\Windows\Fonts\simhei.ttf",    # 黑体
            r"C:\Windows\Fonts\simsun.ttc",    # 宋体
            r"C:\Windows\Fonts\Deng.ttf",      # 等线
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


def _wrap_text(draw, font, text: str, max_width: int) -> list[str]:
    """按字符宽度自动换行（中文无空格，须逐字测量）。保留原有换行。"""
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


def _render_text_longimg(text: str, title: str) -> bytes | None:
    """把文字渲染成白底长图 PNG；缺 Pillow/缺中文字体返回 None。

    返回的 PNG 字节可直接 base64。失败返回 None 由调用方降级为纯文字展示。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    font_path = _find_cjk_font()
    if font_path is None:
        return None

    body = str(text or "").strip()
    if not body:
        return None
    try:
        title_font = ImageFont.truetype(font_path, _LONGIMG_TITLE_SIZE)
        body_font = ImageFont.truetype(font_path, _LONGIMG_BODY_SIZE)
    except Exception:
        return None

    usable_width = _LONGIMG_WIDTH - 2 * _LONGIMG_MARGIN
    draw_probe = Image.new("RGB", (1, 1), _LONGIMG_BG)
    probe = ImageDraw.Draw(draw_probe)

    title_lines = [title] if title else []
    body_lines = _wrap_text(probe, body_font, body, usable_width)
    line_h = int(_LONGIMG_BODY_SIZE * _LONGIMG_LINE_RATIO)
    title_h = int(_LONGIMG_TITLE_SIZE * _LONGIMG_LINE_RATIO)

    n_title = len(title_lines)
    height = (
        _LONGIMG_PAD_TOP
        + n_title * title_h
        + (len(body_lines) * line_h if body_lines else line_h)
        + _LONGIMG_PAD_TOP
        + (0 if not body_lines else _LONGIMG_HEADER_GAP)
    )

    img = Image.new("RGB", (_LONGIMG_WIDTH, height), _LONGIMG_BG)
    draw = ImageDraw.Draw(img)
    y = _LONGIMG_PAD_TOP
    for line in title_lines:
        draw.text((_LONGIMG_MARGIN, y), line, font=title_font, fill=_LONGIMG_FG)
        y += title_h
    if title_lines:
        y += _LONGIMG_HEADER_GAP
    for line in body_lines:
        draw.text((_LONGIMG_MARGIN, y), line, font=body_font, fill=_LONGIMG_FG)
        y += line_h

    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


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
    isClimateImg: bool = True,
) -> dict[str, Any]:
    """POST 获取降水实况文字，渲染成长图返回 base64 与原始文字。"""
    try:
        interval_hours = int(interval) if interval not in (None, "") else 24
    except (TypeError, ValueError):
        interval_hours = 24
    if interval_hours < 1:
        interval_hours = 1

    now = time_source.now(BEIJING_TIMEZONE)
    explicit_window = bool(beginTime and endTime)
    if not endTime:
        endTime = now.strftime("%Y-%m-%d %H:00:00")
    if not beginTime:
        beginTime = (now - timedelta(hours=interval_hours)).strftime("%Y-%m-%d %H:00:00")

    # begin/end 均显式给出且未指定 interval（保持默认 24）时，interval 自动对齐窗口时长。
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
        return {"status": "error", "message": f"获取降水实况文字失败：{_safe_err(exc)}", "base64": "", "text": ""}

    # 接口 data 为降水实况文字（联调确认 2026-08-17）。
    text = ""
    if isinstance(body, dict):
        raw = body.get("data")
        if isinstance(raw, str):
            text = raw.strip()
        elif raw is not None:
            text = str(raw).strip()
    elif isinstance(body, str):
        text = body.strip()

    if not text:
        msg = _scrub_text(body.get("msg")) if isinstance(body, dict) else ""
        return {
            "status": "no_data",
            "message": msg or "接口未返回降水实况文字，请确认时段内是否有有效降水实况数据。",
            "base64": "",
            "text": "",
            "beginTime": beginTime,
            "endTime": endTime,
        }

    rng = str(range or "9")
    rng_desc = {"9": "九", "11": "十一"}.get(rng, rng)
    title = f"海河流域降水实况文字（{rng_desc}分区 {beginTime} ~ {endTime}）"

    try:
        png = _render_text_longimg(text, title)
    except Exception as exc:
        print(f"[rainfall_describe] 长图渲染失败（降级纯文字）：{_safe_err(exc)}")
        png = None

    b64 = base64.b64encode(png).decode("ascii") if png else ""
    return {
        "status": "ok",
        "base64": b64,
        "text": text,
        "render_warning": "" if b64 else "服务器缺少中文字体或 Pillow，本次以文字展示",
        "beginTime": beginTime,
        "endTime": endTime,
        "interval": interval_hours,
        "range": rng,
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
        isClimateImg: bool = True,
    ) -> dict:
        """
        生成海河流域降水实况文字长图（14所 /openapi/rainfall_describe/real）。

        接口返回降水实况**文字**，本工具将其渲染成一张长图（PNG）供前端展示。
        仅当用户要「降水实况文字 / 降水实况文字长图 / 生成降水实况文字」，或**只说
        "长图 / 生成长图 / 出一张长图"而无具体图类型**（本智能体的"长图"即降水实况
        文字长图）时调用。只出图、不返回数值，不能用于回答"下了多少雨 / 天气怎么样
        / 面雨量多少" 等数值查询。

        与「各子流域降雨分布图」get_station_rainfall_real_img 区分：那是分区面雨量的
        **空间分布图**（问句含"降水实况图/降雨分布图/面雨量分布图"）；本工具是降水
        实况的**文字**长图。"长图/文字长图"不属空间分布图，默认走本工具。

        Args:
            beginTime: 开始时间，格式 "YYYY-MM-DD HH:mm:ss"（北京时），不传取当前
                前推 interval 小时
            endTime: 结束时间，格式同上，不传取当前整点
            areaIds: 区域 id 列表，默认海河9大分区 [6,7,8,9,10,11,12,13,14]
            interval: 间隔(小时)，默认 24；begin/end 均给出且未指定 interval 时自动
                对齐窗口时长（超 24h 按累计，如 48h 窗口 interval=48）
            range: 分区，默认 "9"，可传 "9" 或 "11"
            type: 站点类型，"0"=国家站，"1"=区域站，默认 "0"
            isClimateImg: 出图文字颜色是否黑色，默认 True（接口对文本返回无影响）
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
