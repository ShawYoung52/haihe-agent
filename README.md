# haihe-agent

海河流域防汛气象智能体项目。

当前仓库主要包含两个工程：

```text
haiheliuyubaoyuagent-master/
  chainlitexam/                  # 前端 / 智能体交互层：Chainlit 聊天界面、LLM 编排、GIS 联动、用户认证
  haihe-weather-analyzer-mcp/    # 后端 / MCP 能力层：FastMCP 工具服务，提供气象、水文、河网、暴雨影响等工具
```

## 架构说明

```text
浏览器 / GIS 父页面
   ↓ iframe / postMessage
chainlitexam
   - Chainlit 聊天 UI
   - LLM 调用与 Agent 编排
   - 用户登录 / 用户管理
   - GIS 联动消息转发
   - 调用 MCP 工具
   ↓ SSE
haihe-weather-analyzer-mcp
   - FastMCP 服务
   - 降雨、河网、暴雨影响、POI、滚动预报等工具
   - 标准化业务数据返回
```

## 快速启动

### 方式一：使用脚本启动

```bash
# 先启动 MCP 后端
bash scripts/run_mcp_backend.sh

# 再启动 Chainlit 前端 / 智能体交互层
bash scripts/run_chainlit_frontend.sh
```

### 方式二：使用 Docker Compose 启动

```bash
docker compose up --build
```

默认服务地址：

```text
MCP 后端: http://127.0.0.1:3333/sse
Chainlit 前端: http://127.0.0.1:8003
```

### 方式三：手动启动 MCP 后端

```bash
cd haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp
python -m venv .venv
source .venv/bin/activate  # Windows 使用 .venv\Scripts\activate
pip install -r requirements.txt
python main.py --host 0.0.0.0 --port 3333
```

默认 SSE 地址：

```text
http://127.0.0.1:3333/sse
```

### 方式四：手动启动 Chainlit 前端 / 智能体交互层

```bash
cd haiheliuyubaoyuagent-master/chainlitexam
python -m venv .venv
source .venv/bin/activate  # Windows 使用 .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# 修改 .env 中的模型、数据库、MCP 地址等配置
chainlit run chain_gzt.py --host 0.0.0.0 --port 8003
```

默认访问地址：

```text
http://127.0.0.1:8003
```

## 检查命令

```bash
make check
# 或
python scripts/check_repository.py
```

检查内容包括：

- 是否误提交虚拟环境、缓存、本地配置、日志等文件；
- Python 文件是否能通过基础语法编译。

## 环境变量

请参考：

- `haiheliuyubaoyuagent-master/chainlitexam/.env.example`
- `haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/.env.example`

生产环境不要把真实 `.env`、数据库密码、模型 API Key、证书等提交到 GitHub。

## 文档

- `docs/ARCHITECTURE.md`：前端 / 后端职责边界与短临智能体接入建议。
- `docs/CLEANUP_AND_REFACTOR_PLAN.md`：清理记录、下一步安全整改、重构计划、GIS 安全和测试建议。
- `docs/DEPLOYMENT.md`：本地运行、脚本启动、部署顺序和生产环境注意事项。

## 当前已知工程化建议

1. 不要提交 `.venv`、`.venv_new`、`.env`、IDE 配置、缓存文件和生成产物。
2. 模型 API Key、数据库密码、默认管理员密码必须从环境变量读取。
3. `chainlitexam` 当前同时承担 UI、BFF、Agent 编排、用户认证和 GIS 联动，后续建议拆分为 `auth.py`、`db.py`、`llm.py`、`mcp_client.py`、`gis.py`、`app.py`。
4. `haihe-weather-analyzer-mcp` 应保持为后端工具能力中心，业务计算尽量放在 MCP 工具侧，展示格式尽量放在 Chainlit 侧。
5. GIS `postMessage` 和 FastAPI CORS 在生产环境必须收紧白名单。

## 本分支改动说明

`chore/web-cleanup-20260702` 分支完成了网页端整理、无关文件清理和第四版工程化优化：

- 新增根目录 `.gitignore`，防止继续提交虚拟环境、密钥、缓存和生成产物；
- 删除已跟踪的 `haiheliuyubaoyuagent-master/chainlitexam/.venv_new` 虚拟环境目录；
- 新增前后端 `.env.example`；
- 新增前后端 `requirements.txt`；
- 新增 `scripts/check_repository.py` 仓库健康检查脚本；
- 新增 `.github/workflows/python-check.yml`，让 PR 自动执行基础检查；
- 新增 `Makefile`；
- 新增 `.dockerignore`、前后端 `Dockerfile` 和 `docker-compose.yml`；
- 修复 MCP 后端 `server.py` 中 `available_tools` 工具列表少逗号导致字符串拼接的问题；
- MCP 后端启动参数支持 `MCP_HOST`、`MCP_PORT` 环境变量；
- 新增 `scripts/run_mcp_backend.sh` 和 `scripts/run_chainlit_frontend.sh`；
- 新增 `docs/ARCHITECTURE.md`；
- 新增 `docs/CLEANUP_AND_REFACTOR_PLAN.md`；
- 新增 `docs/DEPLOYMENT.md`；
- 扩展 README，补充前后端职责、启动方式和安全注意事项。
