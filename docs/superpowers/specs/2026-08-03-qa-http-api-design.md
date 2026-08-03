# 问答智能体 · 天河小程序 HTTP 问答接口设计

- **状态**：草案（brainstorming 已完成，待用户 review）
- **日期**：2026-08-03
- **作用域**：`haiheliuyubaoyuagent-master/chainlitexam/qa_http_api.py`（新增）+ `chain_gzt.py`（新增接口注册与依赖注入）
- **契约类别**：向后兼容（纯新增；`message_orchestrator.py` 核心业务代码零改动）
- **模型分工**：DeepSeek v4 Flash = 主力执行；DeepSeek v4 Pro = 架构师 / 高级审查
- **相关记忆**：`[[deepseek-model-constraint]]`、`[[user-full-process-workflow]]`、`[[haihe-project-env-quirks]]`

---

## 1. 目标

给天河小程序开放一个 HTTP 问答接口，复用问答智能体现有的全部问答能力（planner LLM + 工具循环 + 快速路径），返回答案正文、图表图片链接、GIS 图层数据和思考过程。

**非目标**：
- 不改 `message_orchestrator.py` 的问答逻辑（4612 行，92 处 Chainlit 调用）
- 不改 Chainlit 网页端行为
- **不做流式返回、不做鉴权（本期）**
- 不动牵引智能体仓库 `hhlyqyxt-master/`

## 2. 核心问题与解法

### 2.1 问题：问答逻辑与 Chainlit 深度耦合

`process_message()` 无法在 Chainlit 会话外调用：

| 耦合点 | 数量 |
|---|---|
| `cl.*` 调用总数 | 92 |
| `cl.user_session` | 53 |
| `cl.Message` | 24 |
| `cl.Image` | 8 |

且 **`process_message()` 永远返回 `None`** —— 答案不是返回值，而是流进 `cl.Message` 的。会话外调用任何 `cl.*` 抛 `ChainlitContextException`（已实测）。

### 2.2 解法：伪造 HTTP 会话 + 自定义 emitter 拦截输出

```
POST /api/v1/qa/ask
   │
   ├─ init_http_context(thread_id=uuid4())     # 造独立 HTTPSession
   ├─ ctx.emitter = CapturingEmitter(...)      # 替换 emitter 拦截输出
   ├─ context_var.set(ctx)                     # 绑到当前 asyncio Task
   │
   ├─ process_message(cl.Message(content=问题), ...)   # 核心逻辑零改动
   │      └─ 内部所有 cl.* 调用正常执行，输出被 emitter 截获
   │
   └─ 从 emitter 汇总 → {answer, images, gis, reasoning}
```

**已实测确认的事实**（`chainlit 2.11.0`）：

| 验证项 | 结果 |
|---|---|
| `init_http_context()` 后所有 `cl.*` 调用 | 正常执行，不抛异常 |
| emitter 拦截答案 | `send_step`/`update_step` 的 `type=="assistant_message"` |
| emitter 拦截思考过程 | 同上，`type` 为其他值 |
| emitter 拦截图片 | `send_element`，含 `chainlitKey`/`mime`/`name` |
| emitter 拦截 GIS 联动包 | `send_window_message`，JSON 字符串 |
| `cl.Image(content=bytes)` 落盘 | 自动写 `<FILES_DIRECTORY>/<session_id>/<key>.png` |
| `process_message` 只读 `message.content` | 可直接构造 `cl.Message`，无需真 websocket |
| 用假 chain 跑完整 `process_message` | 通过，答案与思考步骤正确分离 |
| **并发 3 请求交错执行** | **session / emitter / 答案完全隔离，无串扰** |

并发隔离依据：`context_var` 是 `ContextVar`，asyncio 每个 Task 有独立上下文副本，`cl.user_session` 以 `context.session.id` 为 key。实测三请求各自 session_id 不同、状态不互相覆盖。

## 3. 设计

### 3.1 新增模块 `chainlitexam/qa_http_api.py`

**依赖方向必须是 `chain_gzt` → `qa_http_api`，不能反向。**

原因：`chain_gzt.py` 有大量模块级副作用（创建 FastAPI app、双 mount、matplotlib 字体扫描、数据库表检查）和重依赖（`langchain_mcp_adapters` 等）。若 `qa_http_api` import 它，会继承全部副作用且无法独立测试（已实测：开发环境 import `chain_gzt` 直接 `ModuleNotFoundError`，而单独 import `message_orchestrator` 正常）。

因此 `qa_http_api.py` 只依赖 `message_orchestrator` + `chainlit`，由 `chain_gzt` 在自身初始化完成后**注入** chain 与 callbacks。

#### `CapturingEmitter(BaseChainlitEmitter)`

覆盖四个方法收集输出：

| 方法 | 收集内容 |
|---|---|
| `send_step` / `update_step` | `type=="assistant_message"` → 答案；其他 → 思考过程 |
| `send_element` | 图片元素（`chainlitKey`/`name`/`mime`） |
| `send_window_message` | GIS 联动包 JSON |

