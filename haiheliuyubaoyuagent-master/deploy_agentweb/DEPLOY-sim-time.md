# 切换系统时间功能 — 部署说明（2026-08-21）

在智能体加一个**修改系统时间**的功能：把全局"现在"锚定到任意指定的 年-月-日 时:分（如 `2026-07-10 15:00`），使"今天/明天/未来三天/本周末/今天下午/14时"以及工具取数的默认时间全部按该日期回答。**不是模拟**——是全局切换"现在"。带"恢复真实时间"按钮。

- 入口：AgentWeb 注入独立 JS 面板（同滚轮看图器部署路径，免源码、免重新打包、免重启 Tomcat）。
- 后端：调 8003 的 `/api/v1/admin/system-time`（与 `/qa/ask` 同机同端口不同服务）。
- 穿透进程边界：Chainlit（8003）与 MCP（SSE 3333）是**两个独立进程**，共享一个 JSON 文件作为单一事实源，两包各放一份内容一致的薄模块 `time_source.py`。

---

## 一、改动清单（拷贝清单）

### 新增文件（两包各一份，内容一致）
| 源（本仓库） | 目标（服务器） |
|---|---|
| `chainlitexam/utils/time_source.py` | `.../chainlitexam/utils/time_source.py` |
| `haihe-weather-analyzer-mcp/time_source.py` | `.../haihe-weather-analyzer-mcp/time_source.py` |
| `chainlitexam/AgentWeb/sim-time-agentweb.js` | `.../webapps/AgentWeb/sim-time-agentweb.js`（webapp 根级，2026-08-21 用户确认放根级，无需 public/ 子目录） |

### chainlitexam（8003 进程）— 改动的文件
- `chain_gzt.py`：`【当前日期】`前缀 today_str/weekday → `time_source.now()`；`_orchestrator_runtime_cache_key` 日键 → `time_source.override_date_str()`；新增 3 个 REST 端点 + `_after_system_time_changed()`（清 `qa_http_api._response_cache` + bump orchestrator runtime generation）。
- `qa_http_api.py`：`_runtime_epoch()` → `time_source.override_date_str()`；新增 `clear_response_cache()`。
- `message_orchestrator.py`：THINKING 提示 `current_time` → `time_source.now()`。
- `tools/warning_workflow.py`：`current_time` → `time_source.now()`。
- `tools/decision_weather_core.py`：`_decision_now_bjt()` → `time_source.now(+08:00)`。

### haihe-weather-analyzer-mcp（MCP 进程）— 改动的文件
- `rolling_forecast_service.py`：全部 9 处 `datetime.now(TIANJIN_TIMEZONE)` → `time_source.now(...)`（含主注入点 `now = now or ...`）。
- `current_weather_observation_service.py`：2 处默认"现在"。
- `tools.py`：`get_server_time`、水位"今日零点"、面雨量默认窗口。
- `haihe_mcp_tools.py`：风力整点、降水实况长图默认窗口、应急时间段默认窗口。
- `custom_tools/safe_emergency_response_tool.py`：应急默认当前北京时整点。
- `custom_tools/` 其余默认"现在"锚点：`composite_longimg_tool.py`、`basin_drawing_tool.py`、`rainfall_describe_tool.py`、`hhweb_product_tool.py`、`poi_nearest_observation_tool.py`、`historical_same_period_rainfall_tool.py`、`last_year_max_daily_rainfall_tool.py`、`last_month_areal_rainfall_tool.py`、`year_to_date_areal_rainfall_tool.py`、`risk_warning_tool.py`、`forecast_evaluate_tool.py`。

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

---

## 三、REST 接口

| 方法/路径 | body | 说明 |
|---|---|---|
| `POST /api/v1/admin/system-time` | `{"datetime":"2026-07-10 15:00","note":"..."}` | 设置锚定时间。支持 `YYYY-MM-DD[ HH:MM[:SS]]` / ISO；仅日期（如 `2026-07-10`）时分取设置那一刻的真实时刻 |
| `POST /api/v1/admin/system-time/clear` | `{}` | 恢复真实时间（删共享文件） |
| `GET /api/v1/admin/system-time` | — | 返回 `{active, override_datetime, real_now}`，面板据此常显"模拟中" |

鉴权按 `/qa/ask` 网络层模型（部署时网络层限制，不加管理员校验）。

---

## 四、验证（端到端，6 个测试问题全部按 7月10日 回答）

1. `GET /api/v1/admin/system-time` → `active:false`。
2. `POST /api/v1/admin/system-time {"datetime":"2026-07-10 15:00"}` → `active:true`。
3. 逐问经 `POST /api/v1/qa/ask` 断言日期口径：
   - ① 未来三天天津港附近天气 → 7/11–13
   - ② 今天下午天津港附近有雨吗 → 7/10 下午
   - ③ 本周末适合去泰达航母主题公园游玩吗 → 7/11(六)–7/12(日)
   - ④ 明天适合去蓟州游玩吗 → 7/11
   - ⑤ 下周一津泰达实验学校附近天气 → 7/13
   - ⑥ 生成7月10日下午14时的实况和预报 → 7/10 14:00（14时前=实况、后=预报；长图类工具按 7/10 14 时整点）
4. 每步 `GET /api/v1/admin/system-time` 确认状态；同问题在覆盖前后不串味（`_response_cache` 已清）。
5. AgentWeb 面板实测：设置 → 状态变"模拟中：2026-07-10 15:00" → 发问按 7/10 → 点"恢复" → 回"真实时间"。

---

## 五、风险与已知缺口

1. **滚动预报后端历史起报周期归档深度（最大现实风险）**：覆盖到过去日期问"未来N天"，需要该日 08/20 起报周期数据；若后端只留最近周期不归档 → 返回空/报错。**先用 ⑥ 之类实测确认**；若不归档，降级口径 = 过去日期的"未来N天"提示无预报数据、仅实况/历史可答（实况类 MUSIC 历史有归档）。
2. **忘记恢复（footgun）**：覆盖文件跨服务重启存活，忘 clear 会一直按 7/10 回答 → 面板常显"模拟中"提醒 + `GET` 状态接口兜底。
3. **明确不改（记录发生时刻，非"现在"语义）**：审计/日志时间戳（`generated_at`/`EVT-` 事件码/队列/`rest_api.py` 等）。已知缺口（不在本期范围）：预警正文报告时间、应急网格起报时次选择（`rolling_forecast_grid.py`）、fast path（`ENABLE_FAST_PATHS=false` 默认关闭）。

## 六、回滚

- 删掉共享覆盖文件（`rm` 系统临时目录下的 `haihe_system_time_override.json`，或 `POST .../clear`）→ 回真实时间；前端移除 `index.html` 里的 script 行。
- 全部撤销 = 用改动前的整文件覆盖回 8003 与 MCP 两个包 + 重启两服务。
