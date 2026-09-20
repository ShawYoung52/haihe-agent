# 请求级锚定时间（reference_time）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把"锚定时间"从进程级全局标量改成请求级值——只有携带锚点的那个请求按锚定时间回答，
服务端不保留任何跨用户状态；同时修掉"恢复后仍回答旧日期"的残留缺陷。

**Architecture:** 在 `time_source` 里加一个请求级 ContextVar 作为最高优先级时间源（优先级：
请求 ContextVar → MCP header → 全局文件 → 真实时间），既有 20+ 个模块的 `time_source.now()`
调用点一行不改。Chainlit 侧两个入口（HTTP `/api/v1/qa/ask` 与 WS `@on_message`）解析锚点并
set/reset ContextVar；跨进程靠 `tool_interceptors` 注入 MCP header，MCP 侧用
`fastmcp.get_http_headers()` 读回。`【当前日期】` 前缀改为按日期分槽缓存 runtime，
MCP 工具表跨日期共享一次加载。

**Tech Stack:** Python 3.10+ / asyncio、FastAPI、Chainlit 2.9.6、langchain-mcp-adapters 0.3.2、
fastmcp 3.4.7、pytest 9.x

**Spec:** `docs/superpowers/specs/2026-09-20-per-request-reference-time-design.md`

## Global Constraints

- **不改任何 MCP 工具 schema**，不让 LLM 传时间参数，**不改任何 prompt 文本**。
- 依赖方向单向：`chain_gzt` → `qa_http_api`；`qa_http_api` 禁止 import `chain_gzt`。
- `chainlitexam/utils/time_source.py` 与 `haihe-weather-analyzer-mcp/time_source.py` 是**两份内容一致的拷贝**，改动必须同步。
- 所有测试命令在 `chainlitexam/` 下执行；测试解释器用
  `D:/PythonProject/develop/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/.venv-test/Scripts/python.exe`
  （Git Bash 里的 `python` 是 Windows Store 占位程序，静默退出无输出）。
- 内网地址（IP、SSE URL、数据库连接串）**绝不**写进用户可见输出或提交的文档；日志只打异常类型。
- `tests/stubs.py` 的 `langchain_core` stub 会阻断单个测试文件的选择性运行；**以全量 `python -m pytest tests/` 结果为准**。
- 已知非回归失败：`tests/test_message_orchestrator.py::test_process_message_skips_fast_paths_when_disabled`
  （缺 Chainlit context，既有问题）；`test_decision_weather_tool.py` 有既有 import 失败，全量跑时跳过。

---

### Task 1: `time_source` 请求级锚点（两包同步）

**Files:**
- Modify: `chainlitexam/utils/time_source.py`
- Modify: `haihe-weather-analyzer-mcp/time_source.py`（与上者逐字一致）
- Test: `chainlitexam/tests/test_time_source.py`（追加）

**Interfaces:**
- Consumes: 既有 `_parse_iso(text) -> datetime | None`、`_read_file_dt() -> datetime | None`
- Produces:
  - `REFERENCE_TIME_HEADER: str = "x-haihe-reference-time"`
  - `set_request_override(value: datetime | str) -> Token`（`str` 可为 ISO 或字面量 `"real"`）
  - `reset_request_override(token) -> None`
  - `request_override_header_value() -> str | None`（ISO / `"real"` / `None`）
  - `now(tz=None)` 优先级改为 请求 ContextVar → header → 文件 → 真实时间

- [ ] **Step 1: 写失败测试**

在 `chainlitexam/tests/test_time_source.py` 末尾追加：

```python
def test_request_override_beats_global_file(sim_file):
    """请求级锚点优先级高于全局覆盖文件。"""
    set_override_from_text("2026-07-10 15:00:00")
    token = time_source.set_request_override("2026-03-05T08:30:00+08:00")
    try:
        assert now() == datetime(2026, 3, 5, 8, 30, 0)
        assert override_date_str() == "2026-03-05"
    finally:
        time_source.reset_request_override(token)
    assert now() == datetime(2026, 7, 10, 15, 0, 0)


def test_request_override_real_sentinel_shadows_file(sim_file):
    """显式"真实时间"必须压过遗留的全局覆盖文件（修复"改回去了还是旧日期"）。"""
    set_override_from_text("2026-07-10 15:00:00")
    token = time_source.set_request_override("real")
    try:
        assert datetime.now() - now() < timedelta(seconds=5)
        assert override_date_str() == datetime.now().strftime("%Y-%m-%d")
    finally:
        time_source.reset_request_override(token)
    assert now() == datetime(2026, 7, 10, 15, 0, 0)


def test_request_override_reset_after_double_reset_is_safe(sim_file):
    """重复 reset 不能抛错——它跑在 finally 清理路径上。"""
    token = time_source.set_request_override("2026-03-05T08:30:00+08:00")
    time_source.reset_request_override(token)
    time_source.reset_request_override(token)  # 不应抛


def test_header_value_only_reflects_request_scope(sim_file):
    """interceptor 取值只看请求级：没设请求锚点时返回 None（不能把全局文件当 header 发出去）。"""
    set_override_from_text("2026-07-10 15:00:00")
    assert time_source.request_override_header_value() is None
    token = time_source.set_request_override("2026-03-05T08:30:00+08:00")
    try:
        assert time_source.request_override_header_value() == "2026-03-05T08:30:00+08:00"
    finally:
        time_source.reset_request_override(token)
    token = time_source.set_request_override("real")
    try:
        assert time_source.request_override_header_value() == "real"
    finally:
        time_source.reset_request_override(token)


def test_request_override_is_task_local(sim_file):
    """并发 Task 之间不串味（ContextVar 天然隔离）。"""
    import asyncio

    async def worker(text):
        token = time_source.set_request_override(text)
        try:
            await asyncio.sleep(0)
            return now()
        finally:
            time_source.reset_request_override(token)

    async def main():
        return await asyncio.gather(
            worker("2026-03-05T08:30:00+08:00"),
            worker("2026-09-09T20:00:00+08:00"),
        )

    a, b = asyncio.run(main())
    assert a == datetime(2026, 3, 5, 8, 30, 0)
    assert b == datetime(2026, 9, 9, 20, 0, 0)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd chainlitexam && "D:/PythonProject/develop/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/.venv-test/Scripts/python.exe" -m pytest tests/test_time_source.py -v
```
Expected: 4 个新用例 FAIL（`AttributeError: module 'utils.time_source' has no attribute 'set_request_override'`）

- [ ] **Step 3: 实现**

`chainlitexam/utils/time_source.py`：`from contextvars import ContextVar` 加到 import 区；
把模块 docstring 第 8-13 行那段"单一事实源 = JSON 文件"补一句请求级优先级；
在 `_DATE_ONLY_RE` 定义之后插入：

```python
# ---------------------------------------------------------------------------
# 请求级锚点（2026-09-20）：把"现在"从进程级标量改成请求级值。
#
# 优先级：请求 ContextVar → MCP header → 全局文件 → 真实时间。
# ContextVar 在 asyncio Task 间天然隔离，并发请求不会串味；请求结束即消失，
# 服务端不留任何跨用户状态。
# ---------------------------------------------------------------------------

REFERENCE_TIME_HEADER = "x-haihe-reference-time"

# 字面量哨兵："real" 表示显式要求真实时间，用于压过可能遗留的全局覆盖文件。
_REAL_TEXT = "real"
_REAL = object()

# 请求级锚点：None=未设置（回落 header/文件） / _REAL=强制真实 / datetime=锚定
_req_override: ContextVar = ContextVar("haihe_ref_override", default=None)


def _coerce_request_value(value):
    """入口传入值 → aware datetime / _REAL；None 原样返回。非法抛 ValueError。"""
    if value is None or value is _REAL:
        return value
    if isinstance(value, str) and value.strip().lower() == _REAL_TEXT:
        return _REAL
    dt = value if isinstance(value, datetime) else _parse_iso(value)
    if dt is None:
        raise ValueError(f"无法解析的 reference_time：{value!r}")
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=_CN_TZ)


def set_request_override(value) -> object:
    """在请求入口设置锚点，返回 Token（调用方在 finally 里 reset）。

    value 支持：aware/naive datetime、ISO 字符串、字面量 "real"。
    """
    return _req_override.set(_coerce_request_value(value))


def reset_request_override(token) -> None:
    """复位请求级锚点。重复 reset / 跨 context 的 token 一律忽略，绝不抛错。"""
    try:
        _req_override.reset(token)
    except (ValueError, LookupError):
        pass


def request_override_header_value() -> str | None:
    """供 MCP interceptor：只取请求级锚点，返回 ISO 或 "real"；未设置返回 None。

    刻意**不**回落全局文件：全局覆盖由 MCP 进程自己读文件，不需要也不应该
    被当成 header 再发一遍。
    """
    ov = _req_override.get()
    if ov is None:
        return None
    if ov is _REAL:
        return _REAL_TEXT
    return ov.isoformat()


def _header_override():
    """读 MCP 进程入站 header（fastmcp）。任何异常一律当作"没有锚点"。

    刻意不做缓存：MCP 进程里同一个 asyncio Task 是否跨请求复用无法保证，
    缓存会把上一个请求的 header 泄漏到下一个请求。get_http_headers() 本身
    只是 contextvar 读取 + 小 dict 构造，热路径上不值得为此冒险。
    """
    try:
        from fastmcp.server.dependencies import get_http_headers

        raw = (get_http_headers() or {}).get(REFERENCE_TIME_HEADER)
    except Exception:
        return None
    if not raw:
        return None
    if raw.strip().lower() == _REAL_TEXT:
        return _REAL
    return _parse_iso(raw)


def effective_override():
    """本上下文生效的锚点：请求级 → MCP header → 全局文件；None 表示真实时间。"""
    ov = _req_override.get()
    if ov is None:
        ov = _header_override()
    if ov is None:
        ov = _read_file_dt()
    return ov
```

