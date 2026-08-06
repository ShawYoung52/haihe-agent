# MeteoEvidence 气象专业化中间数据层 设计

**日期**：2026-08-06
**状态**：设计（待用户审阅，**本期不实现**，仅定义）
**范围**：`chainlitexam`（新增 `tools/meteo_evidence.py`，默认不参与回答）

## 背景

当前气象专业化依赖超长 `WEATHER_ASSISTANT_PROMPT`（644 行）让 LLM 自己理解工具结果。但工具结果来源多样（滚动预报、预警、水位、河网、实况），字段结构、单位、时间口径各异，LLM 可能：
- 混淆实况与预报
- 把"未查询到"当成"不存在"、把"接口异常"当成"业务上为零"
- 单位/时间/空间范围不一致
- 把历史预警说成当前生效

**目标**：建立工具数据到气象领域数据的中间层 `MeteoEvidence`，由代码规范时间/单位/空间/状态，Answer 生成时只消费规范化后的证据，从源头减少 LLM 误判。

## 硬约束（GPT 原则）

- **默认关闭**：`ENABLE_METEO_DOMAIN_RENDERER=false`。关闭时走原有回答链，`MeteoEvidence` 完全不参与。
- **不改工具返回关键数值**：MeteoEvidence 只规范元信息（时间/单位/空间/状态），不改数值本身。
- **不自行补全缺失数据**：缺失项明确列出 `missing_fields`，不得补全。
- **专业化失败回退原有回答**：`candidate_answer = render_meteo_answer(evidence); if validate(candidate, evidence): return candidate; return original_answer`。
- **不新增长 Prompt**：MeteoEvidence 是代码层规范化，不增加 prompt 内容。

## 设计

### 1. `MeteoEvidence` dataclass

```python
@dataclass
class MeteoEvidence:
    query_type: str                 # 问题类型：weather/rain/warning/water_level/river_network/...
    data_kind: str                  # observation/forecast/warning/historical/hydrology/impact_analysis/knowledge
    spatial_scope: str              # 市/区县/站点/流域
    observation_time: str | None    # 实况观测截止时间
    forecast_base_time: str | None  # 预报起报时间
    valid_start: str | None         # 预报有效开始
    valid_end: str | None           # 预报有效结束
    source: str                     # 数据来源
    facts: list[dict]               # 规范化后的事实（单位统一）
    uncertainty: list[str]          # 不确定项
    missing_fields: list[str]       # 缺失项
    raw: dict                       # 原始工具结果（供 GIS/图片/业务组装，不送 LLM）
```

其中 `data_kind ∈ {observation, forecast, warning, historical, hydrology, impact_analysis, knowledge}`。

### 2. 数据源 → MeteoEvidence 映射

| 工具 | data_kind | 关键规范化 |
|------|-----------|-----------|
| `query_rolling_forecast` | forecast | `fcst_time`→forecast_base_time；`forecast_start/end_time`→valid_start/end；`daily_summary` 逐日规范化 |
| `query_current_weather_observation` | observation | `observation_time_beijing`→observation_time；站点统计规范化 |
| `get_effective_warning_info` | warning | 生效状态（发布/更新/解除）独立字段；发布时间规范化 |
| `get_history_warning_info` | historical | 明确标注"历史/已解除"，与当前生效区分 |
| `get_national_warning_info` | warning | 影响范围（天津/全国）标注 |
| `query_water_level` | hydrology | 水位单位 m；时间规范化 |
| `get_river_network_for_plot` | impact_analysis | 河网结构（不送 LLM，仅用于图） |
| `query_basin_areal_rainfall` | observation | 面雨量 mm；流域范围 |
| `query_decision_weather_for_poi` | forecast | 点位/代表站；观测/预报区分 |

### 3. 规范化规则（代码实现，不依赖 LLM）

- **时间**：北京时间和 UTC 时次统一为 `YYYY-MM-DD HH:MM`（北京时间）；实况时间、起报时间、有效时间分别入 `observation_time`/`forecast_base_time`/`valid_start/end`。
- **单位**：统一 `mm`（雨量）、`mm/h`（雨强）、`℃`（温度）、`m/s`（风速）、`m`（水位/能见度）。工具返回的 `mm/6h`、`mm/h` 等换算或标注原单位，不猜测。
- **空间**：标注 `市/区县/站点/流域` 尺度。站点值 vs 区域平均 vs 流域平均分别标注，不混淆。
- **状态**：
  - 无数据 ≠ 接口异常 ≠ 0mm ≠ 未发布 ≠ 已解除——五个状态分开。
  - 预警：`发布/更新/解除/生效/过期` 独立字段。
- **缺失**：`missing_fields` 列出缺失项，Answer 不得补全。

### 4. 专业化验证器（`validate_meteo_answer`）

Answer 生成后轻量验证（不调额外 LLM）：
- 数值能在 `MeteoEvidence.facts` 找到
- 单位一致
- 时间无冲突（实况 vs 预报不混）
- 空间范围无冲突
- 预警状态准确（历史不说成当前生效）
- 无工具名/内网地址
- 未遗漏用户直接询问的要素

验证失败 → 优先确定性修正；无法修正 → 用现有回答链。

### 5. 双轨接入（默认关闭）

```python
if ENABLE_METEO_DOMAIN_RENDERER:
    evidence = build_meteo_evidence(tool_results, query_type)
    candidate = render_meteo_answer(evidence, answer_chain)
    if validate_meteo_answer(candidate, evidence):
        return candidate
return original_answer  # 默认路径
```

## 载体（本期不实现）

| 文件 | 改动（未来） |
|------|-------------|
| `chainlitexam/tools/meteo_evidence.py` | 新增 `MeteoEvidence` dataclass + `build_meteo_evidence` + `validate_meteo_answer` |
| `chainlitexam/message_orchestrator.py` | Answer 出口加 `ENABLE_METEO_DOMAIN_RENDERER` 双轨 |

## 风险

- **纯设计，本期零代码改动**。只定义数据结构与规范化规则。
- 实现时严格 `ENABLE_METEO_DOMAIN_RENDERER=false` 默认关闭，专业化失败回退原有回答。
- 不改数值、不补全缺失、不混状态。

## 后续（实现时再做）

1. `build_meteo_evidence` 逐工具映射 + 测试（滚动预报/预警/水位/河网/实况）
2. `validate_meteo_answer` 轻量校验 + 测试
3. Shadow 比较：原回答 vs 专业化回答的关键事实一致性