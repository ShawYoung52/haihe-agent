---
name: qa-http-api
description: 问答智能体 HTTP 问答接口——天河小程序集成
metadata:
  type: project
---

问答智能体新增 HTTP 问答接口 `POST /api/v1/qa/ask` 与 `GET /api/v1/qa/files/{session_id}/{file_id}`，供天河小程序调用。

**核心方案**：`init_http_context()` 伪造 Chainlit HTTP 会话 + `CapturingEmitter` 拦截输出，`message_orchestrator.py`（4612 行、92 处 `cl.*` 调用）**零改动**。

**关键实现细节**：
- 依赖方向单向：`chain_gzt` → `qa_http_api`，禁止反向 import
- 答案归并按 step id 取最终态（`process_message` 先 send 空再 update 填内容），过滤 `❌`/`⏱️`/`📊` 旁路消息
- 图片落盘到 `chainlit.config.FILES_DIRECTORY/<session_id>/`，TTL 清理由 `run_cleanup_loop` 后台任务负责
- `_release_chainlit_session()` 在 `finally` 块手动回收 `user_sessions`/`chat_contexts`——Chainlit 只在 WS 断开时清理，HTTP 会话永不走到那条路
- `_ensure_data_layer_filter()` 用代理按会话标记跳过 Chainlit 表写入——HTTP 临时会话写 `threads`/`steps` 表是纯浪费
- 多轮上下文 `InMemoryConversationStore` + `lock_for(cid)` 防并发竞态，`prune_history` 只留干净问答对
- 环境变量非法值回落默认，防止 `Semaphore(-1)` 导入期崩服务
- 响应出口 + 日志均脱敏（IP/路径/连接串），HTTP 面向外部客户端暴露面大于网页端
- 单 event loop 进程假定成立（chainlit CLI），模块级 `Semaphore`/`Lock` 跨 loop 复用依赖 Python 3.10+

**Why:** 天河小程序需要调用问答能力。直接调 `process_message` 不可行——它深度耦合 Chainlit（92 处 `cl.*`），会话外全部抛 `ChainlitContextException`。

**How to apply:**
- 接小程序时先确认基础路径
- 图片 TTL 默认 30 分钟，不够时调 `QA_API_FILE_TTL_SECONDS`
- 会话 TTL 默认 1 小时、历史最多 10 轮，不够时调 `QA_API_CONVERSATION_TTL_SECONDS` / `QA_API_MAX_HISTORY_TURNS`
- 并发上限 4、超时 180s，不够时调 `QA_API_MAX_CONCURRENCY` / `QA_API_TIMEOUT_SECONDS`
- 内网部署后手工过一遍真实问答（尤其带图表和 GIS 的类型），确认无误再放给小程序

链接：[[qa-agent-rain-impact-sync]] [[deepseek-model-constraint]]
