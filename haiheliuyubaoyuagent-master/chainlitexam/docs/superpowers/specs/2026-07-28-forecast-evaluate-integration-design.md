# 预报检验评估集成到问答智能体 — 设计文档

**日期**: 2026-07-28
**状态**: 已确认

## 1. 背景与目标

### 1.1 现状

`forecast_evaluate 2/forecast_evaluate/scripts/` 目录下有一套独立的预报检验工具：
- `forecast_evaluate.py` — 调用检验 API（`10.226.107.74:31002`），获取 TS/PC/BIAS/MAE/ME 等指标
- `batch_download.py` — 批量下载检验数据到本地 JSON
- `analyzer.py` — 解析 JSON 生成 Markdown 报告
- `config.py` — API 配置、产品映射、区域代码（当前仅天津区县）

这套代码是命令行工具，未集成到问答智能体。用户提问"最近一次暴雨TS评分"、"哪个模式晴雨预报最准"等问题时，智能体无法回答。

### 1.2 目标

将预报检验能力集成到问答智能体（Chainlit 前端 + MCP 后端），打通以下链路：

```
用户提问 → message_orchestrator.py → MCP工具 evaluate_forecast → 检验API
```

支持六类典型问题：

| 用户问题 | 检验类别 |
|----------|----------|
| 最近一次暴雨的TS评分是多少？ | 降水 / 分级(g) / 逐日(daily) |
| 哪个模式的晴雨预报最准？ | 降水 / 晴雨(ng) / 逐时效(time_session) |
| 各家模式的暴雨TS评分对比？ | 降水 / 分级(g) / 分地区(area) |
| 大模型预报效果如何？ | 降水/温度，视API返回产品而定 |
| 有没有模式误差分析？ | 温度 / 逐日(daily)，MAE/ME |
| 哪个模式对暴雨落区预报最好？ | 降水 / 分级(g) / 分地区(area) |

### 1.3 检验API产品

- `NAFP_SCMOC_NC` — 国家指导（智能网格预报指导报）
- `NAFP_BETJ_DS_NC` — 天津预报
- `NAFP_ECTHIN_NC` — ECMWF

未来API新增产品时工具自动支持（产品名从API返回中动态读取）。

## 2. 架构设计

### 2.1 整体数据流

```
用户提问
    │
    ▼
Chainlit UI (chain_gzt.py)
    │
    ▼
message_orchestrator.py
    ├── Fast Path ── 关键词命中 → 直接调 MCP 工具 → 秒回
    │   （TS评分/晴雨预报/模式评估/预报检验/准确率对比/偏差分析/落区预报/误差分析）
    │
    └── Planner LLM ── 识别意图 → tool_calls → evaluate_forecast → LLM 润色
         │
         ▼
haihe-weather-analyzer-mcp（新增模块）
    ├── forecast_evaluate_tool.py    ← MCP 工具定义 + 缓存层
    │       │
    │       ├── 缓存命中 → 直接返回 JSON
    │       └── 缓存未命中 → 调检验API
    │
    └── server.py ← 注册 evaluate_forecast 工具
```

### 2.2 集成方式

**方案：MCP 工具封装（薄封装，不修改原代码）**

- `haihe-weather-analyzer-mcp/` 下新增 `forecast_evaluate_tool.py`
- 通过 `sys.path` 导入 `forecast_evaluate/scripts/` 核心函数（`request_scores`、`run_rain_eva`、`run_temp_eva`）
- `server.py` 注册工具，planner LLM 自动发现

理由：
1. 符合现有架构（所有数据工具走 MCP 注册）
2. 可加 fast path 直通秒回
3. 原有命令行工具保持独立可用

## 3. MCP 工具定义

### 3.1 工具名称

`evaluate_forecast`

### 3.2 入参

| 参数 | 类型 | 必填 | 说明 | 示例 |
|------|------|------|------|------|
| `element` | str | 是 | 检验要素 | `rain24`, `tmax24`, `tmin24`, `t2m` |
| `test_type` | str | 是 | 检验维度 | `daily`, `time_session`, `area` |
| `rain_type` | str | 否 | 降水检验子类（仅降水需要） | `ng`(晴雨), `g`(分级), `acc`(累计) |
| `begin_time` | str | 否 | 开始时间 | `2026-07-01 00:00:00`，默认本月1日 |
| `end_time` | str | 否 | 结束时间 | `2026-07-28 23:59:59`，默认昨天 |
| `time_session` | int | 否 | 预报时效 | `24`, `48`, `72`，默认 `24` |

### 3.3 出参

```json
{
  "element": "24小时降水",
  "element_code": "rain24",
  "test_type": "逐日",
  "test_type_code": "daily",
  "time_range": {"begin": "2026-07-01 00:00:00", "end": "2026-07-27 23:59:59"},
  "rain_type": "g",
  "data_source": "检验API",
  "metrics": {
    "暴雨准确率": {
      "ranking": [
        ["天津预报", 82.5],
        ["国家指导", 78.3],
        ["ECMWF", 75.1]
      ],
      "best": "天津预报",
      "best_value": 82.5,
      "unit": "%"
    },
    "暴雨TS评分": {
      "ranking": [
        ["ECMWF", 0.42],
        ["天津预报", 0.38],
        ["国家指导", 0.31]
      ],
      "best": "ECMWF",
      "best_value": 0.42,
      "unit": ""
    },
    "暴雨偏差": {
      "ranking": [
        ["天津预报", 1.05],
        ["国家指导", 1.18],
        ["ECMWF", 0.82]
      ],
      "best": "天津预报",
      "best_value": 1.05,
      "unit": ""
    }
  },
  "summary": "2026年7月1日至27日，24小时暴雨检验：TS评分 ECMWF(0.42) > 天津预报(0.38) > 国家指导(0.31)；准确率 天津预报(82.5%)最优；偏差 天津预报(1.05)最接近1。",
  "raw_data_md": "| 产品 | 暴雨准确率(%) | 暴雨TS评分 | 暴雨偏差 |\n| ..."
}
```

