# Current Progress

## 当前状态

- 已建立第一版项目规则文档：`PRODUCT.md`、`DESIGN.md`、`AGENTS.md`、`current-progress.md`。
- 近期提交以代码修复为主，主要集中在：
  - 暴雨影响河网逻辑（rainfall impact river）：河流直接匹配、下游追踪、流向校正、GeoJSON 几何清理等。
  - 应急响应路径：优先使用官方一等应急响应、二等观测应急响应的调整与回退。
  - 删除无关代码和临时补丁文件。
- 仓库根目录下存在两个主要业务目录：
  - `haiheliuyubaoyuagent-master/`：海河流域暴雨洪水预报智能体，包含 Chainlit 前端智能体、MCP 后端、REST API、GIS/WMS、应急响应和河网能力。
  - `hhlyqyxt-master/`：海河流域企业微信/预警相关系统，和主智能体目录并列存在。
- 项目 README 中文可读；`AGENTS.md` 中引用的部分独立接口文档（如 `USER_API.md`、`REST_API_README.md`）尚未创建，需以代码为准。

## 已完成内容

- 梳理项目定位：业务工具，不是营销页面。
- 明确目标用户：预报员、应急值守人员、管理员、外部协同用户。
- 明确第一版范围：Chainlit 智能体、用户体系、降雨/预报、面雨量、河网、预警、应急响应、REST/API 和项目规则。
- 明确第一版不做：不重写架构、不扩展角色模型、不做营销首页、不绕过现有工具和规则。
- 明确设计约束：PC 端以值班研判为主，手机端以轻量查询和确认操作为主。
- 明确 Codex 工作手册：新会话先读哪些文件、哪些功能不能破坏、哪些文件改动要谨慎、命令输出如何控制。
- 近期代码侧完成：暴雨影响河网逻辑收紧（直接河流匹配、下游追踪、流向校正、GeoJSON 清理）、应急响应路径调整（官方一等/二等观测的优先级与回退）、删除无关代码。

## 现有能力线索

- `chainlitexam/chain_gzt.py`：Chainlit 会话入口、FastAPI、用户认证和用户管理接口。
- `chainlitexam/message_orchestrator.py`：消息路由、快速路径调用入口、多轮工具执行和 fallback。
- `chainlitexam/fast_paths/`：快速路径包，包含降雨、POI 天气、水位、预警、应急响应、暴雨影响时间等路径。
- `chainlitexam/prompts.py`：天气智能体系统提示词和关键业务规则。
- `chainlitexam/README.md`：项目整体与启动方式说明。
- `haihe-weather-analyzer-mcp/server.py`、`main.py`、`tools.py`、`haihe_mcp_tools.py`：MCP 服务和工具注册。
- `haihe-weather-analyzer-mcp/rest_api.py`、`emergency_api.py`、`emergency_http_server.py`：REST 和应急响应接口（以代码为准，独立接口文档尚未创建）。
- `haihe-weather-analyzer-mcp/WMS_VECTOR_SERVICE.md` 和 `wms_vector_service/`：GIS/WMS 矢量服务。

## 下一步任务

1. 统一确认项目主线：后续开发默认以 `haiheliuyubaoyuagent-master/` 为主，只有明确提到企业微信/预警系统时才进入 `hhlyqyxt-master/`。
2. 补齐缺失的独立文档：视需要创建用户体系接口说明、REST API 说明、河网 API 说明，降低后续接手成本。
3. 验证暴雨影响河网最新修复：确认直接河流匹配、下游追踪、流向校正和 GeoJSON 几何清理在实际数据上的稳定性。
4. 验证应急响应路径调整：确认官方一等/二等观测应急响应的优先级与回退逻辑符合业务规则。
5. 梳理启动方式：分别验证 Chainlit、MCP、REST API、应急 API、WMS 服务的本地启动命令和依赖。
6. 梳理环境变量：形成 `.env.example` 或配置说明，避免把内网地址、账号、密码散落在代码和文档中。
7. 给快速路径补一份测试清单：降雨图、河网图、降雨分析、面雨量、子流域天气、预警、应急响应、暴雨影响河网。
8. 评估用户管理接口现状：确认角色校验、默认管理员、禁用规则、密码重置和数据库表初始化是否符合文档。
9. 根据实际前端目标，决定继续增强 Chainlit 页面，还是增加独立管理/工作台前端。

## 风险与注意事项

- 中文文档存在编码问题，可能导致规则、接口说明或业务术语被误读；项目 README 目前可读。
- `AGENTS.md` 引用的部分独立接口文档尚未创建，后续开发需以代码为准。
- 项目里包含多个相似目录和历史版本，容易改错位置。
- 快速路径已拆分到 `fast_paths/` 包，修改时需同时关注 `message_orchestrator.py` 的调用顺序和具体路径实现。
- 内网服务依赖较多，本地环境可能无法完整验证 MUSIC、PostgreSQL/PostGIS、GeoServer、RAG、预警接口和 MCP SSE。
- `chain_gzt.py` 体量和职责较多，认证、API 和智能体入口耦合在一起，修改需谨慎。
- 快速路径命中顺序影响回答质量，新增意图识别可能造成旧问题走错工具。
- 面雨量、站点降雨、城市预报和子流域天气容易混淆，必须坚持业务边界。
- 应急响应属于高风险业务判断，不能凭模型文本自行生成等级；近期已对官方一等/二等观测路径做调整，需重点验证。
- 暴雨影响河网逻辑近期多次修复，河流名称匹配、流向校正和 GeoJSON 几何处理需要持续验证。
- 工作区已有未跟踪文件和目录，后续提交前要仔细区分本次修改与既有杂项。
