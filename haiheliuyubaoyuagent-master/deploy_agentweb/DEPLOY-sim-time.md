# 切换系统时间功能 — 部署说明

把智能体的"现在"锚定到任意指定的 年-月-日 时:分（如 `2026-07-10 15:00`），使
"今天/明天/未来三天/本周末/今天下午/14时"以及工具取数的默认时间全部按该日期回答。
带"恢复真实时间"按钮。

> **2026-09-20 起改为「请求级锚定」**：锚点由调用方**逐请求携带**，只影响带了锚点的
> 那个请求，服务端**不落任何跨用户状态**。此前是"全局切换"——一人切时间、所有用户
> （含天河小程序调 `/api/v1/qa/ask` 的回答）一起变，且恢复没生效就会一直锚在过去。
> 设计见 `docs/superpowers/specs/2026-09-20-per-request-reference-time-design.md`。

- 入口：AgentWeb 注入独立 JS 面板（同滚轮看图器部署路径，免源码、免重新打包、免重启 Tomcat）。
- 后端：调 8003 的 `/api/v1/admin/system-time`（与 `/qa/ask` 同机同端口不同服务）。
- 穿透进程边界：Chainlit（8003）与 MCP（SSE 3333）是**两个独立进程**。请求级锚点走
  **MCP header**（`x-haihe-reference-time`，由 chainlitexam 的 tool_interceptor 注入、
  fastmcp `get_http_headers()` 读回）；另有一份共享 JSON 文件作为**全局兜底**，
  只供服务端 curl 验收，**不要给前端用**。

---

## 一、改动清单（拷贝清单）

### 新增文件（两包各一份，内容一致）
| 源（本仓库） | 目标（服务器） |
|---|---|
| `chainlitexam/utils/time_source.py` | `.../chainlitexam/utils/time_source.py` |
| `chainlitexam/utils/reference_time.py` | `.../chainlitexam/utils/reference_time.py` |
| `haihe-weather-analyzer-mcp/time_source.py` | `.../haihe-weather-analyzer-mcp/time_source.py` |
| `chainlitexam/AgentWeb/sim-time-agentweb.js` | `.../webapps/AgentWeb/sim-time-agentweb.js`（webapp 根级，2026-08-21 用户确认放根级，无需 public/ 子目录） |

> `chainlitexam/utils/time_source.py` 与 `haihe-weather-analyzer-mcp/time_source.py`
> 必须**逐字一致**（拷完 `diff` 一下）。

### chainlitexam（8003 进程）— 改动的文件
- `utils/time_source.py`：新增请求级锚点（ContextVar + `set_request_override` /
  `reset_request_override` / `request_override_header_value` / `effective_override`）
  与 MCP header 读取；`now()` 优先级改为 **请求 → header → 文件 → 真实**。
- `utils/reference_time.py`（**新增**）：请求锚点解析纯函数（fixed/real/inherit）+ `request_anchor`。
- `mcp_loader.py`：`_inject_reference_time` interceptor（请求级锚点 → `x-haihe-reference-time` header）。
- `chain_gzt.py`：`SetSystemTimeRequest` 三态契约 + `_set_system_time` 分支；
  `_prompt_date_prefix(day)`；`_build_orchestrator_runtime(day, mcp_tools=)` /
  `_orchestrator_runtime_cache_key(day)` / `_orchestrator_runtime_state`（日期槽位表 + 共享
  `mcp_tools`）/ `_evict_runtime_slots` / `_get_orchestrator_runtime(day)` / `_build_qa_runtime(day)`；
  `QAAskRequest` 三个可选字段 + 透传；`on_message` 按 `message.metadata` 锚点取槽位。
- `qa_http_api.py`：`_runtime_epoch(day)`；新增 `_response_cache_key(...)`（键含时间维度 `t`）；
  `QARuntime._runtimes` 按日期分槽 + 上限 `QA_API_RUNTIME_MAX_SLOTS`；
  `ask(metadata=, reference_time=, time_mode=)`；`_run_once(anchor=)` 用 `request_anchor`
  包住 `process_message`。
- `message_orchestrator.py` / `tools/warning_workflow.py` / `tools/decision_weather_core.py`：
  沿用统一时间源，**本次未改**——自动获得按请求生效的能力。

