# 请求级锚定时间（reference_time）设计

日期：2026-09-20
状态：已评审，待实施
相关：`deploy_agentweb/DEPLOY-sim-time.md`（本设计取代其中的"全局切换"口径）

## 背景与问题

"切换系统时间"功能当前把"现在"锚定成一个**进程级全局标量**：

- 单一事实源 = 宿主机临时目录下的 `haihe_system_time_override.json`，Chainlit（8003）与
  MCP（SSE 3333）两个进程各放一份内容一致的 `time_source.py` 读同一个文件。
- 全仓 20+ 个模块的 `time_source.now()` 调用点都读它（Chainlit 侧 5 个、MCP 侧 17 个）。
- `chain_gzt._build_orchestrator_runtime()` 把 `【当前日期：…】` 前缀**烤进 system prompt**，
  该 runtime 按 `(override_date_str(), config)` 缓存在事件循环上；WS 会话还会在
  `_init_runtime_session()` 里把三条 chain 拷进 `cl.user_session` 钉死。

由此产生三个真实缺陷：

1. **全局污染**：任何人调 `POST /api/v1/admin/system-time` 都会翻转**所有用户、所有客户端**
   （含天河小程序调用 `/api/v1/qa/ask` 的回答）的"现在"。
2. **恢复不可靠 / 残留**：覆盖文件跨服务重启存活。测试人员设到 `2026-07-10` 后"改回去"，
   天河小程序仍持续回答 7 月 10 日的内容。前端新增的 `metadata` 契约（见下）在旧接口上会
   因缺 `datetime` 字段被拒，restore 静默失败，文件留在原地。
3. **缓存串味**：`qa_http_api` 的响应缓存键 `{q, r, g}` **不含时间**，只靠"切换时调
   `clear_response_cache()`"兜底；一旦 restore 没生效，同问题的旧答案会持续命中。

前端已确认新的请求契约（对本仓库的 `POST /api/v1/admin/system-time`）：

```json
{ "metadata": { "location": "<window.location.href>", "time_mode": "fixed",
                "reference_time": "2026-07-10T15:00:00+08:00" } }
```

## 目标

把"锚定时间"从**进程级标量**改为**请求级值**：只有携带了锚点的那个请求按锚定时间回答，
服务端**不保留任何跨用户状态**。同时彻底修掉上面 2、3 两条残留缺陷。

非目标：不改任何 MCP 工具 schema，不让 LLM 传时间参数，不改 prompt 文本。

## 已达成的决策

| 议题 | 决策 |
|---|---|
| 隔离标识 | **每请求携带 `reference_time`**（不做 client_id 分槽、不用登录身份） |
| 覆盖入口 | `/api/v1/qa/ask` 与 Chainlit WS（`cl.Message.metadata`）**两个都要** |
| 全局文件的处置 | **保留为最低优先级兜底**；带 `metadata` 的新调用不写它，`time_mode:"real"` 会清掉它 |
| `【当前日期】` 前缀 | **按日期分槽缓存**（不改模板变量），prompt 文本逐字不变 |

## 可行性依据（已核对库源码，均为已装版本）

- `chainlit 2.9.6`：`cl.Message` 的 `metadata` 由客户端 payload 的 `metadata` 字段解析
  （`chainlit/message.py`），WS 入站消息可读。
- `langchain-mcp-adapters 0.3.2`：
  - `MultiServerMCPClient(..., tool_interceptors=[...])` 支持 `ToolCallInterceptor`
    （`interceptors.py` Protocol：`async __call__(request, handler)`）。
  - `MCPToolCallRequest.override(headers=...)` 可改 header；`execute_tool` 会把
    `request.headers` 合并进 connection 的 `headers`，**在每次调用新建 SSE session 之前**
    （`tools.py`）——而 0.3.2 本来就是**每次工具调用新建一个 session**，没有长连接会吞掉
    每调差异。
  - headers 由 `sse_client` 作用在 httpx client 层，`GET /sse` 与 `POST /messages/` 都带。