把 `now()` 改为：

```python
def now(tz=None) -> datetime:
    """返回"现在"。请求级锚点 → MCP header → 覆盖文件 → 真实时间。"""
    override = effective_override()
    if override is None or override is _REAL:
        return datetime.now(tz) if tz is not None else datetime.now()
    if tz is None:
        return override.replace(tzinfo=None)
    return override.astimezone(tz)
```

把 `override_date_str()` 的 docstring 补一句"含请求级锚点"，实现不变（它调 `now`）。

`__all__` 追加 `"REFERENCE_TIME_HEADER"`、`"set_request_override"`、`"reset_request_override"`、
`"request_override_header_value"`、`"effective_override"`。

**把同一份文件原样复制到 `haihe-weather-analyzer-mcp/time_source.py`**（两包必须逐字一致）。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd chainlitexam && "D:/PythonProject/develop/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/.venv-test/Scripts/python.exe" -m pytest tests/test_time_source.py -v
```
Expected: 全部 PASS（含既有 6 个用例）

- [ ] **Step 5: 校验两份拷贝一致**

```bash
cd "D:/PythonProject/develop/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master" && diff chainlitexam/utils/time_source.py haihe-weather-analyzer-mcp/time_source.py && echo "IDENTICAL"
```
Expected: 输出 `IDENTICAL`（无 diff）

- [ ] **Step 6: 提交**

```bash
git add chainlitexam/utils/time_source.py haihe-weather-analyzer-mcp/time_source.py chainlitexam/tests/test_time_source.py
git commit -m "feat(time-source): 请求级锚点 + MCP header 读取，优先级高于全局覆盖文件"
```

---

### Task 2: 请求锚点解析纯函数

**Files:**
- Create: `chainlitexam/utils/reference_time.py`
- Test: `chainlitexam/tests/test_reference_time.py`

**Interfaces:**
- Consumes: Task 1 的 `utils.time_source.{set_request_override, reset_request_override, get_override}`
- Produces:
  - `InvalidReferenceTime(ValueError)`
  - `resolve_reference_time(metadata=None, *, top_level=None) -> tuple[str, datetime | None]`，kind ∈ `{"fixed","real","inherit"}`
  - `resolve_day(kind, value, *, fallback_day: str) -> str`（`"%Y-%m-%d"`）
  - `anchor_cache_key(kind, value) -> str | None`
  - `request_anchor(kind, value)` — contextmanager，`inherit` 时是空操作

- [ ] **Step 1: 写失败测试**

创建 `chainlitexam/tests/test_reference_time.py`：

```python
# -*- coding: utf-8 -*-
"""请求级锚点解析（前端 metadata 契约）测试。纯函数，不依赖内网。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from utils import reference_time, time_source

_CN = timezone(timedelta(hours=8))


@pytest.fixture()
def sim_file(tmp_path, monkeypatch):
    f = tmp_path / "sim.json"
    monkeypatch.setenv("SIM_TIME_FILE", str(f))
    time_source._invalidate()
    yield f
    time_source._invalidate()


def test_fixed_from_metadata():
    kind, value = reference_time.resolve_reference_time(
        {"time_mode": "fixed", "reference_time": "2026-07-10T15:00:00+08:00"}
    )
    assert kind == "fixed"
    assert value == datetime(2026, 7, 10, 15, 0, 0, tzinfo=_CN)


def test_fixed_from_top_level():
    """顶层与 metadata 两种放法都认（前端选哪个都行）。"""
    kind, value = reference_time.resolve_reference_time(
        None, top_level={"reference_time": "2026-07-10 15:00"}
    )
    assert kind == "fixed"
    assert value == datetime(2026, 7, 10, 15, 0, 0, tzinfo=_CN)


def test_metadata_wins_over_top_level():
    kind, value = reference_time.resolve_reference_time(
        {"reference_time": "2026-07-10T15:00:00+08:00"},
        top_level={"reference_time": "2020-01-01T00:00:00+08:00"},
    )
    assert value == datetime(2026, 7, 10, 15, 0, 0, tzinfo=_CN)


@pytest.mark.parametrize("mode", ["real", "REAL", "dynamic", "live"])
def test_real_modes(mode):
    kind, value = reference_time.resolve_reference_time(
        {"time_mode": mode, "reference_time": "2026-07-10T15:00:00+08:00"}
    )
    assert (kind, value) == ("real", None)


def test_empty_is_inherit():
    assert reference_time.resolve_reference_time(None) == ("inherit", None)
    assert reference_time.resolve_reference_time({}) == ("inherit", None)


def test_fixed_mode_without_reference_time_raises():
    with pytest.raises(reference_time.InvalidReferenceTime):
        reference_time.resolve_reference_time({"time_mode": "fixed"})


def test_unparsable_reference_time_raises():
    with pytest.raises(reference_time.InvalidReferenceTime):
        reference_time.resolve_reference_time({"reference_time": "昨天下午"})


def test_csv_and_date_only_forms():
    assert reference_time.resolve_reference_time(
        {"reference_time": "2026-07-10"})[1] == datetime(2026, 7, 10, 0, 0, tzinfo=_CN)
    assert reference_time.resolve_reference_time(
        {"reference_time": "2026-07-10 15:00:30"})[1] == datetime(2026, 7, 10, 15, 0, 30, tzinfo=_CN)


def test_resolve_day():
    kind, value = reference_time.resolve_reference_time({"reference_time": "2026-07-10T15:00:00+08:00"})
    assert reference_time.resolve_day(kind, value, fallback_day="1999-01-01") == "2026-07-10"
    assert reference_time.resolve_day("real", None, fallback_day="1999-01-01") == \
        datetime.now(_CN).strftime("%Y-%m-%d")
    assert reference_time.resolve_day("inherit", None, fallback_day="1999-01-01") == "1999-01-01"


def test_anchor_cache_key(sim_file):
    kind, value = reference_time.resolve_reference_time({"reference_time": "2026-07-10T15:00:00+08:00"})
    assert reference_time.anchor_cache_key(kind, value) == "2026-07-10T15:00:00+08:00"
    assert reference_time.anchor_cache_key("real", None) == "real"
    assert reference_time.anchor_cache_key("inherit", None) is None
    time_source.set_override_from_text("2026-07-10 15:00:00")
    assert reference_time.anchor_cache_key("inherit", None) == "2026-07-10T15:00:00+08:00"
    time_source.clear_override()


def test_request_anchor_context_manager(sim_file):
    with reference_time.request_anchor("fixed", datetime(2026, 7, 10, 15, 0, tzinfo=_CN)):
        assert time_source.now() == datetime(2026, 7, 10, 15, 0, 0)
    assert time_source.now() != datetime(2026, 7, 10, 15, 0, 0)

    with reference_time.request_anchor("real", None):
        assert time_source.request_override_header_value() == "real"

    with reference_time.request_anchor("inherit", None):
        assert time_source.request_override_header_value() is None
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd chainlitexam && "D:/PythonProject/develop/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/.venv-test/Scripts/python.exe" -m pytest tests/test_reference_time.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'utils.reference_time'`

- [ ] **Step 3: 实现**

创建 `chainlitexam/utils/reference_time.py`：

```python
# -*- coding: utf-8 -*-
"""请求级锚定时间的解析（切换系统时间的请求侧契约）。

