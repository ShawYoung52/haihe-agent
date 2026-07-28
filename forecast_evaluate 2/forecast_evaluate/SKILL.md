---
name: forecast_evaluate
description: |
  气象预报检验技能，支持降水和温度检验的查询与分析。触发词：降水检验，温度检验、预报评分、逐日检验、逐时效检验、地区对比检验、预报准确性分析、TS评分、PC评分、BIAS、MAE，月报、综述生成、生成报告。 当用户需要查询降水或温度预报检验结果、比较不同预报产品表现，分析预报准确性、生成月度预报质量报告时使用此skill。
triggers:
  - 降水检验
  - 温度检验
  - 预报评分
  - 逐日检验
  - 逐时效检验
  - 地区对比检验
  - 预报准确性分析
  - TS评分
  - PC评分
  - BIAS
  - MAE
  - 月报
  - 综述生成
  - 生成报告
---

# 预报检验技能

本技能提供降水和温度预报检验能力，支持逐时效、逐日及地区对比检验分析。

**Python环境**: `/opt/miniconda3/envs/langchain/bin/python`

## 完整流程概览

```
用户请求
    │
    ├─▶ 步骤一：下载数据
    │     └─▶ batch_download.py
    │
    ├─▶ 步骤二：生成报告
    │     └─▶ analyzer.py（获取报告保存路径）
    │
    ├─▶ 步骤三：读取综述
    │     └─▶ 从 md 文件 ## 综述 部分提取 summary 字段
    │
    ├─▶ 步骤四：分析数据
    │     └─▶ 按 SKILL_ANA.md 要求生成分析报告
    │
    ├─▶ 步骤五：更新报告文件
    │     └─▶ 替换原 md 文件的 ## 综述 部分
    │
    └─▶ 输出
          ├─Obsidian 链接
          └─分析报告
```

## 步骤一：下载检验数据

```bash
cd /Users/merlinq/.workbuddy/skills/forecast_evaluate/scripts

# 下载全部数据
python batch_download.py --type all

# 仅下载温度
python batch_download.py --type temp

# 仅下载降水
python batch_download.py --type rain

# 下载单个数据集
python batch_download.py --type single --element tmax24 --test-type daily
python batch_download.py --type single --element rain24 --test-type daily --rain-type g

# 下载指定预报时效（仅对 daily 和 area 生效，支持24、48、72...240小时时效）
python batch_download.py --type single --element tmax24 --test-type daily --time-session 24 # 24时效
python batch_download.py --type single --element tmax24 --test-type daily --time-session 48 # 48时效
python batch_download.py --type single --element tmax24 --test-type daily --time-session 72 # 72时效  

# 指定起报时间（08为早08点，20为晚20点，08,20为默认早晚两次）
python batch_download.py --type single --element tmax24 --test-type daily --predict-hours 08  # 仅早08点起报
python batch_download.py --type single --element tmax24 --test-type daily --predict-hours 20  # 仅晚20点起报
python batch_download.py --type single --element tmax24 --test-type daily --predict-hours 08,20  # 早晚两次（默认）

# 指定时间范围
python batch_download.py --begin-time 20260401 --end-time 20260430
```

## 步骤二：生成检验报告

```bash
cd /Users/merlinq/.workbuddy/skills/forecast_evaluate/scripts

# 生成温度检验报告（最高温/逐日检验）
python analyzer.py --element tmax24 --test-type daily --month 202604

# 生成温度检验报告（最低温/分地区检验）
python analyzer.py --element tmin24 --test-type area --month 202604

# 生成温度检验报告（2m温度/逐时效检验）
python analyzer.py --element t2m --test-type time_session --month 202604

# 生成降水检验报告（晴雨不分级/逐日检验）
python analyzer.py --element rain24 --test-type daily --rain-type ng --month 202604

# 生成降水检验报告（分级降水/分地区检验）
python analyzer.py --element rain24 --test-type area --rain-type g --month 202604

# 生成降水检验报告（累计降水/逐时效检验）
python analyzer.py --element rain24 --test-type time_session --rain-type acc --month 202604

# 指定起报时间（08为早08点起报，20为晚20点起报，08,20为默认早晚两次）
python analyzer.py --element tmax24 --test-type daily --month 202604 --predict-hours 08   # 仅早08点起报
python analyzer.py --element tmax24 --test-type daily --month 202604 --predict-hours 20   # 仅晚20点起报
python analyzer.py --element tmax24 --test-type daily --month 202604 --predict-hours 08,20 # 早晚两次（默认）

# 查看帮助
python analyzer.py --help
```

