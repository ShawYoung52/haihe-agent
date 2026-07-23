# Findings: 有向图 pkl 与数据库表 v5 → v6 升级

**日期:** 2026-07-14

## Requirements
- 有向图 pkl 文件及数据库表已从 v5 升级至 v6。
- 需要遍历项目代码，将所有相关引用从 v5 更新为 v6。
- 保持问答智能体、牵引智能体 MCP 工具、本地工具及测试的一致性。

## Research Findings
- 起始文件：`haihe-weather-analyzer-mcp/server.py`（用户已在 IDE 中打开）。
- 关键记忆：[[rain-impact-river-defaults]] 提到 `_resolve_graph_path` 优先选择同目录 `river_directed_v5.pkl`。

## v5 引用清单

### 必须升级（代码/配置/测试）
| 文件 | 引用内容 | 升级后 |
|------|----------|--------|
| `chain_gzt.py` | SQL `haihe_river_directed_full_v5` + 注释 | `haihe_river_directed_full_v6` |
| `tools/rain_analysis.py` | SQL `haihe_river_directed_full_v5` | `haihe_river_directed_full_v6` |
| `haihe-weather-analyzer-mcp/server.py` | 工具描述中的 `haihe_river_directed_full_v5` | `haihe_river_directed_full_v6` |
| `haihe-weather-analyzer-mcp/tools.py` | `river_directed_v5.pkl` 优先级逻辑 + SQL `haihe_river_directed_full_v5` | `river_directed_v6.pkl` + `haihe_river_directed_full_v6` |
| `haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py` | `_resolve_graph_path` 中的 `river_directed_v5.pkl`、IMPACT_RULES 中的 `full_v5`、属性字符串 | `river_directed_v6.pkl`、`full_v6` |
| `haihe-weather-analyzer-mcp/vector_boundary_api.py` | 默认表 `haihe_river_directed_full_v5` + SQL | `haihe_river_directed_full_v6` |
| `haihe-weather-analyzer-mcp/config.ini` | `river_table = haihe_river_directed_simple_v5`、`river_table_full = haihe_river_directed_full_v5` | `haihe_river_directed_simple_v6`、`haihe_river_directed_full_v6` |
| `../hhlyqyxt-master/utils/rainfall_impact_geojson.py` | `DEFAULT_RIVER_TABLE`、默认 `river_directed_v5.pkl`、注释、GeoJSON 属性 `full_v5_*` | v6 对应 |
| `../hhlyqyxt-master/utils/rainstorm_impact_map_service.py` | 默认 `river_table` | `haihe_river_directed_full_v6` |
| `../hhlyqyxt-master/utils/test_rainfall_impact_geojson_db_local.py` | `DEFAULT_GRAPH_PATH`、`DEFAULT_RIVER_TABLE`、注释、断言中的 `full_v5_*` | v6 对应 |
| `../hhlyqyxt-master/utils/test_rain_impact_internal.py` | PowerShell 示例中的 `river_directed_v5.pkl` | `river_directed_v6.pkl` |
| `../hhlyqyxt-master/docs/rainstorm_impact_map_config.md` | 文档中的 pkl 路径与表名 | v6 对应 |

### 保持不升级
- `.planning/2026-07-13-rain-impact-rivers/` 等历史规划文件（归档性质）。
- `*_diff.txt`（`b1a428f_diff.txt`、`e52d74f_diff.txt`、`rain_impact_review_diff.txt`、`review_diff.txt`）为历史 diff，不修改。
- `monitorservice.py` 中的 `haihe_river_directed_full_v4` / `river_directed_v4_asis.pkl` 为旧版监控服务，不在本次 v5→v6 范围内。
- `rainfall_impact_geojson.py` 中 `river_directed_v4_asis.pkl` fallback 属于更旧版本，保留。

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| 同时升级代码、配置、测试、文档中的 v5 引用 | 用户要求「遍历项目代码」，确保一致性 |
| GeoJSON 属性字符串 `full_v5_*` 一并改为 `full_v6_*` | 这些字符串描述真实数据源，表升级后标签应同步 |
| 不修改历史 diff 与归档规划文件 | 避免篡改历史记录 |
| `river_directed_v4_asis.pkl` 等 v4 fallback 保留 | 不在本次 v5→v6 范围内，且可能是兼容性 fallback |
| 使用常量集中版本号 | code-simplifier 建议，避免未来升级时重复修改多处硬编码 |

## Code Review & Simplification Findings
- 迁移范围正确：活动代码中的 `river_directed_v5.pkl`、`haihe_river_directed_full_v5`、`haihe_river_directed_simple_v5`、`full_v5_*` 均已更新为 v6 对应。
- 无活动代码遗漏：剩余 v5 引用仅存在于历史 diff 文件与归档规划文档中。
- code-simplifier agent 进一步集中版本常量：
  - `haihe-weather-analyzer-mcp/constants.py` 新增 `DIRECTED_GRAPH_FILENAME`、`RIVER_TABLE_VERSION`、`RIVER_TABLE_FULL`；
  - `fixed_rainfall_impact_tool.py`、`tools.py`、`server.py`、`vector_boundary_api.py` 改用常量；
  - `hhlyqyxt-master/utils/rainfall_impact_geojson.py` 新增模块级 `DIRECTED_GRAPH_FILENAME`、`RIVER_TABLE_VERSION`；
  - `chain_gzt.py`、`rainstorm_impact_map_service.py`、`test_rain_impact_internal.py`、`test_rainfall_impact_geojson_db_local.py` 复用常量或默认表名。
- 保留 v4 fallback（`river_directed_v4_asis.pkl`、`haihe_river_directed_full_v4`）与历史归档文件，符合用户「v5→v6」范围。

## Verification
| Command | Result |
|---------|--------|
| `pytest ../hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py` | 6 passed |
| `python tests/test_fast_paths.py` | 18 passed |
| `cd chainlitexam && pytest tests/ -v` | 51 passed |
| `py_compile` 全部修改的 Python 文件 | passed |

## Issues Encountered
| Issue | Resolution |
|------------|------------|
| - | - |