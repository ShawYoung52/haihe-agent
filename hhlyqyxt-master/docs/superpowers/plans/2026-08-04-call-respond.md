# 应急响应叫应功能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为牵引智能体应急响应新增"叫应"机制：等级变化时创建叫应任务，值班人员人工确认后后台异步发送叫应话术 + 天河报告文件到受影响单位微信群，并记录逐群发送台账。

**Architecture:** 新建独立 `ScheduledTask/call_respond.py` 模块 + 两张台账表（`qy_call_respond_task` 任务表、`qy_call_respond_send_log` 逐群发送日志表）。等级变化由 `stationProcessMin.calcmaxdataseg5min()` 末尾调用 `on_tick` 创建任务；确认走新建 HTTP API；发送用 `threading.Thread(daemon=True)` 后台执行，微信文件发送走可插拔契约 `utils/wechat_send_file.send_file`（当前为占位，内网代码后续接入替换）。群映射存 `call_respond_config.json`（可配置层，甲方后续给群名）。

**Tech Stack:** Python 3, SQLAlchemy ORM, FastAPI, pytest, pandas (已有), threading。

## Global Constraints

- **venv 用绝对路径**：`D:\PythonProject\haiheliuyubaoyuagent-master\.venv\Scripts\python.exe`（不是系统 `python`/`py`）。
- **不跨仓库 import 问答智能体模块**。
- **CSV/JSON 编码**：配置文件读写用 `encoding="utf-8"`。
- **所有叫应逻辑失败不阻塞主调度**：创建/检查/补发/发送异常均捕获记日志，与 `trigger_weather_bulletin_report` 的日志降级策略一致。
- **数据库异常**：沿用 `Session` 模式，rollback + 记日志。
- **状态常量**（任务状态机）：`pending`(待确认) / `confirmed`(已确认) / `sending`(发送中) / `sent`(已发送) / `pending_send`(待发送，群未配置) / `suspended`(挂起，报告缺失) / `failed`(发送失败)。
- **字段命名**：`report_docx_path`/`report_pdf_path` 存天河报告 docx/pdf 的 **URL（下载源）**，发送时下载到本地临时文件再发。
- **测试运行命令**：`cd hhlyqyxt-master && D:\PythonProject\haiheliuyubaoyuagent-master\.venv\Scripts\python.exe -m pytest ScheduledTask/tests/test_call_respond.py -v`

---

## 文件结构

- **Create** `Models/QyCallRespondTask.py` — 任务表 ORM
- **Create** `Models/QyCallRespondSendLog.py` — 逐群日志表 ORM
- **Create** `call_respond_config.json` — 群映射可配置层（默认空 groups）
- **Create** `utils/wechat_send_file.py` — `send_file` 可插拔契约（占位）
- **Create** `ScheduledTask/call_respond.py` — 核心模块（配置加载、话术渲染、on_tick、confirm_task、send_task、retry_pending_sends）
- **Modify** `Controller/tool_router.py` — 新增 4 个叫应 HTTP 接口
- **Modify** `ScheduledTask/stationProcessMin.py` — `calcmaxdataseg5min()` 末尾接入 `on_tick` + `retry_pending_sends`
- **Create** `ScheduledTask/tests/test_call_respond.py` — 单测

---

### Task 1: ORM 模型（两张表）

**Files:**
- Create: `Models/QyCallRespondTask.py`
- Create: `Models/QyCallRespondSendLog.py`
- Test: `ScheduledTask/tests/test_call_respond.py`（新建，仅含模型测试）

**Interfaces:**
- Produces: `QyCallRespondTask`（字段：id, emergency_monitor_id, response_level, datatime, impact_city, status, report_docx_path, report_pdf_path, confirm_person, confirm_time, send_time, create_time）、`QyCallRespondSendLog`（字段：id, task_id, target_group, status, detail, send_time）。

- [ ] **Step 1: 写模型测试**

```python
# ScheduledTask/tests/test_call_respond.py
"""应急响应叫应功能单元测试（无需真实数据库）。"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Models.QyCallRespondTask import QyCallRespondTask
from Models.QyCallRespondSendLog import QyCallRespondSendLog


def test_task_model_columns():
    t = QyCallRespondTask(
        emergency_monitor_id=1, response_level=2, datatime=None,
        impact_city="天津市", status="pending",
    )
    assert t.emergency_monitor_id == 1
    assert t.response_level == 2
    assert t.status == "pending"
    assert t.impact_city == "天津市"
    assert t.report_docx_path is None


def test_send_log_model_columns():
    log = QyCallRespondSendLog(task_id=1, target_group="天津市防汛值班群", status="success")
    assert log.task_id == 1
    assert log.target_group == "天津市防汛值班群"
    assert log.status == "success"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `D:\PythonProject\haiheliuyubaoyuagent-master\.venv\Scripts\python.exe -m pytest ScheduledTask/tests/test_call_respond.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'Models.QyCallRespondTask'`

- [ ] **Step 3: 写模型实现**

```python
# Models/QyCallRespondTask.py
from pydantic import ConfigDict
from sqlalchemy import Column, DateTime, Integer, SmallInteger, String, text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class QyCallRespondTask(Base):
    __tablename__ = 'qy_call_respond_task'

    id = Column(Integer, primary_key=True)
    emergency_monitor_id = Column(Integer)
    response_level = Column(SmallInteger, nullable=False, default=0)
    datatime = Column(DateTime)
    impact_city = Column(String(512))
    status = Column(String(20), nullable=False, default='pending')
    report_docx_path = Column(String(512))
    report_pdf_path = Column(String(512))
    confirm_person = Column(String(64))
    confirm_time = Column(DateTime)
    send_time = Column(DateTime)
    create_time = Column(DateTime, server_default=text('CURRENT_TIMESTAMP'))

    model_config = ConfigDict(from_attributes=True)

    def __repr__(self):
        return f"QyCallRespondTask(id={self.id}, status={self.status})"


