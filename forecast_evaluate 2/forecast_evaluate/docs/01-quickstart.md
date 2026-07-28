# 快速开始

## 环境要求

**Python环境**: `/opt/miniconda3/envs/langchain/bin/python`

## 项目结构

```
scripts/
├── forecast_evaluate.py  # 主模块
├── config.py             # 配置参数
├── utils.py              # 工具函数
├── batch_download.py     # 批量下载脚本
├── data_loader.py        # 数据加载器
├── analyzer.py           # 数据分析器
└── organize_files.py     # 目录管理工具
```

## 数据目录结构

```
{BASE_SAVE_DIR}/
├── {element}/                    # 要素代码 (t2m, rain24等)
│   ├── {test_type}/              # 检验方式 (daily, time_session, area)
│   │   ├── {subtype}/            # 分组类型 (default, ng, g0.1, acc10等)
│   │   │   ├── {YYYYMM}.json     # 数据文件
│   │   │   └── {YYYYMM}.png      # 图表文件
```

## 最简单的使用方式

### 1. 运行降水检验

```python
from forecast_evaluate import run_rain_eva

result = run_rain_eva(
    'time_session',           # 检验方式
    rain_type='ng',           # 晴雨不分级
    year_month='202604',      # 年月（用于保存路径）
    beginTime='2026-04-01 00:00:00',
    endTime='2026-04-30 23:59:59'
)
```

### 2. 运行温度检验

```python
from forecast_evaluate import run_temp_eva

result = run_temp_eva(
    'time_session',           # 检验方式
    year_month='202604',
    elementCode='t2m',        # 2米温度
    algorithmName='PC:2,MAE'
)
```

### 3. 批量下载全部数据

```bash
python scripts/batch_download.py --type all --year-month 202604
```

## 检验类型说明

| test_type | 说明 | 内部配置 |
|:---------:|------|----------|
| `daily` | 逐日检验 | columnType="daily", scoreType="daily" |
| `time_session` | 逐时效检验 | columnType="timeSession", scoreType="composite" |
| `area` | 地区对比检验 | columnType="area", statisTypes="county" |

## 下一步

- [核心函数详解](./02-core-functions.md)
- [批量下载](./03-batch-download.md)
- [代码对照表](./05-code-tables.md)