把前端传来的 metadata / 顶层字段归一到三态，供两个问答入口（HTTP /api/v1/qa/ask
与 Chainlit WS on_message）共用：

- ("fixed", datetime)  本次请求按该时刻回答
- ("real", None)       本次请求强制真实时间（压过遗留的全局覆盖文件）
- ("inherit", None)    没带锚点 → 回落全局覆盖文件 → 真实时间

纯函数、只依赖标准库与 utils.time_source，可单测、无重依赖。
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone

from utils import time_source

__all__ = [
    "InvalidReferenceTime",
    "resolve_reference_time",
    "resolve_day",
    "anchor_cache_key",
    "request_anchor",
]

_CN_TZ = timezone(timedelta(hours=8))

# time_mode 的"回到真实时间"取值（前端可能用任一写法）。
_REAL_MODES = frozenset({"real", "dynamic", "live"})

_STRPTIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
)


class InvalidReferenceTime(ValueError):
    """reference_time / time_mode 显式给了但不可用——入口应转 400。

    刻意与"没带锚点"区分开：没带是合法的（回落全局/真实时间），
    给了错值则说明调用方有 bug，静默按真实时间回答会掩盖问题。
    """


def _pick(metadata, top_level, key):
    """metadata.<key> 优先，其次顶层 <key>；空串视为未提供。"""
    for source in (metadata, top_level):
        if isinstance(source, dict):
            value = source.get(key)
            if value not in (None, ""):
                return value
    return None


def _parse(text: str) -> datetime:
    s = str(text).strip()
    dt = None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        for fmt in _STRPTIME_FORMATS:
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        raise InvalidReferenceTime(f"无法解析的 reference_time：{s!r}")
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=_CN_TZ)


def resolve_reference_time(metadata=None, *, top_level=None):
    """→ (kind, value)。kind ∈ {"fixed","real","inherit"}；fixed 时 value 为 aware datetime。"""
    raw_mode = _pick(metadata, top_level, "time_mode")
    mode = raw_mode.strip().lower() if isinstance(raw_mode, str) else None
    if mode in _REAL_MODES:
        return ("real", None)
    raw = _pick(metadata, top_level, "reference_time")
    if raw is None:
        if mode == "fixed":
            raise InvalidReferenceTime("time_mode=fixed 需要同时给出 reference_time")
        return ("inherit", None)
    return ("fixed", _parse(raw))


def resolve_day(kind: str, value, *, fallback_day: str) -> str:
    """本请求生效日期的 "%Y-%m-%d"（供 runtime 分槽缓存键）。

    "real" 用真实今天而不是 fallback：调用方刚说了"要真实时间"，重建出来的
    prompt 前缀也必须是真的今天。
    """
    if kind == "fixed":
        return value.strftime("%Y-%m-%d")
    if kind == "real":
        return datetime.now(_CN_TZ).strftime("%Y-%m-%d")
    return fallback_day


def anchor_cache_key(kind: str, value):
    """响应缓存的时间维度键。

    "inherit" 取全局覆盖文件的当前值——否则全局锚点变了、缓存键却不变，
    会返回陈旧答案（这正是本次要修的缺陷之一）。
    """
    if kind == "fixed":
        return value.isoformat()
    if kind == "real":
        return "real"
    override = time_source.get_override()
    return override.isoformat() if override is not None else None


@contextlib.contextmanager
def request_anchor(kind: str, value):
    """把解析结果落到 time_source 请求级锚点，退出时复位。

    "inherit" 是空操作：不设置即自然回落全局文件，无需 reset。
    """
    if kind == "inherit":
        yield
        return
    token = time_source.set_request_override("real" if kind == "real" else value)
    try:
        yield
    finally:
        time_source.reset_request_override(token)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd chainlitexam && "D:/PythonProject/develop/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/.venv-test/Scripts/python.exe" -m pytest tests/test_reference_time.py -v
```
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add chainlitexam/utils/reference_time.py chainlitexam/tests/test_reference_time.py
git commit -m "feat(reference-time): 请求锚点解析纯函数（fixed/real/inherit 三态）"
```

---

### Task 3: MCP header 注入 interceptor

**Files:**
- Modify: `chainlitexam/mcp_loader.py`
- Test: `chainlitexam/tests/test_mcp_reference_time_header.py`

**Interfaces:**
- Consumes: Task 1 的 `time_source.request_override_header_value()`、`time_source.REFERENCE_TIME_HEADER`
- Produces: `mcp_loader._inject_reference_time(request, handler)` —— `langchain_mcp_adapters.interceptors.ToolCallInterceptor`

- [ ] **Step 1: 写失败测试**

创建 `chainlitexam/tests/test_mcp_reference_time_header.py`：

```python
# -*- coding: utf-8 -*-
"""MCP interceptor：只在请求级锚点存在时才注入 header。"""
from __future__ import annotations

import pytest

pytest.importorskip("langchain_mcp_adapters", reason="需要 langchain-mcp-adapters")

from langchain_mcp_adapters.interceptors import MCPToolCallRequest  # noqa: E402

import mcp_loader  # noqa: E402
from utils import time_source  # noqa: E402


@pytest.fixture()
def sim_file(tmp_path, monkeypatch):
    f = tmp_path / "sim.json"
    monkeypatch.setenv("SIM_TIME_FILE", str(f))
    time_source._invalidate()
    yield f
    time_source._invalidate()


def _request(headers=None):
    return MCPToolCallRequest(name="t", args={}, server_name="weather", headers=headers)


async def _call():
    """跑一次 interceptor，返回 handler 实际收到的 request。"""
    seen = {}

    async def handler(req):
        seen["request"] = req
        return "ok"

    return seen, handler


@pytest.mark.asyncio
async def test_no_request_anchor_injects_nothing(sim_file):
    """没带请求锚点时一字节不加——MCP 侧继续自己读全局文件。"""
    time_source.set_override_from_text("2026-07-10 15:00:00")
    seen, handler = await _call()
    await mcp_loader._inject_reference_time(_request(headers={"X-Existing": "1"}), handler)
    assert seen["request"].headers == {"X-Existing": "1"}


@pytest.mark.asyncio
async def test_fixed_anchor_injected(sim_file):
    seen, handler = await _call()
    token = time_source.set_request_override("2026-03-05T08:30:00+08:00")
    try:
        await mcp_loader._inject_reference_time(_request(), handler)
    finally:
        time_source.reset_request_override(token)
    assert seen["request"].headers[time_source.REFERENCE_TIME_HEADER] == "2026-03-05T08:30:00+08:00"


@pytest.mark.asyncio
async def test_real_anchor_injected_as_sentinel(sim_file):
    seen, handler = await _call()
    token = time_source.set_request_override("real")
    try:
        await mcp_loader._inject_reference_time(_request(), handler)
    finally:
        time_source.reset_request_override(token)
    assert seen["request"].headers[time_source.REFERENCE_TIME_HEADER] == "real"


@pytest.mark.asyncio
async def test_existing_headers_preserved(sim_file):
    seen, handler = await _call()
    token = time_source.set_request_override("2026-03-05T08:30:00+08:00")
    try:
        await mcp_loader._inject_reference_time(_request(headers={"X-Existing": "1"}), handler)
    finally:
        time_source.reset_request_override(token)
    assert seen["request"].headers["X-Existing"] == "1"
    assert time_source.REFERENCE_TIME_HEADER in seen["request"].headers
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd chainlitexam && "D:/PythonProject/develop/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/.venv-test/Scripts/python.exe" -m pytest tests/test_mcp_reference_time_header.py -v
```
Expected: FAIL — `AttributeError: module 'mcp_loader' has no attribute '_inject_reference_time'`

- [ ] **Step 3: 实现**

`chainlitexam/mcp_loader.py`：import 区加 `from utils import time_source`；
在 `_load_one_server_tools` 之前插入：

