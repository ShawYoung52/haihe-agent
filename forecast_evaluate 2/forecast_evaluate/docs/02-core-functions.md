# 核心函数详解

## 1. Config 类 - 配置参数

```python
from config import Config

# 访问配置
Config.SAVE_DIR              # 保存目录
Config.API_URL               # API地址
Config.USERNAME              # 用户名
Config.RAIN_ELEMENT_CODE     # 降水要素列表
Config.TEMP_ELEMENT_CODE     # 温度要素列表
```

## 2. create_rain_test_json - 构建降水检验参数

构建降水检验的JSON请求参数。

```python
from forecast_evaluate import create_rain_test_json

json_data = create_rain_test_json(
    'time_session',                    # 检验方式
    beginTime='2026-04-01 00:00:00',
    endTime='2026-04-30 23:59:59',
    elementCode='rain24',              # 要素代码
    algorithmType='ng',                # 检验类型
    algorithmArgs=None,                # 分级阈值
    **kwargs
)
```

**参数说明**:

| 参数 | 说明 | 默认值 |
|------|------|--------|
| test_type | 检验方式 | 必填 |
| elementCode | 要素代码 | rain24 |
| algorithmType | 检验类型 | ng |
| algorithmArgs | 分级/累计阈值 | None |

**检验类型**:
- `ng`: 晴雨不分级
- `g`: 分级检验
- `acc`: 累计检验

## 3. create_temp_test_json - 构建温度检验参数

```python
from forecast_evaluate import create_temp_test_json

json_data = create_temp_test_json(
    'time_session',                    # 检验方式
    elementCode='t2m',                 # 要素代码
    algorithmName='PC:2,MAE',          # 检验算法
    **kwargs
)
```

**温度要素**:
- `t2m`: 2米温度
- `tmax24`: 24小时最高温度
- `tmin24`: 24小时最低温度

## 4. request_scores - 发送请求并绘图

发送检验请求，获取数据并生成图表。

```python
from forecast_evaluate import request_scores

save_paths, response_text = request_scores(json_data)
```

**返回**:
- `save_paths`: 图表保存路径列表
- `response_text`: API原始响应文本

## 5. run_rain_eva - 运行降水检验（便捷函数）

一键运行降水检验，自动构建参数、发送请求、保存结果。

```python
from forecast_evaluate import run_rain_eva

result = run_rain_eva(
    test_type,                # 检验方式
    rain_type='ng',           # 检验类型
    threshold=None,           # 分级阈值
    year_month='202604',      # 年月（用于保存路径）
    save_json=True,           # 是否保存JSON
    base_dir=None,            # 基础目录
    **kwargs                  # 其他参数
)
```

**返回结构**:
```python
{
    'image_url': ['图表路径列表'],
    'data': '原始数据',
    'json_file': 'JSON保存路径'  # 当save_json=True时
}
```

## 6. run_temp_eva - 运行温度检验（便捷函数）

```python
from forecast_evaluate import run_temp_eva

result = run_temp_eva(
    test_type,                # 检验方式
    year_month='202604',      # 年月
    save_json=True,           # 是否保存JSON
    base_dir=None,            # 基础目录
    elementCode='t2m',        # 要素代码
    algorithmName='PC:2,MAE', # 检验算法
    **kwargs
)
```

**返回结构**:
```python
{
    'image_url': ['图表路径列表'],
    'raw_response': 'API原始数据',
    'analysis': '分析报告',
    'json_file': 'JSON保存路径'  # 当save_json=True时
}
```

## 7. run_rain_eva_all_grades - 批量运行所有分级检验

一次性运行所有降水检验类型（ng/g/acc）。

```python
from forecast_evaluate import run_rain_eva_all_grades

results = run_rain_eva_all_grades('time_session')
```

**返回**: 包含所有检验类型的结果字典

## 8. 路径生成工具函数

### get_json_save_path - 生成JSON保存路径

```python
from forecast_evaluate import get_json_save_path

filepath = get_json_save_path(
    element_code='t2m',
    test_type='daily',
    year_month='202604',
    rain_type=None,           # 温度数据为None
    threshold=None,
    base_dir='/Users/merlinq/Workspace/download'
)
# 返回: Path('/Users/merlinq/Workspace/download/t2m/daily/default/202604.json')
```

### get_png_save_path - 生成PNG保存路径

```python
from forecast_evaluate import get_png_save_path

filepath = get_png_save_path(
    element_code='rain24',
    test_type='time_session',
    year_month='202604',
    rain_type='g',
    threshold=10,
    base_dir='/Users/merlinq/Workspace/download'
)
# 返回: Path('/Users/merlinq/Workspace/download/rain24/time_session/g10/202604.png')
```