### 3.4 LLM 提示词规则（`prompts.py` 新增）

> 当用户询问**预报检验、模式评估、TS评分、晴雨准确率、BIAS偏差、误差分析、落区预报**等时，调用 `evaluate_forecast` 工具。从用户问题中提取：降雨(`rain24`)还是温度(`tmax24`/`tmin24`)、逐日(`daily`)还是逐时效(`time_session`)还是分地区(`area`)、时间范围（默认本月1日至昨天）、降水类型（晴雨`ng`/分级`g`/累计`acc`）。回答时优先以**表格**展示各家产品排名和数值，产品名称加粗。

### 3.5 LLM 参数提取映射

| 用户问题 | 提取参数 |
|----------|----------|
| 最近一次暴雨TS评分 | `element=rain24, rain_type=g, test_type=daily` |
| 哪个模式晴雨预报最准 | `element=rain24, rain_type=ng, test_type=time_session` |
| 各家暴雨TS对比 | `element=rain24, rain_type=g, test_type=area` |
| 模式误差分析 | `element=tmax24, test_type=daily`（MAE/ME） |
| 暴雨落区预报最好 | `element=rain24, rain_type=g, test_type=area` |

## 4. 缓存策略

### 4.1 设计

- **存储**: 进程内 `dict`（工具模块级变量），启动时冷启动
- **Key**: `(element, test_type, rain_type, begin_date, end_date, time_session)` 六元组哈希
- **TTL**: 1 小时（检验数据是日级统计汇总，变化频率低）
- **行为**: 缓存命中直接返回；未命中调 `request_scores()` 实时拉取，结果写入缓存

### 4.2 理由

- 检验数据是统计性汇总，不像降雨实况实时变化，小时级缓存不会影响答案准确性
- API 有时延（超时 15 秒），缓存命中后毫秒级返回
- 与 `forecast_evaluate.py` 现有 debug 模式逻辑一致

## 5. Fast Path 设计

### 5.1 触发关键词

`TS评分`、`晴雨预报`、`模式评估`、`预报检验`、`准确率对比`、`偏差分析`、`落区预报`、`误差分析`、`预报评分`、`BIAS`、`MAE`

### 5.2 流程

```
_try_forecast_evaluate_fast_path()
    ├── 关键词匹配 → 未命中返回 False
    ├── 创建 ReasoningStep（模式评估意图）
    ├── 从用户文本提取检验参数（或用默认值）
    ├── 调 evaluate_forecast MCP 工具
    ├── 构建业务化回答（表格 + 排名 + 总结）
    └── _emit_fast_path_result() → 追加到历史
```

### 5.3 遵守 fast path 契约

- 调用 `_show_business_reasoning(...)`
- 在所有返回路径上关闭 reasoning step
- 引用 `generate_fast_path_thinking(...)` 或 `thinking_chain`

## 6. 地区范围扩展

### 6.1 现状

`config.py` 的 `AreaConfig.TJ_AREA_NAMES` 仅覆盖天津 16 个区县。`DefaultConfig.AREA_CODES = '120000'`。

### 6.2 扩展方案

- 第一步：保持现有代码不变，MCP 工具层面支持传入 `area_codes` 参数
- 第二步：验证检验 API 对海河流域区划的支持能力（确认 API 支持的区划代码）
- 第三步：在 `config.py` 新增 `HAIHE_AREA_CODES` 配置，映射海河流域包含的行政区划代码
- 若 API 暂不支持海河流域区划，工具返回时标注 "当前数据覆盖天津地区"，不伪造数据

## 7. 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `haihe-weather-analyzer-mcp/forecast_evaluate_tool.py` | **新增** | MCP 工具定义 + 缓存层 + 结果格式化 |
| `haihe-weather-analyzer-mcp/server.py` | **修改** | 导入并注册 `evaluate_forecast` 工具 |
| `chainlitexam/prompts.py` | **修改** | 新增第 13 条规则（预报检验） |
| `chainlitexam/message_orchestrator.py` | **修改** | 新增 `_try_forecast_evaluate_fast_path`，注册到 `process_message` fast path 列表 |
| `forecast_evaluate 2/forecast_evaluate/scripts/config.py` | **修改** | 新增海河流域区划代码配置，修正硬编码 Mac 路径 |
| `chainlitexam/tests/test_fast_paths.py` | **修改** | 新增预报检验 fast path 的静态检查用例 |

## 8. 不做的

- 不修改 `forecast_evaluate.py` / `batch_download.py` / `analyzer.py` 的核心逻辑
- 不新增数据库表或持久化存储
- 不修改前端 UI（仅通过自然语言交互）
- 不在检验API无数据时伪造结果
- 不处理检验API本身不返回的产品/模式

## 9. 测试策略

- **单元测试**: `forecast_evaluate_tool.py` 缓存逻辑、参数校验、结果格式化
- **Fast path 静态检查**: `test_fast_paths.py` 确保 `_try_forecast_evaluate_fast_path` 符合契约
- **集成测试**: 使用 mock API 响应验证端到端链路
- **手工验证**: 启动 MCP server + Chainlit，用六个典型问题逐个测试
