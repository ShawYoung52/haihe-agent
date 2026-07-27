# 问答智能体暴雨影响河流 · MCP 层契约同步设计

- **状态**：草案（brainstorming 已完成，待用户 review）
- **日期**：2026-07-27
- **作用域**：`haihe-weather-analyzer-mcp/*` 和 `chainlitexam/*` 中的 MCP 层入口
- **契约类别**：向后兼容（新增字段，不修改老字段语义）
- **模型分工**：DeepSeek v4 Flash = 主力执行；DeepSeek v4 Pro = 架构师 / 高级审查
- **前置 PR**：`fe609e9` (feat/rain-impact-arrival-time 已 merge 到 main)
- **相关记忆**：`[[rain-impact-arrival-time-contract]]`、`[[rain-impact-geojson-consistency]]`、`[[traction-review-scope-rule]]`、`[[haihe-project-env-quirks]]`、`[[deepseek-model-constraint]]`、`[[user-full-process-workflow]]`

---

## 1. 目标

**业务目标**

1. 问答智能体（MCP 层）与牵引应急响应智能体（牵引层）的暴雨影响河流契约**保持一致**：站点缓冲半径默认 20km（不再是硬编码的 30km）、GeoJSON 每条 feature 携带 `t0_source_time` / `estimated_arrival_time`（钟表时刻 UTC ISO Z）、顶层 result 带 `reference_time`。
2. LLM 通过 MCP 工具返回的结果，前端可以直接读顶层 `reference_time` 与 `river_geojson.features[*].properties.estimated_arrival_time`。
3. IMPACT_RULES 文本描述与真实行为对齐。

**非目标**

- 不改牵引层 `hhlyqyxt-master/utils/rainfall_impact_geojson.py`（已在 main 完成）。
- 不改 MCP 工具签名（不新增/删除参数）。
- 不改前端。
- 不改上游 `analyze_rainfall_core`。

## 2. 契约变更

### 2.1 硬编码修正

| 位置 | 老值 | 新值 |
|---|---|---|
| `fixed_rainfall_impact_tool.py:23` `IMPACT_RULES["direct"]` | 文字 "默认 30km" | "默认 20km" |
| `fixed_rainfall_impact_tool.py:215` `_empty_response` | `"station_buffer_km": 30.0` | `20.0` |
| `server.py:80` 工具描述 | "30km直接不截断" | "20km直接不截断" |

### 2.2 新增顶层字段

| 字段路径 | 类型 | 语义 |
|---|---|---|
| `reference_time` | `string \| null` (ISO 8601 UTC Z) | 从 builder `result.params.reference_time` 透传，等于所有 rainstorm_stations 中最早 rain_end_time |

### 2.3 新增 station 字段（内部，传给 builder）

| 字段 | 类型 | 语义 |
|---|---|---|
| `station.rain_end_time` | `string \| null` (ISO 或原始时段格式) | 从 rainfall_result 顶层派生（该次查询的时段结束），所有 station 共用；缺失时 None |

### 2.4 IMPACT_RULES 新增描述

新增 key `arrival`，说明 `estimated_arrival_time` / `t0_source_time` / `reference_time` 语义。

### 2.5 river_geojson 内 feature.properties 的 arrival 字段

无需 MCP 层特殊处理 —— 由 builder 直接嵌入 `river_geojson.features[*].properties`，MCP 层原样透传（当前代码已经透传 `river_geojson`）。

## 3. 数据流

```
build_affected_river_network_result(...)
    │
    ▼
analyze_rainfall_core(time_str, pg_conf, custom_timerange)
    → rainfall_result (含 time_range_readable、level_analysis[*].stations)
    │
    ▼
_derive_rain_end_time(rainfall_result)  # 新增
    → str | None（从 time_range_readable / time_range 派生）
    │
    ▼
_extract_rainstorm_stations(rainfall_result, threshold_mm, rain_levels)
    - 内部先调用 _derive_rain_end_time 一次
    - 每个 station 走 _normalize_station(station, level, rain_end_time=derived)
    │
    ▼
builder = _load_impact_builder()  # 动态 import 牵引层 builder
result = builder(stations, ...)
    → result["params"]["reference_time"]（由 builder 侧计算）
    → result["river_geojson"] 已含 arrival 字段（由 builder 侧计算）
    │
    ▼
_format_mcp_response(result, ...)
    → _base_response_fields(..., reference_time=result["params"]["reference_time"])
    → 顶层 dict 加入 reference_time
```

## 4. 关键实现细节

### 4.1 新增辅助函数