"""
CREATE TABLE IF NOT EXISTS qy_call_respond_task (
    id SERIAL PRIMARY KEY,
    emergency_monitor_id INTEGER,
    response_level SMALLINT NOT NULL DEFAULT 0,
    datatime TIMESTAMP,
    impact_city VARCHAR(512),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    report_docx_path VARCHAR(512),
    report_pdf_path VARCHAR(512),
    confirm_person VARCHAR(64),
    confirm_time TIMESTAMP,
    send_time TIMESTAMP,
    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_qy_call_respond_task_status ON qy_call_respond_task(status);
CREATE INDEX IF NOT EXISTS idx_qy_call_respond_task_emergency_monitor_id ON qy_call_respond_task(emergency_monitor_id);
"""
```

```python
# Models/QyCallRespondSendLog.py
from pydantic import ConfigDict
from sqlalchemy import Column, DateTime, Integer, String, text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class QyCallRespondSendLog(Base):
    __tablename__ = 'qy_call_respond_send_log'

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, nullable=False)
    target_group = Column(String(128))
    status = Column(String(20), nullable=False, default='success')
    detail = Column(String(512))
    send_time = Column(DateTime, server_default=text('CURRENT_TIMESTAMP'))

    model_config = ConfigDict(from_attributes=True)

    def __repr__(self):
        return f"QyCallRespondSendLog(id={self.id}, task_id={self.task_id}, status={self.status})"


"""
CREATE TABLE IF NOT EXISTS qy_call_respond_send_log (
    id SERIAL PRIMARY KEY,
    task_id INTEGER NOT NULL,
    target_group VARCHAR(128),
    status VARCHAR(20) NOT NULL DEFAULT 'success',
    detail VARCHAR(512),
    send_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_qy_call_respond_send_log_task_id ON qy_call_respond_send_log(task_id);
"""
```

- [ ] **Step 4: 运行测试确认通过**

Run: `D:\PythonProject\haiheliuyubaoyuagent-master\.venv\Scripts\python.exe -m pytest ScheduledTask/tests/test_call_respond.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: 提交**

```bash
git add Models/QyCallRespondTask.py Models/QyCallRespondSendLog.py ScheduledTask/tests/test_call_respond.py
git commit -m "feat(call-respond): add ORM models for call-respond task and send log"
```

---

### Task 2: 群映射配置 + 话术渲染 + 目标群解析

**Files:**
- Create: `call_respond_config.json`
- Create: `ScheduledTask/call_respond.py`（仅含配置加载、渲染、目标解析函数）
- Test: `ScheduledTask/tests/test_call_respond.py`（追加）

**Interfaces:**
- Produces: `load_group_config(config_path=None) -> dict`（返回 `{"template": str, "groups": {city: [group,...]}}`，文件缺失/解析失败返回空配置）、`render_template(template, city, level) -> str`、`group_targets(impact_city, config) -> list[str]`（按顿号分隔城市，返回去重保序的群列表）。
- Consumes: 无。

- [ ] **Step 1: 写默认配置文件**

```json
{
  "template": "【叫应】{city} 已启动 {level} 级应急响应，请各单位迅速响应，报告见附件。",
  "groups": {}
}
```

- [ ] **Step 2: 写失败测试**

```python
# 追加到 ScheduledTask/tests/test_call_respond.py
from pathlib import Path
import json

from ScheduledTask import call_respond


def test_load_group_config_missing_file_returns_empty(tmp_path):
    cfg = call_respond.load_group_config(str(tmp_path / "nope.json"))
    assert cfg == {"template": "", "groups": {}}


def test_load_group_config_reads_file(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "template": "T{level}",
        "groups": {"天津市": ["A群", "B群"]},
    }, ensure_ascii=False), encoding="utf-8")
    cfg = call_respond.load_group_config(str(p))
    assert cfg["template"] == "T{level}"
    assert cfg["groups"] == {"天津市": ["A群", "B群"]}


def test_render_template_replaces_placeholders():
    out = call_respond.render_template("【叫应】{city} 已启动 {level} 级", "天津市", 2)
    assert out == "【叫应】天津市 已启动 2 级"


def test_group_targets_splits_by_dunhao():
    cfg = {"groups": {"天津市": ["A群"], "廊坊市": ["B群", "C群"]}}
    out = call_respond.group_targets("天津市、廊坊市", cfg)
    assert out == ["A群", "B群", "C群"]


def test_group_targets_empty_config_returns_empty():
    assert call_respond.group_targets("天津市", {"groups": {}}) == []
```

