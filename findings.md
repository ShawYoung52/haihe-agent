# 项目探索发现

## 2026-07-23：暴雨影响河流 — 传播时间功能调研

### 功能链路（三层）
1. **牵引智能体核心算法**：`hhlyqyxt-master/utils/rainfall_impact_geojson.py`
   - `build_rainstorm_impact_thematic_map()` 是唯一拓扑计算入口
   - Dijkstra 下游追踪：`_collect_downstream_edges` / `_save_downstream_edge`，每条下游边记录 `min_distance_km` / `end_distance_km` / `keep_km` / `river_name`
   - 直接边分类：`_classify_graph_edges`，edge_info 含 `length_km`（`get_edge_length_km` 对滦河 34 条 NaN 边有 haversine 兜底）与 `river_name`
   - `_empty_result` 与正常结果同构是硬约定
   - 测试惯例：`utils/tests/test_rainfall_impact_geojson.py`，pandas/psycopg2 用最小 stub，无需真实 DB/pkl
2. **MCP 适配层**：`haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py`
   - 只做参数解析 + 降雨站点提取 + 返回格式适配，不含拓扑逻辑
   - `_format_mcp_response` / `_empty_response` / `_base_response_fields` 组装响应
   - `IMPACT_RULES` 规则说明字典，空结果与有结果都带
   - `server.py:78` 有该工具描述的硬编码字符串
   - 测试惯例：仓库根目录 `test_*.py`（如 `test_rolling_forecast_grid.py`）
3. **问答侧**：`chainlitexam/tools/rainfall_river_impact.py`（本地包装，懒加载 MCP 模块）
   - 简报：`message_orchestrator._build_affected_river_network_brief`（line 997-1034），快路径 `rainstorm_impact_time_fast_path._build_brief` 优先复用它
   - 提示词：`prompts.py` 规则 2.5（line 344）路由到本地工具
   - 测试：`tests/test_rainfall_river_impact.py`（mock `_load_mcp_modules`/`_load_mcp_config`）、`tests/test_message_orchestrator.py`

### 传播时间数据来源结论
- 无需新增图遍历或外部数据：`downstream_edges` 的 `end_distance_km` 就是 Dijkstra 累计传播距离
- 河流级传播距离 = max（下游边 end_distance_km)；仅直接边河流 = max（直接边 length_km)
- 传播时间 = 距离 ÷ （经验流速 2.0 m/s × 3.6)

### 环境注意
- claude-mem 语义检索离线（uvx 缺失），仅关键词检索可用
- 工作区有大量无关 `.venv_new` 删除，提交时必须按文件精确 add
- chainlitexam 测试必须从 `chainlitexam/` 目录运行（否则 `No module named 'utils'`）

## 2026-07-07：项目结构确认（历史）
- 主要业务目录：`haiheliuyubaoyuagent-master/`（chainlitexam 智能体 + haihe-weather-analyzer-mcp 后端）
- `fast_paths/` 快速路径包：rainfall / poi_weather / water_level / risk_warning / emergency_response / rainstorm_impact_time
- `ENABLE_FAST_PATHS` 默认 false，planner LLM 为主路径
