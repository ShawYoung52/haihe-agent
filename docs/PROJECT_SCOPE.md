# 项目范围说明

## 当前主工程

本仓库当前只保留两个主工程：

```text
haiheliuyubaoyuagent-master/chainlitexam
haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp
```

## 工程职责

### chainlitexam

定位：前端 / 智能体交互层 / Agent Gateway。

主要职责：

- Chainlit 聊天界面；
- 用户登录与会话；
- 大模型编排；
- MCP 工具调用；
- GIS 联动消息转发；
- 对最终回答做展示层收口。

### haihe-weather-analyzer-mcp

定位：后端 / MCP 工具能力层。

主要职责：

- FastMCP 服务；
- 降雨、河网、暴雨影响、POI、滚动预报等工具；
- 数据查询和业务计算；
- 向前端返回标准化工具结果。

## 已清理的历史目录

以下目录属于历史实验、重复工程、旧版备份或本地运行产物，不再作为当前交付工程保留：

```text
haiheliuyubaoyuagent-master/chainlitexam-gis
haiheliuyubaoyuagent-master/mcpexam
haiheliuyubaoyuagent-master/weather-analyzer-mcp-20260206
haiheliuyubaoyuagent-master/chainlitexam/.venv_new
```

## 禁止再次提交的目录

仓库健康检查会拦截以下目录：

```text
.venv
.venv_new
venv
env
__pycache__
.idea
.vscode
node_modules
chainlit-gis
chainlitgis
chainlit_gis
chainlitexam-gis
mcpexam
weather-analyzer-mcp-20260206
```

同时，`haiheliuyubaoyuagent-master/` 下只允许保留当前两个主工程目录。新增其它二级目录前，需要先确认它是否确实属于正式交付工程。

## 后续新增能力的建议

如果后续要接入短临预报、雷达、闪电、冰晶结冰、风场相关算法，建议不要把它们直接散落到仓库根目录，而是采用以下方式之一：

```text
haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/tools/nowcasting/
```

或独立成新的正式 MCP 子服务，并在文档中登记职责边界。