- [ ] **Step 3: 运行测试确认失败**

Run: `D:\PythonProject\haiheliuyubaoyuagent-master\.venv\Scripts\python.exe -m pytest ScheduledTask/tests/test_call_respond.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ScheduledTask.call_respond'`

- [ ] **Step 4: 写实现**

```python
# ScheduledTask/call_respond.py
"""应急响应叫应功能。

等级变化时创建叫应任务，值班人员人工确认后后台异步发送
叫应话术 + 天河报告文件到受影响单位微信群，并记录逐群发送台账。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import desc

from Models.QyCallRespondTask import QyCallRespondTask
from Models.QyCallRespondSendLog import QyCallRespondSendLog
from Models.QyEmergencyResponseMonitor import QyEmergencyResponseMonitor
from utils.db import Session
from utils.wechat_send_file import send_file

logger = logging.getLogger(__name__)

# 状态常量
STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_PENDING_SEND = "pending_send"
STATUS_SUSPENDED = "suspended"
STATUS_FAILED = "failed"

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "call_respond_config.json"


def load_group_config(config_path: Optional[str] = None) -> dict:
    """读取群映射配置，返回 {"template": str, "groups": {city: [group,...]}}。

    文件缺失或 JSON 解析失败时返回空配置，不抛异常（调用方降级处理）。
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("读取叫应配置失败（%s），按空配置处理", e)
        return {"template": "", "groups": {}}
    return {
        "template": cfg.get("template", ""),
        "groups": cfg.get("groups", {}),
    }


def render_template(template: str, city: str, level: int) -> str:
    """替换 {city}/{level} 占位符。"""
    return template.replace("{city}", city).replace("{level}", str(level))


def group_targets(impact_city: str, config: dict) -> list:
    """按受影响城市（顿号分隔）返回目标群列表，去重保序。"""
    groups = config.get("groups", {})
    result = []
    for city in (impact_city or "").split("、"):
        city = city.strip()
        if city:
            result.extend(groups.get(city, []))
    return list(dict.fromkeys(result))
```

- [ ] **Step 5: 运行测试确认通过**

Run: `D:\PythonProject\haiheliuyubaoyuagent-master\.venv\Scripts\python.exe -m pytest ScheduledTask/tests/test_call_respond.py -v`
Expected: PASS (6 passed)

- [ ] **Step 6: 提交**

```bash
git add call_respond_config.json ScheduledTask/call_respond.py ScheduledTask/tests/test_call_respond.py
git commit -m "feat(call-respond): add group config loader and template renderer"
```

---

### Task 3: send_file 可插拔契约（占位）

**Files:**
- Create: `utils/wechat_send_file.py`
- Test: `ScheduledTask/tests/test_call_respond.py`（追加）

**Interfaces:**
- Produces: `send_file(group: str, file_path: str, caption: str) -> bool`。当前为占位实现（记 warning 返回 False），内网代码拉入后替换函数体即可。
- Consumes: 无。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 ScheduledTask/tests/test_call_respond.py
from utils.wechat_send_file import send_file


def test_send_file_stub_returns_false():
    assert send_file("某群", "/tmp/r.docx", "话术") is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `D:\PythonProject\haiheliuyubaoyuagent-master\.venv\Scripts\python.exe -m pytest ScheduledTask/tests/test_call_respond.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'utils.wechat_send_file'`

- [ ] **Step 3: 写实现**

```python
# utils/wechat_send_file.py
"""微信发送文件到群（可插拔契约）。

当前为占位实现：内网文件发送能力已实现，后续将代码拉入项目后
替换本函数体即可，无需改动调用方。返回 bool 表示是否发送成功。
"""
import logging

logger = logging.getLogger(__name__)


def send_file(group: str, file_path: str, caption: str) -> bool:
    """发送文件到微信群。group=群名，file_path=本地文件路径，caption=附带话术。

    占位实现：记 warning 并返回 False。接入内网实现后返回真实发送结果。
    """
    logger.warning(
        "send_file 未实现（占位）：group=%s file=%s caption=%s",
        group, file_path, caption,
    )
    return False
```

- [ ] **Step 4: 运行测试确认通过**

