# 任务计划：5 分钟降雨应急响应监测

## 目标

在牵引智能体侧，每 5 分钟基于 `stationProcessMin.py` 已有的 5 分钟站点降雨数据，计算并持久化应急响应监测事件；应急响应决策逻辑由问答智能体消费数据后自行处理。

## 阶段

| 阶段 | 状态 | 说明 |
|------|------|------|
| 1. 需求与阈值确认 | `completed` | 国家站编码 `"011/012/013/016"`、相邻定义忽略、表名 `qy_emergency_response_monitor` 已确认 |
| 2. 设计文档 | `completed` | `docs/superpowers/specs/2026-07-15-5min-emergency-response-design.md` |
| 3. 实现计划 | `completed` | `docs/superpowers/plans/2026-07-15-5min-emergency-response.md` |
| 4. 编码实现 | `completed` | ORM 模型、计算模块、调度集成、查询接口、单元测试 |
| 5. 代码审查 | `completed` | 实现期审查 + IDE 类型标注修复 + 全面遍历审查（2 P0 + 3 P1 + 3 P2 全部修复） |
| 6. 简化重构 | `completed` | code-simplifier 两轮，空 CSV 处理统一收口 |
| 7. 验证测试 | `completed` | 18 个应急响应测试全部通过 |
| 8. 文档与记忆 | `completed` | findings/progress/部署文档 + claude-mem 记忆已更新 |

## 当前状态

全部完成。main 分支最新提交：

- `c2215b7 refactor: simplify emergency monitor hardening`
- `6a5b272 fix: keep Station_levl in circleadd5min, read qmm.id before session close, idempotent emergency insert`
- `8524502 fix: use flat Union[..., None] instead of nested Optional[Union[...]]`

待用户在离线服务器执行：DDL（含 UNIQUE 的 `qy_emergency_response_monitor`）、删除旧 `yangxiao.csv`（可选，有兼容逻辑）、重启调度。

---

# 任务计划：叫应功能（call_respond）

## 目标

应急响应触发后，**值班人员人工确认**才通知受影响单位。等级变化时创建叫应任务，人工确认后后台异步发送叫应话术 + 天河报告文件到受影响单位微信群，记录逐群台账。

## 阶段

| 阶段 | 状态 | 说明 |
|------|------|------|
| 1. 业务口径确认 | `completed` | 人工确认在环、等级变化触发、后台异步、可配置群映射、报告缺失挂起，逐条确认 |
| 2. 设计文档 | `completed` | `docs/superpowers/specs/2026-08-04-call-respond-design.md` |
| 3. 实现计划 | `completed` | `docs/superpowers/plans/2026-08-04-call-respond.md` |
| 4. SDD 实现 | `completed` | 8 任务（ORM/配置/send_file/等级检测/状态机/HTTP API/调度集成/回归），每任务审查 |
| 5. 最终整体审查 | `completed` | opus 捕获 C1（report URL 需反查）+ I1-I4 全部修复 |
| 6. code-review | `completed` | CLAUDE.md 合规通过；逐群重试/confirmed 恢复/状态校验/retry 404 修复 |
| 7. code-simplifier | `completed` | 报告下载一次、网络循环前关 session、RECOVERABLE_STATUSES 常量 |
| 8. 微信网关接入 | `completed` | `send_file` 调 DMZ 网关（send-text+send-file），ok 字段检查，默认端口 18080 |
| 9. 文档与记忆 | `completed` | CLAUDE.md 叫应章节 + 建表 SQL + 部署清单 + claude-mem 记忆 |
| 10. github | `completed` | 推送 main（0413e9c..9519b32） |

## 当前状态

全部完成。累计提交：`4065b72`（设计）→ `9a57c8b`（默认端口）。测试全量 161 passed。

待乙方/甲方提供：群映射（`call_respond_config.json`）、部署 env 变量（`WECHAT_GATEWAY_URL/TOKEN`）、网关 IP/群白名单。
