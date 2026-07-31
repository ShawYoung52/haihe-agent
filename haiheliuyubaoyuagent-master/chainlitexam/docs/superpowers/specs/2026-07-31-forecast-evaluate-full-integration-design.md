# 预报检验功能全量集成设计

## 背景

`forecast_evaluate 2/` 是一个独立的预报检验核心引擎，支持：
- API 数据拉取（TS、PC、BIAS、MAE、ME 指标）
- 柱状图生成（`generate_charts`，matplotlib PNG）
- Markdown 报告生成（`ForecastAnalyzer`，含表格、图片嵌入）
- 较差样本自动分析（`analyzer_analyze.py`，阈值判定）
- 批量下载（`batch_download.py`）

当前 MCP 工具 `evaluate_forecast` 只返回了结构化 JSON（排名 + summary 文字片段），
图表和报告能力在问答智能体中不可用。老师反馈需要完整的图和报告功能。

## 设计目标

将 `forecast_evaluate 2/` 的全部能力集成到问答智能体，让用户提问后能获得：
1. **结构化图表**：单个指标柱状图 + 多指标组合趋势图
2. **完整 Markdown 报告**：总体结论→分段分析→重点定位
3. **文本回答**：含排名对比的简洁自然语言

## 架构

```
用户提问
    │
    ├─ 快速路径命中 "模式评估" / "TS评分" / "预报检验" 等关键词
    │       │
    │       ├─ 图表类意图 ("画图"/"图"/"对比图"/"可视化")
    │       │     └─▶ _build_forecast_chart_answer()
    │       │          → MCP 拿到 PNG base64/path
    │       │          → cl.Image 嵌入回答
    │       │
    │       └─ 报告/文字意图
    │             └─▶ _build_forecast_evaluate_answer()
    │                  → MCP 拿到 Markdown 报告
    │                  → 渲染图文回答
    │
    └─ Planner LLM 路径
          → tool round 调用 evaluate_forecast / generate_forecast_charts
          → 综合回答
```

## 分解为两部分

### Part A: 图表能力

**A1. 增强 `generate_charts` → 多图表类型**

| 图表类型 | 用途 | 触发场景 |
|---------|------|---------|
| 柱状图（现有） | 单指标多产品对比（如各区域准确率） | 所有 `examData` 自动生成 |
| 折线图 | 逐日/逐时效趋势对比 | `test_type=daily` 或 `time_session` |
| 组合图 | 柱状+折线叠加，展示多指标 | 用户问"综合评估" |
| 热力图 | 区域×时效二维矩阵 | 用户问"哪些区域/时效偏差大" |

**A2. MCP 新增 `generate_forecast_charts` 工具**

```
generate_forecast_charts(
    element: str,
    test_type: str,
    rain_type: str = "",
    chart_types: list[str] = ["bar", "line"],
    ...时间参数
) → {
    "charts": [
        {"type": "bar", "title": "...", "base64": "...", "path": "..."},
        {"type": "line", "title": "...", "base64": "...", "path": "..."},
    ],
    "error": null
}
```

**A3. 快速路径渲染**

`_build_forecast_chart_answer()` 将 base64/文件路径渲染为 Chainlit `cl.Image` 元素并嵌入回答。

### Part B: 报告能力

**B1. 增强 `evaluate_forecast` 返回完整报告**

现状：`_format_evaluate_result()` 只返回 `summary` 文字片段。
目标：同时返回 `report_markdown`（`ForecastAnalyzer.format_report_to_markdown()` 的完整输出）。

**B2. 融合 `analyzer_analyze.py` 的较差样本逻辑**

当前两份分析代码独立：
- `analyzer.py` `ForecastAnalyzer`: 排名、表格、summary
- `analyzer_analyze.py` `ForecastAnalyzer`: 较差定义标准、阈值判定

将后者融合进前者，让 `generate_detailed_report()` 即包含较差样本标注。
这样 Markdown 报告的"重点定位"段能自动标识问题区域/时效/日期。

## 改动清单

### 核心引擎 (`forecast_evaluate 2/`)

