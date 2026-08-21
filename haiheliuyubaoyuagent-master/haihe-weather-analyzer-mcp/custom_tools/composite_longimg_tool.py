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
import time_source
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

# 长图版式（严格对齐示范图 download.png，模板素材拼装）：
#   bg.png 浅蓝云纹平铺背景 + 顶部 top-bg 全幅风景头图(叠加 title.png 大标题 +
#   publish-depart.png 发布单位) + 3 大主题板块**各自一张白色圆角卡片**(板间浅蓝缝)
#   + 板块标题(居中蓝字+小图标+装饰线) + 大幅带框地图 + 浅蓝表头表格。
_ASSETS_DIR = os.getenv(
    "LONGIMG_ASSETS_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "longimg"),
)
_BOARD_WIDTH = 1125          # 画布宽 = 模板宽
_BG = (131, 207, 251)        # 外层浅蓝背景（= bg.png 实测色，模板缺失时兜底）
_CARD_MARGIN = 38            # 白色内容卡片左右外边距（示范图实测 ~19px@561 → 38@1125）
_CARD_PAD = 42               # 卡片内边距
_CARD_RADIUS = 26            # 白色卡片圆角
_SECTION_GAP = 30            # 板块卡片之间的浅蓝缝隙（示范图 ~14px@561 → 30@1125）
_HEADER_H = 600              # 顶部风景头图高度（示范图 ~300px@561 → 600@1125）
_HEADER_TITLE_W = 880        # title.png 缩放宽度（910 原生，略缩留边距）
_HEADER_TITLE_Y = 150        # title.png 顶部 y（头图内垂直居中偏上）
_HEADER_PUB_W = 360          # publish-depart.png 发布单位缩放宽度
_HEADER_PUB_XY = (44, 36)    # 发布单位左上角坐标
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
_IMG_WIDE_MAX_H = 1500       # 宽图（雷达）高度上限：撑满内容宽、允许更高，避免被压窄
_BOTTOM_PAD = 48             # 底部留白
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
    now = time_source.now(BEIJING_TIMEZONE)
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
    """③ 降水实况图（isClimateImg=True 出白底黑字图，图例文字正常）。"""
    return _resolve_image_bytes(_post(f"{BASE}/openapi/meteor_img/stationRainRealImg?forceCreate=1", {
        "areaIds": area_ids, "beginTime": begin, "endTime": end,
        "interval": interval, "range": range_, "type": type_, "isClimateImg": True,
    }))


def _fetch_area_rain_real_img(begin: str, end: str, area_ids: list[int], interval: int, range_: str, type_: str) -> bytes | None:
    """④ 实况面雨量图（isClimateImg=True 出白底黑字图，不再是黑底）。"""
    return _resolve_image_bytes(_post(f"{BASE}/openapi/meteor_img/area_rain_real_img?forceCreate=1", {
        "areaIds": area_ids, "beginTime": begin, "endTime": end,
        "interval": interval, "range": range_, "type": type_, "isClimateImg": True,
    }))


def _fetch_station_list(begin: str, end: str, interval: int, type_: str) -> list[dict]:
    """⑤ 点雨量列表（站点数据）。"""
    body = _post(f"{BASE}/openapi/area_rain_station/list", {
        "areaIds": DEFAULT_AREA_IDS, "beginTime": begin, "endTime": end,
        "interval": interval, "sourceType": 2, "type": type_,
    })
    data = body.get("data") if isinstance(body, dict) else body
    return [d for d in (data or []) if isinstance(d, dict)]


# ⑥ 预报面雨量图接口路径：14所 文档给了两个（带 /openapi 与不带），逐个尝试取第一个能出图的。
_FORE_IMG_PATHS = (
    "/openapi/meteor_img/area_rain_fore_img?forceCreate=1",
    "/meteor_img/area_rain_fore_img?forceCreate=1",
)


