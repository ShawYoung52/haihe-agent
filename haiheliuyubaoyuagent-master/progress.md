# 会话日志：58智能体接入 14所长图接口（降水实况文字）

## 2026-08-17 会话（已完成，含代码评审修复轮）

### 第一轮（初版交付）
- [x] 探索代码库：确认 14所同族模式（basin_drawing port8080 URL、get_station_rainfall_real_img port8001 base64）
- [x] 决策 D1/D2：base64 展示路径（不碰 _scrub 白名单）、仅 planner 主路径不加 fast path
- [x] P2 新建 `custom_tools/rainfall_describe_tool.py` + server.py / custom_tools/__init__.py 注册
- [x] P3 message_orchestrator.py + prompts.py 双轨路由
- [x] P4 测试（17 条）+ FastMCP 注册确认 + 6 文件 py_compile
- [x] P5 文档：CLAUDE.md 功能节、chainlitexam/README.md 工具表

### 第二轮（code-review 修复轮，9 项全部处理）
- [x] F1 图片魔数校验：base64/URL/相对路径三口径都校验是真实图片，错误文案/垃圾串→no_data 不报成功；相对路径拼 base 拉取（与 basin_drawing 同族）
- [x] F2 interval 对齐显式窗口：begin/end 均给出且未指定 interval→自动取窗口时长（48h→48）
- [x] F3 prompt 路由边界：问句含"图"字（降水实况图/降雨分布图/面雨量分布图）一律走旧工具
- [x] F4 no_data 的 msg 也过 `_scrub_text` 脱敏
- [x] F5 `_scrub_text` 升级：完整 URL host+path、IP:port、本地路径
- [x] F6 observation_text 携带 区间九/十一分区、国家/区域站 供 LLM 如实说明
- [x] F7 抽共享 `_render_base64_tool_image`：get_station_rainfall_real_img 与新工具共用（单一事实源）
- [x] F8 URL 拉取 20MB 字节上限
- [x] F9 解码失败与发送失败分开捕获、分开报告
- [x] 测试增至 26 条（全过）、FastMCP 注册复验、6 文件 py_compile 全 OK、CLAUDE.md 更新

## 改动文件清单

| 文件 | 动作 |
|---|---|
| haihe-weather-analyzer-mcp/custom_tools/rainfall_describe_tool.py | 新建 |
| haihe-weather-analyzer-mcp/custom_tools/__init__.py | 注册 |
| haihe-weather-analyzer-mcp/server.py | 注册 |
| haihe-weather-analyzer-mcp/tests/test_rainfall_describe_tool.py | 新建（26 条） |
| chainlitexam/message_orchestrator.py | TOOL_DISPLAY_NAMES + 特判 + 共享 `_render_base64_tool_image` |
| chainlitexam/prompts.py | 双轨路由（含"图"字边界） |
| chainlitexam/README.md | 工具表 |
| CLAUDE.md | 功能节（含评审加固说明） |
| task_plan.md / findings.md / progress.md | 规划文件 |

## 环境备注

- 系统只有全局 Python 3.13（D:\Python\Python313），项目 venv 依赖缺失；
  测试用 `haihe-weather-analyzer-mcp/.venv-test`（自建，装 requests/pytest/fastmcp/tzdata）。**勿提交**。
- 测试经文件路径 importlib 直接加载模块，绕开 custom_tools/__init__.py 的重依赖链（networkx/rasterio）。

## 联调待确认

- D1 响应格式假设：base64 图片（同端口 get_station_rainfall_real_img 类比）。若实测为 URL，
  工具已支持 URL/相对路径拉取转 base64（含魔数校验），无需改前端。
- 触发话术：`降水实况文字 / 降水实况文字长图 / 生成降水实况文字`；问句含"图"字走旧工具。