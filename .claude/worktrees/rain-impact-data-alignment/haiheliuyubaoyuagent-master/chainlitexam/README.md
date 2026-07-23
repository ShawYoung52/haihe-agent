# 海河流域暴雨洪水预报智能体

## 项目定位
基于 Chainlit 的海河流域气象问答智能体，主要提供：
- 降雨实况监测（天擎自动站）
- 降雨预报（ECMWF AIFS）
- 子流域面雨量对比（天擎面雨量实况）
- 河网/水系可视化
- 应急响应判定与影响范围分析

## 仓库结构
本仓库包含两个核心目录：
- `chainlitexam/`：前端 Chainlit 智能体（对话入口、FastAPI、GIS 联动、消息编排）。
- `haihe-weather-analyzer-mcp/`：后端 MCP 服务（天气/河网/应急响应/面雨量/预警/RAG 等工具实现）。

---

# chainlitexam/ 目录

## 核心入口
| 文件 | 作用 |
|------|------|
| `chain_gzt.py` | Chainlit 会话入口、FastAPI 服务、GIS 联动、工具绑定、用户认证。 |
| `message_orchestrator.py` | 消息路由、快速路径、多轮工具执行、Fallback 处理。 |
| `prompts.py` | 系统提示词 `WEATHER_ASSISTANT_PROMPT`，含工具使用规范与回答格式约束。 |
| `tools/rain_analysis.py` | 本地降雨分析工具 `local_analyze_rainfall_by_time`。 |
| `external_skill_tools.py` | 合作方能力工具封装（Alpha 水文 / Beta 应急 / 短临预报）。 |
| `mock_vendor_agents.py` | 合作方 mock 实现 + 短临预报真实 HTTP 客户端。 |
| `utils/MusicTool.py` | 天擎 MUSIC API 客户端。 |

## 全部 Python 文件清单
| 文件 | 说明 |
|------|------|
| `answers.py` | FAQ 知识库（14 条气象灾害 Q&A）。 |
| `chain_gzt.py` | 主入口。 |
| `external_skill_tools.py` | 合作方工具封装。 |
| `matcher.py` | FAQ 匹配器（jieba 分词）。 |
| `message_orchestrator.py` | 消息编排与快速路径。 |
| `mock_vendor_agents.py` | 厂商 mock / 短临预报客户端。 |
| `prompts.py` | 系统提示词。 |
| `puzzle.py` | 8 拼图 A* 求解示例（无关业务，可忽略）。 |
| `scripts/apply_chainlit_schema_patch.py` | Chainlit PG 表结构补丁。 |
| `tools/rain_analysis.py` | 本地降雨分析工具。 |
| `utils/config.py` | MUSIC / PG 配置常量。 |
| `utils/db.py` | SQLAlchemy engine。 |
| `utils/MusicTool.py` | MUSIC 客户端。 |

## 工具来源
智能体运行时合并三类工具：
1. **MCP SSE 工具**（`load_sse_tools()`）
   - weather MCP：降雨预报、暴雨影响、河网、面雨量、预警、RAG 等。
   - extreme-weather-statistics MCP：历史极端天气分析/图表/报告。
2. **本地降雨分析工具**（`build_rain_analysis_tools()`）
   - `local_analyze_rainfall_by_time`
3. **合作方 Skill 工具**（`build_external_skill_tools()`）
   - `route_partner_skill`、`invoke_partner_skill_alpha_hydro`、`invoke_partner_skill_beta_emergency`、`invoke_partner_skill_shortterm`