- **`_derive_rain_end_time(rainfall_result: dict) -> str | None`**
  - 优先尝试 `rainfall_result.get("time_range")` 结构化字段（如 `"[YYYYMMDDHHMMSS,YYYYMMDDHHMMSS]"`），parse 出 end 部分转 ISO。
  - 次选 `rainfall_result.get("time_range_readable")`（如 `"2026-07-27 15:30 至 2026-07-28 15:30"`），用宽松正则 parse 末端。
  - 无匹配 / parse 失败 → 返回 None（记 warning）。**不 raise**。
  - 归一化到 ISO 字符串（builder 内的 `_normalize_end_time` 会再走一遍 UTC 归一，此处只需要合法字符串或 None）。

### 4.2 `_normalize_station` 签名升级

- 新增可选形参 `rain_end_time: str | None = None`。
- 输出字典新增 `"rain_end_time": rain_end_time`（None 时也带 key，值为 None）。

### 4.3 `_extract_rainstorm_stations` 传递 rain_end_time

- 函数体开头调用 `_derive_rain_end_time(rainfall_result)` 一次。
- 每次 `_normalize_station(station, level, rain_end_time=derived)`。

### 4.4 `_base_response_fields` 顶层 dict 新增字段

- 新增可选形参 `reference_time: str | None = None`。
- 返回 dict 中新增 `"reference_time": reference_time`（默认 None）。
- 位置：紧挨 `river_propagation` 之后。

### 4.5 `_empty_response` / `_format_mcp_response` 透传

- `_empty_response`：`reference_time=None`。
- `_format_mcp_response`：从 `result.get("params", {}).get("reference_time")` 取（双 `.get()` 兜底老 builder）。

### 4.6 IMPACT_RULES 新增 arrival 段

```python
"arrival": (
    "GeoJSON feature.properties.estimated_arrival_time（UTC ISO 8601 Z 格式）"
    "= t0_source_time + propagation_time_hours。直接段 T0 = 该边所有 trigger 站点中最早 "
    "rain_end_time；下游段 T0 = 上游 direct 段中最早 rain_end_time（沿 BFS 路径传播）。"
    "顶层 reference_time = 所有 rainstorm_stations 中最早 rain_end_time。"
),
```

### 4.7 server.py 工具描述

第 80 行 "30km直接不截断" 改成 "20km直接不截断"。可考虑追加一句说明 arrival，但不修改工具签名。

## 5. 错误处理与降级

- rainfall_result 缺 `time_range` 且缺 `time_range_readable` → `_derive_rain_end_time` 返 None → 所有 station.rain_end_time = None → builder 侧全链路降级为 arrival = null。
- time_range 格式非标准（老版本） → 字符串解析失败记 warning，返 None。
- 老 builder 无 `params.reference_time`（本地开发版本） → `.get()` 双兜底为 None。
- 无站点触发 `_empty_response`：reference_time = None，river_geojson 为空，arrival 字段不出现。

## 6. 测试策略

### 6.1 单元测试增量（`test_fixed_rainfall_impact_propagation.py`）

按 TDD 顺序：

1. `test_default_station_buffer_km_matches_traction_agent`
2. `test_empty_response_station_buffer_km_is_20`
3. `test_derive_rain_end_time_from_time_range_readable`
4. `test_derive_rain_end_time_returns_none_when_missing`
5. `test_derive_rain_end_time_from_time_range_iso_pair`
6. `test_normalize_station_adds_rain_end_time`
7. `test_normalize_station_defaults_rain_end_time_to_none`
8. `test_extract_rainstorm_stations_propagates_rain_end_time`
9. `test_base_response_fields_includes_reference_time`
10. `test_base_response_fields_reference_time_defaults_none`
11. `test_format_mcp_response_extracts_reference_time_from_builder_result`
12. `test_impact_rules_contains_arrival_description`

**基线**：Phase 0 记录 `test_fixed_rainfall_impact_propagation.py` 与 `chainlitexam/tests/test_rainfall_river_impact.py` 各自 passed 数。

### 6.2 chainlitexam 侧对应测试

Phase 0 探查后决定；若发现硬编码 30km 或调用 `_normalize_station`，补对应测试。

### 6.3 内网验证

`scripts/verify_river_propagation_offline.py`（如存在）追加：
- 顶层 `reference_time` 字段存在且为 ISO 格式或 null。
- river_geojson feature 已有 arrival 字段。

## 7. 契约兼容性矩阵