**答案归并规则（关键坑）**：`message_orchestrator.py:4239-4240` 先 `cl.Message(content="")` + `send()`，之后才 `stream_msg.content = text` + `update()`。所以同一条消息会产生多个 step 事件。

必须**按 step `id` 归并、每个 id 取最终态、再按首次出现顺序拼接非空内容**。不能取最后一条（可能是空串），也不能全部拼接（会重复）。

#### 会话与运行时

- 每请求 `init_http_context(thread_id=uuid4())` → 独立 Chainlit session，天然隔离
- 进程级缓存 chain 与 tools（`load_sse_tools()` 无状态，可缓存），`asyncio.Lock` 保护首次初始化
- `asyncio.Semaphore` 限并发（默认 4，`QA_API_MAX_CONCURRENCY` 可调）
- `asyncio.wait_for` 超时（默认 180s，`QA_API_TIMEOUT_SECONDS` 可调）

### 3.2 多轮上下文

小程序传 `conversation_id`，服务端维护历史消息。

**存储**：进程内存字典 + TTL 过期（`QA_API_CONVERSATION_TTL_SECONDS`，默认 3600s）。抽象成 `ConversationStore` 接口（`get` / `save` / `cleanup_expired`），将来换落库或改客户端传历史只改一处实现。

**已知取舍**：服务重启丢上下文，用户需重新开始会话。用户已确认可接受，不行再换落库方案。

**历史裁剪（必须做）**：`process_message` 原地 `messages.append(...)`，一轮跑完后 list 里混着：

| 消息类型 | 内容 | 是否入历史 |
|---|---|---|
| `HumanMessage` | 用户问题 | ✅ |
| `AIMessage`（有 `tool_calls`、`content` 为空） | 工具调用壳 | ❌ |
| `ToolMessage` | 工具原始返回，可达数十 KB | ❌ |
| `AIMessage`（有 `content`） | 最终答案 | ✅ |

两条硬约束：
1. **不能原样存整个 list** —— 几轮后上下文爆炸，且工具原始 JSON 对后续对话无价值。实测两轮对话原始 6045 字符，裁剪后 23 字符。
2. **不能只做「丢掉 ToolMessage」** —— LangChain 硬约束：带 `tool_calls` 的 `AIMessage` 后必须紧跟对应 `ToolMessage`，否则 LLM API 报错。所以**必须同时丢掉带 `tool_calls` 的 `AIMessage` 空壳**，二者成对丢弃。

裁剪规则：只保留 `HumanMessage` 与「有实际文本内容且无 `tool_calls`」的 `AIMessage`。已实测该策略不产生孤儿 `tool_calls`。

**轮数上限**：保留最近 `QA_API_MAX_HISTORY_TURNS`（默认 10）轮问答对，超出丢弃最旧的，防单会话无限增长。

**流程**：
```
请求带 conversation_id
   → store.get(cid) 取历史（已裁剪的干净问答对）
   → 复制一份传给 process_message（它会原地 append 本轮）
   → 跑完后裁剪 + 截断轮数 → store.save(cid, pruned)
```
不传 `conversation_id` → 单轮模式，不存不取。

### 3.3 图片返回：URL + 延迟清理

**不能请求返回后立刻删文件** —— 小程序是拿到 URL 之后才发起图片请求，立即删必然 404。

方案：
- 图片路径从 `session.files` 取（含真实绝对路径）
- 根目录用 `chainlit.config.FILES_DIRECTORY` 读取，**不自己拼路径**（Chainlit 按进程 cwd 决定该目录，实测 cwd 不同则落盘位置不同）
- 返回 `/api/v1/qa/files/{session_id}/{file_id}`
- **TTL 清理**：后台任务定期扫描 `FILES_DIRECTORY` 下的会话子目录，删除超过 `QA_API_FILE_TTL_SECONDS`（默认 1800s = 30 分钟）的目录。每个 session 有独立子目录，清理边界干净

TTL 用户确认「30 分钟应该够，不够改成一天」。做成环境变量，改配置即可，无需改代码。

**路径穿越防护**（三重）：
1. `session_id` / `file_id` 必须匹配严格 UUID 正则
2. 拼接后 `Path.resolve()`
3. 解析结果必须仍在 `FILES_DIRECTORY` 之内（`is_relative_to`）

### 3.4 接口契约

#### `POST /api/v1/qa/ask`

请求：
```json
{
  "question": "那后天呢？",
  "conversation_id": "c8f3a1e2-...",
  "include_reasoning": true,
  "include_gis": true
}
```

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `question` | string | 是 | — | 用户问题，非空，最长 2000 字 |
| `conversation_id` | string | 否 | — | 多轮会话 id（UUID）。不传 = 单轮，不带上下文 |
| `include_reasoning` | bool | 否 | `true` | 是否返回思考过程 |
| `include_gis` | bool | 否 | `true` | 是否返回 GIS 图层（GeoJSON 可达数 MB） |

