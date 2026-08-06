# 证据完整性提前收口（Shadow）设计

**日期**：2026-08-06
**状态**：设计（待用户审阅）
**范围**：`chainlitexam/message_orchestrator.py` + `chainlitexam/tools/meteo_evidence.py`（新增）

## 背景

当前 `Fix A`（`_has_complete_rolling_forecast`）已能在**滚动预报数据完整**时跳过第 2 次 Planner。但它是特判：只认 `code_section` 非空，不判断"用户询问的气象要素是否真的都拿到了"。

GPT 方案阶段五要一个**通用的证据完整性判断** `is_evidence_complete(query_type, tool_results)`，且**初期只记录 `would_early_finalize=true/false`，不改变真实流程**（shadow），验证后再通过 `ENABLE_EVIDENCE_EARLY_FINALIZE=true` 启用。

## 硬约束（GPT 原则）

- **默认关闭**：`ENABLE_EVIDENCE_EARLY_FINALIZE=false`。关闭时只记录 shadow 日志，不改流程。
- **不改现有 Fix A**：Fix A 已上线且验证过，不回归。
- **判断失败/工具异常/字段缺失/结果冲突 → 不跳过**（回到原有 Planner 流程）。
- **不以提速减数据**：证据不完整时绝不跳过。

## 设计

### 1. `is_evidence_complete(query_type, tool_results) -> bool`（纯函数）

```python
def is_evidence_complete(query_type: str, tool_results: list[dict]) -> bool:
    """判断当前工具结果是否足以直接回答，无需再查。

    仅当满足全部条件才返回 True：
    1. 有至少一个工具结果且无失败；
    2. 必要时间字段存在（实况有 observation_time，预报有 valid_start/end）；
    3. 必要空间范围存在；
    4. 用户询问的气象要素已获得（按 query_type 判断）；
    5. 无需要后续工具参数的中间结果；
    6. 无互相冲突的数据。
    """
```

**query_type → 必要字段映射**：

| query_type | 必要字段 |
|-----------|---------|
| `current`（实况） | `observation_time`、`spatial_scope` |
| `forecast`（预报） | `valid_start`、`valid_end`、`spatial_scope` |
| `rain`（降雨） | `rain_mm`、时间窗 |
| `warning`（预警） | 生效/解除状态、发布时间、涉及区域 |
| `water_level`（水位） | 水位值 m、观测时间 |
| `river`（河网/影响） | 河网/区划结果 |
| `default` | 保守返回 False（不跳过） |

**实现原则**：`tool_results` 是规范化后的 `MeteoEvidence` 列表（或现有 compact facts）。无 query_type 映射时返回 False（保守）。

### 2. shadow 记录（默认关闭）

在 `process_message` 的 Fix A 判断附近（约 4783 行前），新增：

```python
    # 阶段五 shadow：记录证据完整性判断，不改变流程（ENABLE_EVIDENCE_EARLY_FINALIZE=false 时只记录）
    try:
        tool_results = []  # 从 messages 中的 ToolMessage 规范化提取
        would_early = is_evidence_complete(_query_category(message.content), tool_results)
        timing = cl.user_session.get("timing_context")
        if timing is not None:
            timing.evidence = {"would_early_finalize": would_early, "query_type": _query_category(message.content)}
    except Exception:
        pass
```

`ENABLE_EVIDENCE_EARLY_FINALIZE=true` 时，才真正用 `would_early` 替代 Fix A 的判断（或与之合并）。

### 3. TimingContext 扩展

`TimingContext` 加 `evidence: dict` 字段（Task 2 的 `as_dict` 输出），记录 `would_early_finalize`/`query_type`。

## 载体

| 文件 | 改动 |
|------|------|
| `chainlitexam/tools/meteo_evidence.py` | 新增 `is_evidence_complete`（纯函数，本期只做判断逻辑） |
| `chainlitexam/message_orchestrator.py` | Fix A 附近加 shadow 记录（默认只记不改流程） |
| `chainlitexam/timing_logger.py` | `TimingContext` 加 `evidence` 字段 |

## 测试

- `is_evidence_complete` 纯函数：实况/预报/预警/水位各类型判断正确。
- 无 query_type 映射 → False（保守）。
- 工具失败/缺时间字段 → False。
- shadow 记录不改变流程（`ENABLE_EVIDENCE_EARLY_FINALIZE=false` 时 Fix A 行为不变）。
- 全量测试回归。

## 风险

- **本期只做 shadow 记录 + 纯函数**，不改变真实流程。`ENABLE_EVIDENCE_EARLY_FINALIZE` 默认 false。
- `is_evidence_complete` 从 `_query_category` 复用 query_type 分类，与现有口径一致。
- 判断保守：不确定时返回 False（不跳过），符合"不以提速减数据"。

## 后续（验证后）

- shadow 数据积累一段时间后，统计 `would_early_finalize=true` 且最终回答正确率。
- 达标后 `ENABLE_EVIDENCE_EARLY_FINALIZE=true`，用 `is_evidence_complete` 扩展 Fix A（不只滚动预报）。