```python
async def _inject_reference_time(request, handler):
    """把请求级锚定时间塞进 MCP header，让 MCP 进程的 time_source 读到。

    只在请求级锚点存在时才注入：没带锚点的请求一字节不加，MCP 侧继续自己读
    全局覆盖文件，行为与本次改动前逐字一致。

    背景：Chainlit 进程与 MCP 进程是两个进程，原先只能靠共享 JSON 文件传"现在"，
    那是个进程级标量（一人改时间全员受影响）。header 是每次工具调用新建的 SSE
    session 上带的（langchain-mcp-adapters 0.3.2 每次调用都会 create_session），
    因此能做到按请求而不是按进程。
    """
    value = time_source.request_override_header_value()
    if value is None:
        return await handler(request)
    headers = {**(request.headers or {}), time_source.REFERENCE_TIME_HEADER: value}
    return await handler(request.override(headers=headers))
```

并把 `_load_one_server_tools` 的 client 构造改为：

```python
    client = MultiServerMCPClient(
        {name: {"transport": "sse", "url": url}},
        tool_interceptors=[_inject_reference_time],
    )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd chainlitexam && "D:/PythonProject/develop/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/.venv-test/Scripts/python.exe" -m pytest tests/test_mcp_reference_time_header.py -v
```
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add chainlitexam/mcp_loader.py chainlitexam/tests/test_mcp_reference_time_header.py
git commit -m "feat(mcp-loader): 请求级锚点经 tool_interceptor 注入 MCP header"
```

---

### Task 4: `/admin/system-time` 新契约

**Files:**
- Modify: `chainlitexam/chain_gzt.py`（`SetSystemTimeRequest` 约 623-625 行、`_set_system_time` 约 634-644 行）
- Test: `chainlitexam/tests/test_system_time_api.py`

**Interfaces:**
- Consumes: Task 2 的 `reference_time.resolve_reference_time` / `InvalidReferenceTime`；既有 `time_source.set_override_from_text` / `clear_override`
- Produces: `POST /api/v1/admin/system-time` 接受三种 body（旧式 datetime / 新式 metadata fixed / 新式 metadata real）

- [ ] **Step 1: 写失败测试**

创建 `chainlitexam/tests/test_system_time_api.py`：

```python
# -*- coding: utf-8 -*-
"""POST /api/v1/admin/system-time 契约测试。

不 import chain_gzt（模块级副作用重）：直接构造同款请求模型 + 调同一套
解析/落盘函数，锁死契约语义。
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from utils import reference_time, time_source


class SetSystemTimeRequest(BaseModel):
    """与 chain_gzt.SetSystemTimeRequest 同款（字段一致性由 test_system_time_request_model_matches 锁定）。"""

    datetime: str | None = Field(None, max_length=32)
    note: str | None = Field(None, max_length=200)
    metadata: dict | None = Field(None)
    time_mode: str | None = Field(None, max_length=32)
    reference_time: str | None = Field(None, max_length=64)


@pytest.fixture()
def sim_file(tmp_path, monkeypatch):
    f = tmp_path / "sim.json"
    monkeypatch.setenv("SIM_TIME_FILE", str(f))
    time_source._invalidate()
    yield f
    time_source._invalidate()


def _resolve(req: SetSystemTimeRequest):
    """镜像 chain_gzt._set_system_time 的分支判定（见实现）。"""
    return reference_time.resolve_reference_time(
        req.metadata,
        top_level={"reference_time": req.reference_time, "time_mode": req.time_mode},
    )


def test_legacy_datetime_still_writes_global_file(sim_file):
    req = SetSystemTimeRequest(datetime="2026-07-10 15:00:00")
    assert req.datetime  # 旧式分支优先
    data = time_source.set_override_from_text(req.datetime, note=req.note)
    assert data["active"] is True
    assert time_source.get_override().strftime("%Y-%m-%d %H:%M") == "2026-07-10 15:00"


def test_new_metadata_fixed_is_stateless(sim_file):
    """新式（带 metadata）：只解析回显，不写全局文件。"""
    req = SetSystemTimeRequest(metadata={"time_mode": "fixed",
                                        "reference_time": "2026-07-10T15:00:00+08:00"})
    kind, value = _resolve(req)
    assert kind == "fixed"
    assert time_source.get_override() is None, "新式契约必须无状态：不得写全局覆盖文件"


def test_new_metadata_real_clears_legacy_file(sim_file):
    """新式 real：回真实时间并清掉遗留的全局文件（修"改回去了还是旧日期"）。"""
    time_source.set_override_from_text("2026-07-10 15:00:00")
    assert time_source.get_override() is not None
    req = SetSystemTimeRequest(metadata={"time_mode": "real"})
    assert _resolve(req) == ("real", None)
    time_source.clear_override()
    assert time_source.get_override() is None


def test_bare_metadata_means_off(sim_file):
    """不带任何锚点信息的调用 = 回到真实时间（并清掉遗留文件）。"""
    time_source.set_override_from_text("2026-07-10 15:00:00")
    req = SetSystemTimeRequest(metadata={"location": "http://x/y"})
    assert _resolve(req) == ("inherit", None)
    time_source.clear_override()
    assert time_source.get_override() is None


def test_top_level_equivalent_to_metadata():
    assert _resolve(SetSystemTimeRequest(reference_time="2026-07-10T15:00:00+08:00"))[0] == "fixed"
    assert _resolve(SetSystemTimeRequest(time_mode="real"))[0] == "real"


def test_invalid_reference_time_raises():
    with pytest.raises(reference_time.InvalidReferenceTime):
        _resolve(SetSystemTimeRequest(reference_time="昨天下午"))


def test_system_time_request_model_matches_chain_gzt():
    """chain_gzt.SetSystemTimeRequest 的字段必须与本测试的同款模型一致。"""
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "chain_gzt.py").read_text(encoding="utf-8")
    block = src.split("class SetSystemTimeRequest(BaseModel):", 1)[1].split("\n\n\n", 1)[0]
    for field in ("datetime", "note", "metadata", "time_mode", "reference_time"):
        assert re.search(rf"^\s+{field}\s*:", block, re.M), f"SetSystemTimeRequest 缺字段 {field}"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd chainlitexam && "D:/PythonProject/develop/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/.venv-test/Scripts/python.exe" -m pytest tests/test_system_time_api.py -v
```
Expected: `test_system_time_request_model_matches_chain_gzt` FAIL（`chain_gzt` 里还没有新字段）

- [ ] **Step 3: 实现**

`chainlitexam/chain_gzt.py`，把 `SetSystemTimeRequest` 换成：

```python
class SetSystemTimeRequest(BaseModel):
    """系统时间设置请求。两种契约并存：

    - 旧式：给 `datetime`（"YYYY-MM-DD[ HH:MM[:SS]]" 或 ISO）→ 写**全局**覆盖文件。
      这是进程级开关，只用于服务端 curl 手工验收，不要给前端用。
    - 新式（前端契约）：给 `metadata.time_mode` / `metadata.reference_time` → **无状态**。
      锚点由客户端逐请求携带（见 utils/reference_time.py），服务端不落任何跨用户状态。
      顶层同名字段等价可用。
    """

    datetime: str | None = Field(None, max_length=32)
    note: str | None = Field(None, max_length=200)
    metadata: dict | None = Field(None)
    time_mode: str | None = Field(None, max_length=32)
    reference_time: str | None = Field(None, max_length=64)
```

（import 区加 `from utils import reference_time as reference_time_util`。）

把 `_set_system_time` 换成：

```python
@api_sub_app.post("/admin/system-time", tags=["系统时间"])
def _set_system_time(req: SetSystemTimeRequest):
    try:
        kind, value = reference_time_util.resolve_reference_time(
            req.metadata,
            top_level={"reference_time": req.reference_time, "time_mode": req.time_mode},
        )
    except reference_time_util.InvalidReferenceTime as e:
        raise HTTPException(400, qa_http_api._scrub(str(e)))

    # 旧式：显式 datetime → 写全局覆盖文件，行为与本次改动前逐字一致。
    if req.datetime:
        try:
            data = time_source.set_override_from_text(req.datetime, note=req.note)
        except ValueError as e:
            raise HTTPException(400, qa_http_api._scrub(str(e)))
        except Exception as e:
            logging.getLogger("chain_gzt").error("set_system_time: %s", type(e).__name__)
            raise HTTPException(500, f"internal: {type(e).__name__}")
        _after_system_time_changed()
        return {"code": 200, "data": data, "message": "success"}

    if kind == "fixed":
        # 新式锚定：无状态回显。客户端据此更新自己的面板状态，后续逐请求带 reference_time。
        return {
            "code": 200,
            "data": {
                "active": True,
                "mode": "per_request",
                "override_datetime": value.isoformat(),
                "display": value.strftime("%Y-%m-%d %H:%M:%S"),
            },
            "message": "success",
        }

    # real / inherit：回到真实时间，并顺手清掉可能遗留的全局覆盖文件。
    # 这一步是"改回去了天河小程序还是旧日期"的直接解药：旧文件跨服务重启存活，
    # 不清就会一直把整个机器锚在过去。
    try:
        data = time_source.clear_override()
    except Exception as e:
        logging.getLogger("chain_gzt").error("clear_system_time: %s", type(e).__name__)
        raise HTTPException(500, f"internal: {type(e).__name__}")
    data["mode"] = "per_request"
    _after_system_time_changed()
    return {"code": 200, "data": data, "message": "success"}
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd chainlitexam && "D:/PythonProject/develop/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/.venv-test/Scripts/python.exe" -m pytest tests/test_system_time_api.py -v
```
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add chainlitexam/chain_gzt.py chainlitexam/tests/test_system_time_api.py
git commit -m "feat(system-time): /admin/system-time 接受前端 metadata 契约；real 顺手清全局文件"
```

---

### Task 5: 运行时按日期分槽

**Files:**
- Modify: `chainlitexam/chain_gzt.py`（`_build_orchestrator_runtime` 2784-2843、`_orchestrator_runtime_cache_key` 2860-2868、`_orchestrator_runtime_state` 2878-2892、`_get_orchestrator_runtime` 2895-2910、`_build_qa_runtime` 710-719）
- Test: `chainlitexam/tests/test_orchestrator_runtime_slots.py`

**Interfaces:**
- Consumes: Task 1 的 `time_source.override_date_str()`
- Produces:
  - `_build_orchestrator_runtime(day: str | None = None, *, mcp_tools: list | None = None) -> dict`
  - `_orchestrator_runtime_cache_key(day: str | None = None)`
  - `_get_orchestrator_runtime(day: str | None = None) -> dict`
  - `_build_qa_runtime(day: str) -> dict`
  - `_orchestrator_runtime_state()` 的 state 含 `{"generation","runtimes","mcp_tools","lock"}`

- [ ] **Step 1: 写失败测试**

创建 `chainlitexam/tests/test_orchestrator_runtime_slots.py`：

```python
# -*- coding: utf-8 -*-
"""运行时按日期分槽：MCP 工具表只加载一次，日期槽位有上限。"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("chainlit.emitter", reason="需要真实 Chainlit 包")

