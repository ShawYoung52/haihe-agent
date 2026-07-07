# AGENTS

## 项目工作手册

本文件写给 Codex 和后续自动化开发代理。进入新会话后，先读规则，再动代码。当前阶段的优先目标是稳定项目上下文和业务边界，不急于重写实现。

## 每次新会话先读

按顺序阅读：

1. `PRODUCT.md`：确认产品给谁用、第一版做什么、不做什么。
2. `DESIGN.md`：确认这是业务工具，以及 PC/手机端和界面约束。
3. `current-progress.md`：确认当前进度、下一步任务和风险。
4. `haiheliuyubaoyuagent-master/README.md`：了解智能体、Chainlit、MCP、REST 服务、工具来源和常见问题。注意该文件在当前环境可能显示编码异常，但仍是重要上下文。
5. `haiheliuyubaoyuagent-master/chainlitexam/README.md`：了解 Chainlit 前端智能体、消息编排、快速路径和本地/外部工具。
6. `haiheliuyubaoyuagent-master/chainlitexam/chain_gzt.py`：了解用户认证、用户管理接口和 FastAPI 路由（用户体系文档尚未单独成文，以代码为准）。
7. `haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/rest_api.py`、`emergency_api.py`、`emergency_http_server.py`：了解 REST 和应急响应接口（接口文档尚未单独成文，以代码为准）。
8. 具体任务相关代码文件。不要在没读相关代码前凭记忆修改。

如果任务涉及旧的企业微信预警系统，再阅读 `hhlyqyxt-master/README.md` 和相关 `Controller/`、`Service/`、`ScheduledTask/` 文件。

## 重要目录

- `haiheliuyubaoyuagent-master/chainlitexam/`：Chainlit 智能体主入口、用户认证、FastAPI 路由、消息编排、快速路径、前端静态资源和外部技能封装。
- `haiheliuyubaoyuagent-master/chainlitexam/fast_paths/`：快速路径实现已拆分为独立包，包含降雨、POI 天气、水位、预警、应急响应、暴雨影响时间等路径。
- `haiheliuyubaoyuagent-master/chainlitexam-gis/`：GIS 相关 Chainlit 试验/联动版本。
- `haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/`：天气分析 MCP、REST API、河网、面雨量、预警、应急响应、WMS 矢量服务等后端能力。
- `hhlyqyxt-master/`：海河流域企业微信/预警相关系统，和主智能体目录并列存在，修改前要确认任务是否真的指向这里。
- `haiheliuyubaoyuagent-master/weather-analyzer-mcp-20260206/`：历史或打包版本参考，不应随意改动。

## 不能破坏的功能

- Chainlit 登录和 `@cl.password_auth_callback` 认证链路。
- `admin`、`forecaster`、`external` 三类角色语义和权限边界。
- 注册接口不得允许普通注册 `admin`。
- 默认管理员账号不能被禁用。
- 消息编排器与 `fast_paths/` 包中的快速路径顺序和业务命中逻辑，尤其是降雨图、河网图、降雨分析、面雨量、预警、子流域天气、应急响应等路径。
- 暴雨影响河网逻辑：直接下游、间接下游、上游的语义不能混淆；河流名称匹配、流向校正、GeoJSON 几何清理等规则不要绕过。
- 子流域天气规则：不能把全流域或天津市预报直接套到某个子流域。
- 面雨量查询规则：面雨量问题应走 `query_basin_areal_rainfall` 等专用能力，不用站点级降雨分析替代。
- 应急响应判定规则：不要绕过既有工具或阈值逻辑自行判断等级。
- 河网上下游关系：直接下游、间接下游、上游的语义不能混淆。
- 外部数据源和内网服务配置：MUSIC、PostgreSQL/PostGIS、GeoServer、RAG、预警接口、MCP SSE 地址等不能硬改为个人环境。
- 现有输出图、样例数据、GIS/WMS 服务入口和接口路径。

## 修改前要谨慎的文件

- `haiheliuyubaoyuagent-master/chainlitexam/chain_gzt.py`：同时包含 Chainlit、FastAPI、认证、用户表初始化和工具绑定，改动影响面大。
- `haiheliuyubaoyuagent-master/chainlitexam/message_orchestrator.py`：消息路由和快速路径调用入口，容易引入路由回归。
- `haiheliuyubaoyuagent-master/chainlitexam/fast_paths/*.py`：各快速路径实现，改动会影响意图命中顺序和工具选择。
- `haiheliuyubaoyuagent-master/chainlitexam/prompts.py`：系统提示词和业务规则集中地，改动会影响回答行为。
- `haiheliuyubaoyuagent-master/chainlitexam/external_skill_tools.py`、`mock_vendor_agents.py`：外部协同能力封装，涉及真实/模拟供应方能力。
- `haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/tools.py`、`haihe_mcp_tools.py`：MCP 工具注册和核心业务能力。
- `haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/rest_api.py`、`emergency_api.py`、`emergency_http_server.py`：REST 和应急事件接口。
- `haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/emergency_*`：应急响应状态、事件存储、管理、同步和接口。
- `haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/config.ini`：可能包含本地或内网配置，不要随意提交敏感修改。
- `*.sql`：数据库结构补丁，执行和修改前要确认目标库、schema 和回滚方案。
- `uv.lock`、`requirements*.txt`、`pyproject.toml`：依赖变更要有明确理由，避免无关锁文件 churn。
- 大型二进制、图片、压缩包、`.venv*`、`micromamba/`、输出目录：通常不要改动或纳入提交。

## 工作方式

- 先判断任务属于文档、前端、Chainlit 编排、MCP 后端、REST API、GIS/WMS、数据库还是旧企业微信系统。
- 小步修改，保持范围贴近用户请求。
- 修改前读相关文件，修改后做最小可行验证。
- 如果已有工作区存在未跟踪或他人改动，不要清理、回滚或覆盖，除非用户明确要求。
- 不要执行破坏性命令，例如强制重置、批量删除、清空目录。
- 不要把内网地址、账号、密码等写入新代码或新文档；已有文档中出现的默认值只作为项目现状记录。
- 输出给用户时使用简短中文，说明改了什么、验证了什么、还有什么风险。

## 命令输出控制

- 查找文件优先用 `rg --files`，查文本优先用 `rg`。
- 读取长文件时限制输出范围，避免一次打印几千行。
- PowerShell 中查看大文件可用 `Select-String`、`Get-Content -TotalCount` 或分段读取。
- 运行测试或服务时保留必要日志即可，不要把完整长日志贴给用户。
- 如果命令会访问内网、数据库或外部服务，先确认当前环境是否允许；失败时记录失败原因，不要反复刷屏重试。
- 不要在用户只要求“先不要写代码”时修改 `.py`、`.js`、`.css`、`.sql` 等运行代码文件；可以新增或更新项目文档。

## 建议验证

- 文档变更：确认 Markdown 文件存在、内容结构完整、没有明显错别字或路径错误。
- 用户体系变更：验证登录、注册、用户列表、禁用/启用、重置密码，以及非管理员访问受限。
- 消息编排变更：至少验证降雨图、降雨分析、面雨量、子流域天气、预警、河网、应急响应中的相关路径。
- MCP/REST 变更：优先用本地单元脚本或健康检查验证，不要默认依赖内网服务在线。
- 前端变更：PC 和手机视口都要检查布局是否重叠、文字是否溢出、按钮是否可操作。
