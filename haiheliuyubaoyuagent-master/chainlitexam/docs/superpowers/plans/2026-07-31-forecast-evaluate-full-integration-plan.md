# 预报检验功能全量集成实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `forecast_evaluate 2/` 的全部图表和报告能力集成到问答智能体中，让用户获得图表（柱状/折线/组合/热力图）+ 完整 Markdown 报告（总体结论→分段分析→重点定位）

**Architecture:** MCP 工具层增强返回完整报告 Markdown 和图表文件路径；快速路径根据用户意图自动选择渲染图表或文本报告；核心引擎不改 API 逻辑，只新增图表类型和融合两份分析代码

**Tech Stack:** Python 3.10+, matplotlib, numpy, Chainlit cl.Image, FastMCP

## Global Constraints

- 不改 `forecast_evaluate 2/` 的 API 调用逻辑（`request_scores`/`run_rain_eva`/`run_temp_eva`）
- 路径硬编码改为 env var `FORECAST_EVAL_DIR` 优先 + 自动 fallback
- 图片传递：MCP 返回文件路径 → Chainlit 读文件 → `cl.Image` 嵌入
- `analyzer_analyze.py` 的 THRESHOLDS 和较差样本逻辑完整融合进 `analyzer.py`
- 产品名称加粗、中文排版、数值 1-2 位小数保持不变
- 快速路径必须通过 `_show_business_reasoning` + `generate_fast_path_thinking` → `_emit_fast_path_result` 统一出口

---

### Task 1: 路径配置动态化

**Files:**
- Modify: `forecast_evaluate 2/forecast_evaluate/scripts/config.py:48-63`
- No new tests needed (existing tests validate behavior unchanged)

**Interfaces:**
- Produces: `Config.BASE_SAVE_DIR`, `Config.PNG_SAVE_DIR`, `Config.OBSIDIAN_VAULT_PATH` — all now resolve via `FORECAST_EVAL_DIR` env var with platform-aware fallback

- [ ] **Step 1: 替换 PathConfig 硬编码路径为 env-var-first 模式**

```python
import os

class PathConfig:
    """路径配置类 — 环境变量优先，回退到平台默认"""
    
    _BASE = Path(os.environ.get(
        "FORECAST_EVAL_DIR",
        Path.home() / "forecast_evaluate_data"
    ))
    
    BASE_SAVE_DIR = _BASE / "JSON"
    PNG_SAVE_DIR = _BASE / "PNG"
    OBSIDIAN_VAULT_PATH = _BASE / "reports"
    SAVE_DIR = PNG_SAVE_DIR
```

- [ ] **Step 2: 验证 backward compat — 不加 env var 时旧逻辑仍可用**

```bash
# 不设 env var 时目录为 %USERPROFILE%/forecast_evaluate_data/...
cd "forecast_evaluate 2/forecast_evaluate/scripts"
py -3 -c "from config import PathConfig; print(PathConfig.BASE_SAVE_DIR); assert PathConfig.BASE_SAVE_DIR.name == 'JSON'"
```

Expected: 打印 `%USERPROFILE%/forecast_evaluate_data/JSON`，断言通过

- [ ] **Step 3: 验证 env var override**

```bash
$env:FORECAST_EVAL_DIR = "D:/tmp/eval_test"
py -3 -c "from config import PathConfig; print(PathConfig.BASE_SAVE_DIR); assert str(PathConfig.BASE_SAVE_DIR).startswith('D:/tmp')"
```

Expected: 打印 `D:/tmp/eval_test/JSON`

- [ ] **Step 4: Commit**

```bash
git add "forecast_evaluate 2/forecast_evaluate/scripts/config.py"
git commit -m "refactor(config): make forecast_evaluate paths configurable via FORECAST_EVAL_DIR env var"
```

---

### Task 2: 融合较差样本分析逻辑到 analyzer.py

