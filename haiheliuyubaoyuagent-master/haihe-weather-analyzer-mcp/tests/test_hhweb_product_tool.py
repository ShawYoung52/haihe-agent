"""14所 hhweb 拼网址长图工具测试。

口径（天河提供 2026-08-18）：拼 product-image 网址，time 必传、radarTime 分开传；
能截图则出长图 base64，否则返回网址。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

_spec = importlib.util.spec_from_file_location(
    "hhweb_product_tool",
    MCP_DIR / "custom_tools" / "hhweb_product_tool.py",
)
hpt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hpt)


class TestBuildProductUrl:
    def test_basic_format(self):
        url = hpt.build_product_url("2025-07-27 10:00:00")
        assert url.startswith(hpt.HHWEB_PRODUCT_BASE + "/hhweb/#/product-image/")
        assert "type=radar,rain,rain-forcast" in url
        assert "time=2025-07-27%2010:00:00" in url

    def test_time_required_space_encoded_colon_kept(self):
        url = hpt.build_product_url("2026-08-17 08:00:00")
        assert "time=2026-08-17%2008:00:00" in url

    def test_radartime_appended_separately(self):
        url = hpt.build_product_url("2025-07-27 10:00:00", "2025-07-27 15:30:00")
        assert "time=2025-07-27%2010:00:00" in url
        assert "radarTime=2025-07-27%2015:30:00" in url

    def test_radartime_omitted_when_empty(self):
        url = hpt.build_product_url("2025-07-27 10:00:00", "")
        assert "radarTime" not in url

    def test_custom_types(self):
        url = hpt.build_product_url("2025-07-27 10:00:00", types="radar,rain")
        assert "type=radar,rain&" in url


class TestCore:
    def test_returns_url_and_status(self):
        r = hpt.get_haihe_product_longimg_core(time="2025-07-27 10:00:00", screenshot=False)
        assert r["status"] == "ok"
        assert r["url"].startswith("http")
        assert "time=2025-07-27%2010:00:00" in r["url"]
        assert r["time"] == "2025-07-27 10:00:00"

    def test_default_time_is_now(self):
        r = hpt.get_haihe_product_longimg_core(screenshot=False)
        assert r["time"], "不传 time 应取当前整点"

    def test_no_browser_falls_back_to_url(self, monkeypatch):
        monkeypatch.setattr(hpt, "_try_screenshot", lambda url: None)
        r = hpt.get_haihe_product_longimg_core(time="2025-07-27 10:00:00")
        assert r["status"] == "ok"
        assert r["base64"] == "", "无浏览器应降级为返回网址"
        assert r["url"] in r["text"], "降级文本应含网址"

    def test_screenshot_success_returns_base64(self, monkeypatch):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
        monkeypatch.setattr(hpt, "_try_screenshot", lambda url: png)
        r = hpt.get_haihe_product_longimg_core(time="2025-07-27 10:00:00")
        assert r["base64"], "截图成功应返回 base64"
        assert "长图" in r["text"]

    def test_radartime_passthrough(self):
        r = hpt.get_haihe_product_longimg_core(
            time="2025-07-27 10:00:00", radarTime="2025-07-27 15:30:00", screenshot=False)
        assert "radarTime=2025-07-27%2015:30:00" in r["url"]
        assert r["radarTime"] == "2025-07-27 15:30:00"
