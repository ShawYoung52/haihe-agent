# 应急响应叫应功能设计文档

## 1. 目标与范围

在牵引智能体侧，为应急响应新增"叫应"机制：当应急响应等级发生变化（0→I-IV 或升级）时，创建叫应任务；值班人员**人工确认**后，自动向受影响单位对应的微信群发送**叫应话术模板 + 天河报告文件**，并记录完整叫应台账。

**本系统只做后端 API/工具与自动化发送**，前端页面由前端团队后续实现。微信群映射由甲方后续提供，做成**可配置层**，未配置时任务挂起待发。

**核心流程（人工确认在环）**：

```
应急响应等级变化 → 创建叫应任务(pending) → 值班人员人工确认 → 后台异步发送
  （叫应话术 + 报告文件）到受影响单位对应群 → 记录逐群发送台账
```

## 2. 关键决策（业务口径）

| 维度 | 决策 |
|---|---|
| 触发时机 | 应急响应**等级变化时**（0→I-IV 或升级）创建任务；同等级持续不重复创建 |
| 人工确认 | 后端 API，由值班人员调用；前端页面后续实现 |
| 发送执行 | **后台异步线程**，不阻塞 5 分钟调度器 240s 预算 |
| 发送内容 | 叫应话术模板 + 天河报告**文件**（直接发文件，不发链接） |
| 群映射 | 可配置层（JSON 配置文件），后续甲方告知后直接改文件 |
| 群未配置 | 任务标记 `pending_send` 挂起，配置后自动补发 |
| 报告缺失 | 任务仍创建，确认后 `suspended` 挂起，报告补成功后自动补发 |
| 文件发送 | 微信自动化发文件能力已在内网实现，后续拉入项目，本设计定义 `send_file` 可插拔契约 |
| 台账 | 记录任务、确认人、确认时间、发送目标、逐群发送结果 |

## 3. 数据模型

### 3.1 表 `qy_call_respond_task`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | SERIAL PRIMARY KEY | 主键 |
| emergency_monitor_id | INTEGER | 关联 `qy_emergency_response_monitor.id` |
| response_level | SMALLINT | 本次叫应对应的应急等级（1-4） |
| datatime | TIMESTAMP | 本次应急响应结束时间 |
| impact_city | VARCHAR(512) | 受影响城市（快照，来自 `qy_minute_monitor`） |
| status | VARCHAR(20) | 状态机（见 §4） |
| report_docx_path | VARCHAR(512) | 报告本地路径（发送文件用），空=报告缺失 |
| report_pdf_path | VARCHAR(512) | 同上 |
| confirm_person | VARCHAR(64) | 确认人（前端传入） |
| confirm_time | TIMESTAMP | 确认时间 |
| send_time | TIMESTAMP | 首次发送完成时间 |
| create_time | TIMESTAMP DEFAULT now() | 创建时间 |

### 3.2 表 `qy_call_respond_send_log`（逐群发送结果台账）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | SERIAL PRIMARY KEY | 主键 |
| task_id | INTEGER | 关联任务 |
| target_group | VARCHAR(128) | 目标群名 |
| status | VARCHAR(20) | `success` / `failed` / `skipped` |
| detail | VARCHAR(512) | 失败原因等 |
| send_time | TIMESTAMP | 该群发送时间 |

### 3.3 建表 SQL

```sql
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

CREATE INDEX IF NOT EXISTS idx_qy_call_respond_task_status
    ON qy_call_respond_task(status);
CREATE INDEX IF NOT EXISTS idx_qy_call_respond_task_emergency_monitor_id
    ON qy_call_respond_task(emergency_monitor_id);

CREATE TABLE IF NOT EXISTS qy_call_respond_send_log (
    id SERIAL PRIMARY KEY,
    task_id INTEGER NOT NULL,
    target_group VARCHAR(128),
    status VARCHAR(20) NOT NULL DEFAULT 'success',
    detail VARCHAR(512),
    send_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_qy_call_respond_send_log_task_id
    ON qy_call_respond_send_log(task_id);

-- 迁移（若表已存在）：
ALTER TABLE qy_call_respond_task ADD COLUMN IF NOT EXISTS report_docx_path VARCHAR(512);
ALTER TABLE qy_call_respond_task ADD COLUMN IF NOT EXISTS report_pdf_path VARCHAR(512);
```

## 4. 状态机

```
pending(待确认) ──值班确认──▶ confirmed(已确认)
                                  │
                  ┌───────────────┼────────────────┐
                  ▼               ▼                ▼
            报告缺失+群已配   报告齐+群未配      报告齐+群已配
                  │               │                │
            suspended(挂起)  pending_send(待发送)  sending(发送中)
                  │               │                │
                  └──自动补发──────┴──────────────▶ sent(已发送) / failed(发送失败)
```

- **`pending`**：等级变化创建，等待确认
- **`confirmed`**：值班确认，起后台发送线程
- **`sending`**：发送线程运行中
- **`sent`**：所有目标群发送完成（逐群结果在 send_log）
- **`pending_send`**：群映射未配置，挂起等配置
- **`suspended`**：报告缺失，挂起等报告
- **`failed`**：发送失败（需人工处理）

每 5 分钟 tick 扫描 `pending_send`/`suspended`，条件满足即自动补发；报告回填成功时也触发检查。

## 5. 模块设计

新建 `ScheduledTask/call_respond.py`，职责单一：