Run: `D:\PythonProject\haiheliuyubaoyuagent-master\.venv\Scripts\python.exe -m pytest ScheduledTask/tests/test_call_respond.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: 提交**

```bash
git add utils/wechat_send_file.py ScheduledTask/tests/test_call_respond.py
git commit -m "feat(call-respond): add send_file pluggable contract stub"
```

---

### Task 4: 等级变化检测 + on_tick 创建任务

**Files:**
- Modify: `ScheduledTask/call_respond.py`
- Test: `ScheduledTask/tests/test_call_respond.py`（追加）

**Interfaces:**
- Consumes: `QyCallRespondTask`、`QyEmergencyResponseMonitor`、`Session`。
- Produces: `on_tick(record, impact_city, config=None) -> Optional[QyCallRespondTask]`（等级变化且 ≥1 级时创建 `pending` 任务并返回；否则返回 None）、`_previous_response_level(datatime) -> int`（返回前一条应急响应记录等级，无则 0）。

**说明：** 等级变化判定 = 当前 `record.response_level` 与 `qy_emergency_response_monitor` 表中按 `datatime` 排序的前一条记录等级比较，不同且当前 ≥1 则创建。

- [ ] **Step 1: 写失败测试（用 monkeypatch 的假 Session）**

```python
# 追加到 ScheduledTask/tests/test_call_respond.py
from datetime import datetime


class _FakeTask:
    def __init__(self, **kw):
        self.id = kw.get("id")
        self.emergency_monitor_id = kw.get("emergency_monitor_id")
        self.response_level = kw.get("response_level")
        self.datatime = kw.get("datatime")
        self.impact_city = kw.get("impact_city")
        self.status = kw.get("status")
        self.report_docx_path = kw.get("report_docx_path")
        self.report_pdf_path = kw.get("report_pdf_path")


class _FakeQuery:
    def __init__(self, session, model):
        self._session = session
        self._model = model
        self._filters = []
        self._order = []

    def filter(self, *args):
        self._filters.append(args)
        return self

    def order_by(self, *args):
        self._order = args
        return self

    def first(self):
        # 返回前一条（None 表示无前一条）
        return self._session.prev_level_obj

    def all(self):
        return self._session.all_rows


class _FakeSession:
    def __init__(self):
        self.added = []
        self.prev_level_obj = None
        self.all_rows = []

    def query(self, model):
        return _FakeQuery(self, model)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def fake_session(monkeypatch):
    sess = _FakeSession()
    monkeypatch.setattr(call_respond, "Session", lambda: sess)
    return sess


def _rec(level=2, datatime="2026-08-04 10:00:00", rid=1, docx="http://x/a.docx"):
    return _FakeTask(
        id=rid, emergency_monitor_id=rid, response_level=level,
        datatime=datetime.strptime(datatime, "%Y-%m-%d %H:%M:%S"),
        impact_city="天津市", status="pending", report_docx_path=docx,
    )


def test_on_tick_creates_task_on_level_change(fake_session):
    fake_session.prev_level_obj = _rec(level=0)  # 前一条 0 级
    task = call_respond.on_tick(_rec(level=2), "天津市")
    assert task is not None
    assert task.status == "pending"
    assert len(fake_session.added) == 1


def test_on_tick_skips_when_same_level(fake_session):
    fake_session.prev_level_obj = _rec(level=2)  # 前一条同等级
    task = call_respond.on_tick(_rec(level=2), "天津市")
    assert task is None
    assert fake_session.added == []


def test_on_tick_skips_when_record_none(fake_session):
    assert call_respond.on_tick(None, "天津市") is None
    assert fake_session.added == []


def test_on_tick_skips_when_level_zero(fake_session):
    fake_session.prev_level_obj = _rec(level=0)
    task = call_respond.on_tick(_rec(level=0), "天津市")
    assert task is None
    assert fake_session.added == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `D:\PythonProject\haiheliuyubaoyuagent-master\.venv\Scripts\python.exe -m pytest ScheduledTask/tests/test_call_respond.py -v`
Expected: FAIL — `AttributeError: module 'ScheduledTask.call_respond' has no attribute 'on_tick'`

- [ ] **Step 3: 写实现**

```python
# 追加到 ScheduledTask/call_respond.py

def _previous_response_level(datatime) -> int:
    """返回 qy_emergency_response_monitor 中 datatime 之前最近一条的等级，无则 0。"""
    session = Session()
    try:
        prev = (
            session.query(QyEmergencyResponseMonitor)
            .filter(QyEmergencyResponseMonitor.datatime < datatime)
            .order_by(desc(QyEmergencyResponseMonitor.datatime))
            .first()
        )
        return prev.response_level if prev is not None else 0
    finally:
        session.close()


def on_tick(record, impact_city: str, config: Optional[dict] = None) -> Optional[QyCallRespondTask]:
    """等级变化时创建叫应任务。

    record 为 None 或 0 级时不创建。等级变化判定：当前等级与前一条
    应急响应记录等级不同且当前 ≥1 时创建 pending 任务。
    """
    if record is None:
        return None
    level = int(record.response_level)
    if level < 1:
        return None
    prev_level = _previous_response_level(record.datatime)
    if prev_level == level:
        return None  # 同等级持续不重复创建

    task = QyCallRespondTask(
        emergency_monitor_id=record.id,
        response_level=level,
        datatime=record.datatime,
        impact_city=impact_city,
        status=STATUS_PENDING,
        report_docx_path=getattr(record, "report_docx_url", None),
        report_pdf_path=getattr(record, "report_pdf_url", None),
    )
    session = Session()
    try:
        session.add(task)
        session.commit()
        session.refresh(task)
        logger.info("等级变化创建叫应任务 id=%s level=%d impact=%s", task.id, level, impact_city)
        return task
    except Exception:
        session.rollback()
        logger.warning("创建叫应任务失败（不阻塞主流程）", exc_info=True)
        return None
    finally:
        session.close()
```

