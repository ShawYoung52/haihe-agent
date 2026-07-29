# 牵引应急响应 · 接入天河报告接口 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 应急响应 I-IV 级触发时调用天河报告接口 POST `/api/report/generate`，只传 `template`，失败容错不阻塞主流程。

**Architecture:** 新增 `ScheduledTask/report_generator.py` 提供 `trigger_weather_bulletin_report(response_level)` 函数，在 `stationProcessMin.py` 的 `calcmaxdataseg5min()` 中 `run_emergency_response_monitor` 之后调用。所有 HTTP 错误容错记 WARNING。

**Tech Stack:** Python 3.9+ / `requests`（项目已有依赖）

## Global Constraints

- 只改/新增：`ScheduledTask/report_generator.py`（新）、`ScheduledTask/tests/test_report_generator.py`（新）、`ScheduledTask/stationProcessMin.py`（修改 3 行）
- report API URL: `http://10.226.188.156:8000/api/report/generate`
- template: `haihe_weather_bulletin`
- 报告失败**不抛异常**、不阻塞主流程
- 应急响应级别 1=I级, 2=II级, 3=III级, 4=IV级, 0=无预警
- 只用 `git add` 精确路径

---

### Task 1: Phase 0 — 分支 + baseline

- [ ] **Step 1: 创建分支**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master
git checkout -b feat/traction-report-api
```

---

### Task 2: Phase 1 — 写 `report_generator.py` + 6 条测试

**Files:**
- Create: `hhlyqyxt-master/ScheduledTask/report_generator.py`
- Create: `hhlyqyxt-master/ScheduledTask/tests/test_report_generator.py`

**Interfaces:**
- Produces: `trigger_weather_bulletin_report(response_level: int, *, timeout: int = 30) -> bool`

- [ ] **Step 1: 写测试文件**

```python
"""天河报告接口调用测试。"""
import pytest
from unittest import mock
from ScheduledTask.report_generator import trigger_weather_bulletin_report, REPORT_API_URL, REPORT_TEMPLATE


def test_trigger_skips_when_level_zero():
    """response_level=0 时不发送 HTTP 请求。"""
    with mock.patch("ScheduledTask.report_generator.requests.post") as mock_post:
        result = trigger_weather_bulletin_report(0)
        assert result is False
        mock_post.assert_not_called()


def test_trigger_sends_when_level_one():
    """response_level=1 时发送请求并返回 True。"""
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    with mock.patch("ScheduledTask.report_generator.requests.post", return_value=mock_resp) as mock_post:
        result = trigger_weather_bulletin_report(1)
        assert result is True
        mock_post.assert_called_once_with(
            REPORT_API_URL,
            json={"template": REPORT_TEMPLATE},
            timeout=30,
        )


def test_trigger_sends_for_all_levels():
    """I-IV 级全部触发。"""
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    with mock.patch("ScheduledTask.report_generator.requests.post", return_value=mock_resp) as mock_post:
        for level in (1, 2, 3, 4):
            assert trigger_weather_bulletin_report(level) is True
        assert mock_post.call_count == 4


def test_trigger_tolerates_timeout():
    """超时不崩溃，返回 False。"""
    import requests as real_requests
    with mock.patch("ScheduledTask.report_generator.requests.post", side_effect=real_requests.exceptions.Timeout()):
        result = trigger_weather_bulletin_report(2)
        assert result is False


def test_trigger_tolerates_connection_error():
    """连接失败不崩溃，返回 False。"""
    import requests as real_requests
    with mock.patch("ScheduledTask.report_generator.requests.post", side_effect=real_requests.exceptions.ConnectionError()):
        result = trigger_weather_bulletin_report(3)
        assert result is False


def test_trigger_tolerates_http_error():
    """HTTP 5xx 不崩溃，返回 False。"""
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"
    with mock.patch("ScheduledTask.report_generator.requests.post", return_value=mock_resp):
        result = trigger_weather_bulletin_report(4)
        assert result is False
```

- [ ] **Step 2: 运行确认 FAIL（模块不存在）**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/hhlyqyxt-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest ScheduledTask/tests/test_report_generator.py -v 2>&1 | tail -5
```

- [ ] **Step 3: 实现 `report_generator.py`**

```python
"""天河报告接口调用。
应急响应 I-IV 级触发时，调用天河报告接口生成海河流域气象公报。
"""
import logging

import requests

logger = logging.getLogger(__name__)

REPORT_API_URL = "http://10.226.188.156:8000/api/report/generate"
REPORT_TEMPLATE = "haihe_weather_bulletin"
DEFAULT_TIMEOUT = 30


def trigger_weather_bulletin_report(
    response_level: int,
    *,
    timeout: int = DEFAULT_TIMEOUT,
) -> bool:
    """I-IV 级应急响应时，调用天河报告接口生成海河流域气象公报。

    Args:
        response_level: 1=I级, 2=II级, 3=III级, 4=IV级, 0=无预警。
        timeout: HTTP 请求超时秒数。

    Returns:
        True 表示调用成功（HTTP < 400），False 表示跳过或失败。
        任何异常均被捕获，不抛给调用方。
    """
    if response_level < 1:
        return False

    try:
        resp = requests.post(
            REPORT_API_URL,
            json={"template": REPORT_TEMPLATE},
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        logger.warning("天河报告接口超时（%ds），应急%d级", timeout, response_level)
        return False
    except requests.exceptions.RequestException as e:
        logger.warning("天河报告接口调用失败：%s，应急%d级", e, response_level)
        return False

    if resp.status_code < 400:
        logger.info("天河报告触发成功（应急%d级），status=%s", response_level, resp.status_code)
        return True
    else:
        logger.warning("天河报告接口返回非成功状态码：%s，body=%s", resp.status_code, resp.text[:200])
        return False
```

- [ ] **Step 4: 运行确认 PASS**

```bash
cd /d/PythonProject/haiheliuyubaoyuagent-master/hhlyqyxt-master
/d/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest ScheduledTask/tests/test_report_generator.py -v 2>&1 | tail -10
```

- [ ] **Step 5: Commit**

```bash
git add hhlyqyxt-master/ScheduledTask/report_generator.py hhlyqyxt-master/ScheduledTask/tests/test_report_generator.py
git commit -m "feat(traction): report API integration - trigger_weather_bulletin_report"
```

---

### Task 3: Phase 2 — `stationProcessMin.py` 调用

**Files:**
- Modify: `hhlyqyxt-master/ScheduledTask/stationProcessMin.py`（line 450-454 后）

- [ ] **Step 1: 修改 `calcmaxdataseg5min()`**

在 `run_emergency_response_monitor(...)` 调用后（line 454）追加：

```python
    )
    # I-IV 级应急响应触发天河报告生成
    if record is not None:
        from ScheduledTask.report_generator import trigger_weather_bulletin_report
        trigger_weather_bulletin_report(record.response_level)
```

注意 `run_emergency_response_monitor()` 返回可能为 `None`（数据为空时），加 `if record is not None` 保护。

- [ ] **Step 2: Commit**

```bash
git add hhlyqyxt-master/ScheduledTask/stationProcessMin.py
git commit -m "feat(traction): call report API after emergency response monitor"
```

---

### Task 4: Phase 3 — finishing

- [ ] **Step 1: 确认全部测试通过**
- [ ] **Step 2: merge to main + push + 删分支**
- [ ] **Step 3: 落 claude-mem `[[traction-report-api]]`**