## 快速路径与路由
`message_orchestrator.py` 在调用 LLM Planner 之前，按以下顺序拦截：
1. 降雨分布图 `_try_rainfall_img_fast_path`
2. 河网图 `_try_river_plot_fast_path`
3. 降雨实况分析 `_try_rainfall_analysis_fast_path`
4. 全市平均降雨 `_try_city_avg_rainfall_fast_path`
5. 今天降雨时长 `_try_today_rain_duration_fast_path`
6. 今天降雨 `_try_today_rainfall_fast_path`
7. 未来一周预报 `_try_weekly_forecast_fast_path`
8. 近期强降雨检查 `_try_heavy_rain_check_fast_path`
9. 子流域预报 `_try_subbasin_forecast_fast_path`
10. 子流域面雨量 `_try_basin_areal_rainfall_fast_path`
11. 预警 `_try_warning_fast_path`
12. 周末活动 `_try_weekend_activity_fast_path`
13. 海河流域整体天气 `_try_basin_weather_fast_path`
14. 通用天气 `_try_general_weather_fast_path`

## 子流域天气查询规范
当用户询问“大清河流域未来三天天气”“子牙河未来几天降雨”等子流域未来天气时：
1. 应以该子流域的代表城市分别查询 `get_city_rainfall_time_range`；
2. 汇总为表格并说明“以代表城市预报近似反映子流域降雨趋势”；
3. 不得把全流域或天津市的预报直接套用到某个子流域上。

代表城市映射（见 `prompts.py`）：
- 大清河 → 保定、廊坊
- 子牙河 → 石家庄、衡水
- 永定河 → 北京、张家口
- 北三河 → 唐山、秦皇岛
- 漳卫南运河 → 邯郸、沧州
- 海河干流 → 天津

## 面雨量查询规范
当用户询问“各子流域面雨量对比”“过去一周哪个河系降雨最多”等面雨量问题时：
- 应使用 `query_basin_areal_rainfall`；
- 默认 11 分区（子流域），可通过 `zone_type` 指定 77/246 等分区；
- 该工具返回的是**实况**数据，数据来源为“天擎面雨量实况”。

---

# haihe-weather-analyzer-mcp/ 目录

## 项目定位
海河流域天气分析 MCP 服务，为前端智能体提供底层工具实现。

## 核心入口
| 文件 | 作用 |
|------|------|
| `server.py` | MCP 服务端入口，SSE 传输。 |
| `main.py` | 启动器。 |
| `tools.py` | 核心工具注册与降雨分析、河网、应急响应逻辑。 |
| `haihe_mcp_tools.py` | MUSIC 客户端与应急响应评估工具注册。 |
| `rest_api.py` | FastAPI REST 服务（端口 8002）。 |
| `emergency_api.py` | 应急响应 FastAPI（端口 8092）。 |
| `emergency_http_server.py` | 应急场景 HTTP 服务（端口 8080）。 |
| `models.py` | Pydantic 数据模型。 |
| `constants.py` | 常量与阈值。 |

## 服务清单
| 服务 | 端口 | 文件 |
|------|------|------|
| MCP SSE | 3333（可配置） | `server.py` |
| REST API | 8002 | `rest_api.py` |
| 应急响应 API | 8092 | `emergency_api.py` |
| 应急 HTTP 服务 | 8080 | `emergency_http_server.py` |
| WMS 矢量服务 | 8008 | `wms_vector_service/app.py` |

## 主要 MCP 工具
| 工具 | 用途 |
|------|------|
| `get_city_rainfall_time_range` | 城市降雨预报（ECMWF AIFS）。 |
| `get_station_rainfall_real_img` | 子流域降雨分布图/降水实况图。 |
| `analyze_rainfall_by_time` | 天擎站点降雨分析。 |
| `query_basin_areal_rainfall` | 子流域面雨量实况对比。 |
| `analyze_rainstorm_impact` | 暴雨影响范围分析（行政区划/分区）。 |
| `get_river_network_for_plot` | 河网拓扑数据。 |
| `get_xialiu_rivername` | 下游河流名称。 |
| `estimate_river_impact_time` | 河流影响时间估算。 |
| `get_effective_warning_info` | 当前生效预警。 |
| `get_history_warning_info` | 历史预警。 |
| `get_national_warning_info` | 国家局/中央气象台预警。 |
| `rag_search` | 知识库检索。 |
| `evaluate_haihe_emergency_response` | 应急响应判定（实况）。 |
| `evaluate_haihe_forecast_emergency_response` | 应急响应判定（预报）。 |