| 文件 | 改动 | 风险 |
|------|------|------|
| `forecast_evaluate.py` | 新增 `generate_trend_chart()` 折线图, `generate_heatmap()` 热力图, `generate_combo_chart()` 组合图 | 中：matplotlib 中文/布局需调 |
| `analyzer.py` | 融合 `analyzer_analyze.py` 的 `THRESHOLDS` 和 `_find_poor_samples()` 到 `ForecastAnalyzer` | 低：逻辑清晰，主要合并 |
| `config.py` | 抽离硬编码路径（`/Users/merlinq/...`）为配置驱动，支持 Windows/Linux | 中：需兼容现有调用方 |
| `analyzer_analyze.py` | 删除（逻辑已融合到 analyzer.py） | 低 |

### MCP 工具层

| 文件 | 改动 | 风险 |
|------|------|------|
| `forecast_evaluate_tool.py` | `evaluate_forecast` 返回加 `report_markdown` + `chart_paths`；新增 `generate_forecast_charts` 工具；图片 base64 编码 | 中：base64 可能大，需截断/压缩 |

### 问答智能体

| 文件 | 改动 | 风险 |
|------|------|------|
| `message_orchestrator.py` | `_build_forecast_evaluate_answer()` 展示完整报告；新增 `_build_forecast_chart_answer()` 渲染图片；新增图表类关键词检测 | 中：Chainlit Image 元素尺寸/布局 |
| `prompts.py` | 第13条规则扩展：区分图表请求和文字请求，指导 Planner 选择合适的工具和参数 | 低 |

### 测试

| 文件 | 改动 |
|------|------|
| `test_forecast_evaluate_fast_path.py` | 新增图表关键词触发测试 |
| `test_forecast_charts.py` | 新：图表生成功能测试（各种 chart_type × element × test_type 组合） |

## 硬编码路径处理

当前 macOS 硬编码路径：
- `Config.BASE_SAVE_DIR = Path('/Users/merlinq/Workspace/download/JSON')`
- `Config.PNG_SAVE_DIR = Path('/Users/merlinq/Workspace/download/PNG')`
- `Config.OBSIDIAN_VAULT_PATH = Path('/Users/merlinq/Documents/Obsidian-Vault/检验报告')`

方案：环境变量优先，回退到当前用户目录：
```python
_BASE = Path(os.environ.get("FORECAST_EVAL_DIR", Path.home() / "forecast_evaluate_data"))
PathConfig.BASE_SAVE_DIR = _BASE / "JSON"
PathConfig.PNG_SAVE_DIR = _BASE / "PNG"
PathConfig.OBSIDIAN_VAULT_PATH = _BASE / "reports"
```

## 图片传递策略

MCP 工具 → 问答智能体：**不使用 base64**（JSON 太大），改为：
1. MCP 工具生成 PNG 保存到共享目录
2. MCP 工具返回 `chart_paths: ["D:/.../bar_xxx.png", ...]`
3. 问答智能体快速路径读文件 → `cl.Image(path=...)` 元素嵌入
4. Planner LLM 路径：LLM 在回答中引用 `![chart](path)`，Chainlit 渲染

备选：启动时注册一个 `/api/v1/charts/<path>` 静态文件路由，让 Chainlit 能通过 HTTP 访问本地 PNG。

## 不做的

- 不改造 `batch_download.py` — 批量下载是管理员操作，不适合问答场景
- 不改 `forecast_evaluate 2/` 的 API 调用逻辑 — 核心稳定
- 不做 Word/PDF 导出 — 当前 focus 是问答智能体内展示
- 不做 Obsidian vault 集成 — 这是另一种使用场景（独立脚本），不混入

## 实现顺序

1. **config.py 路径抽离** — 所有后续工作的基础
2. **融合 analyzer_analyze.py** — 让报告内容完整
3. **新增图表类型**（折线图、组合图、热力图）— 核心交付
4. **MCP 工具增强**（报告 + 图表路径返回）— 对接层
5. **快速路径增强**（图片渲染 + 报告展示）— 用户可见
6. **prompts.py 第13条规则扩展** — Planner 路由
7. **测试 + 全流程验证**