import chain_gzt  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_runtime(monkeypatch):
    monkeypatch.setenv("CACHE_ORCHESTRATOR_RUNTIME", "true")
    chain_gzt._clear_orchestrator_runtime_cache()
    yield
    chain_gzt._clear_orchestrator_runtime_cache()


def test_cache_key_varies_by_day():
    assert chain_gzt._orchestrator_runtime_cache_key("2026-07-10")[0] == "2026-07-10"
    assert chain_gzt._orchestrator_runtime_cache_key("2026-07-11")[0] == "2026-07-11"
    assert chain_gzt._orchestrator_runtime_cache_key("2026-07-10") == \
        chain_gzt._orchestrator_runtime_cache_key("2026-07-10")


def test_mcp_tools_loaded_once_across_days(monkeypatch):
    """两个日期两个槽位，但 MCP 工具表只列一次（这是分槽设计的意义）。"""
    calls = {"n": 0}

    async def fake_load_sse_tools():
        calls["n"] += 1
        return []

    monkeypatch.setattr(chain_gzt, "load_sse_tools", fake_load_sse_tools)
    monkeypatch.setenv("ENABLE_ACTIVE_TOOL_FILTER", "false")

    async def main():
        await chain_gzt._get_orchestrator_runtime("2026-07-10")
        await chain_gzt._get_orchestrator_runtime("2026-07-11")
        await chain_gzt._get_orchestrator_runtime("2026-07-10")

    asyncio.run(main())
    assert calls["n"] == 1


def test_runtime_slots_are_capped(monkeypatch):
    async def fake_load_sse_tools():
        return []

    monkeypatch.setattr(chain_gzt, "load_sse_tools", fake_load_sse_tools)
    monkeypatch.setenv("ORCHESTRATOR_RUNTIME_MAX_DAYS", "2")

    async def main():
        for day in ("2026-07-10", "2026-07-11", "2026-07-12"):
            await chain_gzt._get_orchestrator_runtime(day)
        return chain_gzt._orchestrator_runtime_state()["runtimes"]

    runtimes = asyncio.run(main())
    assert len(runtimes) == 2
    assert all(key[0] != "2026-07-10" for key in runtimes), "最老的槽位应被淘汰"


def test_prompt_prefix_uses_requested_day(monkeypatch):
    """【当前日期】必须按传入的 day 渲染，而不是按进程当前时间。"""
    captured = {}

    class _FakeTemplate:
        def __init__(self, messages):
            captured.setdefault("prefixes", []).append(messages[0][1])

        def __or__(self, other):
            return self

    monkeypatch.setattr(chain_gzt, "ChatPromptTemplate", _FakeTemplate)
    monkeypatch.setattr(chain_gzt, "_build_chat_llm", lambda role: object())

    async def fake_load_sse_tools():
        return []

    monkeypatch.setattr(chain_gzt, "load_sse_tools", fake_load_sse_tools)
    monkeypatch.setenv("ENABLE_ACTIVE_TOOL_FILTER", "false")

    asyncio.run(chain_gzt._build_orchestrator_runtime("2026-07-10"))
    assert any("2026年07月10日" in p for p in captured["prefixes"])
    assert any("星期五" in p for p in captured["prefixes"])
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd chainlitexam && "D:/PythonProject/develop/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/.venv-test/Scripts/python.exe" -m pytest tests/test_orchestrator_runtime_slots.py -v
```
Expected: FAIL — `TypeError: _orchestrator_runtime_cache_key() takes 0 positional arguments but 1 was given`

- [ ] **Step 3: 实现**

`chainlitexam/chain_gzt.py`：

**(a)** `_build_orchestrator_runtime` 签名与首部改为：

```python
async def _build_orchestrator_runtime(day: str | None = None, *, mcp_tools=None) -> dict:
    """构造 planner / answer / thinking chain 与工具表。

    不碰 `cl.user_session`，因此可被网页会话与 HTTP 问答接口共用。

    `day` 决定【当前日期】前缀（None → 走统一时间源的"今天"）；`mcp_tools` 由调用方
    传入时复用已加载的 MCP 工具表——分槽缓存下这层是跨日期共享的，避免为每个日期
    重连一次内网 MCP。
    """
    if day is None:
        day = time_source.override_date_str()
    planner_llm = _build_chat_llm("PLANNER")
    answer_llm = _build_chat_llm("ANSWER")

    tools = list(mcp_tools) if mcp_tools is not None else await load_sse_tools()
    tools = tools + build_external_skill_tools() + build_rain_analysis_tools()
    weekday_map = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    # 走统一时间源：切换系统时间激活时，【当前日期】锚定到指定日期。
    # day 是请求级锚点解析出来的日期（见 utils/reference_time.py），所以这里按
    # 请求渲染，而不是按进程当前时间。
    day_dt = datetime.strptime(day, "%Y-%m-%d")
    today_str = day_dt.strftime("%Y年%m月%d日")
    weekday_str = weekday_map[day_dt.weekday()]
    prompt_prefix = f"【当前日期：{today_str}（{weekday_str}）】请基于这个当前日期来理解用户的相对时间表述（如今天、明天、周末等）。\n\n"
```
其余函数体不变。

**(b)** 缓存键：

```python
def _orchestrator_runtime_cache_key(day: str | None = None):
    """缓存键包含生效日期与关键配置。

    日期取请求级锚点解析结果（None → 统一时间源的"今天"）：不同用户带着不同锚点
    并发提问时各占一个槽位，互不覆盖；日期变化即自动重建该槽位的【当前日期】前缀。
    """
    day = day or time_source.override_date_str()
    config_values = tuple((name, os.environ.get(name, "")) for name in _RUNTIME_CONFIG_ENV_KEYS)
    return day, config_values