### haihe-weather-analyzer-mcp（MCP 进程）— 改动的文件
- `time_source.py`：与 chainlitexam 那份**逐字一致**（即上面那份 + header 读取）。
- 其余 17 个 `time_source.now()` 调用点（`rolling_forecast_service.py`、
  `current_weather_observation_service.py`、`tools.py`、`haihe_mcp_tools.py`、
  `custom_tools/*` 等）：**本次未改**——自动获得按请求生效的能力。
- `probe_reference_time_header.py`（**新增**）：header 投递探针，改完必跑。

**新增环境变量**（可选，都有默认值）：`ORCHESTRATOR_RUNTIME_MAX_DAYS`（默认 4）、
`QA_API_RUNTIME_MAX_SLOTS`（默认 8）——两个进程各自的日期槽位上限。

> 服务器无 git，整文件拷贝覆盖即可（每个文件里的其他功能改动一并带过去）。

---

## 二、部署步骤

1. **拷贝文件**到服务器两个包目录（按第一节清单）。
2. **重启两个服务**（都必须重启，否则 time_source + 锚点改动不生效）：
   - `systemctl restart haihe-chainlit`（8003）
   - MCP `server.py` 进程（SSE 3333，重启后等就绪）
3. **AgentWeb 前端**（2026-08-21 用户确认：文件放 webapp **根级**，不建 public/ 子目录）：
   - 拷 `sim-time-agentweb.js` → `webapps/AgentWeb/`（看图器 `img-zoom-agentweb.js` 同样放根级）；
   - `index.html` 在 `<head>` 里引用两个根级脚本（同看图器注入方式）：
     `<script src="./img-zoom-agentweb.js"></script>` + `<script src="./sim-time-agentweb.js"></script>`；
   - **无需重启 Tomcat**（静态资源即拷即用，必要时清浏览器缓存）。
   - **面板挂载**：脚本运行时自动找页面"说明"元素，把面板锚在其**左侧、垂直居中**（找不到则回退右下角悬浮）。**比例随"说明"自适应**：面板字号/宽度在运行时按"说明"元素的 computed font-size 等比推导（内距用 em），不写死像素；**默认收起为小胶囊**「🕒 系统时间·真实/模拟中」，点胶囊才展开完整控件（避免大卡片压住说明旁）。本地参照页（模拟"使用说明"导航）已截图验证：左侧+居中+比例协调、点按展开/收起正常。
   - **注意**：若前端同事重新构建又把 index.html 改回 `./public/*.js` 引用，404 会复现——把引用改回根级即可。
   - **注意2（2026-09-01，AgentWeb(3) 重建又踩）**：前端同事重新打包**拿了旧版自定义 JS**——`img-zoom-agentweb.js` 被换成缺「思考过程自动折叠」IIFE 的旧版（只剩看图器段），导致折叠失效（后端 chainlit 2.9.6 不发 auto_collapse，折叠全靠该 JS 监听 `chainlit_reasoning_complete`）。**修复 = 用仓库 `chainlitexam/AgentWeb/img-zoom-agentweb.js` 整文件覆盖包内同名文件**（免重启、清缓存）。排查口令：`diff 包内文件 chainlitexam/AgentWeb/img-zoom-agentweb.js`，看是否缺 `chainlit_reasoning_complete`/`scanOpenReasoningSteps`。sim-time JS 本次未受影响。**前端同事每次重建后，务必把仓库里这两个自定义 JS 原样回拷。**
   - **avatar.svg 404（无害）**：`.chainlit/config.toml` 的 `logo_file_url`/`default_avatar_file_url` 指向 `avatar.svg`（后端 chainlit 进程托管 UI 时由其 public/ 提供）。AgentWeb 独立静态包没有该文件 → 控制台 404，但 logo/头像有兜底渲染、不影响功能。要消除：把 `chainlitexam/public/avatar.svg` 拷到包内 `public/avatar.svg`（本批已在 AgentWeb(3) 包内建好）；若部署后仍 404，说明浏览器按站点根绝对路径 `/public/avatar.svg` 解析，需把 avatar.svg 放到 Tomcat 根应用的 `public/` 下，或直接忽略此 console 噪音。
   - **完整构建已入仓库（2026-09-01，R27）**：`chainlitexam/AgentWeb/` 现保存**当前完整前端构建**（assets bundle + index.html + `config/quickQA.json` + `public/avatar.svg` + 两个自定义 JS），与新包 AgentWeb(3) 全内容一致。以后端同事重建后，把新 bundle/index.html/config 同步进该目录、**保留并核对** `img-zoom-agentweb.js`（折叠逻辑）与 `sim-time-agentweb.js` 两自定义文件未被旧版覆盖即可；部署 = 从该目录整体拷到 `webapps/AgentWeb/`。