- `fastmcp 3.4.7`：`fastmcp.server.dependencies.get_http_headers()` 经 MCP SDK 的
  `request_ctx.get().request` 取当前 HTTP 请求（SSE 下即 `POST /messages/`），
  工具体内可直接读；无请求上下文时返回空 dict，不抛异常。

**唯一硬假设**：header 真的能到工具体内。库源码层面成立，但必须内网实测（见"验证"）。

## 设计

### 1. `time_source.py`（两包各一份，内容保持一致）

新增请求级锚点，**既有调用点一行不改**：

```python
_req_override: ContextVar = ContextVar("haihe_ref_override", default=None)

REFERENCE_TIME_HEADER = "x-haihe-reference-time"
_REAL = object()          # 显式"真实时间"哨兵

def now(tz=None):
    ov = _req_override.get()
    if ov is None:
        ov = _header_override()
    if ov is None:
        ov = _read_file_dt()
    if ov is None or ov is _REAL:
        return datetime.now(tz) if tz is not None else datetime.now()
    return ov.astimezone(tz) if tz is not None else ov.replace(tzinfo=None)
```

优先级：**请求 ContextVar → MCP header → 全局文件 → 真实时间**。

新增公开 API：

| 函数 | 作用 |
|---|---|
| `set_request_override(value) -> Token` | 在请求入口设置；`value` 为 ISO 文本/datetime/`"real"` |
| `reset_request_override(token)` | `finally` 里复位 |
| `request_override_header_value() -> str \| None` | 供 interceptor：只取请求级，返回 ISO 或 `"real"`；未设返回 `None` |
| `_header_override() -> datetime \| _REAL \| None` | 读 MCP header（惰性 import fastmcp，任何异常返回 `None`） |

`_REAL` 哨兵是**必需**的：它让"我要真实时间"能压过遗留的全局文件——正面解掉缺陷 2。
header 取 `"real"` 时映射为 `_REAL`。

`_header_override()` 结果按请求缓存进第二个 ContextVar（`_header_cache`），
避免 `time_source.now()` 热路径每次构造 headers dict。

### 2. 请求解析（单一实现，两入口共用）

新增纯函数（放 `chainlitexam/utils/reference_time.py`，可单测、无重依赖）：

```python
def resolve_reference_time(metadata: dict | None, *, top_level: dict | None = None):
    """返回 (kind, value)：("fixed", datetime) / ("real", None) / ("inherit", None)"""
```

规则：

- `time_mode == "real"`（大小写不敏感，另接受 `dynamic`/`live`）→ `("real", None)`，
  **忽略**任何 `reference_time` 与全局文件。
- 否则 `reference_time` 能解析成时间 → `("fixed", dt)`。
- 否则 → `("inherit", None)`（回落全局文件 → 真实时间）。**不报 400**：缺字段是合法的
  "没带锚点"，旧客户端因此天然兼容。
- `reference_time` 存在但**无法解析** → 抛 `ValueError`，入口转 400（显式给了错值应该报错，
  而不是静默按真实时间回答）。

入口同时接受 `metadata.reference_time` 与顶层 `reference_time`（前端选哪个都行）。

### 3. 入口接线

**HTTP `POST /api/v1/qa/ask`**（`chain_gzt.QAAskRequest`）

新增 `metadata: dict | None`、`reference_time: str | None`、`time_mode: str | None`
（Pydantic 默认忽略未知字段，新增字段向后兼容）。
`QARuntime.ask()` 解析后把 `kind/value` 传进 `_run_once()`，在**请求 Task 内**：

```python
token = time_source.set_request_override(value)   # "real" → _REAL
try:
    ...process_message...
finally:
    time_source.reset_request_override(token)
```

**Chainlit WS `@cl.on_message`**（`chain_gzt.py:4164`）