- `on_tick(record, impact_city)`：等级变化判定 → 创建任务；报告回填 → 检查挂起任务自动补发
- `retry_pending_sends()`：扫描 `pending_send`/`suspended`，条件满足即补发
- `confirm_task(task_id, confirm_person)`：人工确认，起后台线程
- `send_task(task_id)`：后台线程执行发送（下载报告 → 逐群 `send_file` → 写 send_log）
- `render_template(template, city, level)`：话术占位符渲染
- `load_group_config()`：读取群映射配置

新增模型：
- `Models/QyCallRespondTask.py`（表 `qy_call_respond_task`）
- `Models/QyCallRespondSendLog.py`（表 `qy_call_respond_send_log`）

## 6. 调度器集成

在 `ScheduledTask/stationProcessMin.py` 的 `calcmaxdataseg5min()` 末尾，报告回填之后新增：

```python
from ScheduledTask import call_respond
...
# 报告 URL 回填（现有逻辑）之后：
call_respond.on_tick(record, impact_city)   # 等级变化→创建任务；报告回填→检查挂起任务补发
call_respond.retry_pending_sends()          # 扫描 pending_send/suspended，条件满足即补发
```

- `record` 为 `None` 或 0 级时：`on_tick` 不创建任务（等级变化才建）
- **等级变化判定**：取当前 `record.response_level`，与 `qy_emergency_response_monitor` 表中按 `datatime` 排序的**前一条**记录的 `response_level` 比较。若两者不同且当前等级 ≥ 1（即 0→I-IV 或升级），则创建任务；若一致（同等级持续）则不创建。
- `impact_city` 从 `qy_minute_monitor` 快照传入（避免实时查）

## 7. 对外接口（`tool_router.py` 新增）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/tool/call-respond/tasks?status=pending` | 查任务列表（可按状态筛） |
| POST | `/tool/call-respond/{id}/confirm` | 确认 + 起后台发送 |
| GET | `/tool/call-respond/{id}/logs` | 查某任务逐群发送日志 |
| POST | `/tool/call-respond/{id}/retry` | 手动重试发送（补发后） |

### 确认流程

```
POST /tool/call-respond/{task_id}/confirm
  body: {"confirm_person": "张三"}
  → 校验任务存在且 status=pending
  → 更新 confirm_person/confirm_time，status=confirmed
  → 起后台线程 send_task(task_id)（不阻塞接口）
  → 立即返回 {task_id, status:"confirmed"}
```

### 发送流程（后台线程 `send_task`）

```
1. 读任务 + 群映射配置
2. 若报告缺失 → status=suspended，返回（等报告）
3. 若群映射未配置 → status=pending_send，返回（等配置）
4. 否则 status=sending
5. 对每个目标群：下载报告文件 → send_file(群, 文件, 话术) → 写 send_log
6. 全部完成 → status=sent（或 failed）
```

## 8. 群映射配置（可配置层）

配置文件 `call_respond_config.json`（JSON，后续甲方给群名直接改文件）：

```json
{
  "template": "【叫应】{city} 已启动 {level} 级应急响应，请各单位迅速响应，报告见附件。",
  "groups": {
    "天津市": ["天津市防汛值班群", "海河防汛会商群"],
    "廊坊市": ["廊坊防汛值班群"]
  }
}
```

- 未配置/配置为空 → 视为"群未配置"，任务标记 `pending_send`
- 发送文件接口 `send_file(group, file_path, caption)` 定义为**可插拔契约**，等内网代码拉入后对接实现

## 9. 错误处理

| 场景 | 行为 |
|---|---|
| 报告缺失（创建任务时） | 任务 `pending` 创建，确认后 `suspended` 挂起，等报告回填自动补发 |
| 群映射未配置 | 确认后 `pending_send` 挂起，等配置后自动补发 |
| 报告文件下载失败 | 该群 send_log 记 `failed` + detail，任务记 `failed`（需人工处理） |
| `send_file` 异常 | 逐群 try/except，单个群失败不影响其他群；全部失败 → 任务 `failed` |
| 数据库写入异常 | 沿用现有 `Session` 模式，rollback + 记日志，不阻塞主调度 |
| 微信自动化超时/失败 | 由 `send_file` 契约捕获并返回布尔，`send_log` 记录 |

**关键约束**：所有叫应逻辑（创建/检查/补发）**失败不阻塞主调度流程**，与现有 `trigger_weather_bulletin_report` 的日志降级策略一致。

## 10. 后台线程管理

- 用 `threading.Thread(daemon=True)` 处理发送，避免阻塞调度器
- 线程内独立 `Session`（与主线程隔离）
- 发送结果写库后线程退出；不等待线程（daemon 随进程退出）

## 11. 测试策略

新增 `ScheduledTask/tests/test_call_respond.py`，复用现有 pytest 模式：

| 测试 | 覆盖 |
|---|---|
| 等级变化创建任务 | 0→2 级创建；2→2 级不重复创建 |
| 状态机流转 | pending→confirmed→sending→sent；挂起分支 |
| 确认接口 | 校验 pending 才可确认；重复确认报错 |
| 报告缺失 | 确认后进 `suspended`；报告回填后自动补发 |
| 群未配置 | 确认后进 `pending_send`；配置后自动补发 |
| 发送执行 | 逐群 send_log 记录；单群失败不影响其他 |
| 话术渲染 | `{city}`/`{level}` 占位符替换 |

发送用 `send_file` 打桩（mock），不依赖真实微信/内网。

## 12. 验证清单

- `qy_call_respond_task` 在等级变化时生成新记录，同等级不重复
- 确认接口返回 `confirmed`，后台开始发送
- 群未配置时任务为 `pending_send` 且不报错
- 报告缺失时任务为 `suspended`，报告回填后自动补发
- `qy_call_respond_send_log` 逐群记录发送结果
- 发送失败不阻塞主调度流程