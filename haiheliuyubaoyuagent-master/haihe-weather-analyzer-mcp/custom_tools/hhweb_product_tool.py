"""14所 hhweb 拼网址长图工具（天河做法，product-image-new 版）。

业务场景：用户问「长图 / 今天的长图 / 降水专题长图」时，除了 PIL 拼接组合长图外，
还可按天河方式**拼 hhweb product-image-new 网址**，得到与示范图完全一致的网页版长图
（白底地图、模板标题）。本机构造好网址后：

- 若本机装有 Playwright + Chromium（内网离线服务器一般没有），直接截图出长图 PNG；
- 否则返回该网址，由前端/用户在内网浏览器打开查看。

网址格式（甲方 2026-09-29《url示例》）：
    http://<host>/hhweb/#/product-image-new/type=radar,rain,rain-forcast
        &time=2026-09-22 08:00:00&radarTime=...&forcastTime=...&areaId=1&areaCodes=ALL
- type       ：出图块顺序，逗号分隔 radar(雷达)/rain(降水实况)/rain-forcast(降水预报)，
               重复类型只保留第一次；未知类型报错
- time       ：实况结束时间，**必传**，"YYYY-MM-DD HH:00:00"；同时作 radarTime/forcastTime 兜底
- radarTime  ：雷达观测时次（截到分钟），默认=time
- forcastTime：降水预报起报时次（08 或 20），默认按 time 取**最近 08/20 起报**（代码确定性，
               不调起报时间接口）；预报开始=该时间、结束+3天
- areaId     ：一级区域（对应 /hhfw/long_image/areas 的 areaId，**静态映射**不调接口）
- areaCodes  ：二级 children[].code 逗号拼接；不传或全选 → ALL

区域口径（静态映射，2026-09-29 用户拍板）：
- area="tj"  → areaId=5 & areaCodes=ALL（天津）
- area="jjj" → areaId=4 & areaCodes=ALL（京津冀）
- area=""    → areaId=1 & areaCodes=ALL（9分区全流域，默认）
- 分区名（如"北三河"）→ 9分区静态表对应 code（areaId=1 & areaCodes=6）

base 默认 http://10.226.107.35:8080，env HHWEB_PRODUCT_BASE 可覆盖。
注意：这是 Vue 前端路由（#/ 哈希），参数在哈希里由前端解析；截图需能访问该页的浏览器。
"""
from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta
import time_source
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastmcp import FastMCP

BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")

HHWEB_PRODUCT_BASE = os.getenv("HHWEB_PRODUCT_BASE", "http://10.226.107.35:8080").rstrip("/")
DEFAULT_TYPES = "radar,rain,rain-forcast"
VALID_TYPES = ("radar", "rain", "rain-forcast")

# 区域别名 → (areaId, areaCodes)。静态映射（不调 areas 接口，2026-09-29 用户拍板）。
# tj/jjj 为 2026-09-02 起的旧 area 参数口径，保留兼容。
_AREA_ALIASES: dict[str, tuple[int, str]] = {
    "tj": (5, "ALL"),
    "天津": (5, "ALL"),
    "jjj": (4, "ALL"),
    "京津冀": (4, "ALL"),
}

# 9分区（areaId=1）静态 children code 表（对应 /hhfw/long_image/areas areaId=1）。
_ZONE9_NAME_TO_CODE: dict[str, str] = {
    "徒骇马颊河": "11",
    "漳卫河": "13",
    "黑龙港运东": "9",
    "海河干流": "8",
    "子牙河": "14",
    "大清河": "7",
    "永定河": "12",
    "北三河": "6",
    "滦河": "10",
}
# 各 areaId 合法的 children code 全集（areaCodes 校验用；ALL 全选恒合法）。
_AREA_CHILD_CODES: dict[int, frozenset[str]] = {
    1: frozenset(_ZONE9_NAME_TO_CODE.values()),
    2: frozenset({"47", "48", "49", "50", "51", "52", "53", "54", "55", "56", "57"}),
    4: frozenset({"561"}),
    5: frozenset({"560"}),
}
_VALID_AREA_IDS = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11})