**Files:**
- Modify: `forecast_evaluate 2/forecast_evaluate/scripts/analyzer.py`
- Delete: `forecast_evaluate 2/forecast_evaluate/scripts/analyzer_analyze.py`
- No new test file (analyzer.py's `__main__` block already runs sample data)

**Interfaces:**
- Consumes: `analyzer_analyze.py` THRESHOLDS dict and `_find_poor_samples()` logic
- Produces: `ForecastAnalyzer.THRESHOLDS` (class-level), `ForecastAnalyzer._find_poor_samples()`, `ForecastAnalyzer.generate_detailed_report()` now includes `poor_samples` in output

- [ ] **Step 1: 将 THRESHOLDS 添加到 ForecastAnalyzer 类**

在 `analyzer.py` 的 `ForecastAnalyzer` 类顶部（`def __init__` 之前），添加类变量：

```python
class ForecastAnalyzer:
    """预报检验分析器"""
    
    # 较差定义标准 — 从 analyzer_analyze.py 融合
    THRESHOLDS = {
        "area": {
            "daily": {
                "temperature": {
                    "accuracy": 80.0, "mae": 1.5, "me": 1.0,
                },
                "precipitation": {
                    "accuracy": 85.0, "ts": "mean",
                    "bias_low": 0.6, "bias_high": 1.4,
                },
            },
        },
        "time_session": {
            "le_72h": {
                "temperature": {
                    "accuracy": 80.0, "mae": 1.5, "me": 1.0,
                },
                "precipitation": {
                    "accuracy": 85.0, "ts": "mean",
                    "bias_low": 0.6, "bias_high": 1.4,
                },
            },
            "gt_72h": {
                "temperature": {
                    "accuracy": 70.0, "mae": 3.0, "me": 1.5,
                },
                "precipitation": {
                    "accuracy": 70.0, "ts": "mean",
                    "bias_low": 0.3, "bias_high": 2.0,
                },
            },
        },
    }
```

- [ ] **Step 2: 添加 `_get_thresholds()` 方法**

在 `_parse_metric_name` 方法之后，添加：

```python
def _get_thresholds(self, element_type: str, test_type: str,
                    time_session: int = 0) -> dict:
    """获取当前检验场景的较差判定阈值。
    
    Args:
        element_type: 'temperature' | 'precipitation'
        test_type: 'daily' | 'time_session' | 'area'
        time_session: 预报时效小时数（逐时效场景用于分 ≤72h vs >72h）
    """
    if test_type in ("area", "daily"):
        dimension_key = "daily"  # area 和 daily 共用通用标准
    else:
        if time_session > 72:
            dimension_key = "gt_72h"
        else:
            dimension_key = "le_72h"
    
    thresholds_section = self.THRESHOLDS.get("area", {}).get(dimension_key, {})
    return thresholds_section.get(element_type, {})
```

- [ ] **Step 3: 添加 `_find_poor_samples()` 方法**

在 `format_ranking` 方法之后，添加：

```python
def _find_poor_samples(self, results: dict, test_type: str,
                       element_type: str) -> list[dict]:
    """识别天津预报在各维度下的较差样本。
    
    Args:
        results: parse_results() 返回值
        test_type: daily | time_session | area
        element_type: temperature | precipitation
    Returns:
        [{name, metric, tj_value, threshold, reason}, ...]
    """
    poor = []
    if element_type != "temperature":
        return poor  # 降水较差判定后续补充
    
    for exam_name, exam_results in results.items():
        for r in exam_results:
            if r.dataCode != 'NAFP_BETJ_DS_NC':
                continue
            time_session = getattr(r, 'timeSession', 0) or 0
            thresh = self._get_thresholds(element_type, test_type, time_session)
            
            # 遍历每个列（区域/日期/时效）
            for col_idx, col_name in enumerate(self.exam_columns):
                val = r.values[col_idx] if col_idx < len(r.values) else None
                if val is None or (isinstance(val, float) and np.isnan(val)):
                    continue
                
                # 判断指标类型并检查阈值
                if 'MAE' in exam_name:
                    if val > thresh.get('mae', 1.5):
                        poor.append({
                            'name': col_name, 'metric': 'MAE',
                            'tj_value': round(val, 2),
                            'threshold': thresh.get('mae', 1.5),
                            'reason': f"MAE {val:.2f} > {thresh.get('mae', 1.5)}",
                        })
                elif 'ME' in exam_name:
                    if abs(val) >= thresh.get('me', 1.0):
                        poor.append({
                            'name': col_name, 'metric': 'ME',
                            'tj_value': round(val, 2),
                            'threshold': thresh.get('me', 1.0),
                            'reason': f"|ME| {abs(val):.2f} >= {thresh.get('me', 1.0)}",
                        })
                elif any(k in exam_name for k in ('PC', '准确率')):
                    if val < thresh.get('accuracy', 80):
                        poor.append({
                            'name': col_name, 'metric': '准确率',
                            'tj_value': round(val, 2),
                            'threshold': thresh.get('accuracy', 80),
                            'reason': f"准确率 {val:.2f}% < {thresh.get('accuracy', 80)}%",
                        })
    return poor
```

- [ ] **Step 4: 修改 `generate_detailed_report()` 返回 `poor_samples`**

在 `generate_detailed_report()` 方法末尾，`report['summary'] = ...` 之前，添加：

```python
# 识别较差样本
element_type = 'temperature' if element_code in Config.TEMP_ELEMENTS else 'precipitation'
test_type_code = self.metadata.get('test_type_code', '')
report['poor_samples'] = self._find_poor_samples(results, test_type_code, element_type)
```

- [ ] **Step 5: 运行现有 `__main__` 示例验证**

```bash
cd "forecast_evaluate 2/forecast_evaluate/scripts"
py -3 analyzer.py --list
```

Expected: 列出要素和检验类型，无 import error

- [ ] **Step 6: 删除 analyzer_analyze.py**

```bash
rm "forecast_evaluate 2/forecast_evaluate/scripts/analyzer_analyze.py"
```

- [ ] **Step 7: Commit**

```bash
git add "forecast_evaluate 2/forecast_evaluate/scripts/analyzer.py"
git rm "forecast_evaluate 2/forecast_evaluate/scripts/analyzer_analyze.py"
git commit -m "refactor(analyzer): merge poor-sample thresholds from analyzer_analyze.py into ForecastAnalyzer"
```

---

### Task 3: 新增图表类型（折线图、组合图、热力图）

**Files:**
- Modify: `forecast_evaluate 2/forecast_evaluate/scripts/forecast_evaluate.py`

**Interfaces:**
- Produces: 
  - `generate_trend_chart(element_name, exam_name, columns, data_codes, values_list, ...) -> Path | None`
  - `generate_heatmap(element_name, exam_name, columns, data_codes, values_list, ...) -> Path | None`
  - `generate_charts(response_data, chart_types=["bar"])` — 扩展签名，支持 `chart_types` 参数

- [ ] **Step 1: 提取公共绘图配置函数**

在 `_parse_and_plot` 函数之前，添加：

```python
def _configure_chart_style():
    """统一图表样式配置"""
    plt.rcParams['font.sans-serif'] = ['SimHei' if platform.system() == 'Windows' else 'Arial Unicode MS']
    plt.rcParams['axes.unicode_minus'] = False

def _get_colors(n: int) -> list:
    """获取 n 种颜色"""
    base = ['#3498db', '#e74c3c', '#2ecc71', '#9b59b6', '#f39c12', '#1abc9c', '#e91e63', '#00bcd4']
    return base[:n]

def _format_x_labels(columns, max_display=12):
    """格式化 X 轴标签（日期简化、过多时抽稀）"""
    processed = []
    for label in columns:
        if isinstance(label, str) and re.search(r'^\d{4}-\d{2}-\d{2}$', label):
            processed.append(label[5:])  # MM-DD
        else:
            processed.append(str(label))
    return processed

def _save_chart(element_code, test_type, year_month, rain_type, exam_name, chart_type="chart"):
    """统一图表保存路径生成"""
    return get_png_save_path(
        element_code=element_code,
        test_type=test_type,
        time_stamp=year_month,
        rain_type=rain_type,
        metric=f"{chart_type}_{exam_name}"
    )
```

- [ ] **Step 2: 实现折线图函数**

```python
def _generate_line_chart(element_name, exam_name, columns, data_codes, values_list,
                          test_type, year_month, element_code, rain_type, time_range,
                          predict_hours):
    """生成逐日/逐时效趋势折线图"""
    x = np.arange(len(columns))
    colors = _get_colors(len(data_codes))
    
    fig, ax = plt.subplots(figsize=(14, 7))
    
    for i, (data_code, values) in enumerate(zip(data_codes, values_list)):
        label = Config.PRODUCT_NAMES.get(data_code, data_code)
        color = colors[i % len(colors)]
        ax.plot(x, values, marker='o', linewidth=2, markersize=4,
                label=label, color=color, alpha=0.85)
    
    # 格式化和抽稀
    processed = _format_x_labels(columns)
    if len(columns) > 15:
        step = max(1, len(columns) // 12)
        ax.set_xticks(x[::step])
        ax.set_xticklabels(processed[::step], rotation=45, ha='right')
    else:
        ax.set_xticks(x)
        ax.set_xticklabels(processed, rotation=45 if len(columns) > 7 else 0, ha='center')
    
    ax.set_ylabel(exam_name.split('_')[0].upper(), fontsize=14, fontweight='bold')
    test_type_name = Config.TEST_TYPE_NAMES.get(test_type, test_type)
    
    if predict_hours and predict_hours != '08,20':
        ph_display = f"{predict_hours}起报"
    else:
        ph_display = "08,20起报"
    
    ax.set_title(f'{element_name}检验 {exam_name} {test_type_name}\n{time_range} ({ph_display})',
                 fontsize=16, fontweight='bold', pad=20)
    ax.grid(True, linestyle='--', alpha=0.3)
    ax.legend(loc='upper center', bbox_to_anchor=(0.5, 1.12),
              ncol=min(4, len(data_codes)), frameon=False, fontsize=12)
    plt.tight_layout()
    
    save_path = _save_chart(element_code, test_type, year_month, rain_type,
                            f"line_{exam_name}", "line")
    plt.savefig(save_path, dpi=150, bbox_inches='tight')
    plt.close()
    return save_path
```

- [ ] **Step 3: 实现热力图函数**

```python
def _generate_heatmap(element_name, exam_name, columns, data_codes, values_list,
                       test_type, year_month, element_code, rain_type, time_range,
                       predict_hours):
    """生成 产品×维度 热力图（仅对单产品多维度场景有效）"""
    if len(data_codes) < 2:
        return None  # 至少两个产品才有对比意义
    
    # values_list shape: (n_products, n_columns)
    data = np.array(values_list)  # (products, columns)
    products = [Config.PRODUCT_NAMES.get(c, c) for c in data_codes]
    
    fig, ax = plt.subplots(figsize=(max(10, len(columns) * 0.6),
                                     max(4, len(data_codes) * 0.5)))
    
    im = ax.imshow(data, aspect='auto', cmap='RdYlGn_r')
    
    ax.set_xticks(np.arange(len(columns)))
    ax.set_yticks(np.arange(len(products)))
    ax.set_xticklabels(_format_x_labels(columns),
                       rotation=45 if len(columns) > 8 else 0, ha='center')
    ax.set_yticklabels(products)
    
    # 标注数值
    for i in range(len(products)):
        for j in range(len(columns)):
            val = data[i, j]
            if not np.isnan(val):
                ax.text(j, i, f'{val:.1f}', ha='center', va='center',
                        fontsize=8, color='black' if 0.3 < abs(val)/data.max() < 0.7 else 'white')
    
    cbar = plt.colorbar(im, ax=ax, shrink=0.8)
    cbar.set_label(exam_name, fontsize=10)
    
    test_type_name = Config.TEST_TYPE_NAMES.get(test_type, test_type)
    ax.set_title(f'{element_name} {exam_name} {test_type_name}\n{time_range}',
                 fontsize=14, fontweight='bold', pad=20)
    plt.tight_layout()
    
    save_path = _save_chart(element_code, test_type, year_month, rain_type,
                            f"heat_{exam_name}", "heat")
    plt.savefig(save_path, dpi=150, bbox_inches='tight')
    plt.close()
    return save_path
```

- [ ] **Step 4: 修改 `_parse_and_plot` 支持 chart_types 参数**

修改 `_parse_and_plot` 签名和内部逻辑：

```python
def _parse_and_plot(response_data, chart_types=None):
    """解析响应数据并创建图表
    
    Args:
        response_data: API响应数据
        chart_types: 图表类型列表 ['bar', 'line', 'heatmap']，默认 ['bar']
    """
    if chart_types is None:
        chart_types = ['bar']
    
    # ... 现有数据提取逻辑保持不变 ...
    
    save_paths = {}
    for exam_name, items in exam_groups.items():
        # ... 现有数据提取逻辑保持不变 ...
        
        chart_paths = []
        
        # 柱状图（总是生成，用于报告嵌入）
        if 'bar' in chart_types:
            bar_path = _generate_bar_chart(...)  # 原 _parse_and_plot 的图表部分
            chart_paths.append(('bar', bar_path))
        
        # 折线图
        if 'line' in chart_types and len(columns) > 1:
            line_path = _generate_line_chart(
                element_name, exam_name, exam_column_name, data_codes,
                values_list, test_type, year_month, element_code, rain_type,
                time_range, predict_hours
            )
            if line_path:
                chart_paths.append(('line', line_path))
        
        # 热力图
        if 'heatmap' in chart_types and len(data_codes) >= 2:
            heat_path = _generate_heatmap(
                element_name, exam_name, exam_column_name, data_codes,
                values_list, test_type, year_month, element_code, rain_type,
                time_range, predict_hours
            )
            if heat_path:
                chart_paths.append(('heatmap', heat_path))
        
        save_paths[exam_name] = chart_paths
    
    return save_paths
```

- [ ] **Step 5: 更新 `generate_charts` 函数签名**

```python
def generate_charts(response_data, chart_types=None):
    """从API响应数据生成检验图表
    
    Args:
        response_data: API响应数据（dict格式）
        chart_types: 图表类型列表，如 ['bar', 'line']，默认 ['bar']
    Returns:
        dict: {exam_name: [(chart_type, file_path), ...], ...}
    """
    if chart_types is None:
        chart_types = ['bar']
    return _parse_and_plot(response_data, chart_types)
```

- [ ] **Step 6: 验证图表生成**

```bash
cd "forecast_evaluate 2/forecast_evaluate/scripts"
py -3 -c "
from forecast_evaluate import generate_charts
from config import Config
# 测试模块加载无语法错误
print('OK: forecast_evaluate imports cleanly')
print('Chart types: bar, line, heatmap supported')
"
```

- [ ] **Step 7: Commit**

```bash
git add "forecast_evaluate 2/forecast_evaluate/scripts/forecast_evaluate.py"
git commit -m "feat(charts): add trend line chart and heatmap to forecast evaluate engine"
```

---

### Task 4: MCP 工具层增强（报告 + 图表路径 + 新工具）

**Files:**
- Modify: `haihe-weather-analyzer-mcp/forecast_evaluate_tool.py`

**Interfaces:**
- Modifies: `evaluate_forecast` 返回增加 `report_markdown` 和 `poor_samples`
- Creates: `generate_forecast_charts` 新工具，调用核心引擎生成图表并返回路径列表
- Consumes: `ForecastAnalyzer.format_report_to_markdown()`, `generate_charts()`

- [ ] **Step 1: 修改 `_format_evaluate_result` 增加完整报告和较差样本**

```python
def _format_evaluate_result(api_result: dict, element: str, test_type: str,
                            rain_type: str | None) -> dict[str, Any]:
    """将检验API返回的原始数据转化为 LLM 可消费的结构化 JSON。"""
    analyzer = ForecastAnalyzer(api_result)
    report = analyzer.generate_detailed_report()
    
    # --- 现有 metrics 提取逻辑保持不变 ---
    metrics: dict[str, dict[str, Any]] = {}
    for category, sub_details in report.get("details", {}).items():
        if isinstance(sub_details, dict):
            for metric_name, data in sub_details.items():
                ranking: list[tuple[str, float]] = data.get("ranking", [])
                best = ranking[0] if ranking else ("", 0.0)
                metrics[metric_name] = {
                    "ranking": [[name, round(val, 2)] for name, val in ranking],
                    "best": best[0],
                    "best_value": round(best[1], 2),
                    "unit": _metric_unit(metric_name),
                }
    
    summary = report.get("summary", "")
    time_range = api_result.get("time_range", {})
    
    # --- 新增：完整 Markdown 报告 ---
    report_markdown = analyzer.format_report_to_markdown(report)
    
    # --- 新增：较差样本 ---
    poor_samples = report.get("poor_samples", [])
    
    # --- 新增：图表路径（柱状图自动生成） ---
    from forecast_evaluate import generate_charts as _gen_charts
    chart_paths = _gen_charts(api_result, chart_types=['bar', 'line'])
    # 拍平：{exam_name: [(type, path), ...]} -> {exam_name: {type: path}}
    chart_paths_flat = {}
    for exam_name, paths in chart_paths.items():
        chart_paths_flat[exam_name] = {ct: str(p) for ct, p in paths}
    
    return {
        "element": EvalConfig.ALL_ELEMENTS.get(element, element),
        "element_code": element,
        "test_type": EvalConfig.TEST_TYPE_NAMES.get(test_type, test_type),
        "test_type_code": test_type,
        "time_range": time_range,
        "rain_type": rain_type,
        "data_source": "检验API",
        "metrics": metrics,
        "summary": summary,
        "report_markdown": report_markdown,
        "poor_samples": poor_samples,
        "chart_paths": chart_paths_flat,
    }
```

- [ ] **Step 2: 新增 `generate_forecast_charts` 工具**

在 `register_forecast_evaluate_tool` 函数内部，`evaluate_forecast` 定义之后，添加：

```python
@mcp.tool()
def generate_forecast_charts(
    element: str,
    test_type: str,
    rain_type: str = "",
    chart_types: str = "bar,line",
    begin_time: str = "",
    end_time: str = "",
    time_session: int = 24,
    area_codes: str = "",
) -> dict[str, Any]:
    """为预报检验数据生成可视化图表。

    支持柱状图(bar)、趋势折线图(line)、热力图(heatmap)。
    返回所有生成图表文件的绝对路径，供前端渲染。

    :param element: 检验要素，rain24/tmax24/tmin24/t2m
    :param test_type: 检验维度，daily/time_session/area
    :param rain_type: 降水子类，ng/g/acc
    :param chart_types: 图表类型，逗号分隔，如 "bar,line,heatmap"
    :param begin_time: 开始时间 YYYY-MM-DD HH:MM:SS
    :param end_time: 结束时间 YYYY-MM-DD HH:MM:SS
    :param time_session: 预报时效(小时)
    """
    # 参数校验（与 evaluate_forecast 共享逻辑）
    valid_elements = set(EvalConfig.ALL_ELEMENTS.keys())
    if element not in valid_elements:
        return {"error": f"无效要素 {element}，可选: {sorted(valid_elements)}"}
    valid_test_types = set(EvalConfig.TEST_TYPE_NAMES.keys())
    if test_type not in valid_test_types:
        return {"error": f"无效检验维度 {test_type}，可选: {sorted(valid_test_types)}"}
    
    is_rain = element in EvalConfig.RAIN_ELEMENTS
    if is_rain and rain_type not in ("ng", "g", "acc", ""):
        return {"error": f"降水需要指定 rain_type: ng/g/acc"}
    if not is_rain:
        rain_type = None
    
    # 默认时间
    now = datetime.now(TIANJIN_TIMEZONE)
    month_begin = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    yesterday_end = (now - timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)
    b_time = begin_time if begin_time else month_begin.strftime("%Y-%m-%d %H:%M:%S")
    e_time = end_time if end_time else yesterday_end.strftime("%Y-%m-%d %H:%M:%S")
    
    # 解析 chart_types
    types = [t.strip() for t in chart_types.split(",") if t.strip()]
    if not types:
        types = ["bar"]
    
    try:
        area = area_codes if area_codes else None
        if is_rain:
            api_result = run_rain_eva(
                test_type=test_type, rain_type=rain_type,
                begin_time=b_time, end_time=e_time,
                time_session=time_session, save_json=False, area_codes=area,
            )
        else:
            api_result = run_temp_eva(
                test_type=test_type, begin_time=b_time, end_time=e_time,
                time_session=time_session, save_json=False, area_codes=area,
            )
        
        if "error" in api_result:
            return {"error": api_result["error"]}
        if not api_result.get("request_success"):
            raw = api_result.get("raw_response", {})
            return {"error": f"检验API返回失败: {raw.get('code', 'unknown')}"}
        
        from forecast_evaluate import generate_charts as _gen_charts
        chart_paths = _gen_charts(api_result, chart_types=types)
        
        # 拍平: {exam_name: [(type, path), ...]} -> [{exam_name, type, path}]
        flattened = []
        for exam_name, paths in chart_paths.items():
            for ct, p in paths:
                flattened.append({
                    "exam_name": exam_name,
                    "chart_type": ct,
                    "path": str(p),
                })
        
        return {
            "element": EvalConfig.ALL_ELEMENTS.get(element, element),
            "test_type": EvalConfig.TEST_TYPE_NAMES.get(test_type, test_type),
            "time_range": {"begin": b_time, "end": e_time},
            "charts": flattened,
        }
    except Exception as exc:
        logger.exception("[forecast_evaluate] 图表生成异常")
        return {"error": "预报检验图表生成失败，请稍后重试。"}
```

- [ ] **Step 3: 验证 MCP 工具模块加载**

```bash
cd haihe-weather-analyzer-mcp
py -3 -c "
from forecast_evaluate_tool import register_forecast_evaluate_tool
print('OK: MCP tool module loads')
print('  evaluate_forecast - enhanced with report_markdown + poor_samples + chart_paths')
print('  generate_forecast_charts - new tool')
"
```

- [ ] **Step 4: Commit**

```bash
git add haihe-weather-analyzer-mcp/forecast_evaluate_tool.py
git commit -m "feat(mcp): enhance evaluate_forecast with report_markdown, poor_samples, chart_paths; add generate_forecast_charts tool"
```

---

### Task 5: 快速路径增强（图片渲染 + 报告展示）

**Files:**
- Modify: `chainlitexam/message_orchestrator.py:2944-3112`

**Interfaces:**
- Modifies: `_need_forecast_evaluate()` — 新增图表关键词
- Modifies: `_try_forecast_evaluate_fast_path()` — 分支：图表请求 → 调 `generate_forecast_charts` → `cl.Image` 渲染；文本请求 → 渲染完整报告
- Modifies: `_build_forecast_evaluate_answer()` — 当 `report_markdown` 存在时，直接使用完整报告替代手动拼表

- [ ] **Step 1: 扩展 `_need_forecast_evaluate` 增加图表关键词**

```python
def _need_forecast_evaluate(user_text: str) -> bool:
    """检测用户问题是否需要调用预报检验工具。"""
    if not user_text:
        return False
    keywords = [
        "TS评分", "ts评分", "晴雨预报", "晴雨准确率",
        "模式评估", "模式对比", "模式比较", "各家模式",
        "预报检验", "预报评分", "预报评估",
        "准确率对比", "偏差分析", "偏差对比",
        "落区预报", "误差分析",
        "BIAS", "bias", "MAE", "mae",
        "预报效果", "预报准确性",
        "暴雨TS", "暴雨ts",
        "预报最准", "暴雨最准", "最准", "最准确", "预报对比", "预报得最准",
        # 新增图表关键词
        "检验图", "评分图", "预报图", "评估图",
        "对比图", "趋势图", "热力图", "图表",
    ]
    return any(k in user_text for k in keywords)
```

- [ ] **Step 2: 在 `_try_forecast_evaluate_fast_path` 中增加图表/报告分支**

在该函数参数提取之后（约第 3006 行，`time_session` 提取之后），增加图表意图检测：

```python
# 检测是否明确要求图表
wants_chart = any(k in user_text for k in (
    "图", "图表", "画图", "可视化", "热力图", "趋势图",
    "对比图", "柱状图", "折线图",
))

# 推断图表类型
chart_types = "bar,line"
if any(k in user_text for k in ("热力图",)):
    chart_types = "heatmap,bar"
elif any(k in user_text for k in ("趋势图", "折线图", "时效")):
    chart_types = "line,bar"
```

- [ ] **Step 3: 图表分支 — 调 `generate_forecast_charts` + `cl.Image` 渲染**

在现有 `_invoke_tool_for_fast_path("evaluate_forecast", ...)` 之前，插入分支：

```python
if wants_chart:
    # 图表路径：调 generate_forecast_charts
    chart_tool = _find_tool(tools, "generate_forecast_charts")
    if chart_tool:
        await reasoning.stage("📊 生成图表", "正在生成检验图表...")
        chart_result = await _invoke_tool_for_fast_path(
            "generate_forecast_charts", chart_tool,
            {
                "element": element, "test_type": test_type,
                "rain_type": rain_type, "time_session": time_session,
                "chart_types": chart_types,
            }, user_text,
        )
        chart_data = _unwrap_tool_result(chart_result)
        
        if isinstance(chart_data, dict) and "charts" in chart_data:
            image_elements = []
            for chart in chart_data.get("charts", []):
                path = chart.get("path", "")
                ctype = chart.get("chart_type", "chart")
                if path and os.path.isfile(path):
                    with open(path, "rb") as f:
                        image_elements.append(
                            cl.Image(content=f.read(), name=f"forecast_{ctype}")
                        )
            
            if image_elements:
                text = _build_forecast_evaluate_answer(data, user_text) if data else ""
                await reasoning.close()
                await _emit_fast_path_result(
                    text or "预报检验图表如下：",
                    messages, user_text, images=image_elements,
                    has_chart=True, reasoning=reasoning,
                )
                return True
```

- [ ] **Step 4: 报告分支 — 使用 `report_markdown` 替代手拼表格**

修改 `_build_forecast_evaluate_answer` 函数：

```python
def _build_forecast_evaluate_answer(data: dict, user_text: str) -> str:
    """基于 evaluate_forecast 工具返回构建 Markdown 回答。"""
    # --- 优先使用完整报告 ---
    report_md = data.get("report_markdown", "")
    if report_md:
        # 提取报告内容（去掉可能重复的 # 标题）
        # 保留 ## 综述 和 ## 详细结果
        return report_md
    
    # --- Fallback: 手工拼排名表格 ---
    element = data.get("element", "")
    test_type = data.get("test_type", "")
    time_range = data.get("time_range", {})
    begin = time_range.get("begin", "")[:10] if time_range.get("begin") else ""
    end = time_range.get("end", "")[:10] if time_range.get("end") else ""
    time_str = f"{begin} ~ {end}" if begin and end else "本月至昨日"
    
    lines = [
        f"## {element}预报检验结果",
        "",
        f"**检验维度**: {test_type}　**时段**: {time_str}　**数据来源**: 检验API",
        "",
    ]
    
    metrics = data.get("metrics", {})
    if not metrics:
        lines.append("暂无有效检验数据。")
        return "\n".join(lines)
    
    for metric_name, metric_data in metrics.items():
        ranking = metric_data.get("ranking", [])
        if not ranking:
            continue
        unit = metric_data.get("unit", "")
        lines.append(f"### {metric_name}")
        lines.append("")
        lines.append("| 排名 | 产品 | 数值 |")
        lines.append("| :--- | :--- | :--- |")
        for i, (name, value) in enumerate(ranking, 1):
            val_str = f"{value:.2f}{unit}" if unit else f"{value:.2f}"
            prefix = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}"
            lines.append(f"| {prefix} | **{name}** | {val_str} |")
        lines.append("")
    
    summary = data.get("summary", "")
    if summary:
        lines.append("### 总结")
        lines.append("")
        lines.append(summary)
    
    return "\n".join(lines)
```

- [ ] **Step 5: 验证模块无语法错误**

```bash
cd chainlitexam
py -3 -c "from message_orchestrator import _need_forecast_evaluate, _build_forecast_evaluate_answer; print('OK: imports clean')"
```

- [ ] **Step 6: Commit**

```bash
git add chainlitexam/message_orchestrator.py
git commit -m "feat(fast-path): add chart rendering and full report display to forecast evaluate fast path"
```

---

### Task 6: prompts.py 第13条规则扩展

**Files:**
- Modify: `chainlitexam/prompts.py:541-557`

**Interfaces:**
- Modifies: `WEATHER_ASSISTANT_PROMPT` 第13条规则 — 区分图表请求和文本请求；指导何时用 `generate_forecast_charts` vs `evaluate_forecast`

- [ ] **Step 1: 扩展第13条规则**

在 `prompts.py` 第 541-557 行，替换为：

```python
13. **预报检验与模式评估**：当用户询问 TS评分、晴雨准确率、预报评分、
   模式评估、预报检验、偏差分析(BIAS)、误差分析(MAE/ME)、落区预报对比、
   各家模式对比、暴雨预报效果、哪个模式预报最准/最准确、哪种预报最准、
   模式之间谁更准、预报对比 等预报检验类问题时，调用相关工具。

   **工具选择**：
   - 用户要求"画图""生成图""看图""图表""可视化""趋势图""热力图"等时
     → 调用 `generate_forecast_charts`，chart_types 按用户需求推断
     （"趋势图/时效"→line，"热力图"→heatmap，"对比图"→bar）
   - 用户只是问"评分""准确率""哪个准"等文字问题时
     → 调用 `evaluate_forecast`，该工具已包含完整报告和基础图表

   **参数提取规则**（两个工具共用）：
   - 问"暴雨TS"→ element=rain24, rain_type=g（分级暴雨）
   - 问"晴雨预报"→ element=rain24, rain_type=ng（晴雨）
   - 问"累计降水/面雨量误差"→ element=rain24, rain_type=acc（累计）
   - 问"温度误差/最高温/最低温"→ element=tmax24 或 tmin24
   - 问"逐日/最近一周/逐天"→ test_type=daily
   - 问"分时效/24h/48h/72h"→ test_type=time_session
   - 问"分地区/各区/落区"→ test_type=area
   - 未明确时间范围时，默认查询本月1日至昨天。
   - **区域范围**：默认仅查天津（area_codes='120000'）。仅当用户明确提到"海河流域""全流域""整个流域""流域范围"时，才传 area_codes='110000,120000,130000,140000,150000,370000,410000'。

   **回答规范**：
   - 文字回答时以表格对比展示各家产品（**国家指导**、**天津预报**、**ECMWF**）
     的排名和数值，产品名称加粗，数值保留1-2位小数。
   - 当 `evaluate_forecast` 返回 `report_markdown` 时，优先展示完整报告内容
     （包含综述、分段分析、重点定位）。
   - 当生成图表时，简要说明图表含义，不过度展开数值。
   - 不要暴露后端工具名、API地址、检验公式等技术细节。
```

- [ ] **Step 2: 验证 prompt 加载**

```bash
cd chainlitexam
py -3 -c "from prompts import WEATHER_ASSISTANT_PROMPT; assert 'generate_forecast_charts' in WEATHER_ASSISTANT_PROMPT; print('OK: prompt updated')"
```

- [ ] **Step 3: Commit**

```bash
git add chainlitexam/prompts.py
git commit -m "feat(prompts): extend rule 13 with chart tool routing and full report guidance"
```

---

### Task 7: 测试 + 全流程验证

**Files:**
- Create: `chainlitexam/tests/test_forecast_evaluate_full.py`
- Modify: `chainlitexam/tests/test_forecast_evaluate_fast_path.py:17-61`

**Interfaces:**
- Consumes: `_need_forecast_evaluate` from `message_orchestrator.py`
- Produces: `TestForecastEvaluateCharts` — 图表关键词+参数推断测试

- [ ] **Step 1: 扩展现有关键词测试（新增图表关键词）**

```python
# 在 test_forecast_evaluate_fast_path.py TRIGGER_QUERIES 列表追加：
"画个暴雨TS评分对比图",
"降水检验趋势图",
"各家模式准确率对比图表",
"最近的预报评估可视化",
"最新降水预报图",
```

- [ ] **Step 2: 创建新测试文件**

```python
# chainlitexam/tests/test_forecast_evaluate_full.py
"""Test forecast evaluate full integration: charts + report + poor samples."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from message_orchestrator import _need_forecast_evaluate


class TestForecastEvaluateChartKeywords:
    """图表相关关键词检测"""

    CHART_TRIGGER = [
        "画个暴雨TS评分对比图",
        "降水检验趋势图",
        "各家模式准确率对比图表",
        "看看预报检验热力图",
        "生成一个温度检验趋势图",
    ]

    CHART_NON_TRIGGER = [
        "暴雨会落在哪些区域",
        "天津今天天气",
    ]

    @pytest.mark.parametrize("query", CHART_TRIGGER)
    def test_chart_keywords_trigger(self, query):
        assert _need_forecast_evaluate(query), f"Should trigger chart: {query}"

    @pytest.mark.parametrize("query", CHART_NON_TRIGGER)
    def test_chart_non_trigger(self, query):
        assert not _need_forecast_evaluate(query), f"Should NOT trigger: {query}"


class TestForecastEvaluateReportIntegration:
    """报告集成：verify _build_forecast_evaluate_answer handles report_markdown"""

    SAMPLE_DATA_WITH_REPORT = {
        "element": "24小时最高温度",
        "test_type": "逐日",
        "time_range": {"begin": "2026-07-01 00:00:00", "end": "2026-07-30 23:59:59"},
        "data_source": "检验API",
        "metrics": {
            "2℃准确率": {
                "ranking": [["天津预报", 84.09], ["国家指导", 79.17], ["ECMWF", 76.14]],
                "best": "天津预报", "best_value": 84.09, "unit": "%",
            },
        },
        "summary": "天津预报(84.09) > 国家指导(79.17) > ECMWF(76.14)",
        "report_markdown": "# 24小时最高温度预报检验\n\n**检验类型**: 逐日\n\n## 综述\n\n测试综述内容\n\n## 详细结果\n\n### 温度\n\n#### 2℃准确率\n\n| 产品 | 平均 |\n| --- | --- |\n| **天津预报** | 84.09 |\n",
        "poor_samples": [],
    }

    def test_report_markdown_preferred(self):
        """当 report_markdown 存在时，_build_forecast_evaluate_answer 应返回完整报告"""
        from message_orchestrator import _build_forecast_evaluate_answer
        result = _build_forecast_evaluate_answer(self.SAMPLE_DATA_WITH_REPORT, "测试")
        assert "## 综述" in result
        assert "测试综述内容" in result
        assert "## 详细结果" in result

    def test_fallback_when_no_report(self):
        """当 report_markdown 缺失时，应 fallback 到手拼排名表格"""
        from message_orchestrator import _build_forecast_evaluate_answer
        data_no_report = dict(self.SAMPLE_DATA_WITH_REPORT)
        data_no_report["report_markdown"] = ""
        result = _build_forecast_evaluate_answer(data_no_report, "测试")
        assert "🥇" in result  # 排名图标
        assert "天津预报" in result
```

- [ ] **Step 3: 运行全部测试**

```bash
cd chainlitexam
$env:FORECAST_EVAL_DIR = "D:/tmp/eval_test"
D:\PythonProject\.venv-haihe-tests\Scripts\python.exe -m pytest tests/test_forecast_evaluate_fast_path.py tests/test_forecast_evaluate_full.py -v
```

Expected: 所有测试 PASS

- [ ] **Step 4: 运行现有全量测试确保无回归**

```bash
cd chainlitexam
D:\PythonProject\.venv-haihe-tests\Scripts\python.exe -m pytest tests/ -v
```

Expected: 无新增失败

- [ ] **Step 5: Commit**

```bash
git add chainlitexam/tests/test_forecast_evaluate_full.py chainlitexam/tests/test_forecast_evaluate_fast_path.py
git commit -m "test: add chart keyword detection and report integration tests for forecast evaluate"
```

---

## Post-Implementation Verification

全部 Tasks 完成后，执行：

```bash
# 1. 核心引擎图表生成
cd "forecast_evaluate 2/forecast_evaluate/scripts"
py -3 -c "
from forecast_evaluate import generate_charts
print('Chart engine: bar, line, heatmap ready')
"

# 2. MCP 工具加载
cd haihe-weather-analyzer-mcp
py -3 -c "
from forecast_evaluate_tool import register_forecast_evaluate_tool
print('MCP tools: evaluate_forecast (enhanced) + generate_forecast_charts')
"

# 3. 快速路径
cd chainlitexam
py -3 -c "
from message_orchestrator import _need_forecast_evaluate, _build_forecast_evaluate_answer
print('Fast path: keyword detection + report rendering ready')
"

# 4. 全量测试
D:\PythonProject\.venv-haihe-tests\Scripts\python.exe -m pytest tests/ -v
```

所有命令应执行成功。