**说明：** `on_tick` 用 `record.report_docx_url`/`report_pdf_url`（应急响应记录上的报告 URL 字段）作为任务上 `report_docx_path`/`report_pdf_path` 的值（下载源）。测试中 `_rec` 的 `report_docx_path` 键即对应此。`QyCallRespondTask` 由真实 ORM 实例化，`fake_session.add` 会捕获它；`session.refresh` 在假 Session 上不存在——需在假 Session 补 `refresh` 方法。

- [ ] **Step 4: 补假 Session.refresh 并运行测试**

给 `_FakeSession` 增加：

```python
    def refresh(self, obj):
        if obj.id is None:
            obj.id = 1
```

Run: `D:\PythonProject\haiheliuyubaoyuagent-master\.venv\Scripts\python.exe -m pytest ScheduledTask/tests/test_call_respond.py -v`
Expected: PASS (11 passed)

- [ ] **Step 5: 提交**

```bash
git add ScheduledTask/call_respond.py ScheduledTask/tests/test_call_respond.py
git commit -m "feat(call-respond): create task on emergency response level change"
```

---

### Task 5: 确认 + 后台发送 + 挂起补发

**Files:**
- Modify: `ScheduledTask/call_respond.py`
- Test: `ScheduledTask/tests/test_call_respond.py`（追加）

**Interfaces:**
- Consumes: `load_group_config`、`render_template`、`group_targets`、`send_file`、`QyCallRespondTask`、`QyCallRespondSendLog`、`Session`。
- Produces: `confirm_task(task_id, confirm_person) -> dict`（校验 pending、置 confirmed、起后台线程）、`send_task(task_id) -> None`（后台：报告缺失→suspended；群未配置→pending_send；否则逐群发送→sent/failed）、`retry_pending_sends() -> None`（扫描 suspended/pending_send 起线程补发）、`_write_send_log(task_id, group, status, detail) -> None`、`_download_report(url) -> str`（下载报告 URL 到本地临时文件返回路径）。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 ScheduledTask/tests/test_call_respond.py
import threading


def test_confirm_task_requires_pending(fake_session):
    fake_session.all_rows = [_rec(level=2, rid=7)]
    result = call_respond.confirm_task(7, "张三")
    assert result["success"] is True
    assert result["status"] == "confirmed"


def test_send_task_suspended_when_no_report(fake_session, monkeypatch):
    monkeypatch.setattr(call_respond, "load_group_config", lambda: {"groups": {"天津市": ["A群"]}})
    task = _rec(level=2, rid=8, docx=None)  # 无报告
    fake_session.all_rows = [task]
    call_respond.send_task(8)
    assert task.status == "suspended"


def test_send_task_pending_send_when_no_group(fake_session, monkeypatch):
    monkeypatch.setattr(call_respond, "load_group_config", lambda: {"groups": {}})
    task = _rec(level=2, rid=9, docx="http://x/a.docx")
    fake_session.all_rows = [task]
    call_respond.send_task(9)
    assert task.status == "pending_send"


def test_send_task_sent_when_ok(fake_session, monkeypatch):
    monkeypatch.setattr(call_respond, "load_group_config", lambda: {"groups": {"天津市": ["A群"]}})
    monkeypatch.setattr(call_respond, "send_file", lambda g, f, c: True)
    monkeypatch.setattr(call_respond, "_download_report", lambda url: "/tmp/a.docx")
    task = _rec(level=2, rid=10, docx="http://x/a.docx")
    fake_session.all_rows = [task]
    fake_session.logs = []
    call_respond.send_task(10)
    assert task.status == "sent"
```

**说明：** `_FakeSession` 需支持 `add` 记录 send_log（追加到 `fake_session.logs`）与 `query(...).all()` 返回 `all_rows`。`_FakeQuery.all()` 已返回 `self._session.all_rows`。确认函数里 `query().filter().first()` 需返回 `all_rows[0]`——需让 `_FakeQuery.first()` 在 `all_rows` 非空时返回第一个。

- [ ] **Step 2: 修正假 Session 的 first() 语义并运行测试确认失败**

将 `_FakeQuery.first()` 改为：

```python
    def first(self):
        if self._session.prev_level_obj is not None:
            return self._session.prev_level_obj
        return self._session.all_rows[0] if self._session.all_rows else None
```

Run: `D:\PythonProject\haiheliuyubaoyuagent-master\.venv\Scripts\python.exe -m pytest ScheduledTask/tests/test_call_respond.py -v`
Expected: FAIL — `AttributeError: module 'ScheduledTask.call_respond' has no attribute 'confirm_task'`

- [ ] **Step 3: 写实现**

```python
# 追加到 ScheduledTask/call_respond.py