```

**(c)** state：

```python
def _orchestrator_runtime_state() -> dict:
    """返回当前事件循环独享的运行时槽位表、共享 MCP 工具表与初始化锁。"""
    loop = asyncio.get_running_loop()
    with _ORCHESTRATOR_RUNTIME_GENERATION_LOCK:
        generation = _ORCHESTRATOR_RUNTIME_GENERATION
    state = getattr(loop, _ORCHESTRATOR_RUNTIME_STATE_ATTR, None)
    if state is None or state.get("generation") != generation:
        state = {
            "generation": generation,
            "runtimes": {},      # cache_key -> runtime（按日期分槽，LRU 上限见 _evict_runtime_slots）
            "mcp_tools": None,   # 跨日期共享：MCP 工具表只加载一次
            "lock": asyncio.Lock(),
        }
        setattr(loop, _ORCHESTRATOR_RUNTIME_STATE_ATTR, state)
    return state


def _evict_runtime_slots(state: dict) -> None:
    """日期槽位超限时按插入顺序淘汰最老的（dict 保序）。"""
    limit = _env_int_optional("ORCHESTRATOR_RUNTIME_MAX_DAYS") or 4
    if limit < 1:
        limit = 1
    runtimes = state["runtimes"]
    while len(runtimes) > limit:
        runtimes.pop(next(iter(runtimes)))
```

**(d)** 取运行时：

```python
async def _get_orchestrator_runtime(day: str | None = None) -> dict:
    """取某日期槽位的运行时；同 loop、同日、同配置只初始化一次。

    跨日期共享 `state["mcp_tools"]`：只重建便宜的 prompt/chain/router，不重连 MCP。
    """
    if not _env_bool("CACHE_ORCHESTRATOR_RUNTIME", True):
        return await _build_orchestrator_runtime(day)

    state = _orchestrator_runtime_state()
    key = _orchestrator_runtime_cache_key(day)
    cached = state["runtimes"].get(key)
    if cached is not None:
        return cached

    async with state["lock"]:
        cached = state["runtimes"].get(key)
        if cached is not None:
            return cached
        if state["mcp_tools"] is None:
            state["mcp_tools"] = await load_sse_tools()
        runtime = await _build_orchestrator_runtime(key[0], mcp_tools=state["mcp_tools"])
        state["runtimes"][key] = runtime
        _evict_runtime_slots(state)
        return runtime
```

**(e)** `_build_qa_runtime`：

```python
async def _build_qa_runtime(day: str) -> dict:
    """给 qa_http_api 注入运行时（按日期分槽，见 _get_orchestrator_runtime）。"""
    await _ensure_chainlit_tables()
    # HTTP 与网页会话复用同一份无会话状态的 chain/tools；复制 dict 后再注入
    # HTTP callbacks，避免污染共享缓存对象。
    runtime = dict(await _get_orchestrator_runtime(day))
    runtime["callbacks"] = _build_orchestrator_callbacks(execution_mode="http")
    runtime["callbacks"]["tool_candidate_index"] = runtime.get("tool_candidate_index")
    runtime["callbacks"]["active_tool_router"] = runtime.get("active_tool_router")
    return runtime
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd chainlitexam && "D:/PythonProject/develop/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/.venv-test/Scripts/python.exe" -m pytest tests/test_orchestrator_runtime_slots.py -v
```
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add chainlitexam/chain_gzt.py chainlitexam/tests/test_orchestrator_runtime_slots.py
git commit -m "feat(runtime): 运行时按日期分槽，MCP 工具表跨日期共享一次加载"
```

---

### Task 6: HTTP 入口接线 + 响应缓存按锚点分槽

**Files:**
- Modify: `chainlitexam/chain_gzt.py`（`QAAskRequest` 544-548、`_qa_ask` 550-570）
- Modify: `chainlitexam/qa_http_api.py`（`_runtime_epoch` 43-49、`QARuntime.__init__` 636-646、`configure`/`_get_runtime` 648-682、`ask` 705-774、`_run_once` 775-851）
- Test: `chainlitexam/tests/test_qa_http_api.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `reference_time.*`、Task 5 的 `_build_qa_runtime(day)`
- Produces: `QARuntime.ask(..., metadata=None, reference_time=None, time_mode=None)`；`QARuntime._get_runtime(day: str)`

- [ ] **Step 1: 写失败测试**

在 `chainlitexam/tests/test_qa_http_api.py` 末尾追加：

```python
def test_runtime_epoch_accepts_explicit_day():
    """epoch 可按请求日期给定；不给时仍回落统一时间源（既有行为不变）。"""
    import qa_http_api
    from utils import time_source

    assert qa_http_api._runtime_epoch("2026-07-10") == "2026-07-10"
    assert qa_http_api._runtime_epoch() == time_source.override_date_str()


def test_response_cache_key_includes_anchor():
    """响应缓存键必须带时间维度：不同锚点问同一句话不能互相命中。"""
    import json

    import qa_http_api

    def key_of(raw_question, anchor):
        return json.dumps(
            {"q": raw_question, "r": True, "g": True, "t": anchor},
            sort_keys=True, ensure_ascii=False,
        )

    assert key_of("天津天气", None) != key_of("天津天气", "2026-07-10T15:00:00+08:00")
    assert key_of("天津天气", "real") != key_of("天津天气", None)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd chainlitexam && "D:/PythonProject/develop/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/.venv-test/Scripts/python.exe" -m pytest tests/test_qa_http_api.py -v
```
Expected: `test_runtime_epoch_accepts_explicit_day` FAIL（`TypeError: _runtime_epoch() takes 0 positional arguments`）

- [ ] **Step 3: 实现**

`chainlitexam/qa_http_api.py`：

**(a)** `_runtime_epoch`：

```python
def _runtime_epoch(day: str | None = None) -> str:
    """HTTP 运行时失效周期；按日期区分，日期变化时刷新带当前日期的 system prompt。

    `day` 是请求级锚点解析出的日期（utils/reference_time.resolve_day）。不传时
    回落统一时间源，等价于既有行为。
    """
    return day or time_source.override_date_str()
```

**(b)** import 区加 `from utils import reference_time as reference_time_util`。

**(c)** `QARuntime.__init__`：把 `self._runtime` / `self._runtime_epoch` 换成按日期槽位：

```python
        self._factory = None
        # 按日期分槽的运行时缓存：cache_key(day) -> runtime。
        self._runtimes: dict[str, dict[str, Any]] = {}
```

`configure` 清空 `self._runtimes = {}`。

**(d)** `_get_runtime(day)`：

```python
    async def _get_runtime(self, day: str) -> dict[str, Any]:
        """取（并首次构造）该日期槽位的运行时。

        工厂里含 `load_sse_tools()`，要连内网 MCP —— 内网抖动时可能长时间挂住，
        所以必须自带超时；否则「180s 超时」形同虚设，所有请求无限期等待。
        失败后清空该槽位，让下一个请求可以重试。
        """
        epoch = _runtime_epoch(day)
        cached = self._runtimes.get(epoch)
        if cached is not None:
            return cached
        if self._factory is None:
            raise QANotConfigured("问答运行时未初始化")
        async with self._init_lock:
            cached = self._runtimes.get(epoch)
            if cached is not None:
                return cached
            try:
                self._runtimes[epoch] = await asyncio.wait_for(
                    self._factory(day), timeout=TIMEOUT_SECONDS
                )
            except BaseException:
                self._runtimes.pop(epoch, None)  # 允许后续请求重试
                raise
        return self._runtimes[epoch]
```

**(e)** `ask()`：新增参数、解析锚点、缓存键带 `t`、传 day 给 `_get_runtime`：

```python
    async def ask(
        self,
        question: str,
        *,
        conversation_id: str | None = None,
        include_reasoning: bool = True,
        include_gis: bool = True,
        metadata: dict | None = None,
        reference_time: str | None = None,
        time_mode: str | None = None,
    ) -> dict[str, Any]:
```

在参数校验之后、构造 `cid` 之前插入：

```python
        # 请求级锚定时间（切换系统时间功能）：只有带了锚点的这个请求按锚定时间回答，
        # 服务端不落任何跨用户状态。
        anchor_kind, anchor_value = reference_time_util.resolve_reference_time(
            metadata,
            top_level={"reference_time": reference_time, "time_mode": time_mode},
        )
        day = reference_time_util.resolve_day(
            anchor_kind, anchor_value, fallback_day=time_source.override_date_str()
        )
        anchor_key = reference_time_util.anchor_cache_key(anchor_kind, anchor_value)