读 `message.metadata` 解析锚点，在 `process_message` 前后 set/reset。
同时**不再使用 `cl.user_session` 里钉死的 chain**，改为按本请求的 `day` 取运行时
（见第 4 节）后再传给 `process_message`（签名不变，chain 本来就是入参）。
`_init_runtime_session()` 保留（首聊/恢复线程仍要它播种 `messages` 等）。

### 4. 运行时按日期分槽

`chain_gzt._build_orchestrator_runtime()` 拆两层：

- **共享层**（按 config 缓存，进程内一次）：`load_sse_tools()` 的 MCP 工具表、
  `ToolCandidateIndex`。贵的部分（跨进程列工具）**不随日期重建**。
- **按日期层**（按 `(day, config)` 缓存）：`planner_template` / `answer_template` /
  三条 chain / `ActiveToolRouter`。

`_orchestrator_runtime_state()["runtimes"]` 改为 `dict[cache_key, runtime]` + 小 LRU
（`ORCHESTRATOR_RUNTIME_MAX_DAYS`，默认 4）。`_get_orchestrator_runtime(day: str | None = None)`
取对应槽位；`day is None` 时用 `time_source.override_date_str()`（等价今日行为）。

**prompt 文本逐字不变**——只是从"一个进程级 chain"变成"每个在用日期一个 chain"。
`_clear_orchestrator_runtime_cache()` 仍清空全部槽位。

### 5. 跨进程：interceptor + header

`chainlitexam/mcp_loader.py`：

```python
async def _inject_reference_time(request, handler):
    value = time_source.request_override_header_value()   # 只取请求级
    if value is None:
        return await handler(request)                     # 没带就一字节不加
    return await handler(request.override(
        headers={**(request.headers or {}), time_source.REFERENCE_TIME_HEADER: value}))

client = MultiServerMCPClient({name: {...}}, tool_interceptors=[_inject_reference_time])
```

**只在有请求级锚点时才注入**——没带时 MCP 侧继续自己读全局文件，现有行为逐字不变。
header 三态：ISO / `real` / 不存在。两个 MCP server 都挂该 interceptor（对不读它的
server 是惰性 header，无副作用）。

### 6. 缓存隔离

- **`qa_http_api` 响应缓存**：键 `{q,r,g}` → 增加 `t`（本请求生效的锚点：`null` / ISO /
  `"real"`）。这比"切换时清缓存"更根本：两个不同锚点的用户问同一句话**不会再互相拿到
  对方的答案**，也不再依赖 restore 必须成功。`clear_response_cache()` 保留（切换全局兜底
  时仍有意义）。
- **`QARuntime`**：不再自己缓存单个 runtime + epoch。`_get_runtime(day)` 委托
  `chain_gzt._get_orchestrator_runtime(day)`（按日期分槽），再 `dict()` 浅拷贝一份注入
  HTTP callbacks（`_build_orchestrator_callbacks(execution_mode="http")` 无状态，可复用）。
  等价于把现有的 `_runtime_epoch()` 换成 `day`，但槽位由 `chain_gzt` 统一持有。
  `_build_qa_runtime(day)` 相应加参。
- **MCP 侧 TTL 缓存**：键基本都已含推导出的时次（`%Y%m%d%H`、"今日零点"、完整 params JSON），
  不同锚点自然分槽。**补回归测试锁死**，并复核无时间维度的键
  （`basin_drawing_tool` 的 `lambda: "areas"` 是静态几何，安全）。
- **`_tianhe_cache`**：按 query 做键，天河回答是原样透传、与锚点无关，安全（写明即可）。

### 7. `POST /api/v1/admin/system-time` 契约

`SetSystemTimeRequest` 扩为可选字段（保留旧字段）：

| body | 行为 |
|---|---|
| `{"datetime": "...", "note": ...}` | **原样**写全局文件（curl / 验收流程不变） |
| `{"metadata": {"time_mode":"fixed","reference_time":"..."}}` | **无状态**：校验 + 回显 `{active:true, mode:"per_request", override_datetime, display}`，**不写全局文件** |
| `{"metadata": {"time_mode":"real"}}` | 回显 `{active:false}`，**并清掉遗留的全局文件** |
| 顶层 `reference_time` / `time_mode` | 同 `metadata` 内字段，等价 |

