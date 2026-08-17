# 任务计划：58智能体接入 14所长图接口（降水实况文字）

## 目标

给问答智能体（chainlitexam + haihe-weather-analyzer-mcp）接入 14所 `/openapi/rainfall_describe/real`
（降水实况文字长图）接口：用户询问"降水实况文字 / 生成降水实况 / 降水实况"等时，调用该接口
生成长图并展示。

## 背景与关键决策

- 14所已有 `basin_drawing_tool.py`（2026-08-14 接入，日报图片代理 URL，port 8080）作为同族模式。
- 本接口 `rainfall_describe/real` 位于 `10.226.107.35:8001`，与既有 `get_station_rainfall_real_img`
  （`/openapi/meteor_img/stationRainRealImg`）同一 host:port。
- **响应格式假设（待联调确认）**：与同端口 `get_station_rainfall_real_img` 一致，返回 base64 图片字符串
  在 `data` 字段。故走「base64 → cl.Image」展示路径，**不改动** `_IMAGE_URL_ALLOW_HOSTS` 网络安全白名单
  （比 URL 方案更小暴露面）。工具端做防御性前缀剥离（`data:image/...;base64,`）。
- 交付路径：planner 主路径（`ENABLE_FAST_PATHS` 默认 false）。新增 MCP 工具 + `_run_tool_round`
  base64 渲染特判 + 双轨 prompt 路由。不新增 fast path（与 basin_drawing 一致）。

## 阶段

- [x] **P1 探索与依赖分析**——已完成（见 findings.md）
- [x] **P2 MCP 工具**：`haihe-weather-analyzer-mcp/custom_tools/rainfall_describe_tool.py`
      （核心函数 + `register_rainfall_describe_tool(mcp)`），`server.py` + `custom_tools/__init__.py` 注册
- [x] **P3 前端展示**：`message_orchestrator.py` 的 `TOOL_DISPLAY_NAMES` +
      `_run_tool_round` 特判（base64 → cl.Image，:2183），`prompts.py` 双轨路由描述
- [x] **P4 测试**：`haihe-weather-analyzer-mcp/tests/test_rainfall_describe_tool.py`（26 条，全过）
- [x] **P5 验证 + 文档 + 评审**：FastMCP 注册确认、6 文件 py_compile、CLAUDE.md/README 维护、
      code-review 后台评审 9 项全部修复（图片魔数校验/URL+相对路径拉取/interval 对齐窗口/
      prompt"图"字边界/msg+异常脱敏/observation 带分区类型/共享渲染 helper/字节上限/解码发送分离）

## 决策日志

- D1：响应格式假设为 base64（同端口类比）。若联调发现是 URL，再补 `_IMAGE_URL_ALLOW_HOSTS` 白名单。
- D2：仅走 planner 主路径，不加 fast path（与 14所 basin_drawing 同机制、改动最小）。

## 环境变量

- `RAINFALL_DESCRIBE_API_BASE`（默认 `http://10.226.107.35:8001`）