执行 `analyzer.py` 后，stdout 会输出以下内容：

1. **报告保存路径**：
   ```
   已保存到: /Users/merlinq/Documents/Obsidian-Vault/检验报告/24小时最高温度_分地区_202604.md
   ```

2. **JSON 数据**（包含 summary 字段）：
   ```json
   {
     "details": {},
     "summary": "20260401 至 20260430\n24小时最高温度：\n平均绝对误差表现为..."
   }
   ```

## 步骤三：读取综述

从步骤二获取的报告保存路径，读取 md 文件中的 `## 综述` 部分，提取 `summary` 字段内容。

## 步骤四：分析数据

将步骤三读取的 `summary` 字段按 `SKILL_ANA.md` 的要求进行分析，生成结构化的分析报告。

## 步骤五：更新检验报告文件

1. **获取文件路径**：从步骤二 stdout 解析报告保存路径
2. **读取原文件**：读取完整的 md 文件内容
3. **替换综述**：将 `## 综述` 部分替换为步骤四生成的分析报告
4. **写回文件**：保持文件其余部分（详细结果、图片等）不变

## 输出格式

智能体最终只能输出以下两项内容，**不得包含其他任何内容**：

1. **Obsidian 链接** - 报告的保存位置
2. **分析报告** - 按 SKILL_ANA.md 要求生成的分析报告

### 输出示例

```markdown
**检验评估报告**

**报告正文**
### 总体结论
2026年4月，**天津预报**24小时最高温度在2°C准确率（84.09%）和MAE（1.18）上均优于**国家指导**（79.17%，1.30）和**ECMWF**（76.14%，1.33），整体表现最好。主要不足在于**蓟州区、滨海新区、宝坻区**的准确率偏低（66.67%~79.17%）。

### 分段分析

**准确率**（**天津预报**整体值84.09%，低于80%的区域）

| 区域 | 蓟州区 | 滨海新区 | 宝坻区 |
|------|--------|----------|--------|
| 准确率(%) | 66.67 | 70.83 | 79.17 |

**MAE**（**天津预报**整体值1.18，高于1.5°C的区域）

无

**ME**（**天津预报**整体值+0.26，绝对值≥1.0°C的区域）

无

### 重点定位

**天津预报**在所有区域的平均值：准确率84.09%，MAE 1.18，ME +0.26。

根据较差定义标准，表现相对较弱的区域（满足任一条件）有：**蓟州区、滨海新区、宝坻区**。按严重程度排序，逐一对比其他预报：

- **蓟州区**（准确率66.67%）：**天津预报**准确率66.67%，**国家指导**79.17%，**ECMWF**50.0%。**天津预报**准确率低于**国家指导**，但优于**ECMWF**。
- **滨海新区**（准确率70.83%）：**天津预报**准确率70.83%，**国家指导**70.83%，**ECMWF**29.17%。**天津预报**与**国家指导**持平，明显优于**ECMWF**。
- **宝坻区**（准确率79.17%）：**天津预报**准确率79.17%，**国家指导**83.33%，**ECMWF**79.17%。**天津预报**准确率略低于**国家指导**，与**ECMWF**持平。

**关键产物**：
- ![检验报告](/Users/merlinq/Documents/Obsidian-Vault/检验报告/24小时最高温度_分地区_202604.md)
```

> **说明**：以上示例为 `area` 维度分析。若输入数据对应 `daily` 或 `time_session` 维度，请根据 `SKILL_ANA.md` 中对应的分析要求调整输出格式。

## 报告存放位置

报告自动保存到 Obsidian vault 目录：`/Users/merlinq/Documents/Obsidian-Vault/检验报告`