`GET /api/v1/admin/system-time` 语义不变（只报全局兜底开关 + `real_now`）。
面板"模拟中"状态改由前端自身状态显示。

`time_mode:"real"` 顺手清全局文件是**有意**的：这正是"改回去了还是 7月10号"那条路径的解药。

## 验证

**内网一次性探针** `haihe-weather-analyzer-mcp/probe_reference_time_header.py`
（沿用既有 `probe_*.py` 习惯，脱敏输出）：起一个最小 FastMCP SSE server + 带
interceptor 的 `MultiServerMCPClient`，断言工具体内 `get_http_headers()` 能读到注入值。
**这是唯一硬假设，实测通过前不上生产。**

**自动化测试**（TDD，先红后绿）：

| 文件 | 覆盖 |
|---|---|
| `chainlitexam/tests/test_time_source.py`（扩） | 优先级（请求 > header > 文件 > 真实）、`_REAL` 压制文件、reset 语义、并发 Task 隔离 |
| `chainlitexam/tests/test_reference_time.py`（新） | `resolve_reference_time` 全分支：fixed/real/inherit/非法抛错/大小写/顶层兼容 |
| `chainlitexam/tests/test_mcp_reference_time_header.py`（新） | interceptor 仅在请求级存在时注入、三态取值、handler 透传 |
| `haihe-weather-analyzer-mcp/tests/test_time_source_header.py`（新） | monkeypatch `get_http_headers` → `now()` 读 header；无上下文不炸；`real` → 真实时间 |
| `chainlitexam/tests/test_qa_http_api.py`（扩） | `/qa/ask` 带 metadata → 锚定；不带 → 真实；**并发两个不同锚点互不污染**；响应缓存按锚点分槽 |
| `chainlitexam/tests/test_system_time_api.py`（新） | 三种 body 契约；`{"datetime"}` 仍写文件；`real` 清文件；非法 `reference_time` → 400 |
| `chainlitexam/tests/test_orchestrator_runtime_slots.py`（新） | 两天两槽、MCP 只列一次、LRU 上限、generation 清空 |

回归：`python -m pytest tests/`（在 `chainlitexam/` 下跑）。
`test_decision_weather_tool.py` 的既有 import 失败照旧 `--ignore`。

## 风险

1. **header 投递**（唯一硬假设）。探针不过则退回"全局文件 + 每请求 ContextVar"的降级形态：
   `/qa/ask` 侧仍能按请求隔离（Chainlit 进程内的工具、prompt、缓存都对），只有 MCP 工具
   默认时间仍受全局文件影响——**比现状仍是净改善**。降级路径不需要改客户端契约。
2. **并发不同锚点**会让 MCP 缓存与 runtime 槽位多出几个条目，数量受"同时在用的不同锚点
   个数"限制，有界（LRU 兜底）。
3. **全局兜底仍存在**（Fork 1 决策 A）。它只在有人显式调旧式 `{"datetime"}` 时才生效；
   文档需明确写着"仅用于服务端 curl 验收"。
4. 现有 `test_message_orchestrator.py::test_process_message_skips_fast_paths_when_disabled`
   等已知 flaky/顺序依赖用例照旧，不计为本设计回归。

## 回滚

- 代码层：`time_source` 的请求级分支读取失败即回落文件，等价旧行为；
  新增字段均为可选，旧客户端不受影响。
- 前端未上线时，旧式 `{"datetime"}` + 全局文件路径与本设计前**逐字一致**。
- 完整撤销 = 还原 `chainlitexam/utils/time_source.py`、`haihe-weather-analyzer-mcp/time_source.py`、
  `chainlitexam/mcp_loader.py`、`chainlitexam/chain_gzt.py`、`chainlitexam/qa_http_api.py`
  的相关改动 + 重启两进程。