def _write_send_log(task_id: int, group: str, status: str, detail: str = "") -> None:
    """写入一条逐群发送日志，失败不抛出。"""
    log = QyCallRespondSendLog(task_id=task_id, target_group=group, status=status, detail=detail)
    session = Session()
    try:
        session.add(log)
        session.commit()
    except Exception:
        session.rollback()
        logger.warning("写入叫应发送日志失败：task=%s group=%s", task_id, group, exc_info=True)
    finally:
        session.close()


def _download_report(url: str) -> str:
    """下载报告 URL 到本地临时文件，返回路径。失败抛异常由调用方处理。"""
    import requests
    import tempfile
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    suffix = Path(url).suffix or ".docx"
    fd, path = tempfile.mkstemp(suffix=suffix)
    with open(fd, "wb") as f:
        f.write(resp.content)
    return path


def _send_to_group(task, group: str, config: dict) -> bool:
    """向单个群发送报告文件 + 话术，写 send_log，返回是否成功。"""
    caption = render_template(config.get("template", ""), task.impact_city, task.response_level)
    try:
        file_path = _download_report(task.report_docx_path)
        ok = send_file(group, file_path, caption)
        _write_send_log(task.id, group, "success" if ok else "failed",
                        detail="" if ok else "send_file 返回 False")
        return ok
    except Exception as e:
        _write_send_log(task.id, group, "failed", detail=str(e)[:500])
        return False


def send_task(task_id: int) -> None:
    """后台发送。读任务+配置，按状态分支：
    报告缺失→suspended；群未配置→pending_send；否则逐群发送→sent/failed。
    """
    session = Session()
    try:
        task = session.query(QyCallRespondTask).filter(QyCallRespondTask.id == task_id).first()
        if task is None:
            return
        config = load_group_config()
        if not task.report_docx_path and not task.report_pdf_path:
            task.status = STATUS_SUSPENDED
            session.commit()
            return
        targets = group_targets(task.impact_city, config)
        if not targets:
            task.status = STATUS_PENDING_SEND
            session.commit()
            return
        task.status = STATUS_SENDING
        session.commit()
        all_ok = True
        for group in targets:
            if not _send_to_group(task, group, config):
                all_ok = False
        task.status = STATUS_SENT if all_ok else STATUS_FAILED
        task.send_time = datetime.now()
        session.commit()
    except Exception:
        logger.warning("发送叫应任务失败：task=%s", task_id, exc_info=True)
    finally:
        session.close()


def confirm_task(task_id: int, confirm_person: str) -> dict:
    """人工确认。校验任务存在且 pending，置 confirmed，起后台线程发送。"""
    session = Session()
    try:
        task = session.query(QyCallRespondTask).filter(QyCallRespondTask.id == task_id).first()
        if task is None:
            return {"success": False, "detail": "任务不存在"}
        if task.status != STATUS_PENDING:
            return {"success": False, "detail": f"任务状态为 {task.status}，不可确认"}
        task.status = STATUS_CONFIRMED
        task.confirm_person = confirm_person
        task.confirm_time = datetime.now()
        session.commit()
        tid = task.id
    finally:
        session.close()
    threading.Thread(target=send_task, args=(tid,), daemon=True).start()
    return {"success": True, "task_id": tid, "status": STATUS_CONFIRMED}


def retry_pending_sends() -> None:
    """扫描 suspended/pending_send 任务，起后台线程补发（条件满足则发送，否则重新挂起）。"""
    session = Session()
    try:
        tasks = (
            session.query(QyCallRespondTask)
            .filter(QyCallRespondTask.status.in_([STATUS_SUSPENDED, STATUS_PENDING_SEND]))
            .all()
        )
        ids = [t.id for t in tasks]
    finally:
        session.close()
    for tid in ids:
        threading.Thread(target=send_task, args=(tid,), daemon=True).start()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `D:\PythonProject\haiheliuyubaoyuagent-master\.venv\Scripts\python.exe -m pytest ScheduledTask/tests/test_call_respond.py -v`
Expected: PASS (15 passed)

**注意：** `send_task`/`confirm_task` 内部起的 `threading.Thread` 在测试中会异步执行，可能造成 flaky。测试中 `send_task` 直接同步调用（不走线程），故稳定；`confirm_task` 起的线程调用 `send_task`，其行为在 `test_confirm_task_requires_pending` 中只断言返回字典，不等待线程，可接受。

- [ ] **Step 5: 提交**

```bash
git add ScheduledTask/call_respond.py ScheduledTask/tests/test_call_respond.py
git commit -m "feat(call-respond): confirm task and async background send with suspend/retry"
```

---

### Task 6: HTTP API 接口

**Files:**
- Modify: `Controller/tool_router.py`
- Test: `ScheduledTask/tests/test_call_respond.py`（追加，用 FastAPI TestClient 或直接调函数）

**Interfaces:**
- Consumes: `call_respond.confirm_task`、`call_respond.send_task`、`QyCallRespondTask`、`QyCallRespondSendLog`。
- Produces: 4 个端点——`GET /tool/call-respond/tasks`、`POST /tool/call-respond/{task_id}/confirm`、`GET /tool/call-respond/{task_id}/logs`、`POST /tool/call-respond/{task_id}/retry`。

