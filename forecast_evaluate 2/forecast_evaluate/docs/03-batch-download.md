# 批量下载

## Python API 方式

### 下载全部检验数据

下载温度（9个文件）+ 降水（132个文件），共141个文件。

```python
from forecast_evaluate.scripts.batch_download import download_all_forecast_data

result = download_all_forecast_data(
    save_dir='/Users/merlinq/Workspace/download/',
    year_month='202604'       # 可选，默认当前年月
)

print(f"下载完成: {result['total_files']} 个文件")
print(f"成功: {result['success']}, 失败: {result['failed']}")
```

### 仅下载温度检验数据

下载3要素 × 3方式 = 9个文件。

```python
from forecast_evaluate.scripts.batch_download import download_temp_only

result = download_temp_only(
    save_dir='/Users/merlinq/Workspace/download/',
    year_month='202604'
)
```

### 仅下载降水检验数据

下载4要素 × 3方式 × 11类型 = 132个文件。

```python
from forecast_evaluate.scripts.batch_download import download_rain_only

result = download_rain_only(
    save_dir='/Users/merlinq/Workspace/download/',
    year_month='202604'
)
```

### 下载指定要素

```python
from forecast_evaluate.scripts.batch_download import download_by_element

# 下载24小时降水全部检验数据
result = download_by_element('rain24', year_month='202604')

# 下载2米温度全部检验数据
result = download_by_element('t2m', year_month='202604')
```

## 命令行方式

```bash
# 下载全部数据
python scripts/batch_download.py --type all

# 仅下载温度
python scripts/batch_download.py --type temp

# 仅下载降水
python scripts/batch_download.py --type rain

# 下载指定要素
python scripts/batch_download.py --element rain24

# 指定年月
python scripts/batch_download.py --type all --year-month 202604

# 指定保存目录
python scripts/batch_download.py --type all --save-dir /path/to/save
```

## 下载要素清单

### 温度要素（3个）

| 要素代码 | 说明 |
|----------|------|
| t2m | 2米温度 |
| tmax24 | 24小时最高温度 |
| tmin24 | 24小时最低温度 |

### 降水要素（4个）

| 要素代码 | 说明 |
|----------|------|
| rain | 小时降水 |
| rain3 | 3小时累积降水 |
| rain12 | 12小时累积降水 |
| rain24 | 24小时累积降水 |

### 检验方式（3种）

| 方式 | 说明 |
|------|------|
| daily | 逐日检验 |
| time_session | 逐时效检验 |
| area | 地区对比检验 |

### 降水检验类型（11种）

| 类型 | 说明 | 阈值 |
|------|------|------|
| ng | 晴雨不分级 | - |
| g0.1 | 分级检验-小雨 | 0.1mm |
| g10.0 | 分级检验-中雨 | 10mm |
| g25.0 | 分级检验-大雨 | 25mm |
| g50.0 | 分级检验-暴雨 | 50mm |
| g100.0 | 分级检验-大暴雨 | 100mm |
| acc0.1 | 累计检验-小雨 | 0.1mm |
| acc10.0 | 累计检验-中雨 | 10mm |
| acc25.0 | 累计检验-大雨 | 25mm |
| acc50.0 | 累计检验-暴雨 | 50mm |
| acc100.0 | 累计检验-大暴雨 | 100mm |

## 文件数量计算

- 温度：3要素 × 3方式 = 9个文件
- 降水：4要素 × 3方式 × 11类型 = 132个文件
- **总计：141个文件**
