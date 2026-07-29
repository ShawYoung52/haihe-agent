# 牵引应急响应 · 接入天河报告接口设计

- **状态**：草案（brainstorming 已完成，待用户 review）
- **日期**：2026-07-28
- **作用域**：`hhlyqyxt-master/ScheduledTask/report_generator.py`（新增）+ `stationProcessMin.py`（修改 1 行调用）
- **契约类别**：向后兼容（新增函数，不改老逻辑）
- **模型分工**：DeepSeek v4 Flash = 主力执行；DeepSeek v4 Pro = 架构师 / 高级审查
- **相关记忆**：`[[traction-emergency-hhly-source]]`、`[[traction-review-scope-rule]]`、`[[haihe-project-env-quirks]]`、`[[deepseek-model-constraint]]`、`[[user-full-process-workflow]]`

---

## 1. 目标

在应急响应 I-IV 级触发时，调用天河报告接口生成海河流域气象公报。报告只传 `template: "haihe_weather_bulletin"`，其他参数用天河默认值。

**非目标**：不改应急响应级别判定逻辑、不改报告 API 签名、不改前端。

## 2. 设计

### 2.1 新增模块：`ScheduledTask/report_generator.py`

只暴露一个函数 `trigger_weather_bulletin_report(response_level, *, timeout=30) -> bool`：

- `response_level < 1`（无预警）→ 不发送，返回 False
- I-IV 级 → POST `http://10.226.188.156:8000/api/report/generate`，body `{"template": "haihe_weather_bulletin"}`
- 成功（HTTP < 400）→ `logger.info(...)`，返回 True
- 失败（超时/连接错误/非 2xx）→ `logger.warning(...)`，返回 False，**不抛异常**

### 2.2 调用点：`stationProcessMin.py` → `calcmaxdataseg5min()`

紧接 `run_emergency_response_monitor(...)` 之后，读返回的 ORM 对象的 `response_level` 字段触发报告。

### 2.3 数据流

```
calcmaxdataseg5min()
  └─ run_emergency_response_monitor(...)
       → 返回 ORM 对象 record (含 response_level: 0-4)
            │
            ├─ 0 → 跳过
            └─ 1/2/3/4 → trigger_weather_bulletin_report(record.response_level)
                            → POST /api/report/generate
```

### 2.4 应急响应级别定义（现有逻辑，不改）

| response_level | 含义 | 触发条件 |
|---|---|---|
| 1 | I 级 | 特大暴雨站点占比 ≥ 15% |
| 2 | II 级 | 大暴雨站点占比 ≥ 15% |
| 3 | III 级 | 12h 暴雨站点占比 ≥ 20% |
| 4 | IV 级 | 24h 暴雨站点占比 ≥ 20% |
| 0 | 无预警 | 不满足任何条件 |

## 3. 错误处理

| 场景 | 处理 |
|---|---|
| API 超时 | logger.warning，return False |
| 连接失败 | logger.warning，return False |
| HTTP ≥ 400 | logger.warning + status_code + body 摘要，return False |
| 网络完全不可达 | logger.warning，return False |
| **任何异常** | 不抛，不阻塞后续流程 |

## 4. 测试策略

- `test_trigger_skips_when_level_zero` — response_level=0 时不发送 HTTP 请求
- `test_trigger_sends_when_level_one` — response_level=1 时发送请求（mock requests.post）
- `test_trigger_tolerates_timeout` — 超时不崩溃，返回 False
- `test_trigger_tolerates_connection_error` — 连接失败不崩溃
- `test_trigger_tolerates_http_error` — 4xx/5xx 不崩溃
- `test_trigger_payload_matches_spec` — body 精确 `{"template":"haihe_weather_bulletin"}`

## 5. 执行编排

- **Phase 0** — 分支 + baseline
- **Phase 1** — 写 `report_generator.py` + 6 条测试
- **Phase 2** — `stationProcessMin.py` 加 3 行调用
- **Phase 3** — code-simplifier + Pro 最终审查
- **Phase 4** — finishing (Push + main merge + memory)

## 6. 模型分工

| 阶段 | 模型 | 职责 |
|---|---|---|
| brainstorming | Opus（本会话） | 业务口径对齐 |
| writing-plans | 本会话 | Phase 编排 |
| TDD 红/绿执行 | DeepSeek v4 Flash 行为约束 | 分 phase 落地 |
| code-simplifier | Flash 行为约束 | 清理 |
| code-review 最终审 | DeepSeek v4 Pro 行为约束 | PR 前审查 |
| github / claude-mem | — | 交付 + 记忆 |

## 7. 交付物

- `hhlyqyxt-master/ScheduledTask/report_generator.py`（新增）
- `hhlyqyxt-master/ScheduledTask/tests/test_report_generator.py`（新增 6 条）
- `hhlyqyxt-master/ScheduledTask/stationProcessMin.py`（1 处调用）
- `docs/superpowers/specs/2026-07-28-traction-report-api-design.md`（本文档）
- `docs/superpowers/plans/2026-07-28-traction-report-api-plan.md`（下一步产出）
- 新记忆 `[[traction-report-api]]`
