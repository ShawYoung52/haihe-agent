# Progress: 预报网格数据汛期切换

## 2026-07-17

### 已完成
- 核查现有滚动预报接口 `query_rolling_forecast`：确认仅天津 11 区县、点-based、非网格
- 核查 EC（ECMWF AIFS）预报：海河流域网格、GRIB2/TIF、用于应急/面雨量/产品图
- 与用户澄清：
  - 汛期判定：6/1-9/30 自动切换 ✓
  - 现有天津滚动预报接口不是项目经理所指的接口 ✓
  - 范围矛盾待用户与项目经理确认 ✓
- 创建 planning 文件：task_plan.md、findings.md、progress.md

### 待完成（阻塞）
- 等待用户提供"滚动预报网格接口"规格文档
- 等待用户与项目经理确认范围矛盾处理方式
- 接口到位后进入 Phase 1 设计

### 验证结果
| 检查项 | 状态 |
|--------|------|
| 现有接口范围核查 | ✓ |
| 汛期规则确认 | ✓ |
| planning 文件创建 | ✓ |
| 接口规格获取 | ⏳ 阻塞 |
| 范围矛盾澄清 | ⏳ 阻塞 |

## 2026-07-17 接口规格到位 + Phase 1-2 实现

### 接口规格（用户提供）
- 数据湖路径：`/CMADAAS/DATA/SEVP/BETJ/USR_QXT_YTH/M.3200.0006.M001/TP1H/000/{YYYYMM}/{YYYYMMDD}/{YYYYMMDDHH}/`
- 文件名：`GRID_TJQX_LYPUB_TP1H_AEHH_000_DT_{YYYYMMDDHHMMSS}_000-240_{NNNN}.nc`
- 格式：NetCDF（.nc），变量 TP1H（1 小时累计降水），时效 000-240（0-240h）
- 更新频率：每天 08:00 / 20:00
- 外网开发环境无法访问数据湖（内网离线服务器挂载）

### 实现
新建模块 `haihe-weather-analyzer-mcp/rolling_forecast_grid.py`：
- `is_flood_season(now)` — 6/1-9/30 汛期判定（month in {6,7,8,9}）
- `select_latest_forecast_cycle(now)` — 选最近 08/20 起报时次
- `find_rolling_forecast_grid_file(root, cycle, max_fallback)` — 按路径模式发现 .nc 文件，多文件取 NNNN 最大，时次回溯
- `inspect_rolling_forecast_grid(path)` — 打开 NetCDF 返回元信息（供内网样本补全变量提取）
- `resolve_forecast_grid_source(now, ec_output_path, rolling_root)` — 汛期切换器：汛期+有文件→滚动预报；汛期+无文件→降级 EC；非汛期→EC

### code-review 发现并修复
- **major**：`resolve_forecast_grid_source` 中 `is_flood_season(now)` 在 `moment` 捕获前调 `datetime.now()`，跨午夜/月末边界可能两次取到不同时刻 → 修正为先捕获 `moment` 再传给所有调用
- **minor**：`max_fallback=0` 被 `max(1, ...)` 静默改成 1 → 修正为 `<=0` 直接返回 None
- 新增 spy 测试验证 `datetime.now()` 至多调用一次

### 验证
- 28/28 单测（含汛期边界、时次选择、文件发现回溯、切换器、now 一致性）
- 32/32 rain impact 单测、19/19 fast-path、51/51 chainlitexam pytest 全通过（无回归）

### 待内网补全
- `inspect_rolling_forecast_grid` 的变量提取逻辑（需样本 .nc 文件确认变量名/维度/坐标系）
- 接入下游 EC 消费方（`draw_haihe_precip_product.py`、`RainfallAnalyzer.py`、`emergency_api.py`）——需先确认 .nc 结构
