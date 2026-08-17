"""14所降水实况文字长图工具测试（/openapi/rainfall_describe/real）。

联调确认（2026-08-17）：接口 `data` 返回降水实况**文字**（非图片），工具用 Pillow
渲染成长图（PNG base64）。走 base64 → cl.Image 展示路径，不改动
chainlitexam/qa_http_api.py 的 _IMAGE_URL_ALLOW_HOSTS 白名单。
缺中文字体/缺 Pillow 时降级返回 text（base64 空），不报错。
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

# 直接按文件路径加载模块，避免触发 custom_tools/__init__.py 的完整包导入
# （该 __init__ 会连带 tools.py → networkx/rasterio 等重依赖，与本工具无关）。
_spec = importlib.util.spec_from_file_location(
    "rainfall_describe_tool",
    MCP_DIR / "custom_tools" / "rainfall_describe_tool.py",
)
rdt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rdt)

# 带 PNG 魔数前缀的假图片字节（mock 渲染返回值）。
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_FAKE_IMG = _PNG_MAGIC + b"fake-rendered-png-bytes"
_PNG_B64 = base64.b64encode(_FAKE_IMG).decode("ascii")

_REAL_TEXT = (
    "8月16日15时-17日15时，海河流域出现小雨，局部中雨，个别站大雨。"
    "最大面雨量出现在永定河，为4.6毫米。最大点雨量出现在河北省承德市平泉市平泉站，"
    "为36.2毫米。目前已造成海河流域1座水库超汛限，为盘石头。"
)


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _mock_post(monkeypatch, calls, payload=_REAL_TEXT):
    def fake_post(url, json=None, timeout=None):
        calls["n"] += 1
        calls["url"] = url
        calls["body"] = json
        return _FakeResp({"code": 200, "success": True, "data": payload})

    monkeypatch.setattr(rdt.requests, "post", fake_post)


def _mock_render(monkeypatch, result: bytes | None):
    monkeypatch.setattr(rdt, "_render_text_longimg", lambda text, title: result)


class TestScrubText:
    def test_scrubs_full_url_with_path(self):
        out = rdt._scrub_text("Connection refused: http://10.226.107.35:8001/openapi/rainfall_describe/real")
        for token in ("10.226.107.35", "8001", "openapi"):
            assert token not in out

    def test_scrubs_bare_ip_and_windows_path(self):
        assert "10.226.107.35" not in rdt._scrub_text("失败 host=10.226.107.35:8001")
        assert "[路径]" in rdt._scrub_text("无法打开 C:\\data\\config.ini")


class TestRender:
    def test_find_cjk_font_on_windows(self):
        font = rdt._find_cjk_font()
        if sys.platform.startswith("win"):
            assert font, "Windows 应能找到中文字体（微软雅黑等）"
            assert Path(font).is_file()
        else:
            # Linux 上可能没有；此时断言返回值要么存在要么为 None（不崩）
            assert font is None or Path(font).is_file()

    def test_render_longimg_produces_png(self):
        png = rdt._render_text_longimg(_REAL_TEXT, "海河流域降水实况文字（九分区）")
        assert png, "应渲染出 PNG 字节"
        assert png[:8] == b"\x89PNG\r\n\x1a\n", "PNG 魔数正确"

    def test_render_longimg_without_text_returns_none(self):
        assert rdt._render_text_longimg("", "标题") is None
        assert rdt._render_text_longimg("   ", "标题") is None

    def test_wrap_text_does_not_exceed_width(self):
        from PIL import Image, ImageDraw, ImageFont

        font = ImageFont.truetype(rdt._find_cjk_font(), rdt._LONGIMG_BODY_SIZE)
        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        max_w = rdt._LONGIMG_WIDTH - 2 * rdt._LONGIMG_MARGIN
        lines = rdt._wrap_text(probe, font, _REAL_TEXT, max_w)
        assert lines
        for line in lines:
            assert probe.textlength(line, font=font) <= max_w + 1, "换行后不应超宽"


class TestGenerate:
    def test_default_window_and_body(self, monkeypatch):
        """默认窗口 + body 组装正确；渲染成功返回 base64。"""
        calls = {"n": 0}
        _mock_post(monkeypatch, calls)
        _mock_render(monkeypatch, _FAKE_IMG)

        r = rdt.generate_rainfall_describe_longimg_core()
        assert r["status"] == "ok"
        assert r["base64"] == _PNG_B64
        assert r["text"] == _REAL_TEXT
        body = calls["body"]
        assert body["areaIds"] == [6, 7, 8, 9, 10, 11, 12, 13, 14]
        assert body["interval"] == 24
        assert body["range"] == "9"
        assert body["type"] == "0"
        assert body["beginTime"] and body["endTime"]

    def test_explicit_params_forwarded(self, monkeypatch):
        calls = {"n": 0}
        _mock_post(monkeypatch, calls)
        _mock_render(monkeypatch, _FAKE_IMG)

        r = rdt.generate_rainfall_describe_longimg_core(
            beginTime="2026-08-03 08:00:00",
            endTime="2026-08-04 08:00:00",
            areaIds=[6, 7],
            interval=24,
            range="11",
            type="1",
            isClimateImg=True,
        )
        assert r["status"] == "ok"
        body = calls["body"]
        assert body["beginTime"] == "2026-08-03 08:00:00"
        assert body["endTime"] == "2026-08-04 08:00:00"
        assert body["areaIds"] == [6, 7]
        assert body["range"] == "11"
        assert body["type"] == "1"
        assert body["isClimateImg"] is True

    def test_render_failure_degrades_to_text(self, monkeypatch):
        """渲染失败（缺中文字体/Pillow）→ status ok + base64 空 + text 保留。"""
        calls = {"n": 0}
        _mock_post(monkeypatch, calls)
        _mock_render(monkeypatch, None)

        r = rdt.generate_rainfall_describe_longimg_core()
        assert r["status"] == "ok"
        assert r["base64"] == "", "降级时不应携带 base64"
        assert r["text"] == _REAL_TEXT, "降级时必须保留原文供前端展示"
        assert r["render_warning"]

    def test_interval_reconciles_to_explicit_window(self, monkeypatch):
        calls = {"n": 0}
        _mock_post(monkeypatch, calls)
        _mock_render(monkeypatch, _FAKE_IMG)
        rdt.generate_rainfall_describe_longimg_core(
            beginTime="2026-08-03 00:00:00", endTime="2026-08-05 00:00:00",
        )
        assert calls["body"]["interval"] == 48

    def test_explicit_interval_preserved(self, monkeypatch):
        calls = {"n": 0}
        _mock_post(monkeypatch, calls)
        _mock_render(monkeypatch, _FAKE_IMG)
        rdt.generate_rainfall_describe_longimg_core(
            beginTime="2026-08-03 00:00:00", endTime="2026-08-05 00:00:00", interval=6,
        )
        assert calls["body"]["interval"] == 6

    def test_area_ids_cleansed(self, monkeypatch):
        calls = {"n": 0}
        _mock_post(monkeypatch, calls)
        _mock_render(monkeypatch, _FAKE_IMG)
        rdt.generate_rainfall_describe_longimg_core(areaIds=[6, "x", 7.0])
        assert calls["body"]["areaIds"] == [6, 7]

        _mock_post(monkeypatch, calls)
        rdt.generate_rainfall_describe_longimg_core(areaIds=[])
        assert calls["body"]["areaIds"] == [6, 7, 8, 9, 10, 11, 12, 13, 14]

    def test_no_data_when_text_empty(self, monkeypatch):
        """接口 data 为空/缺失 → no_data，不渲染。"""
        def fake_post(url, json=None, timeout=None):
            return _FakeResp({"msg": "暂无降水实况数据", "data": None})

        monkeypatch.setattr(rdt.requests, "post", fake_post)
        r = rdt.generate_rainfall_describe_longimg_core()
        assert r["status"] == "no_data"
        assert "暂无降水实况" in r["message"]
        assert r["base64"] == ""

    def test_no_data_msg_scrubbed(self, monkeypatch):
        """no_data 的 msg 脱敏，内网 IP 不得透出。"""
        def fake_post(url, json=None, timeout=None):
            return _FakeResp({"msg": "请联系 10.226.107.35:8001 检查配置", "data": None})

        monkeypatch.setattr(rdt.requests, "post", fake_post)
        r = rdt.generate_rainfall_describe_longimg_core()
        assert r["status"] == "no_data"
        assert "10.226.107.35" not in r["message"]

    def test_error_on_request_raise(self, monkeypatch):
        """接口不可达 → status error，异常文本脱敏。"""
        def failing_post(url, json=None, timeout=None):
            raise RuntimeError("Connection refused: http://10.226.107.35:8001/openapi/rainfall_describe/real")

        monkeypatch.setattr(rdt.requests, "post", failing_post)
        r = rdt.generate_rainfall_describe_longimg_core()
        assert r["status"] == "error"
        assert r["base64"] == ""
        for token in ("10.226.107.35", "8001", "openapi"):
            assert token not in r["message"]

    def test_render_exception_degrades_gracefully(self, monkeypatch):
        """渲染抛异常 → 降级纯文字，不把异常带出。"""
        calls = {"n": 0}
        _mock_post(monkeypatch, calls)

        def boom(text, title):
            raise RuntimeError("render boom")

        monkeypatch.setattr(rdt, "_render_text_longimg", boom)
        r = rdt.generate_rainfall_describe_longimg_core()
        assert r["status"] == "ok"
        assert r["base64"] == ""
        assert r["text"] == _REAL_TEXT

    def test_posts_to_describe_url(self, monkeypatch):
        calls = {"n": 0}
        _mock_post(monkeypatch, calls)
        _mock_render(monkeypatch, _FAKE_IMG)
        rdt.generate_rainfall_describe_longimg_core()
        assert calls["url"].endswith("/openapi/rainfall_describe/real")