def _fetch_area_rain_fore_img(fore_time: str, begin: str, end: str, area_ids: list[int], interval: int) -> bytes | None:
    """⑥ 预报面雨量图（isClimateImg=True 出白底黑字图；两个候选路径逐个尝试）。

    14所 文档中本接口同时存在 /openapi/meteor_img/... 与 /meteor_img/... 两种路径，
    不确定部署的是哪一个，故按序尝试：路径不存在/该时次未就绪就换下一个，都失败则
    抛出最后一个异常（交由上层起报时次回退处理）。
    """
    body = {
        "areaIds": area_ids, "foreTime": fore_time, "beginTime": begin, "endTime": end,
        "intval": interval, "modelTypes": ["ECMF"], "range": "9", "isClimateImg": True,
    }
    last_exc: Exception | None = None
    for path in _FORE_IMG_PATHS:
        try:
            img = _resolve_image_bytes(_post(f"{BASE}{path}", body))
        except Exception as exc:
            last_exc = exc
            continue
        if img:
            return img
    if last_exc is not None:
        raise last_exc
    return None


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
    now = time_source.now(BEIJING_TIMEZONE)
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


def _dark_fraction(img: Any, thresh: int = 90) -> float:
    """粗略估计图中"近黑像素"占比（缩小采样），用于判断是否黑底图。"""
    from PIL import Image
    small = img.convert("RGB").resize((64, 64))
    px = small.load()
    n = dark = 0
    for y in range(64):
        for x in range(64):
            r, g, b = px[x, y]
            n += 1
            if max(r, g, b) < thresh:
                dark += 1
    return dark / n if n else 0.0