def _normalize_types(types: str) -> str:
    """type 出图块顺序：逗号分隔，重复只留第一次；空→默认；未知类型抛 ValueError。"""
    raw = (types or "").strip()
    if not raw:
        return DEFAULT_TYPES
    seen: list[str] = []
    for part in raw.split(","):
        t = part.strip()
        if not t:
            continue
        if t not in VALID_TYPES:
            raise ValueError(f"type 只能是 radar/rain/rain-forcast 的组合，不支持：{t}")
        if t not in seen:
            seen.append(t)
    return ",".join(seen) if seen else DEFAULT_TYPES


def _resolve_area(area: str, area_id: int | str | None, area_codes: str) -> tuple[int, str]:
    """归一区域参数为 (areaId, areaCodes)。

    优先级：显式 area_id/area_codes > area 别名（tj/jjj/分区名）> 默认 9分区全流域。
    areaCodes 按静态表校验：不属于该 areaId 的 code 抛 ValueError；空/全选→ALL。
    """
    if area_id is not None and str(area_id).strip() != "":
        try:
            aid = int(str(area_id).strip())
        except (TypeError, ValueError):
            raise ValueError(f"areaId 必须是数字，收到：{area_id}")
        if aid not in _VALID_AREA_IDS:
            raise ValueError(f"不支持的 areaId：{aid}")
        codes_raw = (area_codes or "").strip()
        if not codes_raw or codes_raw.upper() == "ALL":
            return aid, "ALL"
        codes = [c.strip() for c in codes_raw.split(",") if c.strip()]
        if not codes:
            return aid, "ALL"
        if codes[0].upper() == "ALL" and len(codes) > 1:
            raise ValueError("areaCodes 全选只能单独写 ALL，不能与具体 code 混拼")
        known = _AREA_CHILD_CODES.get(aid)
        if known is not None:
            bad = [c for c in codes if c not in known]
            if bad:
                raise ValueError(f"areaId={aid} 不支持这些 areaCodes：{'、'.join(bad)}")
        return aid, ",".join(codes)

    a = (area or "").strip()
    if not a:
        return 1, "ALL"
    lower = a.lower()
    if lower in _AREA_ALIASES:
        return _AREA_ALIASES[lower]
    if a in _AREA_ALIASES:
        return _AREA_ALIASES[a]
    if a in _ZONE9_NAME_TO_CODE:
        return 1, _ZONE9_NAME_TO_CODE[a]
    raise ValueError(
        "area 只能是 tj（天津）/ jjj（京津冀）/ 9分区名（北三河、滦河等）；"
        "或用 areaId+areaCodes 显式指定"
    )


