# 使用示例

## 示例1：降水逐时效检验

```bash
python3 -c "
from forecast_evaluate import create_rain_test_json, request_scores

json_data = create_rain_test_json(
    'time_session',
    beginTime='2026-04-01 00:00:00',
    endTime='2026-04-30 23:59:59'
)
save_paths, data = request_scores(json_data)
print('Charts:', save_paths)
"
```

## 示例2：温度地区对比检验

```bash
python3 -c "
from forecast_evaluate import create_temp_test_json, request_scores

json_data = create_temp_test_json(
    'area',
    elementCode='t2m',
    algorithmName='PC:2,MAE'
)
save_paths, data = request_scores(json_data)
print('Charts:', save_paths)
"
```

## 示例3：使用便捷函数运行降水检验

```bash
python3 -c "
from forecast_evaluate import run_rain_eva

result = run_rain_eva(
    'daily',
    rain_type='ng',
    timeSessions='48',
    areaCodes='120000',
    year_month='202604'
)
print('Images:', result['image_url'])
print('JSON saved to:', result.get('json_file'))
"
```

## 示例4：批量下载所有检验数据

```bash
python3 -c "
from forecast_evaluate.scripts.batch_download import download_all_forecast_data

result = download_all_forecast_data(
    save_dir='/Users/merlinq/Workspace/download/',
    year_month='202604'
)
print(f\"下载完成: {result['total_files']} 个文件\")
print(f\"成功: {result['success']}, 失败: {result['failed']}\")
"
```

## 示例5：运行所有分级降水检验

```bash
python3 -c "
from forecast_evaluate import run_rain_eva_all_grades

results = run_rain_eva_all_grades('time_session')
for key, value in results.items():
    print(f'{key}: {value.get(\"element\", \"N/A\")}')
"
```

## 示例6：加载已保存的检验数据

```bash
python3 -c "
from forecast_evaluate.scripts.data_loader import ForecastDataLoader

loader = ForecastDataLoader('/Users/merlinq/Workspace/download/')

# 按路径加载
data = loader.load_by_path('t2m', 'daily', 'default', '202604')
print('Element:', data.get('element'))
print('Test type:', data.get('test_type'))
"
```

## 示例7：生成目录结构报告

```bash
python scripts/organize_files.py --report
```

## 典型场景

### 场景1：查询降水逐时效检验

```
用户: "帮我查询2026年4月的降水逐时效检验结果"

执行:
  1. create_rain_test_json('time_session', beginTime='2026-04-01 00:00:00', endTime='2026-04-30 23:59:59')
  2. request_scores(json_data)
  3. 返回图表和分析结果
```

### 场景2：温度地区对比检验

```
用户: "比较不同地区的温度预报准确性"

执行:
  1. create_temp_test_json('area', elementCode='t2m')
  2. request_scores(json_data)
  3. 返回地区对比图表
```

### 场景3：批量获取月度数据

```
用户: "下载2026年4月全部检验数据"

执行:
  1. download_all_forecast_data(year_month='202604')
  2. 返回下载结果统计
```
