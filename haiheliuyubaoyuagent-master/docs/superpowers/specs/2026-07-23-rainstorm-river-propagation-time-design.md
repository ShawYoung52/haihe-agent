# 暴雨影响河流 — 传播时间返回 设计文档

- 日期：2026-07-23
- 状态：已获用户批准（方案 A）
- 需求来源：牵引智能体中"暴雨影响河流"模块需要在返回结果中增加暴雨影响沿河流的传播时间

## 1. 业务需求（已与用户确认）

| 决策点 | 结论 |
| --- | --- |
| 传播时间口径 | **河流级汇总的预计影响时长**：暴雨影响沿河道传播至下游的预计时间，用于文本答复展示 |
| 流速来源 | **统一经验流速常量**（可配置），默认 2.0 m/s（≈7.2 km/h，经验洪水波传播速度） |
| 返回位置 | 工具返回结构中**新增独立汇总字段** `river_propagation`；`affected_rivers` 字符串列表保持不变，向后兼容 |

## 2. 架构与数据流

```
build_rainstorm_impact_thematic_map (牵引智能体核心, hhlyqyxt-master/utils/rainfall_impact_geojson.py)
    │  新增 flow_velocity_mps 参数 → 聚合 river_propagation
    ▼
fixed_rainfall_impact_tool.py (MCP 适配层)  ──透传──▶  _format_mcp_response / _empty_response
    ▼
chainlitexam 本地工具 (tools/rainfall_river_impact.py) / MCP SSE 工具
    ▼
快路径简报 (message_orchestrator._build_affected_river_network_brief) + LLM 文本答复
```

## 3. 核心算法改动（`hhlyqyxt-master/utils/rainfall_impact_geojson.py`）

- `build_rainstorm_impact_thematic_map` 新增关键字参数 `flow_velocity_mps: float = 2.0`；`_validate_params` 增加校验（必须 > 0，否则 `ValueError`）。
- 新增纯函数 `_build_river_propagation(direct_edges, downstream_edges, velocity_mps) -> dict`：
  - 每条河流的传播距离 = 其下游边 `end_distance_km` 的最大值；直接边按 0 km 计（暴雨站点缓冲区内即视为已受影响）；
  - 若某河流只有直接边、无下游边，传播距离 = 其直接边中最长 `length_km`（影响就地发生，传播时间≈河段通过时间）；
  - `propagation_time_hours = distance_km / (velocity_mps * 3.6)`，保留 1 位小数；
  - 返回结构（按 `propagation_time_hours` 降序）：
    ```json
    {
      "flow_velocity_mps": 2.0,
      "rivers": [
        {
          "river_name": "滦河",
          "propagation_distance_km": 48.2,
          "propagation_time_hours": 6.7,
          "arrival_estimate_readable": "约6.7小时"
        }
      ]
    }
    ```
  - `arrival_estimate_readable`：< 1 小时显示"约X分钟"（取整），否则"约X.X小时"。
- `_empty_result` 增加同构空块 `river_propagation: {"flow_velocity_mps": v, "rivers": []}`，保证无站点/无结果时结构一致。

## 4. MCP 适配层（`haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py`）

- `build_affected_river_network_result` 与注册的工具函数 `get_affected_river_network_by_rainfall` 新增可选参数 `flow_velocity_mps: float = 0.0`。**0 = 使用核心层默认值：MCP 层在调用核心前将 0 替换为 2.0，保证核心层只会收到 > 0 的值**；负数直接报错。
- `_format_mcp_response` / `_base_response_fields` 透传 `river_propagation`；`_empty_response` 带同构空块（遵守"空结果与有结果返回相同 key"的现有约定）。
- `IMPACT_RULES` 增加 `propagation` 条目，说明经验流速估算口径。
- 检查 `server.py` 对该工具的描述/参数硬编码覆盖，若有则同步更新。
- `chainlitexam/tools/rainfall_river_impact.py` 本地包装工具签名同步新增 `flow_velocity_mps` 透传参数。

## 5. 问答层（`chainlitexam`）

- `message_orchestrator._build_affected_river_network_brief`：在河系列表后追加一行汇总——取传播时间最长的河流为代表，如：
  > 按经验流速 2.0 m/s 估算，影响预计约 6.7 小时内沿河道传播至下游最远约 48.2 公里（滦河）。
  `river_propagation` 缺失或为空时静默跳过（向后兼容旧核心算法输出）。
- 快路径 `fast_paths/rainstorm_impact_time_fast_path._build_brief` 优先调用 `mo._build_affected_river_network_brief`，自动获得该能力，无需单独改动。
- `prompts.py` 暴雨影响河流相关规则补一句：答复时若工具返回含传播时间估算，需一并说明，并注明"按经验流速估算"。

## 6. 错误处理

- 核心层：`flow_velocity_mps <= 0` 抛 `ValueError`（与 `_validate_params` 现有风格一致）；MCP 层与本地包装层 0 = 默认（调用核心前替换为 2.0），负数报错。
- NaN 距离：滦河 `len_km=NaN` 场景沿用 `get_edge_length_km` 的 haversine 兜底；聚合时跳过非有限值。
- 字段缺失容忍：下游消费者一律 `.get("river_propagation") or {}`，旧版核心输出不报错。
- 错误文本沿用现有脱敏约定（不含 IP/路径）。

## 7. 测试

- `hhlyqyxt-master` 侧：`_build_river_propagation` 单测——直接+下游混合、仅直接边、空输入、非法流速、NaN 距离跳过。
- `chainlitexam/tests/test_rainfall_river_impact.py`：传播时间字段透传断言、空结果结构一致性断言、简报拼接测试（有/无 `river_propagation` 两种输入）。
- 回归：`python -m pytest tests/ -v`（必须从 `chainlitexam/` 目录运行）+ `python tests/test_fast_paths.py` 静态检查。

## 8. 兼容性

- `affected_rivers`、`segments`、`river_geojson` 等现有字段完全不变；
- 新字段为纯增量，旧消费方无感知；
- 未传 `flow_velocity_mps` 时行为 = 默认 2.0 m/s，全链路口径一致。