- [ ] **Step 1: 写失败测试（直接测路由处理函数）**

```python
# 追加到 ScheduledTask/tests/test_call_respond.py
from fastapi import HTTPException
from Controller.tool_router import (
    list_call_respond_tasks, confirm_call_respond, get_call_respond_logs,
    retry_call_respond, CallRespondConfirmRequest,
)


def test_confirm_endpoint_calls_confirm_task(fake_session, monkeypatch):
    monkeypatch.setattr(call_respond, "confirm_task", lambda tid, person: {"success": True, "task_id": tid, "status": "confirmed"})
    result = confirm_call_respond(7, CallRespondConfirmRequest(confirm_person="张三"))
    assert result["success"] is True


def test_confirm_endpoint_raises_when_not_found(fake_session, monkeypatch):
    monkeypatch.setattr(call_respond, "confirm_task", lambda tid, person: {"success": False, "detail": "任务不存在"})
    import pytest as _pytest
    with _pytest.raises(HTTPException) as ei:
        confirm_call_respond(999, CallRespondConfirmRequest(confirm_person="张三"))
    assert ei.value.status_code == 404


def test_list_tasks_returns_list(fake_session):
    fake_session.all_rows = [_rec(level=2, rid=3)]
    rows = list_call_respond_tasks()
    assert isinstance(rows, list)


def test_logs_endpoint_returns_list(fake_session):
    fake_session.all_rows = []
    rows = get_call_respond_logs(1)
    assert isinstance(rows, list)


def test_retry_endpoint_returns_success(fake_session, monkeypatch):
    monkeypatch.setattr(call_respond, "send_task", lambda tid: None)
    result = retry_call_respond(5)
    assert result["success"] is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `D:\PythonProject\haiheliuyubaoyuagent-master\.venv\Scripts\python.exe -m pytest ScheduledTask/tests/test_call_respond.py -v`
Expected: FAIL — `ImportError: cannot import name 'list_call_respond_tasks' from 'Controller.tool_router'`

- [ ] **Step 3: 写实现（追加到 tool_router.py 末尾）**

```python
# 追加到 Controller/tool_router.py 末尾
from pydantic import BaseModel, Field
from fastapi import HTTPException
from sqlalchemy import desc
from threading import Thread

from Models.QyCallRespondTask import QyCallRespondTask
from Models.QyCallRespondSendLog import QyCallRespondSendLog
from ScheduledTask import call_respond


class CallRespondConfirmRequest(BaseModel):
    confirm_person: str = Field(..., description="确认人姓名")


def _serialize_task(t) -> dict:
    return {
        "id": t.id,
        "emergency_monitor_id": t.emergency_monitor_id,
        "response_level": t.response_level,
        "datatime": _format_datetime(t.datatime),
        "impact_city": t.impact_city,
        "status": t.status,
        "report_docx_path": t.report_docx_path,
        "report_pdf_path": t.report_pdf_path,
        "confirm_person": t.confirm_person,
        "confirm_time": _format_datetime(t.confirm_time),
        "send_time": _format_datetime(t.send_time),
        "create_time": _format_datetime(t.create_time),
    }


@toolrouter.get("/call-respond/tasks")
def list_call_respond_tasks(status: Optional[str] = None, limit: int = 50):
    """按状态查询叫应任务列表。"""
    limit = max(1, min(limit, 200))
    session = Session()
    try:
        q = session.query(QyCallRespondTask)
        if status:
            q = q.filter(QyCallRespondTask.status == status)
        rows = q.order_by(desc(QyCallRespondTask.id)).limit(limit).all()
        return [_serialize_task(r) for r in rows]
    finally:
        session.close()


@toolrouter.post("/call-respond/{task_id}/confirm")
def confirm_call_respond(task_id: int, req: CallRespondConfirmRequest):
    """人工确认叫应任务并触发后台发送。"""
    result = call_respond.confirm_task(task_id, req.confirm_person)
    if not result["success"]:
        raise HTTPException(status_code=404, detail=result["detail"])
    return result


@toolrouter.get("/call-respond/{task_id}/logs")
def get_call_respond_logs(task_id: int):
    """查询某任务逐群发送日志。"""
    session = Session()
    try:
        rows = (
            session.query(QyCallRespondSendLog)
            .filter(QyCallRespondSendLog.task_id == task_id)
            .order_by(desc(QyCallRespondSendLog.id))
            .all()
        )
        return [
            {
                "id": r.id,
                "task_id": r.task_id,
                "target_group": r.target_group,
                "status": r.status,
                "detail": r.detail,
                "send_time": _format_datetime(r.send_time),
            }
            for r in rows
        ]
    finally:
        session.close()


@toolrouter.post("/call-respond/{task_id}/retry")
def retry_call_respond(task_id: int):
    """手动重试发送（补发挂起任务）。"""
    Thread(target=call_respond.send_task, args=(task_id,), daemon=True).start()
    return {"success": True, "task_id": task_id}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `D:\PythonProject\haiheliuyubaoyuagent-master\.venv\Scripts\python.exe -m pytest ScheduledTask/tests/test_call_respond.py -v`
