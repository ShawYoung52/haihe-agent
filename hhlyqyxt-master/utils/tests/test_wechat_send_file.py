"""微信发送网关接入 send_file 单元测试（mock requests，不依赖真实网关）。"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import utils.wechat_send_file as wsf


class _Resp:
    def __init__(self, status_code=200, text="", json=None):
        self.status_code = status_code
        self.text = text
        self._json = json

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


@pytest.fixture
def fake_post(monkeypatch):
    calls = []

    def _post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _Resp()

    monkeypatch.setattr(wsf.requests, "post", _post)
    return calls


def test_send_file_calls_text_then_file(monkeypatch, tmp_path):
    """成功路径：先 send-text 发话术，再 send-file 上传报告。"""
    calls = []
    monkeypatch.setattr(wsf, "_gateway_url", lambda: "http://gw:8000")
    monkeypatch.setattr(wsf, "_gateway_token", lambda: "tok")
    monkeypatch.setattr(wsf.requests, "post", lambda *a, **k: calls.append((a[0], k)) or _Resp())

    f = tmp_path / "r.docx"
    f.write_bytes(b"data")

    assert wsf.send_file("天津市防汛群", str(f), "话术") is True

    assert len(calls) == 2
    text_url, text_kw = calls[0]
    file_url, file_kw = calls[1]

    assert text_url == "http://gw:8000/api/v1/send-text"
    assert text_kw["json"] == {"target": "天津市防汛群", "message": "话术", "send": True}
    assert text_kw["headers"] == {"Authorization": "Bearer tok"}

    assert file_url == "http://gw:8000/api/v1/send-file"
    assert file_kw["headers"] == {"Authorization": "Bearer tok"}
    assert file_kw["data"] == {"target_key": "天津市防汛群", "send": "true"}
    # multipart 文件名用 Path(file_path).name
    assert file_kw["files"]["file"][0] == "r.docx"


def test_send_file_uses_base_url_default(monkeypatch, tmp_path):
    """未配置 WECHAT_GATEWAY_URL 时用默认 base URL。"""
    calls = []
    monkeypatch.setenv("WECHAT_GATEWAY_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("WECHAT_GATEWAY_TOKEN", "tok")
    monkeypatch.setattr(wsf.requests, "post", lambda *a, **k: calls.append(a) or _Resp())

    f = tmp_path / "r.pdf"
    f.write_bytes(b"data")
    assert wsf.send_file("某群", str(f), "话术") is True
    assert calls[0][0] == "http://127.0.0.1:8000/api/v1/send-text"
    assert calls[1][0] == "http://127.0.0.1:8000/api/v1/send-file"


def test_send_file_returns_false_when_text_fails(monkeypatch, tmp_path):
    """send-text 失败（非 2xx）→ 返回 False，不再调 send-file。"""
    calls = []
    monkeypatch.setattr(wsf, "_gateway_url", lambda: "http://gw:8000")
    monkeypatch.setattr(wsf, "_gateway_token", lambda: "tok")
    monkeypatch.setattr(wsf.requests, "post",
                        lambda *a, **k: calls.append(a) or _Resp(status_code=500, text="boom"))

    f = tmp_path / "r.docx"
    f.write_bytes(b"data")
    assert wsf.send_file("某群", str(f), "话术") is False
    assert len(calls) == 1  # 只调了 send-text


def test_send_file_returns_false_when_file_fails(monkeypatch, tmp_path):
    """send-text 成功但 send-file 失败 → 返回 False。"""
    seq = [_Resp(status_code=200), _Resp(status_code=400, text="bad")]
    monkeypatch.setattr(wsf, "_gateway_url", lambda: "http://gw:8000")
    monkeypatch.setattr(wsf, "_gateway_token", lambda: "tok")
    monkeypatch.setattr(wsf.requests, "post", lambda *a, **k: seq.pop(0))

    f = tmp_path / "r.docx"
    f.write_bytes(b"data")
    assert wsf.send_file("某群", str(f), "话术") is False


def test_send_file_returns_false_on_request_exception(monkeypatch, tmp_path):
    """网关不可达（requests 抛异常）→ 返回 False 不抛。"""
    monkeypatch.setattr(wsf, "_gateway_url", lambda: "http://gw:8000")
    monkeypatch.setattr(wsf, "_gateway_token", lambda: "tok")

    def boom(*a, **k):
        raise wsf.requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(wsf.requests, "post", boom)

    f = tmp_path / "r.docx"
    f.write_bytes(b"data")
    assert wsf.send_file("某群", str(f), "话术") is False


def test_send_file_returns_false_when_gateway_ok_false_on_text(monkeypatch, tmp_path):
    """HTTP 200 但网关 ok=false（微信发送失败）→ send-text 报错 → 返回 False。"""
    monkeypatch.setattr(wsf, "_gateway_url", lambda: "http://gw:8000")
    monkeypatch.setattr(wsf, "_gateway_token", lambda: "tok")
    monkeypatch.setattr(
        wsf.requests, "post",
        lambda *a, **k: _Resp(status_code=200, json={"ok": False, "result": {"msg": "not logged in"}}),
    )

    f = tmp_path / "r.docx"
    f.write_bytes(b"data")
    assert wsf.send_file("某群", str(f), "话术") is False


def test_send_file_returns_false_when_gateway_ok_false_on_file(monkeypatch, tmp_path):
    """send-text 成功但 send-file 网关 ok=false → 返回 False。"""
    seq = [
        _Resp(status_code=200, json={"ok": True, "result": {}}),
        _Resp(status_code=200, json={"ok": False, "result": {"msg": "target not found"}}),
    ]
    monkeypatch.setattr(wsf, "_gateway_url", lambda: "http://gw:8000")
    monkeypatch.setattr(wsf, "_gateway_token", lambda: "tok")
    monkeypatch.setattr(wsf.requests, "post", lambda *a, **k: seq.pop(0))

    f = tmp_path / "r.docx"
    f.write_bytes(b"data")
    assert wsf.send_file("某群", str(f), "话术") is False


def test_send_file_ok_true_still_succeeds(monkeypatch, tmp_path):
    """网关 ok=true（HTTP 200）→ 仍成功返回 True。"""
    monkeypatch.setattr(wsf, "_gateway_url", lambda: "http://gw:8000")
    monkeypatch.setattr(wsf, "_gateway_token", lambda: "tok")
    monkeypatch.setattr(
        wsf.requests, "post",
        lambda *a, **k: _Resp(status_code=200, json={"ok": True, "result": {}}),
    )

    f = tmp_path / "r.docx"
    f.write_bytes(b"data")
    assert wsf.send_file("某群", str(f), "话术") is True