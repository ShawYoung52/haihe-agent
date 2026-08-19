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


class _FakePage:
    def __init__(self, sink):
        self._sink = sink

    def goto(self, url, wait_until=None, timeout=None):
        self._sink["goto"] = {"url": url, "wait_until": wait_until, "timeout": timeout}

    def wait_for_timeout(self, ms):
        self._sink["wait_ms"] = ms

    def screenshot(self, full_page=False, type=None):
        self._sink["screenshot"] = {"full_page": full_page, "type": type}
        return b"\x89PNG\r\n\x1a\n" + b"0" * 8


class _FakeBrowser:
    def __init__(self, sink):
        self._sink = sink

    def new_page(self, viewport=None):
        self._sink["viewport"] = viewport
        return _FakePage(self._sink)

    def close(self):
        self._sink["closed"] = True


class _FakePW:
    def __init__(self, sink, launch_exc=None):
        self._sink = sink
        self._launch_exc = launch_exc
        self.chromium = self

    def launch(self, args=None):
        if self._launch_exc:
            raise self._launch_exc
        return _FakeBrowser(self._sink)


class _FakePWCM:
    def __init__(self, pw):
        self._pw = pw

    def __enter__(self):
        return self._pw

    def __exit__(self, *a):
        return False


def _install_fake_playwright(monkeypatch, sink, launch_exc=None):
    """注入假 playwright.sync_api（sync_playwright 返回假浏览器上下文管理器）。"""
    import types as _t
    play = _t.ModuleType("playwright")
    sync_api = _t.ModuleType("playwright.sync_api")
    pw = _FakePW(sink, launch_exc)
    sync_api.sync_playwright = lambda: _FakePWCM(pw)
    play.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", play)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)


class TestScreenshotDiagnostics:
    """截图失败必须给出脱敏的具体原因（用户 2026-08-19 反馈"为啥不出图"无法定位）。"""

    def test_no_playwright_reason(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)  # 强制 ImportError
        assert hpt._try_screenshot("http://x/") is None
        assert "playwright" in hpt._LAST_SCREENSHOT_REASON
        assert "http" not in hpt._LAST_SCREENSHOT_REASON, "原因须脱敏，不含 URL"

    def test_chromium_missing_reason(self, monkeypatch):
        sink = {}
        _install_fake_playwright(monkeypatch, sink, launch_exc=RuntimeError("Executable doesn't exist"))
        assert hpt._try_screenshot("http://x/") is None
        assert "Chromium" in hpt._LAST_SCREENSHOT_REASON

    def test_uses_load_not_networkidle_and_full_page(self, monkeypatch):
        """hhweb 是带轮询的 Vue 页，networkidle 可能永不触发（每次白等 60s 超时）。"""
        sink = {}
        _install_fake_playwright(monkeypatch, sink)
        out = hpt._try_screenshot("http://x/")
        assert out and out[:8] == b"\x89PNG\r\n\x1a\n"
        assert sink["goto"]["wait_until"] == "load", "应用 load 而非 networkidle"
        assert sink["screenshot"]["full_page"] is True
        assert sink["closed"] is True, "浏览器应被关闭"

    def test_core_exposes_screenshot_error(self, monkeypatch):
        def fake_shot(url):
            hpt._LAST_SCREENSHOT_REASON = "服务器未安装 playwright"
            return None

        monkeypatch.setattr(hpt, "_try_screenshot", fake_shot)
        r = hpt.get_haihe_product_longimg_core(time="2025-07-27 10:00:00")
        assert r["base64"] == ""
        assert r["screenshot_error"] == "服务器未安装 playwright"
        assert "服务器未安装 playwright" in r["text"], "降级文案应带具体原因"

