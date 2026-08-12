"""天河 Fixed QA 问答接口接入测试。mock 共享 AsyncClient，不依赖内网。"""

from __future__ import annotations

import httpx
import pytest

import external_skill_tools as est


class _Resp:
    def __init__(self, status_code, json_body):
        self.status_code = status_code
        self._json = json_body

    def json(self):
        return self._json


class _FakeClient:
    """模拟 httpx.AsyncClient：可配置返回体或抛指定异常。"""

    def __init__(self, *, json_body=None, exc=None, status_code=200, seen=None):
        self._json_body = json_body
        self._exc = exc
        self._status_code = status_code
        self.seen = seen if seen is not None else {}

    async def post(self, url, json):
        self.seen["url"] = url
        self.seen["json"] = json
        if self._exc is not None:
            raise self._exc
        return _Resp(self._status_code, self._json_body)


def _install_fake_client(monkeypatch, **kwargs):
    """安装假 client 并清空 TTL 缓存，返回 seen 记录。"""
    seen = {}
    monkeypatch.setattr(est, "_get_tianhe_client", lambda: _FakeClient(seen=seen, **kwargs))
    est._tianhe_cache.clear()
    return seen


@pytest.mark.asyncio
async def test_call_returns_answer(monkeypatch):
    """正常返回 answer，body 契约正确。"""
    seen = _install_fake_client(monkeypatch, json_body={"answer": "今天下雨持续了 3 小时。"})
    out = await est.call_tianhe_qa_api("今天雨下了多长时间")
    assert out == "今天下雨持续了 3 小时。"
    assert seen["json"]["stream"] is False, "必须显式传 stream=false"
    assert seen["json"]["history"] == [], "单轮 history=[]"


@pytest.mark.asyncio
async def test_call_empty_query_returns_hint_without_http(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("空 query 不应发起 HTTP 请求")

    monkeypatch.setattr(est, "_get_tianhe_client", fail)
    est._tianhe_cache.clear()
    out = await est.call_tianhe_qa_api("   ")
    assert "不能为空" in out


@pytest.mark.asyncio
async def test_call_timeout_returns_hint(monkeypatch):
    _install_fake_client(monkeypatch, exc=httpx.ConnectTimeout("timeout"))
    out = await est.call_tianhe_qa_api("今天雨下了多长时间")
    assert "超时" in out


@pytest.mark.asyncio
async def test_call_connect_error_returns_hint(monkeypatch):
    _install_fake_client(monkeypatch, exc=httpx.ConnectError("refused"))
    out = await est.call_tianhe_qa_api("今天雨下了多长时间")
    assert "暂时不可用" in out


@pytest.mark.asyncio
async def test_call_http_500_returns_hint(monkeypatch):
    _install_fake_client(monkeypatch, status_code=500, json_body={"detail": "boom"})
    out = await est.call_tianhe_qa_api("今天雨下了多长时间")
    assert "暂时不可用" in out


@pytest.mark.asyncio
async def test_call_missing_answer_returns_hint(monkeypatch):
    _install_fake_client(monkeypatch, json_body={"foo": "bar"})
    out = await est.call_tianhe_qa_api("今天雨下了多长时间")
    assert "格式异常" in out


@pytest.mark.asyncio
async def test_call_degraded_body_passthrough(monkeypatch):
    """200 但降级正文原样透传，不判定为失败。"""
    degraded = "智能体服务暂时不可用，请稍后重试。"
    _install_fake_client(monkeypatch, json_body={"answer": degraded})
    out = await est.call_tianhe_qa_api("今天雨下了多长时间")
    assert out == degraded


@pytest.mark.asyncio
async def test_call_uses_env_url(monkeypatch):
    """环境变量 TIANHE_QA_API_URL 覆盖默认地址。"""
    monkeypatch.setattr(est, "TIANHE_QA_API_URL", "http://fake:9999/api/qa")
    seen = _install_fake_client(monkeypatch, json_body={"answer": "ok"})
    await est.call_tianhe_qa_api("今天雨下了多长时间")
    assert seen["url"] == "http://fake:9999/api/qa"


@pytest.mark.asyncio
async def test_call_caches_same_query(monkeypatch):
    """相同问题 TTL 内命中缓存，不重复请求。"""
    calls = {"n": 0}

    def counting_client():
        async def _post(self, url, json):
            calls["n"] += 1
            return _Resp(200, {"answer": "ok"})
        return type("C", (), {"post": _post})()

    monkeypatch.setattr(est, "_get_tianhe_client", counting_client)
    est._tianhe_cache.clear()
    monkeypatch.setattr(est, "TIANHE_QA_CACHE_TTL", 300)

    await est.call_tianhe_qa_api("今天雨下了多长时间")
    await est.call_tianhe_qa_api("今天雨下了多长时间")
    assert calls["n"] == 1, f"第二次应命中缓存，实际请求 {calls['n']} 次"


def test_tool_description_mentions_fixed_qa_examples():
    """工具描述包含已知 Fixed QA 示例。"""
    desc = est.query_tianhe_fixed_qa.description or ""
    assert "今天雨下了多长时间" in desc
    assert "暴雨天气的防范建议" in desc


def test_tianhe_error_texts_exported():
    """工具级失败文案应以单一事实源集合导出，供 orchestrator 区分"失败回退"与"命中/降级透传"。"""
    assert isinstance(est.TIANHE_ERROR_TEXTS, frozenset)
    assert est.TIANHE_ERROR_TEXTS == {
        est._TIANHE_ERR_EMPTY,
        est._TIANHE_ERR_CONNECT,
        est._TIANHE_ERR_UNAVAILABLE,
        est._TIANHE_ERR_FORMAT,
    }
    # 200 降级文案（文档 9.4）不在失败集合内——它由 API 在 200 时返回，应原样透传而非回退
    assert "智能体服务暂时不可用，请稍后重试。" not in est.TIANHE_ERROR_TEXTS