```

响应缓存键改为：

```python
            response_cache_key = json.dumps(
                {"q": text, "r": include_reasoning, "g": include_gis, "t": anchor_key},
                sort_keys=True, ensure_ascii=False,
            )
```

`runtime = await self._get_runtime()` → `runtime = await self._get_runtime(day)`；
`_run_once(...)` 调用加 `anchor=(anchor_kind, anchor_value),`。

**(f)** `_run_once` 加 `anchor` 参数并用 contextmanager 包住 `process_message`：

```python
    async def _run_once(
        self,
        question: str,
        conversation_id: str,
        runtime: dict[str, Any],
        include_reasoning: bool,
        include_gis: bool,
        anchor: tuple[str, Any] = ("inherit", None),
        http_queue_wait_ms: float = 0.0,
    ) -> dict[str, Any]:
```

把 `with _suppress_chainlit_data_layer(): await process_message(...)` 包进锚点上下文：

```python
        pq_started = time.time()
        try:
            # 请求级锚点：只在本次请求的 Task 内生效（ContextVar 天然隔离），
            # 请求结束即消失——这正是"不再影响其他用户"的实现处。
            with reference_time_util.request_anchor(*anchor):
                # HTTP 模式跳过 chainlit data-layer 落库（见 _suppress_chainlit_data_layer）。
                with _suppress_chainlit_data_layer():
                    await process_message(
                        message=cl.Message(content=question),
                        planner_chain=runtime["planner_chain"],
                        answer_chain=runtime["answer_chain"],
                        thinking_chain=runtime["thinking_chain"],
                        tools=runtime["tools"],
                        messages=history,
                        callbacks=runtime["callbacks"],
                    )
        finally:
```

**(g)** `chainlitexam/chain_gzt.py`：`QAAskRequest` 加三个可选字段；`_qa_ask` 透传并映射 400：

```python
class QAAskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=qa_http_api.MAX_QUESTION_LENGTH)
    conversation_id: str | None = Field(None)
    include_reasoning: bool = Field(True)
    include_gis: bool = Field(True)
    # 请求级锚定时间（切换系统时间功能）。metadata 与顶层两种放法都认。
    metadata: dict | None = Field(None)
    reference_time: str | None = Field(None, max_length=64)
    time_mode: str | None = Field(None, max_length=32)
```

`_qa_ask` 的 `runtime.ask(...)` 调用追加：

```python
            metadata=req.metadata,
            reference_time=req.reference_time,
            time_mode=req.time_mode,
```

并在该 handler 的异常映射里加一条 `reference_time_util.InvalidReferenceTime` → 400
（若原 handler 用 `except Exception` 统一 500，则在其**之前**插入：

```python
    except reference_time_util.InvalidReferenceTime as e:
        raise HTTPException(400, qa_http_api._scrub(str(e)))
```
）。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd chainlitexam && "D:/PythonProject/develop/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/.venv-test/Scripts/python.exe" -m pytest tests/test_qa_http_api.py tests/test_reference_time.py -v
```
Expected: 全部 PASS

- [ ] **Step 5: 写并发隔离测试并确认通过**

在 `chainlitexam/tests/test_qa_http_api.py` 追加：

```python
def test_concurrent_anchors_do_not_cross_contaminate():
    """两个不同锚点的请求并发跑，各自的"现在"互不污染。"""
    import asyncio
    from datetime import datetime

    from utils import reference_time
    from utils import time_source

    async def one(iso):
        kind, value = reference_time.resolve_reference_time({"reference_time": iso})
        with reference_time.request_anchor(kind, value):
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return time_source.now()

    async def main():
        return await asyncio.gather(
            one("2026-03-05T08:30:00+08:00"),
            one("2026-09-09T20:00:00+08:00"),
        )

    a, b = asyncio.run(main())
    assert a == datetime(2026, 3, 5, 8, 30, 0)
    assert b == datetime(2026, 9, 9, 20, 0, 0)
```

```bash
cd chainlitexam && "D:/PythonProject/develop/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/.venv-test/Scripts/python.exe" -m pytest tests/test_qa_http_api.py -v
```
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add chainlitexam/qa_http_api.py chainlitexam/chain_gzt.py chainlitexam/tests/test_qa_http_api.py
git commit -m "feat(qa-api): /qa/ask 支持请求级 reference_time；响应缓存按锚点分槽"
```

---

### Task 7: WS 入口接线

**Files:**
- Modify: `chainlitexam/chain_gzt.py`（`on_message` 4164-4196）
- Test: `chainlitexam/tests/test_reference_time.py`（追加一条）

**Interfaces:**
- Consumes: Task 2 的 `reference_time.resolve_reference_time/resolve_day/request_anchor`；Task 5 的 `_get_orchestrator_runtime(day)`
- Produces: `on_message` 按 `message.metadata` 的锚点取对应日期槽位的链，不再用 `cl.user_session` 里钉死的 chain

- [ ] **Step 1: 写失败测试**

在 `chainlitexam/tests/test_reference_time.py` 末尾追加：

```python
def test_message_metadata_shapes():
    """WS 侧 message.metadata 与 HTTP 侧 metadata 用同一套解析（含 None 容错）。"""
    assert reference_time.resolve_reference_time(None) == ("inherit", None)
    assert reference_time.resolve_reference_time({"location": "http://x/y"}) == ("inherit", None)
    kind, value = reference_time.resolve_reference_time(
        {"location": "http://x/y", "time_mode": "fixed",
         "reference_time": "2026-07-10T15:00:00+08:00"}
    )
    assert kind == "fixed"
    assert value == datetime(2026, 7, 10, 15, 0, 0, tzinfo=_CN)
```

- [ ] **Step 2: 跑测试确认通过（本步只补契约覆盖）**

```bash
cd chainlitexam && "D:/PythonProject/develop/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/.venv-test/Scripts/python.exe" -m pytest tests/test_reference_time.py -v
```
Expected: PASS（解析器已在 Task 2 实现，这条锁的是 WS 复用它）

- [ ] **Step 3: 实现**

`chainlitexam/chain_gzt.py` 的 `on_message` 整体替换为：

```python
@cl.on_message
async def on_message(message: cl.Message):
    # 请求级锚定时间：AgentWeb 面板把 time_mode/reference_time 挂进 user_message 的
    # metadata（Chainlit 本来就透传 metadata，原先只放了 location）。没带锚点时
    # resolve 返回 inherit，行为与本次改动前一致。
    anchor_kind, anchor_value = reference_time_util.resolve_reference_time(
        getattr(message, "metadata", None)
    )
    day = reference_time_util.resolve_day(
        anchor_kind, anchor_value, fallback_day=time_source.override_date_str()
    )

    with reference_time_util.request_anchor(anchor_kind, anchor_value):
        # 按本请求的日期取运行时槽位。不能再用 cl.user_session 里钉死的 chain——
        # 那是会话开始那一刻的日期，同一会话内切换锚点会失效。
        runtime = await _get_orchestrator_runtime(day)

        messages = cl.user_session.get("messages")
        if not isinstance(messages, list):
            # 兜底：恢复线程或服务热重载后会话里没有消息列表（顺带确保数据表就绪）。
            await _init_runtime_session(messages_seed=[])
            messages = cl.user_session.get("messages")

        callbacks = _build_orchestrator_callbacks()
        # 用本槽位 runtime 自带的索引/router：router 的 full_chain 里烤着该日期的
        # prompt 前缀，跨日期复用会串味。
        callbacks["tool_candidate_index"] = runtime.get("tool_candidate_index")
        callbacks["active_tool_router"] = runtime.get("active_tool_router")
        await process_message(
            message=message,
            planner_chain=runtime["planner_chain"],
            answer_chain=runtime["answer_chain"],
            thinking_chain=runtime["thinking_chain"],
            tools=runtime["tools"],
            messages=messages,
            callbacks=callbacks,
        )
