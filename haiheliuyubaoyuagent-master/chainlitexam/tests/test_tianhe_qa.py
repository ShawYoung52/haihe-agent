"""天河 Fixed QA 问答接口接入测试。mock httpx，不依赖内网。"""

from __future__ import annotations

import httpx
import pytest

import external_skill_tools as est


def _Resp(status_code, json_body):
    class R:
        def __init__(self):
            self.status_code = status_code
            self._json = json_body

        def json(self):
            return self._json

    return R()


@pytest.mark.asyncio
async def test_call_returns_answer(monkeypatch):
    """正常返回 answer。"""
    async def fake_post(url, json, timeout):
        assert json["stream"] is False, "必须显式传 stream=false"
        assert json["history"] == [], "单轮 history=[]"
        return _Resp(200, {"answer": "今天下雨持续了 3 小时。"})

    monkeypatch.setattr(est.httpx, "post", fake_post)
    out = await est.call_tianhe_qa_api("今天雨下了多长时间")
    assert out == "今天下雨持续了 3 小时。"


@pytest.mark.asyncio
async def test_call_empty_query_returns_hint_without_http(monkeypatch):
    called = {"n": 0}

    async def fake_post(url, json, timeout):
        called["n"] += 1
        return _Resp(200, {"answer": "x"})

    monkeypatch.setattr(est.httpx, "post", fake_post)
    out = await est.call_tianhe_qa_api("   ")
    assert "不能为空" in out
    assert called["n"] == 0, "空 query 不应发起 HTTP 请求"


@pytest.mark.asyncio
async def test_call_timeout_returns_hint(monkeypatch):
    async def fake_post(url, json, timeout):
        raise httpx.ConnectTimeout("timeout")

    monkeypatch.setattr(est.httpx, "post", fake_post)
    out = await est.call_tianhe_qa_api("今天雨下了多长时间")
    assert "暂不可用" in out or "超时" in out


@pytest.mark.asyncio
async def test_call_connect_error_returns_hint(monkeypatch):
    async def fake_post(url, json, timeout):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(est.httpx, "post", fake_post)
    out = await est.call_tianhe_qa_api("今天雨下了多长时间")
    assert "暂时不可用" in out


@pytest.mark.asyncio
async def test_call_http_500_returns_hint(monkeypatch):
    async def fake_post(url, json, timeout):
        return _Resp(500, {"detail": "boom"})

    monkeypatch.setattr(est.httpx, "post", fake_post)
    out = await est.call_tianhe_qa_api("今天雨下了多长时间")
    assert "暂时不可用" in out


@pytest.mark.asyncio
async def test_call_missing_answer_returns_hint(monkeypatch):
    async def fake_post(url, json, timeout):
        return _Resp(200, {"foo": "bar"})

    monkeypatch.setattr(est.httpx, "post", fake_post)
    out = await est.call_tianhe_qa_api("今天雨下了多长时间")
    assert "格式异常" in out


@pytest.mark.asyncio
async def test_call_degraded_body_passthrough(monkeypatch):
    """200 但降级正文原样透传，不判定为失败。"""
    async def fake_post(url, json, timeout):
        return _Resp(200, {"answer": "智能体服务暂时不可用，请稍后重试。"})

    monkeypatch.setattr(est.httpx, "post", fake_post)
    out = await est.call_tianhe_qa_api("今天雨下了多长时间")
    assert out == "智能体服务暂时不可用，请稍后重试。"


@pytest.mark.asyncio
async def test_call_uses_env_url(monkeypatch):
    """环境变量 TIANHE_QA_API_URL 覆盖默认地址。"""
    seen = {}

    async def fake_post(url, json, timeout):
        seen["url"] = url
        return _Resp(200, {"answer": "ok"})

    monkeypatch.setattr(est, "TIANHE_QA_API_URL", "http://fake:9999/api/qa")
    monkeypatch.setattr(est.httpx, "post", fake_post)
    await est.call_tianhe_qa_api("今天雨下了多长时间")
    assert seen["url"] == "http://fake:9999/api/qa"


def test_tool_description_mentions_fixed_qa_examples():
    """工具描述包含已知 Fixed QA 示例。"""
    desc = est.query_tianhe_fixed_qa.description or ""
    assert "今天雨下了多长时间" in desc
    assert "暴雨天气的防范建议" in desc
