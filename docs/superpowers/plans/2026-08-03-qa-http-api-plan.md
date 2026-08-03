# 问答智能体 · 天河小程序 HTTP 问答接口 实施计划

- **设计文档**：`docs/superpowers/specs/2026-08-03-qa-http-api-design.md`
- **日期**：2026-08-03
- **分支**：`feat/qa-http-api`
- **模型分工**：DeepSeek v4 Flash = 主力执行；DeepSeek v4 Pro = 架构师 / 高级审查

---

## Global Constraints

1. **`message_orchestrator.py` 零改动**。若发现必须改，先停下来跟用户确认。
2. **不动牵引智能体仓库** `hhlyqyxt-master/`。本次改动只在 `haiheliuyubaoyuagent-master/`。
3. **依赖方向**：`chain_gzt` → `qa_http_api`。`qa_http_api.py` 禁止 import `chain_gzt`（会继承其模块级副作用与重依赖，且无法独立测试）。
4. **Python 解释器**：`D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe`。Git Bash 的 `python` 是 Windows Store 占位程序，静默 exit 49 无输出。
5. **测试须从 `chainlitexam/` 目录运行**，否则 `ModuleNotFoundError: No module named 'utils'`。
6. **脱敏**：内网 IP（`10.226.90.120` / `10.226.107.130` / `10.226.188.156`）和文件路径不得进入响应、日志、或提交的文档。
7. **git add 只许精确路径**，禁止 `git add -A` / `git add .`（仓库有大量未跟踪的临时文件与 worktree）。
8. 测试全部用假 chain，**不依赖内网连通性**。

---

## Task 1: Phase 0 — 分支 + baseline

1. `git checkout -b feat/qa-http-api`
2. 跑基线全量测试，记录当前失败项（已知 `test_decision_weather_tool.py` 有前置 import 失败，与本次无关）：
   ```
   cd chainlitexam
   <venv>/python.exe -m pytest tests/ -v --ignore=tests/test_decision_weather_tool.py
   ```
3. 记录通过/失败数，作为回归对比基准。

**验收**：分支已建，baseline 数字已记录。

---

## Task 2: Phase 1 — `qa_http_api.py` + 测试（TDD）

### 2.1 先写测试 `chainlitexam/tests/test_qa_http_api.py`

按设计文档第 5 节的 10 条，重点是第 2 条（答案按 id 归并）—— 这是最容易错的地方：
`message_orchestrator.py:4239-4240` 先 `cl.Message(content="")` + `send()`，之后 `stream_msg.content = text` + `update()`。同一 id 会有多个 step 事件，必须按 id 取最终态、按首现顺序拼接非空内容。

测试清单：
1. `CapturingEmitter` 分离答案与思考过程
2. 答案按 id 归并：先空后填 → 只得最终正文，无空串无重复
3. 多条答案消息按顺序拼接
4. 图片 → URL 映射，`session.files` 路径可读
5. 路径穿越防护：`../`、绝对路径、非 UUID、跨 session 全拒
6. 并发多请求上下文隔离
7. 超时返回 504
8. 空 / 超长 question 返回 400
9. `include_reasoning=false` / `include_gis=false` 时对应字段为空
10. 答案为空时返回兜底文案

多轮上下文专项（**第 11、12 条是重点**）：

11. **历史裁剪**：`ToolMessage` 与带 `tool_calls` 的 `AIMessage` 空壳被丢弃
12. **无孤儿 `tool_calls`**：裁剪后不存在带 `tool_calls` 的 `AIMessage` 后面缺对应 `ToolMessage` 的情况
13. 带 `conversation_id` 时历史正确注入
14. 不传 `conversation_id` → 单轮，不读不写 store
15. `conversation_id` 不存在/过期 → 当新会话，不报错
16. 轮数超上限丢最旧
17. `ConversationStore` TTL 清理
18. 并发同一 `conversation_id` 不错乱

### 2.2 再写实现

模块结构：

- `CapturingEmitter(BaseChainlitEmitter)` — 覆盖 `send_step` / `update_step` / `send_element` / `send_window_message`
- `_merge_answers(steps)` — 按 id 归并答案（核心逻辑，单独函数便于测试）
- `_prune_history(messages)` — 历史裁剪（核心逻辑，单独函数便于测试）
- `ConversationStore` — 抽象接口 `get` / `save` / `cleanup_expired`；本期内存字典实现（`InMemoryConversationStore`），将来换落库只改这一处
- `QARuntime` — 持有注入的 chain 与 callbacks；进程级缓存；`asyncio.Lock` 保护首次初始化
- `ask(question, *, conversation_id, include_reasoning, include_gis)` — 主入口
- `resolve_file(session_id, file_id)` — 三重路径校验后返回真实路径
- `cleanup_expired_files()` — TTL 清理，扫 `chainlit.config.FILES_DIRECTORY` 下会话子目录

