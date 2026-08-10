# 天河 Fixed QA 问答接口接入 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `query_tianhe_fixed_qa` 工具，让 planner 在天河 Fixed QA 命中时调用 `POST /api/qa` 拿 answer 直接透传。

**Architecture:** 在 `chainlitexam/external_skill_tools.py` 新增两个函数：`call_tianhe_qa_api(query)`（真实 HTTP 调用，脱敏、失败返回提示）+ `query_tianhe_fixed_qa(query)`（@tool 包装）。注册进 `build_external_skill_tools()`。不改 planner 主流程。

**Tech Stack:** httpx（项目已有依赖）、LangChain @tool（项目已有）、pytest（项目已有）。

## Global Constraints

1. `message_orchestrator.py` / `process_message` **零改动**。
2. `TIANHE_QA_API_URL` 默认 `http://10.226.188.156:8001/api/qa`，可环境变量覆盖。
3. 请求 body 必须含 `"stream": false`（天河默认 true，会变流式）。
4. 单轮 `history=[]`，不透传多轮。
5. 失败**不抛异常**，返回中文提示（不含内网 IP/路径）。
6. 连接超时 5s，响应超时 120s：`httpx.timeout=(5, 120)`。
7. 测试 mock httpx，不依赖内网。
8. 测试须从 `chainlitexam/` 目录跑（`D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe`）。
9. git add 只许精确路径。

---

### Task 1: 实现 `call_tianhe_qa_api` + `query_tianhe_fixed_qa` + 测试

**Files:**
- Create: `chainlitexam/tests/test_tianhe_qa.py`
- Modify: `chainlitexam/external_skill_tools.py`（文件末尾 `build_external_skill_tools()` 前加两个函数 + 常量）

**Interfaces:**
- Produces:
  - `async def call_tianhe_qa_api(query: str) -> str` — 返回 answer 或中文提示
  - `async def query_tianhe_fixed_qa(query: str) -> str` — @tool，返回 `await call_tianhe_qa_api(query)`
  - `TIANHE_QA_API_URL` 模块级常量
  - `call_tianhe_qa_api` 加入 `build_external_skill_tools()` 返回列表

- [ ] **Step 1: 先看现有 `external_skill_tools.py` 末尾和 `build_external_skill_tools()`**

运行：
```bash
cd D:/PythonProject/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master/chainlitexam
tail -30 external_skill_tools.py
```

- [ ] **Step 2: 写失败测试 `chainlitexam/tests/test_tianhe_qa.py`**

```python
"""天河 Fixed QA 问答接口接入测试。mock httpx，不依赖内网。"""

from __future__ import annotations

import httpx
import pytest

import external_skill_tools as est


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
    assert "暂不可用" in out


@pytest.mark.asyncio
async def test_call_http_500_returns_hint(monkeypatch):
    async def fake_post(url, json, timeout):
        return _Resp(500, {"detail": "boom"})
    monkeypatch.setattr(est.httpx, "post", fake_post)
    out = await est.call_tianhe_qa_api("今天雨下了多长时间")
    assert "暂不可用" in out


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
    desc = est.query_tianhe_fixed_qa.__doc__ or ""
    assert "今天雨下了多长时间" in desc
    assert "暴雨天气的防范建议" in desc


def _Resp(status_code, json_body):
    class R:
        status_code = status_code
        def json(self):
            return json_body
    return R()
```

- [ ] **Step 3: 运行测试确认失败**

运行：`PYTHONIOENCODING=utf-8 D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_tianhe_qa.py -q`
Expected: FAIL（`ImportError: cannot import name 'call_tianhe_qa_api'`）

- [ ] **Step 4: 实现 `external_skill_tools.py`**

在文件顶部确认已有 `import httpx` 和 `import os`（若无则加）。在 `build_external_skill_tools()` 定义前插入：

