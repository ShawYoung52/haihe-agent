# 目录管理

## 功能概述

目录管理工具提供以下功能：
- 验证目录结构是否正确
- 生成目录结构报告
- 清理空目录

## 验证目录结构

检查数据目录是否符合规范的结构。

```python
from forecast_evaluate.scripts.organize_files import verify_directory_structure

# 验证目录结构
result = verify_directory_structure('/Users/merlinq/Workspace/download/')

# 返回布尔值，表示是否验证通过
print(f"验证结果: {'通过' if result else '未通过'}")
```

## 生成结构报告

生成详细的目录结构统计报告。

```python
from forecast_evaluate.scripts.organize_files import generate_structure_report

# 生成目录结构报告
report = generate_structure_report('/Users/merlinq/Workspace/download/')
print(report)
```

报告内容包括：
- 各要素的文件总数
- 检验方式分布
- 分组类型分布
- 时间范围

## 清理空目录

删除目录结构中的空文件夹。

```python
from forecast_evaluate.scripts.organize_files import clean_empty_directories

# 试运行（不实际删除）
clean_empty_directories('/Users/merlinq/Workspace/download/', dry_run=True)

# 实际清理
clean_empty_directories('/Users/merlinq/Workspace/download/', dry_run=False)
```

## 命令行使用

```bash
# 验证目录结构
python scripts/organize_files.py --verify

# 生成结构报告
python scripts/organize_files.py --report

# 清理空目录
python scripts/organize_files.py --clean

# 清理空目录（试运行）
python scripts/organize_files.py --clean --dry-run
```

## 报告示例

```
============================================================
预报检验数据目录结构报告
============================================================
生成时间: 2026-04-24 09:07:26
基础目录: /Users/merlinq/Workspace/download

目录结构: {element}/{test_type}/{subtype}/{YYYYMM}.json
------------------------------------------------------------

【rain】
  文件总数: 33
  检验方式: area, daily, time_session
  分组类型: acc0.1, acc10.0, acc100.0, acc25.0, acc50.0, g0.1, g10.0, g100.0, g25.0, g50.0, ng
  时间范围: 202604
  各检验方式文件数:
    - area: 11
    - daily: 11
    - time_session: 11

【t2m】
  文件总数: 3
  检验方式: area, daily, time_session
  分组类型: default
  时间范围: 202604

============================================================
报告结束
============================================================
```