文件名格式：`{element}_{rain_type}_{test_type}_{yearmonth}.md`

例如：
- `24小时最高温度_逐日_202604.md`
- `24小时降水_NG_逐日_202604.md`
- `24小时降水_G_分地区_202604.md`

## 数据存放位置

JSON 数据默认保存到：`/Users/merlinq/Workspace/download/JSON`

目录结构：`{element}/{subtype}/{test_type}/{YYYYMM}.json`

当指定非默认起报时间时（如 `--predict-hours 08` 或 `--predict-hours 20`），目录结构为：`{element}/{subtype}/{test_type}/{predict_hours}/{YYYYMM}.json`

例如：
- 默认（08,20）：`tmax24/default/daily/202604.json`
- 仅早08点起报：`tmax24/default/daily/08/202604.json`
- 仅晚20点起报：`tmax24/default/daily/20/202604.json`

## 强制要求

- 必须使用 `batch_download.py` 下载数据。
- 必须使用 `analyzer.py` 读取数据并生成报告。
- 必须根据 `SKILL_ANA.md` 要求进行分析，切勿修改输出格式。

## 代码说明

### 核心模块

| 模块 | 功能 |
|------|------|
| `analyzer.py` | **报告生成** - 解析JSON、生成Markdown，保存Obsidian |
| `batch_download.py` | **批量下载** - 从API下载检验数据 |
| `config.py` | **配置** - 路径、要素、检验类型映射 |

### 要素代码

| 代码 | 名称 |
|------|------|
| **温度要素** |
| `t2m` | 2米温度 |
| `tmax24` | 24小时最高温度 |
| `tmin24` | 24小时最低温度 |
| **降水要素** |
| `rain` | 小时降水 |
| `rain3` | 3小时降水 |
| `rain12` | 12小时降水 |
| `rain24` | 24小时降水 |

### 检验类型

| 代码 | 名称 |
|------|------|
| `daily` | 逐日检验 |
| `time_session` | 逐时效检验 |
| `area` | 分地区检验 |

### 降水检验类型

| 代码 | 名称 |
|------|------|
| `ng` | 晴雨不分级 |
| `g` | 分级降水 |
| `acc` | 累计降水 |

## 命令行参数

### batch_download.py

| 参数 | 说明 | 可选值 | 默认值 |
|------|------|--------|--------|
| `--type` | 下载类型 | `all`, `temp`, `rain`, `single` | `all` |
| `--element` | 要素代码 | `t2m`, `tmax24`, `tmin24`, `rain`, `rain3`, `rain12`, `rain24` | - |
| `--test-type` | 检验类型 | `daily`, `time_session`, `area` | - |
| `--rain-type` | 降水检验类型 | `ng`, `g`, `acc` | `ng` |
| `--time-session` | 预报时效 | `24`, `48`, `72` 等 | `24` (仅对 `daily` 和 `area` 生效) |
| `--predict-hours` | 起报时间 | `08`, `20`, `08,20` | `08,20` |
| `--begin-time` | 开始时间 (YYYYMMDD) | `20260401` | - |
| `--end-time` | 结束时间 (YYYYMMDD) | `20260430` | - |
| `--save-dir` | 保存目录 | 任意目录 | `/Users/merlinq/Workspace/download/JSON` |

### analyzer.py

| 参数 | 说明 | 可选值 | 默认值 |
|------|------|--------|--------|
| `--path` | JSON文件目录 | 任意目录 | `/Users/merlinq/Workspace/download/JSON` |
| `--element` | 要素代码 | `t2m`, `tmax24`, `tmin24`, `rain`, `rain3`, `rain12`, `rain24` | `tmax24` |
| `--test-type` | 检验类型 | `daily`, `time_session`, `area` | `daily` |
| `--rain-type` | 降水检验类型 | `ng`, `g`, `acc` | `ng` |
| `--month` | 年月 | `202604` | 当前年月 |
| `--predict-hours` | 起报时间 | `08`, `20`, `08,20` | `08,20` |
| `--list` | 列出可用选项 | - | - |