## 数据来源
| 来源 | 用途 |
|------|------|
| MUSIC（天擎）`10.226.90.120` | 自动站实况、统计降水。 |
| ECMWF AIFS / EC GRIB2 | 城市降雨预报、面雨量。 |
| PostgreSQL/PostGIS `10.226.107.130` | 河网几何、行政区划、分区图层、应急事件。 |
| CMA 预警接口 | 国家/本地预警信息。 |
| RAG 知识库 `10.226.188.156:8033` | 标准规范、专家经验等检索。 |
| 降水实况图服务 `10.226.107.35:8001` | 子流域降雨分布图生成。 |

## 关键环境变量 / 配置
- `MUSIC_SERVICE_IP`、`MUSIC_SERVICE_NODE_ID`、`MUSIC_USER_ID`、`MUSIC_PASSWORD`
- `config.ini`：paths / postgres / geoserver 配置（运行时必需，仓库中可能缺失）。

---

# 常见问题排查

## 1. 大清河流域未来三天天气
- **预期工具**：`get_city_rainfall_time_range` 分别查保定、廊坊未来 3 天，然后由模型汇总。
- **快速路径行为**：`_try_basin_weather_fast_path` 明确排除子流域关键词（`sub_basins` 包含“大清河”），因此不会走全流域快速路径，应交回 Planner 处理。
- **风险点**：
  - 若 Planner 未严格按 `prompts.py` 规范执行，可能误用天津市或全流域数据回答。
  - 若 `_try_subbasin_forecast_fast_path` 存在但拦截逻辑不完善，可能未命中而进入通用路径。
- **验证方法**：查看运行日志中实际调用的 `tool.name` 和参数。

## 2. 各子流域面雨量对比
- **预期工具**：`query_basin_areal_rainfall`（子流域面雨量实况排名）。
- **常见误调用**：
  - 命中 `_try_rainfall_analysis_fast_path` → 调用 `analyze_rainfall_by_time` / `local_analyze_rainfall_by_time`，返回站点级结果。
  - 命中 `_try_rainfall_img_fast_path` → 只出图无数值。
- **原因**：
  - “面雨量”关键词可能未被 Planner 识别；
  - 本地降雨分析工具与天擎面雨量工具功能边界模糊。
- **验证方法**：确认日志中是否出现 `query_basin_areal_rainfall`。

## 3. 日志定位
- MCP 工具加载：`✅ MCP 工具加载成功，共 X 个工具：[...]`
- 本地工具合并：`✅ 本地工具已合并，当前工具列表：[...]`
- 每轮工具调用：`=== 第 N 轮工具调用 ===`、`[工具] <tool_name> 参数: ...`
- GIS 联动：`[GIS_JSON]{...}`
- 快速路径：`=== XXX 快速路径 ===`

---

# 环境变量

## chainlitexam
- `MCP_SERVER_URL`：weather MCP SSE 地址，默认 `http://localhost:3333/sse`
- `EXTRM_SERVER_URL`：extreme-weather-statistics MCP SSE 地址，默认 `http://10.226.107.133:8000/sse`
- `CHAINLIT_DB_*`：Chainlit 历史会话数据库连接
- `DB_*`：河网绘图 PostgreSQL 连接

## haihe-weather-analyzer-mcp
- `MUSIC_SERVICE_IP`、`MUSIC_SERVICE_NODE_ID`、`MUSIC_USER_ID`、`MUSIC_PASSWORD`
- `GEOSERVER_BASE_URL`
- `config.ini`：paths / postgres / geoserver

---

# 启动方式

## 启动后端 MCP 服务
```bash
cd haihe-weather-analyzer-mcp
python server.py
# 或
python -m server
```

## 启动前端智能体
```bash
cd chainlitexam
chainlit run chain_gzt.py
```