# 海河流域智能体架构说明

## 1. 工程划分

本项目当前分为两个主要工程：

```text
haiheliuyubaoyuagent-master/
  chainlitexam/                  # 前端 / 智能体交互层
  haihe-weather-analyzer-mcp/    # 后端 / MCP 工具能力层
```

## 2. chainlitexam 职责

`chainlitexam` 不是传统意义上的纯前端，它更接近 Agent Gateway / BFF 层，当前承担：

- Chainlit 聊天界面；
- LLM 初始化与 Agent 编排；
- MCP Client 连接；
- 用户登录、用户管理接口；
- Chainlit 历史会话持久化；
- GIS iframe / postMessage 联动；
- 河网图、前端展示、回答格式收口。

后续建议逐步拆分为：

```text
chainlitexam/
  app.py
  auth.py
  db.py
  llm.py
  mcp_client.py
  gis.py
  settings.py
  prompts/
  tools/
```

## 3. haihe-weather-analyzer-mcp 职责

`haihe-weather-analyzer-mcp` 是后端 MCP 工具服务，建议只负责业务能力和数据返回：

- 降雨查询；
- 河网查询；
- 暴雨影响分析；
- 实况 / 预报应急响应判定；
- POI 查询；
- 专题图任务；
- 滚动预报查询。

后端工具应尽量返回结构化 JSON，由 `chainlitexam` 负责转成领导简报、表格、GIS 联动消息或自然语言回答。

## 4. 调用链路

```text
用户 / GIS 平台
   ↓
Chainlit 聊天界面
   ↓
LLM Planner / Answerer
   ↓
MCP Client
   ↓
FastMCP SSE: haihe-weather-analyzer-mcp
   ↓
业务工具 / 数据库 / 天擎 / GIS 数据源
```

## 5. 部署建议

开发环境：

```bash
# 后端
cd haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp
python main.py --host 0.0.0.0 --port 3333

# 前端 / Agent Gateway
cd haiheliuyubaoyuagent-master/chainlitexam
cp .env.example .env
chainlit run chain_gzt.py --host 0.0.0.0 --port 8003
```

生产环境建议：

- 通过环境变量或配置中心注入数据库、模型、MCP 地址；
- 不使用默认管理员密码；
- 收紧 CORS 和 postMessage origin 白名单；
- 使用 bcrypt / argon2 存储密码；
- 为 MCP 工具建立 schema、测试和变更记录；
- 前后端分别容器化部署。

## 6. 短临智能体接入建议

短临智能体不建议直接塞进 `chain_gzt.py` 主流程，而应优先包装成 MCP 工具或独立 MCP 子服务：

```text
short-term-nowcasting-mcp
  - radar_echo_analysis
  - qpf_short_range
  - lightning_density
  - convective_warning
  - nowcasting_brief_report
```

然后由 `chainlitexam` 通过统一 MCP Client 调用。这样可以保持前端编排层稳定，也方便后续接入多个合作方或多个业务智能体。