Expected: PASS (20 passed)

- [ ] **Step 5: 提交**

```bash
git add Controller/tool_router.py ScheduledTask/tests/test_call_respond.py
git commit -m "feat(call-respond): add HTTP API endpoints for tasks, confirm, logs, retry"
```

---

### Task 7: 调度器集成

**Files:**
- Modify: `ScheduledTask/stationProcessMin.py`（`calcmaxdataseg5min` 末尾，`report_docx_url`/`report_pdf_url` 回写块之后，约 664 行处）
- Test: 复用现有全量测试（无新增单测，集成靠模块单测 + 现有套件通过）

**Interfaces:**
- Consumes: `call_respond.on_tick(record, impact_city)`、`call_respond.retry_pending_sends()`。
- Produces: 无（副作用：等级变化创建任务；挂起任务补发）。

- [ ] **Step 1: 读现有代码确认插入点**

读 `calcmaxdataseg5min()` 中 `run_emergency_response_monitor` 调用与其后报告 URL 回写块（约 626-664 行），确认 `impact_city` 局部变量在作用域内、`record` 变量名。

- [ ] **Step 2: 写集成代码**

在 `calcmaxdataseg5min()` 末尾报告 URL 回写块之后追加：

```python
    # 叫应：等级变化时创建叫应任务，并扫描挂起任务补发
    try:
        call_respond.on_tick(record, impact_city)
        call_respond.retry_pending_sends()
    except Exception as e:
        logger.warning("叫应流程执行失败（不阻塞主流程）：%s", e, exc_info=True)
```

并在文件顶部 import 区（`from ScheduledTask.report_generator import trigger_weather_bulletin_report` 之后）追加：

```python
from ScheduledTask import call_respond
```

- [ ] **Step 3: 运行全量测试确认无回归、导入正常**

Run: `D:\PythonProject\haiheliuyubaoyuagent-master\.venv\Scripts\python.exe -m pytest ScheduledTask/tests/ utils/tests/ -q`
Expected: PASS（原 104 + 新增 20 = 124 passed），无 import 错误。

- [ ] **Step 4: 提交**

```bash
git add ScheduledTask/stationProcessMin.py
git commit -m "feat(call-respond): integrate call-respond on_tick and retry into scheduler"
```

---

### Task 8: 全量测试回归 + 收尾

**Files:**
- 无新增文件。运行全量测试。

- [ ] **Step 1: 运行完整测试套件**

Run: `D:\PythonProject\haiheliuyubaoyuagent-master\.venv\Scripts\python.exe -m pytest ScheduledTask/tests/ utils/tests/ -q`
Expected: 全部通过（原 104 + 新增 20 = 124 passed）。

- [ ] **Step 2: 代码嗅探（复用现有配置目录）**

确认 `call_respond_config.json` 在仓库根（`hhlyqyxt-master/`）下，`DEFAULT_CONFIG_PATH` 解析正确。

- [ ] **Step 3: 提交收尾**

```bash
git status
```

确认无未提交改动后进入 code-review 流程。

---

## Self-Review

**1. Spec coverage：**
- §3 数据模型（两张表）→ Task 1 ✓
- §5 模块设计（on_tick/retry_pending_sends/confirm_task/send_task/render_template/load_group_config）→ Task 2/4/5 ✓
- §6 调度器集成（calcmaxdataseg5min 末尾 on_tick + retry_pending_sends）→ Task 7 ✓
- §7 对外接口（4 个端点）→ Task 6 ✓
- §8 群映射配置（call_respond_config.json 可配置层）→ Task 2 ✓
- §9 错误处理（不阻塞主流程、逐群 try/except、报告缺失 suspended、群未配置 pending_send）→ Task 5/7 ✓
- §10 后台线程（daemon Thread、独立 Session）→ Task 5 ✓
- §11 测试策略（等级变化/状态机/确认接口/报告缺失/群未配置/发送执行/话术渲染）→ Task 4/5/6 覆盖 ✓
- §12 验证清单 → Task 8 手工验证 ✓

**2. Placeholder scan：** 所有步骤含具体代码与预期输出，无 "TBD/TODO/implement later"。`send_file` 为明确契约占位（记 warning 返回 False），非占位符。

**3. Type consistency：** `on_tick(record, impact_city)`、`confirm_task(task_id, confirm_person) -> dict`、`send_task(task_id) -> None`、`retry_pending_sends() -> None`、`load_group_config() -> dict`、`render_template(template, city, level) -> str`、`group_targets(impact_city, config) -> list`、`send_file(group, file_path, caption) -> bool` 在 Task 2-7 中签名一致。状态常量 `STATUS_*` 各任务统一引用。`report_docx_path`/`report_pdf_path` 存报告 URL（下载源），`on_tick` 从 `record.report_docx_url`/`report_pdf_url` 取值，`send_task` 里 `_download_report` 下载——一致。