响应（沿用现有 `{code, data, message}` 封装，与 `/api/v1/admin/users` 一致）：
```json
{
  "code": 200,
  "data": {
    "answer": "海河流域明天多云，局地小雨……",
    "conversation_id": "c8f3a1e2-...",
    "images": [
      {"name": "chart_0", "url": "/api/v1/qa/files/<session>/<file>.png", "mime": "image/png"}
    ],
    "gis": [{"type": "gis_linkage", "schema_version": "v2", "scene": "...", "map": {...}, "panel": {...}}],
    "reasoning": ["🔍 理解问题：……", "📡 查询数据：……"],
    "elapsed_seconds": 12.3
  },
  "message": "success"
}
```

`conversation_id` 始终回传：小程序首次不传时，服务端生成新 id 返回，后续请求带上即可延续对话。

`answer` 为空时（工具全失败等）返回兜底文案，不返回空串。

#### `GET /api/v1/qa/files/{session_id}/{file_id}`

返回图片二进制（`FileResponse`）。校验失败 → 400；文件不存在或已过期 → 404。

### 3.5 为什么四块内容全返

用户确认「你推荐」。结论是全返但分层：这些数据服务端**已经算出来了**（emitter 里就能拿到），不返回等于浪费；但用开关控制体积大的部分（`reasoning`、`gis`），小程序用不上就关掉或不读。

## 4. 错误处理

| 场景 | HTTP | 处理 |
|---|---|---|
| `question` 为空 / 超长 | 400 | Pydantic 校验 |
| `conversation_id` 格式非法 | 400 | UUID 正则校验 |
| `conversation_id` 不存在/已过期 | 200 | 当新会话处理，回传新 id，不报错 |
| 并发超限 | 429 | Semaphore 满且等待超时 |
| 处理超时（180s） | 504 | `asyncio.wait_for` → `TimeoutError` |
| `process_message` 抛异常 | 500 | 捕获 + 日志，返回脱敏错误 |
| 答案为空 | 200 | 返回兜底文案 |
| 图片 id 非法 | 400 | 正则 + 路径校验 |
| 图片过期/不存在 | 404 | — |

**错误信息脱敏**：沿用项目既有约定，内网 IP（MUSIC `10.226.90.120`、PG `10.226.107.130`、LLM `10.226.188.156`）和文件路径不得出现在响应或日志中。

**超时取消的资源清理**：已实测 `asyncio.wait_for` 超时时，`CancelledError` 沿调用链正常传播，各层 `finally` 均执行（工具级 → `process_message` 级）。因此直接用 `wait_for` 取消即可，**不用 `asyncio.shield`** —— shield 会让任务在超时后继续在后台占用 MCP 连接和 LLM 配额，反而更糟（已实测确认 shield 下任务确实继续执行）。

## 5. 测试策略

`chainlitexam/tests/test_qa_http_api.py`，全部用假 chain，不依赖内网：

1. `CapturingEmitter` 正确分离答案与思考过程
2. **答案按 id 归并**：先 send 空、再 update 填内容 → 只得最终正文，无空串无重复
3. 多条答案消息按顺序拼接
4. 图片 → URL 映射正确，`session.files` 路径可读
5. 路径穿越防护：`../`、绝对路径、非 UUID、跨 session 全部拒绝
6. 并发多请求上下文隔离（复用已验证脚本思路）
7. 超时返回 504
8. 空 / 超长 question 返回 400
9. `include_reasoning=false` / `include_gis=false` 时对应字段为空
10. 答案为空时返回兜底文案

多轮上下文专项：

11. **历史裁剪**：`ToolMessage` 与带 `tool_calls` 的 `AIMessage` 空壳被丢弃，只留干净问答对
12. **无孤儿 `tool_calls`**：裁剪后不存在「带 `tool_calls` 的 `AIMessage` 后面没有对应 `ToolMessage`」的情况（否则 LLM API 报错）
13. 带 `conversation_id` 时历史被正确注入 `process_message`
14. 不传 `conversation_id` → 单轮，不读不写 store
15. `conversation_id` 不存在/过期 → 当新会话，回传新 id，不报错
16. 轮数超 `QA_API_MAX_HISTORY_TURNS` 时丢弃最旧
17. `ConversationStore` TTL 过期清理
18. 并发同一 `conversation_id` 不产生历史错乱

运行环境：`D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe`（Git Bash 的 `python` 是 Windows Store 占位程序，静默 exit 49）。测试须从 `chainlitexam/` 目录跑。

## 6. 执行编排

| Phase | 内容 | 模型 |
|---|---|---|
| 0 | 设计 + 计划（本文档）+ 架构评审 | Pro |
| 1 | `qa_http_api.py` + 测试（TDD） | Flash |
| 2 | `chain_gzt.py` 注册接口 + 依赖注入 | Flash |
| 3 | code-review 双代理 → code-simplifier → 全量回归 | Pro 审查 |
| 4 | 更新 CLAUDE.md + 写记忆 + 小程序对接文档 + 提交 | Flash |

## 7. 交付物

- `chainlitexam/qa_http_api.py`
- `chainlitexam/tests/test_qa_http_api.py`
- `chain_gzt.py` 接口注册（新增，不改现有逻辑）
- `docs/问答接口对接文档.md`（给小程序同事，含 curl 示例，内网地址用占位符）
- CLAUDE.md 更新 + 记忆写入
