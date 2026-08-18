"""14所 hhweb 拼网址长图工具（天河做法）。

业务场景：用户问「长图 / 今天的长图 / 降水专题长图」时，除了 PIL 拼接组合长图外，
还可按天河方式**拼 hhweb product-image 网址**，得到与示范图完全一致的网页版长图
（白底地图、模板标题）。本机构造好网址后：

- 若本机装有 Playwright + Chromium（内网离线服务器一般没有），直接截图出长图 PNG；
- 否则返回该网址，由前端/用户在内网浏览器打开查看。

网址格式（天河提供 2026-08-18）：
    http://10.226.107.35:8070/hhweb/#/product-image/type=radar,rain,rain-forcast&time=2025-07-27 10:00:00
    可选 &radarTime=2025-07-27 15:30:00
- time      ：产品(预报)时次，**必传**，"YYYY-MM-DD HH:00:00"
- radarTime ：雷达观测时次，可选，与 time **分开传**
- type      ：产品类型，逗号分隔 radar(雷达)/rain(降水实况)/rain-forcast(降水预报)

注意：这是 Vue 前端路由（#/ 哈希），参数在哈希里由前端解析；截图需能访问该页的浏览器。
base 默认 http://10.226.107.35:8070，env HHWEB_PRODUCT_BASE 可覆盖。
"""
from __future__ import annotations

import base64
import os
from datetime import datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastmcp import FastMCP

BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")

HHWEB_PRODUCT_BASE = os.getenv("HHWEB_PRODUCT_BASE", "http://10.226.107.35:8070").rstrip("/")
DEFAULT_TYPES = "radar,rain,rain-forcast"

_SCREENSHOT_TIMEOUT_MS = 60000
_VIEWPORT = {"width": 1125, "height": 1600}


def _fmt(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:00:00")


def build_product_url(time_str: str, radar_time: str = "", types: str = DEFAULT_TYPES) -> str:
    """按天河格式拼 product-image 网址。time 必传；radarTime 传了才拼。"""
    types = (types or DEFAULT_TYPES).strip()
    q = f"type={types}&time={quote(time_str.strip(), safe=':-')}"
    if radar_time and radar_time.strip():
        q += f"&radarTime={quote(radar_time.strip(), safe=':-')}"
    return f"{HHWEB_PRODUCT_BASE}/hhweb/#/product-image/{q}"


def _try_screenshot(url: str) -> bytes | None:
    """用 Playwright + Chromium 全页截图出长图；缺浏览器/失败返回 None（不报错）。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                page = browser.new_page(viewport=_VIEWPORT)
                page.goto(url, wait_until="networkidle", timeout=_SCREENSHOT_TIMEOUT_MS)
                page.wait_for_timeout(3000)  # 等图表/图片渲染
                return page.screenshot(full_page=True, type="png")
            finally:
                browser.close()
    except Exception:
        return None


def get_haihe_product_longimg_core(
    time: str = "",
    radarTime: str = "",
    types: str = DEFAULT_TYPES,
    screenshot: bool = True,
) -> dict[str, Any]:
    """拼 product-image 网址；能截图则返回长图 base64，否则返回网址。"""
    time_str = time.strip() or _fmt(datetime.now(BEIJING_TIMEZONE))
    url = build_product_url(time_str, radarTime, types)

    png = _try_screenshot(url) if screenshot else None
    b64 = base64.b64encode(png).decode("ascii") if png else ""

    if b64:
        text = "已用 hhweb 网页版生成长图（白底地图、与示范图一致）。"
    else:
        text = (
            "网页版长图地址（内网打开即可查看，含白底地图）：\n" + url
            + "\n（本机未检测到可用浏览器，未能直接截图；在内网浏览器打开上面的网址即得长图。）"
        )
    return {
        "status": "ok",
        "url": url,
        "base64": b64,
        "text": text,
        "time": time_str,
        "radarTime": radarTime.strip(),
        "types": types or DEFAULT_TYPES,
        "message": "已构造 hhweb 拼网址长图。" if b64 else "已构造 hhweb 拼网址长图网址。",
    }


def register_hhweb_product_tool(mcp: FastMCP) -> None:
    @mcp.tool()
    def get_haihe_product_image_url(
        time: str = "",
        radarTime: str = "",
        types: str = DEFAULT_TYPES,
        screenshot: bool = True,
    ) -> dict:
        """
        用**拼网址**方式生成海河流域降水专题长图（天河做法，与示范图完全一致：白底地图）。

        构造 hhweb product-image 网址；本机有浏览器时直接截图返回长图 base64，
        否则返回该网址由前端/用户在内网浏览器打开。适合要"白底地图版"长图、或
        PIL 拼接版地图底色不符时使用。

        Args:
            time: 产品(预报)时次 "YYYY-MM-DD HH:00:00"（北京时），必传；不传取当前整点
            radarTime: 雷达观测时次，可选，与 time 分开传；不传则不拼该参数
            types: 产品类型，逗号分隔，默认 "radar,rain,rain-forcast"（雷达/降水实况/降水预报）
            screenshot: 是否尝试本机浏览器截图出图，默认 True（无浏览器自动降级为返回网址）
        """
        return get_haihe_product_longimg_core(
            time=time, radarTime=radarTime, types=types, screenshot=screenshot,
        )