```

- [ ] **Step 4: 跑相关测试**

```bash
cd chainlitexam && "D:/PythonProject/develop/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/.venv-test/Scripts/python.exe" -m pytest tests/test_reference_time.py tests/test_message_orchestrator.py -v
```
Expected: `test_reference_time.py` 全 PASS；`test_message_orchestrator.py` 除已知
`test_process_message_skips_fast_paths_when_disabled` 外 PASS

- [ ] **Step 5: 提交**

```bash
git add chainlitexam/chain_gzt.py chainlitexam/tests/test_reference_time.py
git commit -m "feat(ws): on_message 按 message.metadata 锚点取对应日期槽位运行时"
```

---

### Task 8: 内网 header 投递探针

**Files:**
- Create: `haihe-weather-analyzer-mcp/probe_reference_time_header.py`

**Interfaces:**
- Consumes: Task 1 的 `time_source.REFERENCE_TIME_HEADER`；Task 3 的 interceptor 同款逻辑
- Produces: 一个可独立运行的内网实测脚本（本机自起 server + client，不依赖内网 MCP）

> 这是本设计唯一的硬假设（header 真能到工具体内）。**实测通过前不上生产。**

- [ ] **Step 1: 写探针**

创建 `haihe-weather-analyzer-mcp/probe_reference_time_header.py`：

```python
# -*- coding: utf-8 -*-
"""探针：验证 MCP tool_interceptor 注入的 header 真能到 MCP 工具体内。

本设计唯一的硬假设——Chainlit 进程按请求塞进 SSE 的 header，MCP 进程的
time_source 能通过 fastmcp.get_http_headers() 读回来。库源码层面成立
（langchain-mcp-adapters 0.3.2 每次工具调用新建 SSE session；headers 作用在
httpx client 层；MCP SDK 的 ServerMessageMetadata.request_context 即 POST /messages/），
但必须实测。

本机自起一个最小 FastMCP SSE server + 带 interceptor 的 client，不碰内网。

跑法：
    python probe_reference_time_header.py
期望输出末行：
    [PROBE] RESULT=OK received=<注入值>
"""
from __future__ import annotations

import asyncio
import threading
import time

from fastmcp import FastMCP
from langchain_mcp_adapters.client import MultiServerMCPClient

HEADER = "x-haihe-reference-time"
PROBE_VALUE = "2026-07-10T15:00:00+08:00"
PORT = 3899


def _build_server() -> FastMCP:
    server = FastMCP("probe")

    @server.tool
    def echo_reference_time() -> str:
        """回显本请求携带的锚定时间 header（验证跨进程投递）。"""
        from fastmcp.server.dependencies import get_http_headers

        return (get_http_headers() or {}).get(HEADER, "")

    return server


def _serve() -> None:
    _build_server().run(transport="sse", host="127.0.0.1", port=PORT, show_banner=False)


async def _probe() -> str:
    async def inject(request, handler):
        headers = {**(request.headers or {}), HEADER: PROBE_VALUE}
        return await handler(request.override(headers=headers))

    client = MultiServerMCPClient(
        {"probe": {"transport": "sse", "url": f"http://127.0.0.1:{PORT}/sse"}},
        tool_interceptors=[inject],
    )
    tools = await client.get_tools()
    tool = next(t for t in tools if t.name == "echo_reference_time")
    return await tool.ainvoke({})


def main() -> None:
    threading.Thread(target=_serve, daemon=True).start()
    time.sleep(3)  # 等 SSE server 就绪
    received = asyncio.run(_probe())
    ok = received == PROBE_VALUE
    print(f"[PROBE] sent={PROBE_VALUE}")
    print(f"[PROBE] received={received!r}")
    print(f"[PROBE] RESULT={'OK' if ok else 'FAIL'}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 本机跑探针**

```bash
cd haihe-weather-analyzer-mcp && "D:/PythonProject/develop/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/.venv-test/Scripts/python.exe" probe_reference_time_header.py
```
Expected: `[PROBE] RESULT=OK`

- [ ] **Step 3: 内网跑探针（由用户执行）**

在内网服务器上跑同一脚本。**未通过则执行降级方案**（见 spec「风险 1」）：保留全局文件路径，
请求级锚点只在 Chainlit 进程内生效（prompt/缓存/Chainlit 侧工具仍按请求隔离），
不注入 header。

- [ ] **Step 4: 提交**

```bash
git add haihe-weather-analyzer-mcp/probe_reference_time_header.py
git commit -m "test(probe): MCP header 投递探针（验证 interceptor header 能到工具体内）"
```

---

### Task 9: 全量回归 + 文档

**Files:**
- Modify: `deploy_agentweb/DEPLOY-sim-time.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: 前 8 个任务的全部产出
- Produces: 更新后的部署说明与仓库约定

- [ ] **Step 1: 跑全量回归**

```bash
cd chainlitexam && "D:/PythonProject/develop/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/.venv-test/Scripts/python.exe" -m pytest tests/ -v --ignore=tests/test_decision_weather_tool.py
```
Expected: 除已知 flaky（`test_process_message_skips_fast_paths_when_disabled`）外全部 PASS。
**若有新增失败，在此停下修完再继续**——不要带着回归提交。

- [ ] **Step 2: 跑 MCP 侧全量**

```bash
cd haihe-weather-analyzer-mcp && "D:/PythonProject/develop/haiheliuyubaoyuagent-master/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/.venv-test/Scripts/python.exe" -m pytest tests/ -q
```
Expected: 与改动前同样的通过数（本设计不改 MCP 侧业务逻辑，只改 `time_source.py`）

- [ ] **Step 3: 更新部署文档**

`deploy_agentweb/DEPLOY-sim-time.md`：
- 第 3 行"**不是模拟**——是全局切换"改为按请求锚定的口径。
- 「三、REST 接口」表补三种 body（旧式 datetime / metadata fixed / metadata real）。
- 新增一节「前端契约」：`POST /api/v1/qa/ask` 的 `metadata.{time_mode,reference_time}`
  与 WS `user_message` 的 `metadata` 同款字段。
- 「五、风险与已知缺口」的第 2 条（忘记恢复）改为说明"请求级锚点请求结束即失效；
  全局兜底仅服务端 curl 验收用"。

- [ ] **Step 4: 更新 CLAUDE.md**

在「近期功能」末尾追加一节，记录：请求级锚点的优先级链、两个入口的字段、`/admin/system-time`
三种 body 语义、`ORCHESTRATOR_RUNTIME_MAX_DAYS`、以及"全局兜底仅用于 curl 验收"。

- [ ] **Step 5: 提交**

```bash
git add deploy_agentweb/DEPLOY-sim-time.md CLAUDE.md
git commit -m "docs: 请求级锚定时间契约与部署说明"
```

---

## Self-Review

**Spec coverage：**

| spec 章节 | 任务 |
|---|---|
| §1 `time_source` 请求级锚点 | Task 1 |
| §2 请求解析纯函数 | Task 2 |
| §3 入口接线（HTTP） | Task 6 |
| §3 入口接线（WS） | Task 7 |
| §4 运行时按日期分槽 | Task 5 |
| §5 跨进程 interceptor + header | Task 3（+ Task 8 验证） |
| §6 缓存隔离 | Task 6（响应缓存）、Task 5（槽位）、Task 9（回归） |
| §7 `/admin/system-time` 契约 | Task 4 |
| 验证（探针） | Task 8 |
| 验证（自动化测试） | 各任务内 + Task 9 回归 |
| 风险 1 降级路径 | Task 8 Step 3 |
| 回滚 | 各任务独立 commit，可单独 revert |

**Placeholder scan：** 无 TBD/TODO；每个代码步骤都给了可直接粘贴的完整代码。

**Type consistency：**
- `resolve_reference_time(metadata=None, *, top_level=None)` → `(kind, value)` 在 Task 2 定义，
  Task 4/6/7 一致使用。
- `resolve_day(kind, value, *, fallback_day)` 关键字参数名三处一致。
- `request_anchor(kind, value)` 在 Task 2 定义，Task 6（`_run_once` 位置参数解包）与
  Task 7（`with` 语句）一致使用。
- `_get_orchestrator_runtime(day)` 在 Task 5 定义，Task 6/7 一致使用。
- `_build_qa_runtime(day)` 在 Task 5 定义，Task 6 的 `QARuntime._get_runtime(day)` 以
  `self._factory(day)` 调用——`configure()` 的注入点是 `chain_gzt._build_qa_runtime`，签名一致。
- `time_source.REFERENCE_TIME_HEADER` 在 Task 1 定义，Task 3/8 一致使用。