---

## 三、REST 接口

### `POST /api/v1/admin/system-time`（三种 body）

| body | 行为 |
|---|---|
| `{"datetime":"2026-07-10 15:00","note":"..."}` | **旧式**：写**全局**覆盖文件。进程级开关，一人改全员受影响。**只用于服务端 curl 手工验收**。仅日期（如 `2026-07-10`）时时分取设置那一刻的真实时刻 |
| `{"metadata":{"time_mode":"fixed","reference_time":"2026-07-10T15:00:00+08:00"}}` | **新式**：**无状态**，只校验 + 回显 `{active:true, mode:"per_request", override_datetime, display}`，**不写文件**。锚点由客户端后续逐请求携带 |
| `{"metadata":{"time_mode":"real"}}` | 回 `{active:false}`，**并清掉遗留的全局覆盖文件**（修"改回去了还是旧日期"） |

顶层同名字段等价可用（`{"reference_time":"..."}` / `{"time_mode":"real"}`）。
两个字段都不给 = 回到真实时间（并清文件）。`reference_time` 非法 → 400。

### 其它

| 方法/路径 | 说明 |
|---|---|
| `POST /api/v1/admin/system-time/clear` | 恢复真实时间（删共享文件） |
| `GET /api/v1/admin/system-time` | 返回 `{active, override_datetime, real_now}`——`active` 指**全局兜底开关**，不是请求级锚点 |

鉴权按 `/qa/ask` 网络层模型（部署时网络层限制，不加管理员校验）。

## 三之二、前端契约（请求级锚点）

锚点逐请求携带，**两个入口同款字段**：

- **HTTP 问答**：`POST /api/v1/qa/ask` body 里带
  `{"metadata": {"time_mode": "fixed", "reference_time": "2026-07-10T15:00:00+08:00"}}`
  （或顶层 `reference_time` / `time_mode`）。
- **网页聊天（WebSocket）**：把同样字段挂进 user_message 的 `metadata`
  （Chainlit 本来就透传 `metadata`，原先只放了 `location`）。

语义：

- `time_mode: "fixed"` + `reference_time` → 本次请求按该时刻回答。
- `time_mode: "real"`（另接受 `dynamic`/`live`）→ 本次请求强制真实时间，
  **压过**遗留的全局覆盖文件。
- 都不给 → 回落全局兜底文件 → 真实时间。

**不带就完全不受影响**——因此旧客户端行为与改造前逐字一致。

## 三之三、网页端（AgentWeb）已实现请求级锚定

2026-09-20 起 `sim-time-agentweb.js` **已按请求级锚定改写**，三步都在本文件里：

1. 锚点存 `localStorage["haihe_reference_time"]`（ISO 串 = 锚定；`"real"` = 强制真实时间）；
2. 用 `WebSocket.prototype.send` 钩子把锚点塞进 `client_message` 帧的 `metadata`
   （Chainlit 服务端 `Message.from_dict` 会透传 metadata 到 `on_message`）；
3. 面板**不再**调旧式 `{"datetime":...}`，改调无状态 `metadata` 契约；
   "恢复"还会让服务端清掉可能遗留的**全局**覆盖文件。

**为什么钩 WebSocket 而不是改 bundle**：`assets/index-*.js` 是打包产物，前端同事重建后整体覆盖；
本文件是"重建后必须回拷"的两个自定义 JS 之一，改动能活下来。钩在协议层（socket.io 帧），
**bundle 怎么重建都不影响本机制**（只要事件名仍是 `client_message`）。

### 部署与验证

- 拷 `sim-time-agentweb.js` → `webapps/AgentWeb/`（与 `img-zoom-agentweb.js` 同位置），
  `index.html` 的 `<head>` 引用；**无需重启 Tomcat**，但**浏览器可能缓存旧版 → 让用户强刷**。
- ⚠️ 前端同事每次重建后务必把仓库里这两个自定义 JS 原样回拷（旧版 sim-time 会退回全局行为）。
- 控制台应看到 `[SIM_TIME_AW] WebSocket 锚点注入钩子已安装`；设锚点后首次提问出现
  `[SIM_TIME_AW] 锚点已随请求下发`。现场排查：控制台执行 `__haiheSimTime.selfTest()`。
- 断言（单浏览器）：设置 → 本机提问按锚定日期；**另一个浏览器/天河小程序不受影响**
  （这是本次要修的核心）。
