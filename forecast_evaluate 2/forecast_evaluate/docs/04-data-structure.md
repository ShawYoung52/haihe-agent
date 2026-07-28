# 数据结构与存储规范

## 目录结构

```
{BASE_SAVE_DIR}/
├── {element}/                    # 要素代码 (t2m, rain24等)
│   ├── {test_type}/              # 检验方式 (daily, time_session, area)
│   │   ├── {subtype}/            # 分组类型 (default, ng, g0.1, acc10等)
│   │   │   ├── {YYYYMM}.json     # 数据文件
│   │   │   └── {YYYYMM}_{metric}.png  # 图表文件（按指标命名）
```

**说明**: PNG图表文件包含 `{metric}` 指标名称，避免不同检验指标的结果相互覆盖。

## 统一数据格式

所有检验结果JSON文件（除summary外）均包含以下统一结构：

```json
{
  "element": "要素名称",
  "element_code": "要素代码",
  "test_type": "检验方式名称",
  "test_type_code": "检验方式代码",
  "time_range": {"begin": "开始时间", "end": "结束时间"},
  "query_time": "查询时间",
  "image_url": ["图表路径列表"],
  "raw_response": {"API原始返回数据"},
  "analysis": {"分析报告"}
}
```

### 降水特有字段

```json
{
  "rain_type": "ng",
  "rain_type_desc": "晴雨不分级",
  "threshold": null
}
```

### 温度数据结构

温度数据无额外特有字段，使用统一格式。

## 图片存储规范

### 存储路径

图片与JSON数据保存在同一目录结构下：

```
{BASE_SAVE_DIR}/{element}/{test_type}/{subtype}/{YYYYMM}.png
```

### 文件名规范

PNG文件命名包含检验指标，避免不同指标的结果相互覆盖：
- `{YYYYMM}_{metric}.png`
- `metric` 处理规则：
  1. 只取"_"之前的部分（如 `TS_ng:0` → `TS`）
  2. 移除所有冒号（如 `PC:2` → `PC2`）

**示例**：
- `202604_PC2.png` - 准确率(PC:2)
- `202604_TS.png` - TS评分(晴雨检验)
- `202604_MAE.png` - 平均绝对误差
- `202604_BIAS.png` - 偏差

### 路径生成工具

```python
from forecast_evaluate import get_json_save_path, get_png_save_path

# 生成JSON保存路径
json_path = get_json_save_path(
    element_code='t2m',
    test_type='daily',
    year_month='202604',
    rain_type=None,
    base_dir='/Users/merlinq/Workspace/download'
)

# 生成PNG保存路径（包含指标名称）
png_path = get_png_save_path(
    element_code='rain24',
    test_type='time_session',
    year_month='202604',
    rain_type='g',
    threshold=10,
    metric='TS_g0',  # 新增参数：检验指标
    base_dir='/Users/merlinq/Workspace/download'
)
```

## 数据加载

### 使用 ForecastDataLoader

```python
from forecast_evaluate.scripts.data_loader import ForecastDataLoader

loader = ForecastDataLoader('/Users/merlinq/Workspace/download/')

# 按路径加载（新目录结构）
data = loader.load_by_path('t2m', 'daily', 'default', '202604')

# 按模式加载
data_list = loader.load_by_pattern(
    element_code='t2m',
    test_type='daily',
    year_month='202604'
)

# 加载单个文件
data = loader.load_json('t2m/daily/default/202604.json')
```

### 生成汇总报告

```python
from forecast_evaluate import generate_rain_report, generate_temp_report

# 生成降水检验汇总报告
rain_report = generate_rain_report('202604')

# 生成温度检验汇总报告
temp_report = generate_temp_report('202604')
```