关键实现约束：
- 会话根目录读 `chainlit.config.FILES_DIRECTORY`，**不自己拼路径**（Chainlit 按进程 cwd 决定该位置）
- **历史裁剪必须成对丢弃** `ToolMessage` 和带 `tool_calls` 的 `AIMessage` 空壳，否则孤儿 `tool_calls` 会让 LLM API 报错
- 传给 `process_message` 的是历史的**副本**（它会原地 append），跑完再裁剪回存
- 超时用 `asyncio.wait_for` 直接取消，**不用 `shield`**（已实测清理链完整；shield 会让任务后台继续占资源）
- 环境变量：`QA_API_MAX_CONCURRENCY`（4）、`QA_API_TIMEOUT_SECONDS`（180）、`QA_API_FILE_TTL_SECONDS`（1800）、`QA_API_CONVERSATION_TTL_SECONDS`（3600）、`QA_API_MAX_HISTORY_TURNS`（10）

**验收**：18 条测试全绿。

---

## Task 3: Phase 2 — `chain_gzt.py` 注册接口 + 依赖注入

1. 把 `on_message` 里那 21 个 callbacks 提取成一个 `_build_orchestrator_callbacks()` 函数，`on_message` 与 HTTP 接口共用（**避免两处维护漂移** —— 这是 CLAUDE.md 里反复出现的教训）。
2. 复用 `_init_runtime_session` 的 chain 构造逻辑，提取出可复用的构造函数（不带 `cl.user_session.set`），供 HTTP 侧进程级缓存调用。
3. 在 `api_sub_app` 上注册：
   - `POST /qa/ask` → 实际路径 `/api/v1/qa/ask`
   - `GET /qa/files/{session_id}/{file_id}` → 实际路径 `/api/v1/qa/files/...`
4. 现有双 mount 机制（`app.mount` + `chainlit_app.router.routes.insert`）自动覆盖新接口，无需额外改动。
5. 启动时注册 TTL 清理后台任务。

**验收**：
- `on_message` 行为不变（网页端回归通过）
- 新接口在 `chainlit run` 下可达
- callbacks 只有一处定义

---

## Task 4: Phase 3 — 审查 + 简化 + 回归

1. **code-review 双代理**（Pro 模型，并行）：
   - 代理 A：CLAUDE.md 合规性 + 项目约定一致性
   - 代理 B：正确性 / 并发安全 / 路径穿越 / 资源泄漏
2. 修复审查发现的问题（P0/P1 必修，P2 记录）
3. **code-simplifier** 过一遍
4. 全量回归，对比 Task 1 的 baseline 数字：
   ```
   cd chainlitexam
   <venv>/python.exe -m pytest tests/ -v --ignore=tests/test_decision_weather_tool.py
   ```
5. `superpowers:verification-before-completion`

**验收**：审查问题已修，回归无新增失败。

---

## Task 5: Phase 4 — 文档 + 记忆 + 提交

1. **`docs/问答接口对接文档.md`**（给小程序同事）：
   - 请求 / 响应完整示例
   - `curl` 调用示例
   - 错误码表
   - **内网地址用占位符** `${QA_API_BASE}`，不写真实 IP
2. `claude-md-management:revise-claude-md` — 更新 `haiheliuyubaoyuagent-master/CLAUDE.md`：新增模块说明、依赖方向约束、环境变量、答案归并坑
3. 写记忆：接口契约 + 依赖方向约束 + 答案归并坑
4. `git add` **精确路径**后提交推送

**验收**：文档可交付、CLAUDE.md 已更新、记忆已写、已推送。

---

## 风险与回滚

| 风险 | 缓解 |
|---|---|
| 提取 callbacks 时改坏 `on_message` | 网页端手动回归 + 现有测试 |
| 并发下 session 串扰 | 已实测隔离正常；测试第 6 条覆盖 |
| 历史裁剪漏丢 `tool_calls` 空壳 → LLM API 报错 | 测试第 11、12 条专门覆盖 |
| 内存 store 服务重启丢上下文 | 用户已确认可接受；抽成接口，将来换落库只改一处 |
| 单会话历史无限增长 | 轮数上限 + TTL 双重约束 |
| 图片 URL 被小程序访问时已过期 | TTL 1800s（可调至一天）；每 session 独立子目录 |
| 内网不通导致无法端到端验证 | 测试全用假 chain；真实验证留到内网部署 |

回滚：纯新增改动，`git revert` 即可；`message_orchestrator.py` 未动，核心问答不受影响。