def _lightest_green(img: Any, green_low: int = 200, bright_low: int = 150) -> tuple[int, int, int]:
    """采样图里"最浅的绿"（绿主导 + 高亮浅绿），用作黑块零值区填充色；无则回落浅绿。"""
    from PIL import Image
    img = img.convert("RGB")
    px = img.load()
    W, H = img.size
    rs = gs = bs = n = 0
    for y in range(0, H, 2):
        for x in range(0, W, 2):
            r, g, b = px[x, y]
            if g > 150 and g > r + 20 and g > b + 20 and g > green_low and r > bright_low:
                rs += r
                gs += g
                bs += b
                n += 1
    if n:
        return (rs // n, gs // n, bs // n)
    return (205, 242, 205)


def _to_white_map(img: Any, bg: tuple[int, int, int] = (255, 255, 255),
                  text: tuple[int, int, int] = (15, 15, 15),
                  chroma_thresh: int = 40, dark_thresh: int = 90,
                  mid_thresh: int = 245, blob_open: int = 15,
                  large_open: int = 15, content_blur: int = 5,
                  content_thresh: int = 110, text_min_area: int = 25,
                  fill_min_core: int = 120, erode_iter: int = 2) -> Any:
    """让地图文字在白底长图上清晰可读，彩色雨区/图例色条原样保留。

    注意（2026-08-19 用户决定）：本函数**不再接入 ③④⑥ 板块渲染**——用户明确
    "③④⑥ 完全不用后处理，用 14所 原图"（历次后处理都引入新毛病）。函数与测试
    保留备用，若未来恢复后处理，用本版（连通域腐蚀核心分类）而非 c1133f5/2e4b292
    的 near 带逻辑。

    14所 ④⑥ 面雨量图接口样式不稳定（isClimateImg=True 已按接口文档以 JSON 布尔传入
    body，但服务端并不总是生效）：可能返回黑底白字图、白底浅灰字图，甚至白底绿区白字
    图（⑥ 偶发，值/站名印在绿色雨区上不可见）。本函数分两种模式处理：
      * 黑底为主（dark_fraction >= 0.35）：近黑背景铺白、浅色文字/图例刻度转深字；
      * 白底为主：把低色度像素按连通域腐蚀核心分成"稀疏笔画（文字/分区名/刻度/线条）
        与实心块（0 值/低值白色填充区、色标、页面白底，哪怕内部印了黑字被打成洞）"，
        前者加深为深字，后者保持原色——既能修复"白底白字看不见"，又不会把 0 值区/淡绿
        低值点误加深成黑块黑斑（旧的 near 带逻辑 2e4b292/c1133f5 都栽在这里）。
    饱和色（雨区、图例色条、红色标题）一律保留原色。
    """
    from PIL import Image, ImageChops, ImageFilter
    img = img.convert("RGB")
    r, g, b = img.split()
    mx = ImageChops.lighter(ImageChops.lighter(r, g), b)   # 每像素 max
    mn = ImageChops.darker(ImageChops.darker(r, g), b)     # 每像素 min
    chroma = ImageChops.subtract(mx, mn)                   # 色度
    achrom = chroma.point(lambda v: 255 if v < chroma_thresh else 0)  # 低色度（文字/底色/线条）
    if _dark_fraction(img) >= 0.35:
        # 黑底图：黑→白底，浅字→深字
        m_bg = ImageChops.multiply(achrom, mx.point(lambda v: 255 if v < dark_thresh else 0))
        m_text = ImageChops.multiply(achrom, mx.point(lambda v: 255 if v >= dark_thresh else 0))
        base_bg, base_text = bg, text
    else:
        # 白底图：只把"稀疏笔画"（文字/分区名/图例刻度/线条）加深，实心块（0 值/低值
        # 白色或淡色填充区、色标、页面白底，哪怕内部印了黑字被打成洞）一律保持原色。
        # 按亮度拆成 灰(90..245)/白(>=245) 两个掩码分别做连通域分类——若混在一起，
        # 白底会与压在它上面的灰字连成同一个连通域，整块被误判为实心填充而不加深。
        fill = _lightest_green(img)
        black = ImageChops.multiply(achrom, mx.point(lambda v: 255 if v < dark_thresh else 0))
        # 形态学开运算：只留"大型"近黑区块（个别黑底零值填充区），细小的黑字/线条被去掉
        blob = black.filter(ImageFilter.MinFilter(blob_open)).filter(ImageFilter.MaxFilter(blob_open))
        to_fill = ImageChops.multiply(black, blob.point(lambda v: 255 if v > 0 else 0))
        gray = ImageChops.multiply(achrom, mx.point(
            lambda v: 255 if dark_thresh <= v < mid_thresh else 0))
        white = ImageChops.multiply(achrom, mx.point(lambda v: 255 if v >= mid_thresh else 0))
        m_text = _achrom_text_mask(gray, text_min_area=text_min_area,
                                   fill_min_core=fill_min_core, erode_iter=erode_iter)
        m_text_white = _achrom_text_mask(white, text_min_area=text_min_area,
                                         fill_min_core=fill_min_core, erode_iter=erode_iter)
        if m_text is not None and m_text_white is not None:
            m_text = ImageChops.lighter(m_text, m_text_white)
        elif m_text_white is not None:
            m_text = m_text_white
        if m_text is None:
            # 无 numpy/scipy 时的回退：按亮度/近邻近似（旧的 near 带逻辑）。
            # 仅此回退路径保留 m_gray/near/large_white 的旧语义。
            green = chroma.point(lambda v: 255 if v >= chroma_thresh else 0)
            content = ImageChops.lighter(green, to_fill)
            near = content.filter(ImageFilter.BoxBlur(content_blur)).point(
                lambda v: 255 if v > content_thresh else 0)
            m_white = ImageChops.multiply(achrom, mx.point(lambda v: 255 if v >= mid_thresh else 0))
            large_white = m_white.filter(ImageFilter.MinFilter(large_open)).filter(ImageFilter.MaxFilter(large_open))
            m_gray = ImageChops.multiply(achrom, mx.point(
                lambda v: 255 if dark_thresh <= v < mid_thresh else 0))
            m_white_near = ImageChops.subtract(ImageChops.multiply(m_white, near), large_white)
            m_text = ImageChops.lighter(m_gray, m_white_near)
        m_bg = to_fill
        base_bg, base_text = fill, text
    low = ImageChops.lighter(m_bg, m_text)                 # 需改色的低色度像素
    br, bg_, bb = base_bg
    tr, tg_, tb = base_text

    def chan(src, base_bg, base_text):
        base = Image.composite(Image.new("L", src.size, base_text),
                               Image.new("L", src.size, base_bg), m_text)
        return Image.composite(base, src, low)   # 需改色→base（浅绿底/深字），其余→原色

    return Image.merge("RGB", (chan(r, br, tr), chan(g, bg_, tg_), chan(b, bb, tb)))


def _achrom_text_mask(mask: Any, text_min_area: int = 25,
                      fill_min_core: int = 120, erode_iter: int = 2) -> Any | None:
    """对给定低色度掩膜（灰 90..245 或 白 >=245 的连通域）分出"需加深的文字"。

    判据：文字是细笔画，腐蚀 erode_iter 次后核心几乎消失；实心填充（0 值/低值白色区、
    色标、页面白底，哪怕内部印了黑字被打成洞）仍有大块实心核心。返回需加深像素的
    L 掩膜；无 numpy/scipy 时返回 None（调用方回退旧的 near 带逻辑）。

    注意：灰/白必须分掩膜各自调用——合在一起时白底会与压在它上面的灰字连成同一
    连通域，整块被误判为实心填充而不加深。

    实测（2026-08-19 真实 ③④⑥ 图）：文字连通域面积 80~283、腐蚀 2 次后核心
    0~58；白底 0 值填充区（15504/21416/6826/6393/10167 等）腐蚀后核心
    4420~18873、页面白底核心占 88%~95%。core>=120 即可干净分开。
    """
    try:
        import numpy as np
        from scipy import ndimage
        from PIL import Image
    except Exception:
        return None
    m = np.asarray(mask) > 0
    lab, n = ndimage.label(m)
    if n == 0:
        return None
    struct = np.ones((3, 3), dtype=bool)
    text = np.zeros_like(m)
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        if sl is None:
            continue
        comp = lab[sl] == i
        area = int(comp.sum())
        if area < text_min_area:
            continue  # 微小噪声/细点：不动，避免造出黑点
        core = comp.copy()
        for _ in range(erode_iter):
            core = ndimage.binary_erosion(core, struct)
        if int(core.sum()) < fill_min_core:
            text[sl][comp] = True  # 稀疏笔画 → 文字
    return Image.fromarray((text * 255).astype("uint8"), "L")


def _black_bg_to_white(img: Any, dark_frac_thresh: float = 0.35, chroma_thresh: int = 40,
                       dark_thresh: int = 90, bg: tuple[int, int, int] = (255, 255, 255),
                       text: tuple[int, int, int] = (15, 15, 15)) -> Any:
    """④⑥ 面雨量图"仅黑底才转白"最小处理（用户 2026-08-19 确认采用）。

    14所 ④⑥ 接口 isClimateImg=True 并不总是生效：偶尔返回黑底白字图（白底时正常）。
    本函数只处理黑底情形——整图近黑占比 >= dark_frac_thresh 时，黑底铺白、浅色文字/
    刻度/分区名加深为深字，彩色雨区/图例色条原样保留；**已是白底（或任何非偏黑）
    的图一个像素都不动，原样返回**——不会进入 _to_white_map 的白底分支，也就不会
    引入当初黑块/黑斑的旧毛病。仅接线 ④⑥；③（isClimateImg 可靠生效）与雷达不经
    本函数。逻辑等价于 _to_white_map 的黑底分支（dark_fraction >= 0.35 支路）。
    """
    from PIL import Image, ImageChops
    img = img.convert("RGB")
    if _dark_fraction(img, dark_thresh) < dark_frac_thresh:
        return img                                   # 非黑底：一个像素都不动
    r, g, b = img.split()
    mx = ImageChops.lighter(ImageChops.lighter(r, g), b)   # 每像素 max
    mn = ImageChops.darker(ImageChops.darker(r, g), b)     # 每像素 min
    chroma = ImageChops.subtract(mx, mn)                   # 色度
    achrom = chroma.point(lambda v: 255 if v < chroma_thresh else 0)  # 低色度（底色/文字）
    m_bg = ImageChops.multiply(achrom, mx.point(lambda v: 255 if v < dark_thresh else 0))
    m_text = ImageChops.multiply(achrom, mx.point(lambda v: 255 if v >= dark_thresh else 0))
    low = ImageChops.lighter(m_bg, m_text)                 # 需改色的低色度像素
    br, bg_, bb = bg
    tr, tg_, tb = text

    def chan(src, base_bg, base_text):
        base = Image.composite(Image.new("L", src.size, base_text),
                               Image.new("L", src.size, base_bg), m_text)
        return Image.composite(base, src, low)   # 需改色→base（白底/深字），其余→原色

    return Image.merge("RGB", (chan(r, br, tr), chan(g, bg_, tg_), chan(b, bb, tb)))


def _radar_black_to_white(img: Any, dark_thresh: int = 48, min_area: int = 3000) -> Any:
    """把 swan3 雷达图的无回波大黑块转成白底，保留黑色标题/分区标签/坐标轴/色标。

    swan3 雷达图的无回波区是纯黑 (0,0,0) 的大块连通区域；而标题、分区标签（如
    "静海区"）、坐标轴刻度也是黑色但笔画细。两者同为黑色、色值无法区分，只能靠
    **连通域面积**：对近黑掩膜做连通域标记，面积 >= min_area 的大黑块填白，
    细黑字（连通域小）原样保留；彩色回波（非近黑）一律不动。

    注意：不能用纯阈值（会把纯黑标题一并填白），也不能用 PIL 形态学开/闭运算
    （MaxFilter/MinFilter 是方核，会在回波边缘产生块状锯齿，实测两种都不可行）。
    连通域标记用 numpy + scipy.ndimage（项目核心文件已依赖，生产环境自带）；
    任一不可用则原样返回（雷达保持黑底，不崩溃、不误判为错误）。
    """
    try:
        import numpy as np
        from scipy import ndimage
        from PIL import Image
    except Exception:
        return img
    a = np.asarray(img.convert("RGB"))
    black = a.max(axis=2) < dark_thresh
    if not black.any():
        return img
    lab, n = ndimage.label(black)
    if n == 0:
        return img
    sizes = np.bincount(lab.ravel())
    large = sizes >= min_area
    large[0] = False            # 0 号是非黑背景
    whiten = large[lab]
    if not whiten.any():
        return img
    out = a.copy()
    out[whiten] = (255, 255, 255)
    return Image.fromarray(out, "RGB")


def _fit_image(img: Any, usable_w: int, fill_width: bool = False) -> Any:
    """等比缩放子图。默认只缩不放大、高度不超过 _IMG_MAX_H。

    fill_width=True（雷达等宽图）：等比撑满内容宽（允许放大），仅受更高的
    _IMG_WIDE_MAX_H 约束，避免 tall 图被高度上限压成窄条。
    """
    w, h = img.size
    if not w or not h:
        return img
    if fill_width:
        scale = usable_w / w
        if h * scale > _IMG_WIDE_MAX_H:
            scale = _IMG_WIDE_MAX_H / h
    else:
        scale = min(1.0, usable_w / w, _IMG_MAX_H / h)
    if abs(scale - 1.0) > 1e-3:
        img = img.resize((max(1, int(round(w * scale))), max(1, int(round(h * scale)))))
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


def _paste_rgba(base: Any, tpl: Any | None, box: tuple[int, int], width: int) -> None:
    """把 RGBA 模板按宽度等比缩放后贴到 base 的 box 位置（透明混合）。缺失/失败跳过。"""
    if tpl is None:
        return
    try:
        w, h = tpl.size
        th = max(1, int(h * width / w))
        t = tpl.resize((width, th))
        base.paste(t, box, t if tpl.mode == "RGBA" else None)
    except Exception:
        return


def _draw_header(img: Any, draw) -> None:
    """顶部全幅风景头图 + 模板大标题(title.png) + 发布单位(publish-depart.png)。"""
    band = _header_band()
    if band is not None:
        img.paste(band, (0, 0))
    else:  # 模板缺失：纯蓝条兜底
        draw.rectangle([0, 0, _BOARD_WIDTH, _HEADER_H], fill=(70, 150, 210))
    # 发布单位（左上，publish-depart.png 白字蓝影）
    _paste_rgba(img, _load_template("publish-depart.png"), _HEADER_PUB_XY, _HEADER_PUB_W)
    # 大标题（居中，title.png 白字蓝影"海河流域水文气象公报"）
    _paste_rgba(img, _load_template("title.png"),
                ((_BOARD_WIDTH - _HEADER_TITLE_W) // 2, _HEADER_TITLE_Y), _HEADER_TITLE_W)


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
    """板块标题：图标+标题作为整体居中，两侧蓝色小粗点+细长装饰线。

    装饰线一律画在"图标+标题"整体之外，避免横穿图标（用户反馈 2026-08-18）。
    返回结束 y。
    """
    cx = _BOARD_WIDTH // 2
    cy = y + int(_SEC_SIZE * 0.7)
    tw = draw.textlength(title, font=font)
    line_y = cy + _SEC_SIZE // 2
    icon_w = 64 if icon else 0          # 图标预留宽
    gap = 18 if icon else 0             # 图标与标题间距
    unit_w = icon_w + gap + tw          # 图标+标题整体宽度
    unit_left = cx - unit_w / 2
    if icon:
        _draw_sec_icon(draw, unit_left + icon_w / 2, cy + _SEC_SIZE * 0.28, icon)
    text_cx = unit_left + icon_w + gap + tw / 2
    far_l = cx - (_BOARD_WIDTH / 2 - _CARD_MARGIN - _CARD_PAD)
    far_r = cx + (_BOARD_WIDTH / 2 - _CARD_MARGIN - _CARD_PAD)
    for sign, edge, far in ((-1, unit_left, far_l), (1, unit_left + unit_w, far_r)):
        # 细长装饰线（整体之外）
        draw.line([edge + sign * 30, line_y, far, line_y], fill=_RULE, width=3)
        # 蓝色小粗点（紧贴整体）
        draw.line([edge + sign * 8, line_y, edge + sign * 20, line_y], fill=_SEC_BLUE, width=5)
    _draw_centered(draw, text_cx, cy, title, font, _SEC_BLUE)
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


def _measure_image(img: Any, usable_w: int, fill_width: bool = False) -> int:
    return _fit_image(img, usable_w, fill_width).size[1] + 20


def _render_image_block(img: Any, draw, x: int, y: int, usable_w: int, sub: Any, fill_width: bool = False) -> int:
    """地图：白底 + 细框 + 居中（14所 图自带红色小标题）。fill_width=True 撑满内容宽。"""
    sub = _fit_image(sub, usable_w, fill_width)
    px = x + (usable_w - sub.size[0]) // 2
    draw.rectangle([px - 8, y + 2, px + sub.size[0] + 8, y + sub.size[1] + 12],
                   fill=(255, 255, 255), outline=_IMG_BORDER, width=2)
    if getattr(sub, "mode", "RGB") == "RGBA":  # 浅色主题图（④⑥黑底转透明）用自身 alpha 贴，白底透出
        img.paste(sub, (px, y + 7), sub)
    else:
        img.paste(sub, (px, y + 7))
    return y + sub.size[1] + 20


_RANK_COLS = (0.11, 0.27, 0.19, 0.22, 0.21)  # 序号|站点|省|市|降水量（雨量列加宽，配合缩字号完整显示表头）


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
        f = font
        # 先缩字号尽量完整显示（如"降水量(毫米)"），缩到下限仍超宽才截断
        while t and f.size > 20 and draw.textlength(t, font=f) > cw - 12:
            f = f.font_variant(size=f.size - 2)
        while t and draw.textlength(t, font=f) > cw - 12:
            t = t[:-1]
        tw = draw.textlength(t, font=f)
        draw.text((cx + (cw - tw) / 2, y + (_TBL_ROW_H - f.size) // 2 - 2), t, font=f, fill=fg)

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
    if kind == "image_wide":
        return _measure_image(payload, usable_w, fill_width=True)
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
    if kind == "image_wide":
        return _render_image_block(img, draw, x, y, usable_w, payload, fill_width=True)
    if kind == "rank":
        return _render_rank(draw, fonts["tbl"], x, y, usable_w, payload[0], payload[1])
    if kind == "fore":
        return _render_fore(draw, fonts["tbl"], x, y, usable_w, payload[1], payload[2])
    return y


def _background(height: int) -> Any:
    """生成整页背景：优先用 bg.png（浅蓝云纹）按宽缩放后竖向裁剪/平铺到 height。"""
    from PIL import Image
    bg = _load_template("bg.png")
    if bg is None:
        return Image.new("RGB", (_BOARD_WIDTH, height), _BG)
    try:
        bg = bg.convert("RGB")
        bw, bh = bg.size
        if bw != _BOARD_WIDTH:
            bh = int(bh * _BOARD_WIDTH / bw)
            bg = bg.resize((_BOARD_WIDTH, bh))
        if height <= bh:
            return bg.crop((0, 0, _BOARD_WIDTH, height))
        out = Image.new("RGB", (_BOARD_WIDTH, height), _BG)
        y = 0
        while y < height:
            out.paste(bg, (0, y))
            y += bh
        return out
    except Exception:
        return Image.new("RGB", (_BOARD_WIDTH, height), _BG)


_CARD_TOP_PAD = 26           # 卡片内顶部留白
_CARD_BOTTOM_PAD = 24        # 卡片内底部留白
_BLOCK_GAP = 14              # 卡片内块间距


def _section_card_h(draw, fonts, content_w: int, sec: dict) -> int:
    h = _CARD_TOP_PAD + _sec_header_h() + 6
    for kind, payload in sec["blocks"]:
        h += _measure_block(draw, fonts, content_w, kind, payload) + _BLOCK_GAP
    return h + _CARD_BOTTOM_PAD


def _compose_longimg(sections: list[dict]) -> bytes | None:
    """把 3 大主题板块渲染成一张严格对齐示范图的模板化长图 PNG。

    sections: [{"header": 板块名, "icon": 图标键, "blocks": [(kind, payload), ...]}]，
    kind ∈ text/image/caption/rank/fore。每个板块一张白色圆角卡片，板间露出浅蓝背景。
    缺 Pillow/缺中文字体返回 None（调用方降级文字）。
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

    # 第一遍：量各板块卡片高度，累加总高
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1), (255, 255, 255)))
    card_heights = [_section_card_h(probe, fonts, content_w, sec) for sec in sections]
    total_h = _HEADER_H + _SECTION_GAP + sum(card_heights) + _SECTION_GAP * len(sections) + _BOTTOM_PAD

    img = _background(total_h)
    draw = ImageDraw.Draw(img)

    # 顶部风景头图（全幅 + 模板标题/发布单位）
    _draw_header(img, draw)

    # 第二遍：逐板块画白色卡片并渲染
    y = _HEADER_H + _SECTION_GAP
    for sec, ch in zip(sections, card_heights):
        draw.rounded_rectangle([card_x, y, card_x + card_w, y + ch],
                               radius=_CARD_RADIUS, fill=(255, 255, 255))
        cy = y + _CARD_TOP_PAD
        cy = _draw_sec_header(draw, cy, sec["header"], fonts["sec"], sec.get("icon", "")) + 6
        for kind, payload in sec["blocks"]:
            cy = _render_block(img, draw, fonts, content_x, cy, content_w, kind, payload) + _BLOCK_GAP
        y += ch + _SECTION_GAP

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- 主函数

def _load_hhweb_product_tool() -> Any:
    """按文件路径加载同目录 hhweb_product_tool（绕开 custom_tools/__init__.py 重依赖，
    与测试 importlib 加载方式同口径）。"""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "hhweb_product_tool",
        Path(__file__).resolve().parent / "hhweb_product_tool.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _hhweb_time(end_time: str) -> str:
    """hhweb time 口径（用户确认"改调用时间即可"）：显式 endTime 向下取整点，否则当前北京时整点。"""
    s = (end_time or "").strip()
    if s:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:00:00")
            except ValueError:
                continue
    return time_source.now(BEIJING_TIMEZONE).strftime("%Y-%m-%d %H:00:00")


def generate_haihe_composite_longimg_core(
    beginTime: str = "",
    endTime: str = "",
    areaIds: list | None = None,
    interval: int = 24,
    range: str = "9",
    type: str = "0",
) -> dict[str, Any]:
    """生成组合长图：**hhweb 拼网址 + 本机浏览器截图**（2026-08-19 用户决定改天河做法）。

    背景：PIL 逐板块拼接的样式反复出问题（④⑥ 服务端 isClimateImg 不稳定、偶发黑底原图，
    ③ 副标题/图例白字印白底）。用户决定改用 hhweb product-image 网页版长图（服务端页面
    自己渲染，与示范图完全一致：白底地图、模板标题），只需按当前时次拼网址（time 与
    radarTime 同传当前北京时整点），本机有浏览器则截图出长图，否则降级返回网址。

    原 PIL 7 板块拼接实现完整保留在 `_generate_haihe_composite_longimg_core_pil`
    （当前未被调用、不删除）；回滚只需把下方 return 换成调用它。
    """
    time_str = _hhweb_time(endTime)
    hhweb = _load_hhweb_product_tool()
    result = hhweb.get_haihe_product_longimg_core(
        time=time_str, radarTime=time_str, screenshot=True,
    )
    b64 = result.get("base64") or ""
    warn = "" if b64 else (
        f"未能直接截图（{result.get('screenshot_error') or '本机无可用浏览器'}），已降级返回 hhweb 长图网址"
    )
    return {
        "status": result.get("status", "ok"),
        "base64": b64,
        "text": result.get("text") or "（长图见 hhweb 网址）",
        "render_warning": warn,
        "beginTime": beginTime,
        "endTime": endTime,
        "range": str(range or "9"),
        "type": str(type or "0"),
        "url": result.get("url", ""),
        "message": result.get("message", "已生成 hhweb 拼网址长图。"),
    }


def _generate_haihe_composite_longimg_core_pil(
    beginTime: str = "",
    endTime: str = "",
    areaIds: list | None = None,
    interval: int = 24,
    range: str = "9",
    type: str = "0",
) -> dict[str, Any]:
    """【保留不删，当前未接线】原 PIL 7 板块逐接口拼装实现（2026-08-19 前的主路径）。

    用户 2026-08-19 决定改用 hhweb 拼网址截图（见 generate_haihe_composite_longimg_core）。
    本函数完整保留原实现用于回滚/对照——回滚只需把 generate_haihe_composite_longimg_core
    的 return 换成调用本函数。注意：④⑥ 需 `_black_bg_to_white` 处理服务端偶发黑底原图。
    """
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
    query_time = time_source.now(BEIJING_TIMEZONE).strftime("%Y%m%d%H0000")

    texts: list[str] = []

    # ==================================================== 雷达拼图（② swan3）
    try:
        swan = _fetch_swan3(query_time)
    except Exception as exc:
        swan = None
        texts.append(f"雷达图获取失败：{_safe_err(exc)}")
    swan_img = _radar_black_to_white(_load_image(swan)) if swan else None
    radar_blocks = [("image_wide", swan_img)] if swan_img else [("text", "（雷达图获取失败）")]

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
    area_real_img = _black_bg_to_white(_load_image(area_real)) if area_real else None
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
    fore_img = _black_bg_to_white(_load_image(fore_img_bytes)) if fore_img_bytes else None
    fore_blocks.append(("image", fore_img) if fore_img else ("text", "（预报面雨量图获取失败）"))

    # ⑦ 每日河系雨量预报表（独立容错：⑥图失败也要尽量出表）。
    # ⑥成功则沿用其起报时次；⑥失败则自行探测一个有预报数据的起报时次，互不拖累。
    data_fore = used_fore
    if not data_fore:
        for fc in fore_cycles:
            try:
                fdt = datetime.strptime(fc, "%Y-%m-%d %H:00:00")
                d1 = (fdt + timedelta(hours=24)).strftime("%Y-%m-%d %H:00:00")
                if _fetch_forecast(fc, fc, d1, area_ids, 24):
                    data_fore = fc
                    break
            except Exception:
                continue
    if data_fore:
        fdt = datetime.strptime(data_fore, "%Y-%m-%d %H:00:00")
        for day in _RANGE(_FORECAST_DAYS):
            d0 = fdt + timedelta(hours=24 * day)
            d1 = d0 + timedelta(hours=24)
            b, e = d0.strftime("%Y-%m-%d %H:00:00"), d1.strftime("%Y-%m-%d %H:00:00")
            try:
                fc_data = _fetch_forecast(data_fore, b, e, area_ids, 24)
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
        生成海河流域 14所 降水专题**组合长图**（hhweb 拼网址网页版，天河做法）。

        拼 hhweb product-image 网址（雷达 + 降水实况 + 降水预报，与示范图完全一致：
        白底地图、模板标题），本机有浏览器时直接截图出长图（PNG，base64 由前端自动
        展示），无浏览器时降级返回网址供内网浏览器打开。只出图、不返回数值，不能
        用于回答 "下了多少雨 / 天气怎么样 / 面雨量多少" 等数值查询。

        触发：用户说"长图 / 组合长图 / 降水专题长图 / 出今天的长图 / 出一张长图"
        等，未指明具体图类型时默认走本工具（本智能体的"长图"即降水专题组合长图）。

        与 get_station_rainfall_real_img（各子流域降雨分布图）区分：那是单一分区
        面雨量空间分布图；本工具是多板块组合长图。

        Args:
            beginTime: 开始时间 "YYYY-MM-DD HH:mm:ss"（北京时），网页版仅用于兼容旧参数，可不传
            endTime: 结束时间，用作 hhweb 的产品时次（向下取整点）；不传取当前北京时整点
            areaIds: 区域 id 列表，网页版忽略，默认海河9大分区
            interval: 间隔(小时)，网页版忽略，默认 24
            range: 分区，网页版忽略，默认 "9"
            type: 站点类型，网页版忽略，默认 "0"
        """
        return generate_haihe_composite_longimg_core(
            beginTime=beginTime,
            endTime=endTime,
            areaIds=areaIds,
            interval=interval,
            range=range,
            type=type,
        )