```python
TIANHE_QA_API_URL = os.getenv("TIANHE_QA_API_URL", "http://10.226.188.156:8001/api/qa")


async def call_tianhe_qa_api(query: str) -> str:
    """真实调用天河平台问答接口（POST /api/qa），返回 answer 字符串。

    天河 Fixed QA 是整句精确匹配；本函数只做 HTTP 调用与解析，不判断命中。
    失败不抛异常，返回中文提示（供 planner 兜底走本地工具）。
    """
    q = (query or "").strip()
    if not q:
        return "问题不能为空。"

    try:
        resp = await httpx.post(
            TIANHE_QA_API_URL,
            json={"question": q, "history": [], "stream": False},
            timeout=(5, 120),
        )
    except httpx.ConnectTimeout:
        return "天河问答服务连接超时，请稍后重试或换一种问法。"
    except httpx.RequestError:
        return "天河问答服务暂时不可用，请稍后重试。"

    if resp.status_code >= 400:
        return "天河问答服务暂时不可用，请稍后重试。"

    try:
        data = resp.json()
    except ValueError:
        return "天河问答服务返回格式异常，请稍后重试。"

    answer = data.get("answer") if isinstance(data, dict) else None
    if not isinstance(answer, str) or not answer.strip():
        return "天河问答服务返回格式异常，请稍后重试。"

    # 200 但降级正文（如"智能体服务暂时不可用"）原样透传，不自动重试
    return answer


@tool
async def query_tianhe_fixed_qa(query: str) -> str:
    """调用天河平台 Fixed QA 固定问答接口，获取模板化回答。

    适用于天河已配置固定问答目录的问题，命中后由天河返回标准回答。
    当前已知的 Fixed QA 示例（整句精确匹配，会做去空白/去句末标点规范化）：
    - 今天雨下了多长时间
    - 全市现在下了多少雨
    - 市区现在气温和风的实况
    - 暴雨天气的防范建议

    参数 query：用户问题原文（中文）。不要自行改写或提炼——Fixed QA 是整句匹配。
    返回：天河生成的完整回答正文（UTF-8 字符串，可能含 Markdown 表格）。
    接口失败时返回中文提示，planner 应改用其他本地工具回答。
    """
    return await call_tianhe_qa_api(query)
```

- [ ] **Step 5: 把工具加进 `build_external_skill_tools()` 返回列表**

在 `build_external_skill_tools()` 的 return 列表中加入 `query_tianhe_fixed_qa`。

- [ ] **Step 6: 运行测试确认通过**

运行：`PYTHONIOENCODING=utf-8 D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/test_tianhe_qa.py -q`
Expected: 9 passed

- [ ] **Step 7: 提交**

```bash
cd D:/PythonProject/haiheliuyubaoyuagent-master
git add haiheliuyubaoyuagent-master/chainlitexam/external_skill_tools.py haiheliuyubaoyuagent-master/chainlitexam/tests/test_tianhe_qa.py
git commit -m "feat(qa): add Tianhe Fixed QA tool (query_tianhe_fixed_qa) for planner"
```

---

### Task 2: 全量回归 + 审查 + 简化 + 文档 + 提交

- [ ] **Step 1: 全量回归**

运行：`cd D:/PythonProject/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master/chainlitexam && PYTHONIOENCODING=utf-8 D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/test_decision_weather_tool.py`
Expected: 无新增失败（既有 1 个 stub 顺序干扰失败除外）

- [ ] **Step 2: code-review**（DeepSeek v4 Pro）审查新增代码

- [ ] **Step 3: code-simplifier** 过一遍

- [ ] **Step 4: 更新 CLAUDE.md**（新增工具说明 + TIANHE_QA_API_URL 环境变量）

- [ ] **Step 5: 写记忆**（`memory/tianhe-qa-tool.md` + MEMORY.md 索引）

- [ ] **Step 6: 提交推送**

```bash
git add <修改文件>
git commit -m "docs(qa): Tianhe Fixed QA tool CLAUDE.md + memory"
git push origin main
```
