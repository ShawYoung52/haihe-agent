# 项目探索发现

## 文档现状
- 仓库根目录（`C:\Users\gaozr\Downloads\haiheliuyubaoyuagent-master (3)\`）已存在 4 份目标文档：
  - `PRODUCT.md`
  - `DESIGN.md`
  - `AGENTS.md`
  - `current-progress.md`
- 文档质量较高，面向非技术人员可读，但 `AGENTS.md` 和 `current-progress.md` 存在与实际项目状态不一致的地方。

## 项目结构确认
- 主要业务目录：`haiheliuyubaoyuagent-master/`
  - `chainlitexam/`：Chainlit 智能体入口
    - `chain_gzt.py`：会话入口、FastAPI、认证
    - `message_orchestrator.py`：消息编排
    - `prompts.py`：系统提示词
    - `fast_paths/`：快速路径包（新结构）
      - `rainfall_fast_paths.py`
      - `poi_weather_fast_paths.py`
      - `water_level_fast_paths.py`
      - `risk_warning_fast_paths.py`
      - `emergency_response_fast_path.py`
      - `rainstorm_impact_time_fast_path.py`
    - `tools/rain_analysis.py`：本地降雨分析
    - `external_skill_tools.py`：合作方能力
    - `utils/`：MUSIC 客户端、数据库配置
  - `haihe-weather-analyzer-mcp/`：后端 MCP 服务
    - `server.py`、`tools.py`、`haihe_mcp_tools.py`：MCP 工具
    - `rest_api.py`、`emergency_api.py`、`emergency_http_server.py`：REST/应急接口
    - `wms_vector_service/`：GIS/WMS 矢量服务
    - `custom_tools/`：扩展工具
    - `analyzers/`：分析器

## AGENTS.md 中的问题
- 引用不存在的文件：
  - `chainlitexam/USER_API.md`
  - `chainlitexam/USER_AND_FRONTEND_INTEGRATION.md`
  - `haihe-weather-analyzer-mcp/REST_API_README.md`
  - `haihe-weather-analyzer-mcp/RIVER_API_README.md`
- 缺少对 `fast_paths/` 目录的说明。
- `message_orchestrator.py` 仍在，但快速路径已拆分到 `fast_paths/` 包。

## current-progress.md 中的问题
- 声称“当前没有修改运行代码；只做项目上下文整理”，但最近 20 条提交显示大量代码修改，尤其是：
  - 暴雨影响河网（rainfall impact river）相关修复
  - 应急响应路径调整
  - 删除无关代码

## 最近提交主题（top 20）
1. 删除一些无关的代码
2. debug: log rainfall impact river features
3. revert: keep rainfall impact direct river selection unchanged
4. fix: align QA rainfall impact with realtime station logic
5. fix: tighten rainstorm impact direct river selection
6. fix: expose flow direction in rainfall impact QA tool
7. fix: orient downstream geojson coordinates by flow direction
8. refactor: remove extra rainstorm impact graph start rules file
9. refactor: remove rainstorm impact monkey patch installer
10. fix: install strict rainfall impact start rule
11. fix: add strict rainfall impact graph start rule
12. fix: restrict downstream tracing to direct river matches
13. fix: sanitize river impact geojson line geometries
14. fix: revert: disable first-class emergency response helper
15. fix: keep emergency response fast path to second-class observation only
16. fix: keep emergency response tool to second-class observation only
17. fix: prefer official first-class emergency response in fast path
18. fix: check first-class official response before rainfall rules
19. feat: add official emergency response status helper
20. fix: run emergency response locally and correct daypart times

## 数据来源
- 直接读取仓库中的 Markdown 文件
- `git log --oneline -20`
- `Glob` 扫描 `.md`、`.py` 文件
- `Read` 关键源码文件片段
