# Task Plan: 预报网格数据汛期切换（滚动预报 vs EC）

**PLAN_ID:** 2026-07-17-forecast-grid-flood-season-switch  
**创建时间:** 2026-07-17  
**Goal:** 预报网格数据源按汛期自动切换——汛期（6/1-9/30）用滚动预报，平时用 EC（ECMWF AIFS）。

## Current Phase
Phase 1: 设计 + 实现（接口规格已到位，NetCDF 变量提取待内网样本补全）

## 背景

### 现状（代码核查确认）
- **现有滚动预报接口** `query_rolling_forecast`（`haihe-weather-analyzer-mcp/haihe_mcp_tools.py:2800`）
  - 范围：**仅天津 11 个区县**（天津市区/蓟州/宝坻/武清/宁河/静海/北辰/西青/津南/东丽/滨海新区）
  - 数据类型：**点-based**（站点天气要素：WEA/TMAX/TMIN/EDA/RHMAX/RHMIN/TP1H）
  - 用途：天津天气问答（`decision_weather`、weekly forecast fast path）
  - **结论：这不是项目经理所指的"预报网格数据"接口**
- **EC（ECMWF AIFS）预报**
  - 范围：**海河流域网格**（GRIB2/TIF）
  - 数据类型：**网格-based**（12/24/36/48/60/72h 累计降水）
  - 用途：应急响应产品、面雨量预报、降水产品图绘制（`draw_haihe_precip_product.py`、`RainfallAnalyzer.py`、`emergency_api.py`）
  - 文件路径：`/home/ev/data/ec/EC_AIFS/{年}/{YYYYMMDD}/*.grib2`

### 已澄清的需求
- 汛期判定：**6 月 1 日 - 9 月 30 日自动切换**（按当前日期判断，无需环境变量）
- 范围矛盾（滚动预报点 vs EC 网格）：**待用户与项目经理确认后回来**

### 待澄清（阻塞 NetCDF 变量提取）
- [ ] 样本 .nc 文件的变量名、维度（time/lat/lon/level?）、坐标系（EPSG?）、空间范围
- [ ] 文件名末尾 NNNN（1002/9062）的含义——批次号/集合成员/文件分片？多文件取哪个？
- [x] 接口路径模式（已确认）
- [x] 文件名规范（已确认）
- [x] 更新频率 08:00/20:00（已确认）
- [x] 数据格式 NetCDF（已确认）
- [ ] 用户与项目经理确认范围矛盾处理方式（滚动预报覆盖海河还是仅天津？文件名 LYPUB 待解读）

## Phases

### Phase 0: 需求澄清（当前）
- [x] 核查现有滚动预报接口范围（确认仅天津 11 点）
- [x] 确认汛期判定规则（6/1-9/30 自动）
- [ ] 获取项目经理所指的"滚动预报网格接口"规格文档
- [ ] 确认切换层级和数据适配方案
- [ ] 用户与项目经理确认范围矛盾处理方式
- **Status:** in_progress（阻塞，等待用户回复）

### Phase 1: 设计
- [ ] 根据接口规格设计切换层（数据源选择器 + 适配层）
- [ ] 设计汛期判定工具函数（可单测、不依赖系统时间）
- [ ] 设计配置项（哪些产品/工具受切换影响）
- **Status:** pending

### Phase 2: 实现
- [ ] 汛期判定函数 + 单测
- [ ] 数据源选择器（滚动预报 vs EC）
- [ ] 适配层（若接口要素/格式不一致）
- [ ] 在受影响的产品/工具中接入切换
- **Status:** pending

### Phase 3: 测试
- [ ] 汛期内/外切换的单测
- [ ] 既有 EC 路径回归测试
- [ ] 滚动预报路径集成测试
- **Status:** pending

### Phase 4: code-review + simplifier + verification
- **Status:** pending

### Phase 5: 文档与记忆
- [ ] 更新 CLAUDE.md
- [ ] 更新滚动预报接口文档
- [ ] 记录决策到 auto-memory
- **Status:** pending

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| 汛期 6/1-9/30 自动切换 | 用户确认，海河流域惯例 |
| 现有天津滚动预报接口不是目标接口 | 仅 11 点，非网格，范围不匹配 |
| 阻塞实现直到接口规格明确 | 无接口规格无法设计适配层 |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| 无 | - | - |
