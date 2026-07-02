# 清理与重构计划

## 已完成

- 新增 `.gitignore`，阻止继续提交虚拟环境、缓存、日志、密钥和生成产物。
- 新增前端与后端 `.env.example`，把关键运行配置显式化。
- 删除 `haiheliuyubaoyuagent-master/chainlitexam/.venv_new` 已跟踪的虚拟环境文件。
- 修复 MCP 后端 `server.py` 中工具列表少逗号导致字符串拼接的问题。
- 补充 README 和架构说明。
- 补充前端、后端 `requirements.txt`，方便本地重新安装依赖。

## 下一步 P0：安全整改

1. 吊销已经暴露过的大模型 API Key，并重新生成。
2. 清理 Git 历史中的敏感信息；如果仓库已经公开过，建议使用 `git filter-repo` 或 BFG Repo-Cleaner 后重新推送。
3. 删除代码中的默认数据库密码、默认管理员密码、默认内网地址。
4. 生产环境必须通过 `.env`、部署平台 Secret 或配置中心注入敏感配置。

## 下一步 P1：工程结构拆分

当前 `chainlitexam/chain_gzt.py` 职责过重，建议按以下结构拆分：

```text
chainlitexam/
  app.py                 # Chainlit / FastAPI 入口
  settings.py            # 环境变量与配置读取
  auth.py                # 用户登录、角色、密码哈希
  db.py                  # Chainlit 数据层与 PostgreSQL 连接
  llm.py                 # 大模型初始化
  mcp_client.py          # MCP Client 初始化
  gis.py                 # GIS postMessage / payload 构造
  callbacks.py           # Chainlit 回调
  prompts/
  tools/
```

## 下一步 P1：前后端契约

建议为 MCP 工具建立统一返回结构：

```json
{
  "code": 200,
  "message": "success",
  "data": {},
  "trace_id": "uuid",
  "warnings": []
}
```

并为重点工具补充：

- 入参 schema；
- 出参 schema；
- 示例请求；
- 示例返回；
- 错误码；
- 是否废弃；
- 替代工具。

## 下一步 P1：GIS 联动安全

1. 禁止生产环境使用 `postMessage("*", ...)`。
2. 增加 `ALLOWED_PARENT_ORIGINS` 白名单。
3. 校验 `event.origin`、`event.source`、消息 `type` 和 payload schema。
4. 对关键 GIS 指令加 nonce 或签名，避免被其它 iframe 或页面伪造。

## 下一步 P2：测试与 CI

建议先补最小测试：

```text
tests/
  test_mcp_service_info.py
  test_env_settings.py
  test_gis_payload_schema.py
  test_rainstorm_prompt_rules.py
```

再增加 GitHub Actions：

```text
.github/workflows/python-check.yml
```

检查内容：

- Python 语法检查；
- import smoke test；
- 单元测试；
- secret scan。

## 本地清理建议

如果你本地已经 clone 过旧版本，可以执行：

```bash
git fetch origin
git checkout chore/web-cleanup-20260702

# 重新创建虚拟环境，不要使用仓库里提交过的旧 .venv_new
cd haiheliuyubaoyuagent-master/chainlitexam
python -m venv .venv
source .venv/bin/activate  # Windows 使用 .venv\Scripts\activate
pip install -r requirements.txt
```

MCP 后端同理：

```bash
cd haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py --host 0.0.0.0 --port 3333
```