| 消费者 | 影响 | 处理 |
|---|---|---|
| 老 LLM 提示 | 无影响（新字段仅增加） | 无需协调 |
| MCP client 老代码 | 忽略新字段 | 无需协调 |
| 老前端 | 忽略新字段 | 无需协调 |
| 新前端 | 直接读顶层 `reference_time` 和 feature.properties.estimated_arrival_time | 契约文档明确 |
| chainlitexam 消费者 | 与 MCP 同源 | 同 MCP 变化 |
| pytest 基线 | 新增测试 12 条，老测试不回归 | Phase 0 记录基线 |

## 8. 命名一致性检查

- MCP 层顶层 `reference_time` 命名与牵引层 `result.params.reference_time` 底层字段名一致（避免 MCP 侧再改名）。
- `IMPACT_RULES["arrival"]` 描述文字与实际字段名精确匹配（`t0_source_time` / `estimated_arrival_time` / `reference_time`）。
- 与 [[rain-impact-geojson-consistency]] 一致：per-edge 与顶层命名口径同步。

## 9. 执行编排（Phase 划分）

- **Phase 0** — 分支 + baseline + chainlitexam 探查
- **Phase 1** — MCP 硬编码 30→20
- **Phase 2** — rain_end_time 派生 + 传递
- **Phase 3** — reference_time 顶层透传
- **Phase 4** — IMPACT_RULES 增补 arrival 描述
- **Phase 5** — 内网验证脚本同步（如脚本存在）
- **Phase 6** — code-simplifier + Pro 最终审查
- **Phase 7** — finishing (PR + memory + main merge)

**M-Gate**

- **M1**（Phase 1）：MCP 层无 30km 硬编码；pytest 全绿
- **M2**（Phase 3）：reference_time 端到端透传；测试全过
- **M3**（Phase 6）：Pro 审查通过
- **M4**（Phase 7）：main 已 push

## 10. 风险与预案

| 风险 | 概率 | 预案 |
|---|---|---|
| chainlitexam 侧 30km 硬编码 grep 遗漏 | 中 | Phase 0 grep 3 种模式："30km" / "30.0" / "station_buffer_km * 30" |
| rainfall_result.time_range_readable 中文格式化变化 | 中 | 宽松正则 + fallback，无匹配返回 None |
| 老 upstream builder 无 `params.reference_time` | 低 | `.get()` 双兜底 |
| Windows Store python 占位陷阱 | 高（本机） | 显式 `.venv\Scripts\python.exe` 绝对路径（[[haihe-project-env-quirks]]） |
| git add 误吞 | 中 | 只 `git add` 精确路径 |
| MCP 工具重复注册（旧签名残留） | 低 | 现有 `_unregister_existing_tool` 已在，不动 |

## 11. 模型分工

| 阶段 | 模型 | 职责 |
|---|---|---|
| brainstorming | Opus（本会话） | 业务口径对齐（已完成） |
| writing-plans | 本会话 | Phase 编排 |
| writing-plans 审查 | **DeepSeek v4 Pro** 行为约束 | 架构 / 契约 / 边界评审 |
| TDD 红/绿执行 | **DeepSeek v4 Flash** 行为约束 | 按 phase 落地 |
| code-simplifier | Flash 行为约束 | 清理 / 简化 |
| code-review 最终审 | **DeepSeek v4 Pro** 行为约束 | PR 前高级审查 |
| context7 | 按需 | datetime / 字符串解析用法（预计用不上） |
| github | — | Push + PR + main merge |
| claude-mem | — | 落新记忆 `[[qa-agent-rain-impact-sync]]` |
| claude-md-management | — | 更新 `haiheliuyubaoyuagent-master/CLAUDE.md` |

**注**：本 harness 无 DeepSeek 子代理直连，仍以 `general-purpose`（Claude）派出，prompt 内声明"行为约束按 Flash/Pro"。

## 12. 交付物清单

- `haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py`（改）
- `haihe-weather-analyzer-mcp/server.py`（改，工具描述）
- `haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py`（新增 12 测试）
- `chainlitexam/tools/rainfall_river_impact.py`（视 Phase 0 探查）
- `chainlitexam/tests/test_rainfall_river_impact.py`（视 Phase 0 探查）
- `scripts/verify_river_propagation_offline.py`（如脚本存在，补 arrival + reference_time 检查）
- `haiheliuyubaoyuagent-master/CLAUDE.md`（追加 20km + arrival 契约说明）
- `docs/superpowers/specs/2026-07-27-qa-agent-rain-impact-sync-design.md`（本文档）
- `docs/superpowers/plans/2026-07-27-qa-agent-rain-impact-sync-plan.md`（下一步 writing-plans 产出）
- 新记忆 `[[qa-agent-rain-impact-sync]]`（PR 合入后落）
