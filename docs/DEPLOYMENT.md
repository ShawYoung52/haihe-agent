# 部署与运行指南

## 1. 推荐目录边界

仓库中保留以下内容：

```text
README.md
.gitignore
.dockerignore
docker-compose.yml
Makefile
docs/
scripts/
haiheliuyubaoyuagent-master/
  chainlitexam/
  haihe-weather-analyzer-mcp/
```

以下内容不应提交到仓库：

```text
.venv/
.venv_new/
.env
__pycache__/
*.log
.idea/
.vscode/
output/
tmp/
```

## 2. MCP 后端启动

### 手动启动

```bash
cd haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py --host 0.0.0.0 --port 3333
```

### 使用脚本启动

```bash
bash scripts/run_mcp_backend.sh
```

可选环境变量：

```bash
export MCP_HOST=0.0.0.0
export MCP_PORT=3333
```

## 3. Chainlit 前端启动

### 手动启动

```bash
cd haiheliuyubaoyuagent-master/chainlitexam
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 修改 .env 后再启动
chainlit run chain_gzt.py --host 0.0.0.0 --port 8003
```

### 使用脚本启动

```bash
bash scripts/run_chainlit_frontend.sh
```

可选环境变量：

```bash
export CHAINLIT_HOST=0.0.0.0
export CHAINLIT_PORT=8003
export MCP_WEATHER_URL=http://127.0.0.1:3333/sse
```

## 4. Docker Compose 启动

```bash
docker compose up --build
```

启动后默认端口：

```text
MCP 后端: http://127.0.0.1:3333/sse
Chainlit 前端: http://127.0.0.1:8003
```

说明：

- `docker-compose.yml` 当前用于开发和联调环境。
- 容器内前端通过 `http://mcp-backend:3333/sse` 访问 MCP 后端。
- 生产环境建议使用独立配置文件或部署平台环境变量覆盖数据库、模型和外部数据源配置。

## 5. 质量检查

```bash
make check
# 或
python scripts/check_repository.py
```

检查内容：

- 是否误提交虚拟环境、缓存、本地配置、日志等文件；
- Python 文件是否能通过基础语法编译。

PR 和 push 会自动触发 `.github/workflows/python-check.yml`。

## 6. 生产环境注意事项

1. 不要使用 `.env.example` 中的占位密码。
2. 真实 `.env` 不要提交 GitHub。
3. 大模型 API Key、数据库密码、管理员密码必须通过 Secret 或配置中心注入。
4. FastAPI CORS 和 GIS `postMessage` 必须配置白名单。
5. 建议前后端分别部署，并通过内网地址连接 MCP SSE。
6. 建议增加 Nginx / API Gateway 统一转发和 TLS。

## 7. 推荐启动顺序

```text
1. 启动数据库 / 数据服务
2. 启动 haihe-weather-analyzer-mcp
3. 确认 http://<mcp-host>:3333/sse 可访问
4. 配置 chainlitexam/.env 中的 MCP_WEATHER_URL
5. 启动 chainlitexam
6. 打开浏览器访问 Chainlit 页面
```

## 8. 当前仍需人工处理

由于仓库历史里曾经提交过敏感配置和本地环境文件，建议后续在本地执行：

```bash
# 示例：彻底从历史中清理无关目录，需要谨慎操作
# git filter-repo --path haiheliuyubaoyuagent-master/chainlitexam/.venv_new --invert-paths
```

清理历史后需要重新推送仓库，并通知团队重新 clone。
