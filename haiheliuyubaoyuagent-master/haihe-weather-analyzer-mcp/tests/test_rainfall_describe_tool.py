"""14所降水实况文字长图工具测试（/openapi/rainfall_describe/real）。

口径：与同端口 get_station_rainfall_real_img 类比，接口返回 base64 图片；走 base64 →
cl.Image 展示路径，不改动 chainlitexam/qa_http_api.py 的 _IMAGE_URL_ALLOW_HOSTS 白名单。
base 默认 http://10.226.107.35:8001，env RAINFALL_DESCRIBE_API_BASE 可覆盖；
实时出图不做 TTL 缓存。
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

# 带 PNG 魔数前缀的假图片字节（工具会做魔数校验，非图片内容按 no_data 处理）。
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_FAKE_IMG = _PNG_MAGIC + b"fake-png-bytes"
_PNG_B64 = base64.b64encode(_FAKE_IMG).decode("ascii")


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeBytesResp:
    """模拟图片 URL 拉取返回二进制内容。"""

    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        pass


def _mock_post(monkeypatch, calls, payload=_PNG_B64):
    def fake_post(url, json=None, timeout=None):
        calls["n"] += 1
        calls["url"] = url
        calls["body"] = json
        return _FakeResp({"code": 1, "success": True, "data": payload})

    monkeypatch.setattr(rdt.requests, "post", fake_post)


class TestResolveImageBase64:
    def test_plain_base64_in_data(self):
        assert rdt._resolve_image_base64({"data": _PNG_B64}) == _PNG_B64

    def test_data_uri_prefix_stripped(self):
        raw = "data:image/png;base64," + _PNG_B64
        assert rdt._resolve_image_base64({"data": raw}) == _PNG_B64

    def test_nested_dict_data(self):
        assert rdt._resolve_image_base64({"data": {"base64": _PNG_B64}}) == _PNG_B64

    def test_result_and_image_keys_fallback(self):
        assert rdt._resolve_image_base64({"result": _PNG_B64}) == _PNG_B64
        assert rdt._resolve_image_base64({"image": _PNG_B64}) == _PNG_B64

    def test_whole_payload_is_base64_string(self):
        assert rdt._resolve_image_base64(_PNG_B64) == _PNG_B64

    def test_non_image_content_rejected(self):
        """base64 解码后不是图片（错误文案/垃圾串）→ 返回空串，不报成功。"""
        junk = base64.b64encode("出图失败，请稍后重试".encode("utf-8")).decode("ascii")
        assert rdt._resolve_image_base64({"data": junk}) == ""

    def test_url_returns_fetched_base64(self, monkeypatch):
        """响应是图片 URL 时，内部拉取字节并转 base64，前端无感知。"""
        monkeypatch.setattr(
            rdt.requests, "get",
            lambda url, timeout=None: _FakeBytesResp(_FAKE_IMG),
        )
        out = rdt._resolve_image_base64({"data": "http://10.226.107.35:8001/hhly/xxx.png"})
        assert base64.b64decode(out) == _FAKE_IMG

    def test_relative_path_joined_with_base(self, monkeypatch):
        """响应是相对路径（与 basin_drawing 同族）→ 拼 base 后拉取。"""
        captured: dict = {}

        def fake_get(url, timeout=None):
            captured["url"] = url
            return _FakeBytesResp(_FAKE_IMG)

        monkeypatch.setattr(rdt.requests, "get", fake_get)
        out = rdt._resolve_image_base64({"data": "/hhly/meteor_img_profile/xxx.png"})
        assert captured["url"].startswith(rdt.RAINFALL_DESCRIBE_API_BASE)
        assert captured["url"].endswith("/hhly/meteor_img_profile/xxx.png")
        assert base64.b64decode(out) == _FAKE_IMG

    def test_empty(self):
        assert rdt._resolve_image_base64({}) == ""
        assert rdt._resolve_image_base64(None) == ""


class TestScrubText:
    def test_scrubs_full_url_with_path(self):
        out = rdt._scrub_text("Connection refused: http://10.226.107.35:8001/openapi/rainfall_describe/real")
        assert "10.226.107.35" not in out
        assert "openapi" not in out, "接口路径也不得泄漏"
        assert "8001" not in out

    def test_scrubs_bare_ip_and_windows_path(self):
        assert "10.226.107.35" not in rdt._scrub_text("失败 host=10.226.107.35:8001")
        assert "[路径]" in rdt._scrub_text("无法打开 C:\\data\\config.ini")


class TestGenerate:
    def test_default_window_and_body(self, monkeypatch):
        calls = {"n": 0}
        _mock_post(monkeypatch, calls)

        r = rdt.generate_rainfall_describe_longimg_core()
        assert r["status"] == "ok"
        assert r["base64"] == _PNG_B64
        assert calls["n"] == 1
        body = calls["body"]
        assert body["areaIds"] == [6, 7, 8, 9, 10, 11, 12, 13, 14]
        assert body["interval"] == 24
        assert body["range"] == "9"
        assert body["type"] == "0"
        assert body["isClimateImg"] is False
        assert body["beginTime"] and body["endTime"], "默认时间窗应自动填充"

    def test_explicit_params_forwarded(self, monkeypatch):
        calls = {"n": 0}
        _mock_post(monkeypatch, calls)

        r = rdt.generate_rainfall_describe_longimg_core(
            beginTime="2026-08-03 08:00:00",
            endTime="2026-08-04 08:00:00",
            areaIds=[6, 7],
            interval=24,
            range="9",
            type="1",
            isClimateImg=True,
        )
        assert r["status"] == "ok"
        body = calls["body"]
        assert body["beginTime"] == "2026-08-03 08:00:00"
        assert body["endTime"] == "2026-08-04 08:00:00"
        assert body["areaIds"] == [6, 7]
        assert body["type"] == "1"
        assert body["isClimateImg"] is True

    def test_interval_string_and_too_small_clamped(self, monkeypatch):
        calls = {"n": 0}
        _mock_post(monkeypatch, calls)
        rdt.generate_rainfall_describe_longimg_core(interval="12")
        assert calls["body"]["interval"] == 12

        _mock_post(monkeypatch, calls)
        rdt.generate_rainfall_describe_longimg_core(interval=0)
        assert calls["body"]["interval"] == 1, "interval<1 应钳到 1"

    def test_interval_reconciles_to_explicit_window(self, monkeypatch):
        """begin/end 均给出且 interval 保持默认 24 → 自动对齐窗口时长（48h→48）。"""
        calls = {"n": 0}
        _mock_post(monkeypatch, calls)
        rdt.generate_rainfall_describe_longimg_core(
            beginTime="2026-08-03 00:00:00", endTime="2026-08-05 00:00:00",
        )
        assert calls["body"]["interval"] == 48, "48h 窗口未指定 interval 应对齐为 48"

    def test_explicit_interval_preserved(self, monkeypatch):
        """显式传 interval=6 时不被窗口时长覆盖。"""
        calls = {"n": 0}
        _mock_post(monkeypatch, calls)
        rdt.generate_rainfall_describe_longimg_core(
            beginTime="2026-08-03 00:00:00", endTime="2026-08-05 00:00:00", interval=6,
        )
        assert calls["body"]["interval"] == 6

    def test_area_ids_cleansed(self, monkeypatch):
        """非法项丢弃、空列表回落默认 9 分区。"""
        calls = {"n": 0}
        _mock_post(monkeypatch, calls)
        rdt.generate_rainfall_describe_longimg_core(areaIds=[6, "x", 7.0])
        assert calls["body"]["areaIds"] == [6, 7]

        _mock_post(monkeypatch, calls)
        rdt.generate_rainfall_describe_longimg_core(areaIds=[])
        assert calls["body"]["areaIds"] == [6, 7, 8, 9, 10, 11, 12, 13, 14], "空列表回落默认分区"

    def test_no_data_when_empty_base64(self, monkeypatch):
        """上游返回空 data → no_data，且不携带 base64。"""
        calls = {"n": 0}
        _mock_post(monkeypatch, calls, payload="")
        r = rdt.generate_rainfall_describe_longimg_core()
        assert r["status"] == "no_data"
        assert r["base64"] == ""

    def test_no_data_when_message_only(self, monkeypatch):
        """上游 HTTP 200 但 data 缺失（如 msg 提示无记录）→ no_data。"""
        def fake_post(url, json=None, timeout=None):
            return _FakeResp({"msg": "暂无降水实况数据", "data": None})

        monkeypatch.setattr(rdt.requests, "post", fake_post)
        r = rdt.generate_rainfall_describe_longimg_core()
        assert r["status"] == "no_data"
        assert "暂无降水实况" in r["message"]

    def test_no_data_msg_scrubbed(self, monkeypatch):
        """no_data 的 msg 同样脱敏，内网 IP 不得透出。"""
        def fake_post(url, json=None, timeout=None):
            return _FakeResp({"msg": "请联系 10.226.107.35:8001 检查配置", "data": None})

        monkeypatch.setattr(rdt.requests, "post", fake_post)
        r = rdt.generate_rainfall_describe_longimg_core()
        assert r["status"] == "no_data"
        assert "10.226.107.35" not in r["message"]

    def test_non_image_payload_treated_as_no_data(self, monkeypatch):
        """上游返回非图片内容（如错误文案 base64）→ no_data 而非 ok。"""
        calls = {"n": 0}
        junk = base64.b64encode("服务异常".encode("utf-8")).decode("ascii")
        _mock_post(monkeypatch, calls, payload=junk)
        r = rdt.generate_rainfall_describe_longimg_core()
        assert r["status"] == "no_data"
        assert r["base64"] == ""

    def test_error_on_request_raise(self, monkeypatch):
        """接口不可达/异常 → status error，禁止抛到上层。"""
        def failing_post(url, json=None, timeout=None):
            raise RuntimeError("出图接口不可达")

        monkeypatch.setattr(rdt.requests, "post", failing_post)
        r = rdt.generate_rainfall_describe_longimg_core()
        assert r["status"] == "error"
        assert r["base64"] == ""

    def test_error_message_scrubs_internal_ip(self, monkeypatch):
        """异常文本中的内网 URL/IP/路径必须脱敏（CLAUDE.md 约定）。"""
        def failing_post(url, json=None, timeout=None):
            raise RuntimeError("Connection refused: http://10.226.107.35:8001/openapi/rainfall_describe/real")

        monkeypatch.setattr(rdt.requests, "post", failing_post)
        r = rdt.generate_rainfall_describe_longimg_core()
        assert r["status"] == "error"
        for token in ("10.226.107.35", "8001", "openapi"):
            assert token not in r["message"], f"{token} 不得出现在错误文本里"

    def test_error_on_url_fetch_failure(self, monkeypatch):
        """响应是 URL 但拉取失败 → status error。"""
        def fake_post(url, json=None, timeout=None):
            return _FakeResp({"data": "http://10.226.107.35:8001/hhly/xxx.png"})

        def failing_get(url, timeout=None):
            raise RuntimeError("图片下载失败")

        monkeypatch.setattr(rdt.requests, "post", fake_post)
        monkeypatch.setattr(rdt.requests, "get", failing_get)
        r = rdt.generate_rainfall_describe_longimg_core()
        assert r["status"] == "error"
        assert r["base64"] == ""

    def test_error_on_oversized_url_fetch(self, monkeypatch):
        """URL 拉取字节超过上限 → status error（防超大图撑爆内存）。"""
        def fake_post(url, json=None, timeout=None):
            return _FakeResp({"data": "http://10.226.107.35:8001/hhly/big.png"})

        monkeypatch.setattr(rdt.requests, "post", fake_post)
        monkeypatch.setattr(rdt.requests, "get",
                            lambda url, timeout=None: _FakeBytesResp(b"\x89PNG" + b"x" * 100))
        monkeypatch.setattr(rdt, "_MAX_IMAGE_BYTES", 16)
        r = rdt.generate_rainfall_describe_longimg_core()
        assert r["status"] == "error"

    def test_posts_to_describe_url(self, monkeypatch):
        """默认打在 base + /openapi/rainfall_describe/real 上。"""
        calls = {"n": 0}
        _mock_post(monkeypatch, calls)
        rdt.generate_rainfall_describe_longimg_core()
        assert calls["url"].endswith("/openapi/rainfall_describe/real")