# Task Plan: 流域天气回答两个 Bug 修复

**PLAN_ID:** 2026-07-22-basin-weather-bugfix
**创建时间:** 2026-07-22
**Goal:** 修复 (1) 流域天气回答开头错误出现“已绘制河网可视化并叠加行政区划底图”前缀；(2) 今天/明天返回相同数据（start_time 未生效 + TP1H 单时次未累计）。

## 根因（已定位）

### Bug 1: 错误的开头前缀
- `chainlitexam/message_orchestrator.py:1661` `_build_thinking_summary` 关键词 `["河网","水系","河流","暴雨影响","河系"]` 中，`"河流"` 是 `"海河流域"`/`"子牙河流域"` 的子串（海**河流**域），流域天气问题误命中河网可视化分支。
- 现有测试 `test_river_network_summary` 用 "海河流域河网水系情况" 期望河网前缀 —— 该用例靠"河网/水系"命中，不依赖"河流"，修复后仍应通过。

### Bug 2: 今天/明天数据相同
- `analyzers/RainfallAnalyzer.resolve_forecast_raster_path` 滚动预报分支：只用最新可用 cycle，`materialize_rolling_forecast_to_files(nc_path, [forecast_hours])` 取单时次切片，完全忽略 `start_time` → 今天/明天同一 cycle 同一时效，数据必然相同。
- TP1H 是 1 小时降水量；"24 小时雨量"应对窗口内各时次求和（EC 的 `rain_total_24h.tif` 就是累计量），单时次切片口径不一致且系统性偏小。
- 城市工具 `get_city_rainfall_time_range` 走同一共享函数，存在同样隐患，共享修复可一并解决。

## Phases

### Phase 1: 根因定位（完成）
- [x] 定位前缀来源：`_build_thinking_summary` 关键词误命中
- [x] 定位数据相同原因：resolve 忽略 start_time + 单时次切片

### Phase 2: TDD 修复
- [ ] 失败测试 1：`_build_thinking_summary("今天海河流域天气怎么样")` 不得返回河网前缀；"明天海河流域天气怎么样" 返回预报前缀
- [ ] 失败测试 2：lead 小时偏移计算（cycle 08:00，明天 00:00 + 24h → (16, 40)）
- [ ] 失败测试 3：滚动预报窗口累计（mock/monkeypatch 验证 resolve 传入累计函数的正确 hour 区间）
- [ ] 修复 1：`河流` 关键词改为负向断言（不匹配"河流域"）；预报分支补"天气"
- [ ] 修复 2：`rolling_forecast_grid` 新增累计物化函数 + lead 偏移；`resolve_forecast_raster_path` 使用窗口累计

### Phase 3: 验证
- [ ] MCP 全量 pytest 通过
- [ ] Chainlit 全量 pytest 通过（含 test_thinking_summary 既有用例不回归）

### Phase 4: code-review + code-simplifier
- [ ] 审查 diff
- [ ] 简化

### Phase 5: 收尾
- [ ] CLAUDE.md / claude-mem 更新
- [ ] 提交

## Decisions

| Decision | Rationale |
|----------|-----------|
| `河流` 关键词用 `河流(?!域)` 负向断言，而非 `"流域" not in q` 全局排除 | "海河流域河网水系情况" 是合法的河网问题，不能因含"流域"而失效 |
| 修复放在共享 `resolve_forecast_raster_path` | 城市工具与河系工具同一隐患，深层修复一次解决 |
| 滚动预报窗口累计对齐 EC `rain_total_Nh` 语义 | 两数据源口径一致，避免系统性偏小 |