def _default_forcast_time(time_str: str) -> str:
    """forcastTime 默认取 time 的最近 08/20 起报时次（代码确定性，不调起报时间接口）。

    time < 当天 08:00 → 昨日 20:00；08:00<=time<20:00 → 当天 08:00；>=20:00 → 当天 20:00。
    """
    try:
        dt = datetime.strptime(time_str.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return time_str.strip()
    if dt.hour >= 20:
        cycle = dt.replace(hour=20, minute=0, second=0)
    elif dt.hour >= 8:
        cycle = dt.replace(hour=8, minute=0, second=0)
    else:
        cycle = (dt - timedelta(days=1)).replace(hour=20, minute=0, second=0)
    return cycle.strftime("%Y-%m-%d %H:%M:%S")


def _normalize_forcast_time(forcast_time: str, time_str: str) -> str:
    """forcastTime：显式给出且格式合法才采用（截到分钟回零秒），否则按 time 自动推导。"""
    raw = (forcast_time or "").strip()
    if raw:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(raw, fmt)
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
    return _default_forcast_time(time_str)

_SCREENSHOT_TIMEOUT_MS = 60000
_RENDER_WAIT_MS = 5000   # load 事件后再等图表/图片渲染（networkidle 在轮询型 Vue 页可能永不触发）
_VIEWPORT = {"width": 1125, "height": 1600}

# 最近一次截图失败的脱敏原因（不含 URL/IP），供上层降级文案展示
_LAST_SCREENSHOT_REASON = ""


def _fmt(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:00:00")


def build_product_url(
    time_str: str,
    radar_time: str = "",
    types: str = DEFAULT_TYPES,
    area: str = "",
    forcast_time: str = "",
    area_id: int | str | None = None,
    area_codes: str = "",
) -> str:
    """按甲方 product-image-new 格式拼网址。

    time 必传；radarTime 传了才拼（默认前端取 time）；forcastTime 自动取 time 的
    最近 08/20 起报（可显式覆盖）；areaId/areaCodes 由 area 别名或显式参数归一。
    """
    types_norm = _normalize_types(types)
    aid, codes = _resolve_area(area, area_id, area_codes)
    fcst = _normalize_forcast_time(forcast_time, time_str)
    q = f"type={types_norm}&time={quote(time_str.strip(), safe=':-')}"
    if radar_time and radar_time.strip():
        q += f"&radarTime={quote(radar_time.strip(), safe=':-')}"
    q += f"&forcastTime={quote(fcst, safe=':-')}"
    q += f"&areaId={aid}&areaCodes={codes}"
    return f"{HHWEB_PRODUCT_BASE}/hhweb/#/product-image-new/{q}"


def _try_screenshot(url: str) -> bytes | None:
    """从 hhweb product-image 页拿长图 PNG；缺浏览器/失败返回 None（不报错）。

    取图优先级（2026-08-19 按真实页面 DOM 诊断结果定）：
      1. 点页面自带的「下载」按钮拿官方导出文件（全分辨率、无页面杂边——页面预览
         是 CSS 缩小的，整页截图分辨率低且带侧栏，下载按钮才是页面设计的产品出口）；
      2. 兜底截产品容器 `#image-download-target` 元素本体（element.screenshot 不受
         外层裁剪限制，能拿到完整高度）；
      3. 再兜底整页 full_page 截图。

    失败时把**脱敏**原因写入模块级 `_LAST_SCREENSHOT_REASON`（不含 URL/内网 IP）。
    注意用 wait_until="load" 而非 networkidle：hhweb 是带轮询的 Vue 页，networkidle
    可能永不触发而每次白等 60s 超时。
    """
    global _LAST_SCREENSHOT_REASON
    _LAST_SCREENSHOT_REASON = ""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        _LAST_SCREENSHOT_REASON = "服务器未安装 playwright"
        return None
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            except Exception:
                _LAST_SCREENSHOT_REASON = "playwright 已安装但 Chromium 浏览器缺失（需执行 playwright install chromium）"
                return None
            try:
                page = browser.new_page(viewport=_VIEWPORT, device_scale_factor=2)
                page.goto(url, wait_until="load", timeout=_SCREENSHOT_TIMEOUT_MS)
                try:
                    # rain-forcast-part 是最后一个板块，它出现说明三块产品都渲染了
                    page.wait_for_selector(".rain-forcast-part", timeout=30000)
                except Exception:
                    pass
                page.wait_for_timeout(_RENDER_WAIT_MS)  # 再等图表/图片加载
                # 1) 优先：页面自带「下载」按钮，拿官方导出图
                try:
                    with page.expect_download(timeout=15000) as dl_info:
                        page.locator("text=下载").first.click(timeout=5000)
                    dl_path = dl_info.value.path()
                    with open(dl_path, "rb") as f:
                        return f.read()
                except Exception:
                    pass
                # 2) 兜底：截产品容器元素（含被外层裁剪的完整高度）
                el = page.query_selector("#image-download-target")
                if el is not None:
                    return el.screenshot(type="png")
                # 3) 再兜底：整页
                return page.screenshot(full_page=True, type="png")
            except Exception:
                _LAST_SCREENSHOT_REASON = "hhweb 页面加载/渲染失败（页面不可达或渲染超时）"
                return None
            finally:
                browser.close()
    except Exception:
        if not _LAST_SCREENSHOT_REASON:
            _LAST_SCREENSHOT_REASON = "截图失败（playwright 运行时错误）"
        return None


def get_haihe_product_longimg_core(
    time: str = "",
    radarTime: str = "",
    types: str = DEFAULT_TYPES,
    area: str = "",
    forcastTime: str = "",
    areaId: str = "",
    areaCodes: str = "",
    screenshot: bool = True,
) -> dict[str, Any]:
    """拼 product-image-new 网址；能截图则返回长图 base64，否则返回网址。"""
    time_str = time.strip() or _fmt(time_source.now(BEIJING_TIMEZONE))
    try:
        url = build_product_url(
            time_str, radarTime, types, area,
            forcast_time=forcastTime,
            area_id=areaId if str(areaId or "").strip() else None,
            area_codes=areaCodes,
        )
    except ValueError as e:
        return {
            "status": "error", "url": "", "base64": "", "text": str(e),
            "time": time_str, "radarTime": radarTime.strip(),
            "forcastTime": "", "types": types or DEFAULT_TYPES,
            "area": (area or "").strip(),
            "screenshot_error": "", "message": str(e),
        }

    global _LAST_SCREENSHOT_REASON
    _LAST_SCREENSHOT_REASON = ""
    png = _try_screenshot(url) if screenshot else None
    screenshot_error = _LAST_SCREENSHOT_REASON
    b64 = base64.b64encode(png).decode("ascii") if png else ""

    if b64:
        text = "已用 hhweb 网页版生成长图（白底地图、与示范图一致）。"
    else:
        reason = screenshot_error or "本机未检测到可用浏览器"
        text = (
            "网页版长图地址（内网打开即可查看，含白底地图）：\n" + url
            + f"\n（未能直接截图：{reason}；在内网浏览器打开上面的网址即得长图。）"
        )
    return {
        "status": "ok",
        "url": url,
        "base64": b64,
        "text": text,
        "time": time_str,
        "radarTime": radarTime.strip(),
        "forcastTime": _normalize_forcast_time(forcastTime, time_str),
        "types": _normalize_types(types),
        "area": (area or "").strip(),
        "screenshot_error": screenshot_error,
        "message": "已构造 hhweb 拼网址长图。" if b64 else "已构造 hhweb 拼网址长图网址。",
    }


def register_hhweb_product_tool(mcp: FastMCP) -> None:
    @mcp.tool()
    def get_haihe_product_image_url(
        time: str = "",
        radarTime: str = "",
        types: str = DEFAULT_TYPES,
        area: str = "",
        forcastTime: str = "",
        areaId: str = "",
        areaCodes: str = "",
        screenshot: bool = True,
    ) -> dict:
        """
        用**拼网址**方式生成海河流域降水专题长图（product-image-new 网页版，白底地图）。

        构造 hhweb product-image-new 网址；本机有浏览器时直接截图返回长图 base64，
        否则返回该网址由前端/用户在内网浏览器打开。适合要"白底地图版"长图、或
        PIL 拼接版地图底色不符时使用。

        Args:
            time: 实况结束时间 "YYYY-MM-DD HH:00:00"（北京时），必传；不传取当前整点
            radarTime: 雷达观测时次，可选，与 time 分开传；不传前端默认取 time
            types: 出图块顺序，逗号分隔 radar/rain/rain-forcast（雷达/降水实况/降水预报），
                默认 "radar,rain,rain-forcast"；重复类型只保留第一次
            area: 区域，可选："tj"（天津）/"jjj"（京津冀）/9分区名（北三河、滦河等）；
                不传默认 9分区全流域（areaId=1&areaCodes=ALL）
            forcastTime: 降水预报起报时次（08 或 20），可选；不传自动按 time 取最近 08/20 起报
            areaId: 一级区域 id（1=9分区、2=11分区、4=京津冀、5=天津…），与 areaCodes 配套显式指定
            areaCodes: 二级分区 code 逗号拼接（如 "6,7,8"）；不传或全选为 ALL
            screenshot: 是否尝试本机浏览器截图出图，默认 True（无浏览器自动降级为返回网址）
        """
        return get_haihe_product_longimg_core(
            time=time, radarTime=radarTime, types=types, area=area,
            forcastTime=forcastTime, areaId=areaId, areaCodes=areaCodes,
            screenshot=screenshot,
        )