- 若将来 Chainlit 改掉 `client_message` 事件名，注入会**静默失效**（退化为不发锚点、
  回落真实时间）——不会报错也不会污染别的帧；用上面两条控制台日志确认。

### 测试

`chainlitexam/tests/test_sim_time_agentweb.js`（26 项，Node 直跑，无依赖）：

```bash
node chainlitexam/tests/test_sim_time_agentweb.js   # 期望末行 ALL PASS
```

覆盖：未设锚点原样放行（逐字不变）、注入后原有字段保留、`real` 哨兵、8 类不该碰的帧
一律原样放行、前端自带锚点不覆盖、**带 ack id 的帧前缀不丢**、localStorage 抛异常不影响发送。

## 四、验证（端到端）

### 4.1 请求级锚点（新，推荐）

1. `POST /api/v1/qa/ask`，body 带
   `{"question":"今天下午天津港附近有雨吗","metadata":{"time_mode":"fixed","reference_time":"2026-07-10T15:00:00+08:00"}}`
   → 断言按 7/10 下午回答。
2. 同一问题**不带** metadata → 按真实日期回答（证明没串味）。
3. **并发**两个不同 `reference_time` 的请求 → 各自按各自日期回答（这是本次要修的核心）。
4. body 带 `{"metadata":{"time_mode":"real"}}` → 即使遗留了全局覆盖文件也按真实时间回答。
5. 用 `GET /api/v1/admin/system-time` 确认 `active:false`（新式调用不写全局文件）。
6. 非法 `reference_time`（如 `"昨天下午"`）→ 400。

### 4.2 旧式全局兜底（仅服务端 curl 验收）

1. `GET /api/v1/admin/system-time` → `active:false`。
2. `POST /api/v1/admin/system-time {"datetime":"2026-07-10 15:00"}` → `active:true`。
3. 逐问经 `POST /api/v1/qa/ask`（**不带 metadata**）断言日期口径：
   - ① 未来三天天津港附近天气 → 7/11–13
   - ② 今天下午天津港附近有雨吗 → 7/10 下午
   - ③ 本周末适合去泰达航母主题公园游玩吗 → 7/11(六)–7/12(日)
   - ④ 明天适合去蓟州游玩吗 → 7/11
   - ⑤ 下周一津泰达实验学校附近天气 → 7/13
   - ⑥ 生成7月10日下午14时的实况和预报 → 7/10 14:00
4. **恢复**：`POST /api/v1/admin/system-time {}`（空 body 也回真实时间并清文件）
   或 `{"metadata":{"time_mode":"real"}}` → `GET` 回 `active:false`，
   再问同一问题按真实日期回答。

### 4.3 跨进程 header 投递（改完必跑）

```bash
cd haihe-weather-analyzer-mcp && <venv>/python.exe probe_reference_time_header.py
```
期望末行 `[PROBE] RESULT=OK`。这条验证的是"请求级锚点能到 MCP 工具"这个硬假设，
**不通过就不要上生产**（降级方案见 spec「风险 1」）。

---

## 五、风险与已知缺口

1. **滚动预报后端历史起报周期归档深度（最大现实风险）**：覆盖到过去日期问"未来N天"，需要该日 08/20 起报周期数据；若后端只留最近周期不归档 → 返回空/报错。**先用 ⑥ 之类实测确认**；若不归档，降级口径 = 过去日期的"未来N天"提示无预报数据、仅实况/历史可答（实况类 MUSIC 历史有归档）。
2. **忘记恢复（footgun）**：**请求级锚点不受影响**——请求结束即失效，不需要任何"恢复"。
   只有旧式全局兜底仍会跨服务重启存活，忘 clear 就一直按 7/10 回答；因此全局兜底
   **只留给服务端 curl 验收**，前端路径一律走请求级。
3. **明确不改（记录发生时刻，非"现在"语义）**：审计/日志时间戳（`generated_at`/`EVT-` 事件码/队列/`rest_api.py` 等）。已知缺口（不在本期范围）：预警正文报告时间、应急网格起报时次选择（`rolling_forecast_grid.py`）、fast path（`ENABLE_FAST_PATHS=false` 默认关闭）。

## 六、回滚

- 删掉共享覆盖文件（`rm` 系统临时目录下的 `haihe_system_time_override.json`，或 `POST .../clear`）→ 回真实时间；前端移除 `index.html` 里的 script 行。
- 全部撤销 = 用改动前的整文件覆盖回 8003 与 MCP 两个包 + 重启两服务。
