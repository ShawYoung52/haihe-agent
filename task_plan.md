# 任务计划：暴雨影响河流 — 传播时间返回 + 牵引应急响应 HHLY 改造

## 目标
牵引智能体"暴雨影响河流"模块返回结构新增 `river_propagation` 河流级传播时间汇总字段（已完成、已推送）；
牵引智能体应急响应改用 HHLY 数据源 + 2 位国家站口径 11/12/13/16（进行中）。
- 口径：河流级汇总的预计影响时长（用户确认）
- 流速：统一经验流速常量，默认 2.0 m/s，可配置（用户确认）
- 位置：新增独立汇总字段，`affected_rivers` 不变，向后兼容（用户确认）

依据文档：
- 设计：`haiheliuyubaoyuagent-master/docs/superpowers/specs/2026-07-23-rainstorm-river-propagation-time-design.md`
- 实施计划：`haiheliuyubaoyuagent-master/docs/superpowers/plans/2026-07-23-rainstorm-river-propagation-time.md`

## 阶段

### 阶段 0：需求与设计（已完成）
- [x] superpowers:brainstorming 需求澄清（3 个决策点均经用户确认）
- [x] 方案 A 获用户批准（核心算法层计算 + 三层透传）
- [x] 设计文档写入并提交 git（commit 064c17d）
- [x] superpowers:writing-plans 制定 5 任务实施计划并提交（commit 5a478d0）

### 阶段 1：核心算法（牵引智能体 hhlyqyxt-master）
- [x] `rainfall_impact_geojson.py` 新增 `_build_river_propagation` + `flow_velocity_mps` 参数链路
- [x] `utils/tests/test_rainfall_impact_geojson.py` 新增传播时间测试
- [x] pytest 通过后提交（c7e7452）

### 阶段 2：MCP 适配层（haihe-weather-analyzer-mcp）
- [x] `fixed_rainfall_impact_tool.py` 透传 + `_resolve_flow_velocity` + IMPACT_RULES
- [x] `server.py` 工具描述同步
- [x] 新建 `test_fixed_rainfall_impact_propagation.py`，pytest 通过后提交（ed676ee）

### 阶段 3：问答侧本地工具（chainlitexam）
- [x] `tools/rainfall_river_impact.py` 新增 `flow_velocity_mps` 透传参数
- [x] 扩展 `tests/test_rainfall_river_impact.py`，pytest 通过后提交（99ebec6）

### 阶段 4：问答层简报与提示词（chainlitexam）
- [x] `message_orchestrator._build_affected_river_network_brief` 追加传播时间说明行
- [x] `prompts.py` 规则 2.5 补充传播时间表述要求
- [x] `tests/test_message_orchestrator.py` 新增测试 + fast_paths 静态检查，通过后提交（09b6755）

### 阶段 5：全链路回归 + 质量流程
- [x] 三个模块测试套件全部通过（41 + 6 + 69，1 个既有失败与本改动无关）
- [x] code-review 双代理审查（合规 + 正确性）并修复（61c519a：河名口径/NaN 防护/brief 措辞）
- [x] code-simplifier 简化检查并应用（4e6f2d1：合并聚合循环、_empty_propagation、测试去重）
- [x] superpowers:verification-before-completion（全新重跑验证通过）
- [x] claude-md-management:revise-claude-md 更新 CLAUDE.md（ebc1f03）
- [x] claude-mem 写入记忆（文件记忆 2 条；服务端 observation_add 需 server runtime，worker 模式不可用）
- [x] git push（dd1f4b1..ebc1f03 → origin/main）

---

## 任务 B：牵引应急响应 HHLY 改造（进行中）

依据：`docs/superpowers/specs/2026-07-23-traction-emergency-hhly-source-design.md`（commit 52ca8e5）
计划：`docs/superpowers/plans/2026-07-23-traction-emergency-hhly-source.md`（commit 1fc96bd）

强约束：代码写进 `hhlyqyxt-master/ScheduledTask/emergency_response_monitor.py` 自己，复用牵引侧 `utils.MusicTool`，不得跨仓库 import 问答侧模块。

### B1：国家站口径改 2 位 11/12/13/16（去前导零）
- [x] `NATIONAL_STATION_LEVELS` + `_normalize_station_level`（commit 9f4773a，18 passed）

### B2：新增 HHLY 拉取函数 `_fetch_hhly_rainfall_for_emergency`
- [x] 复用 MusicClient，basin_codes=HHLY、data_code=SURF_CHN_MUL_MIN（commit fa48740，22 passed）

### B3：`compute_emergency_response_stats` 扩容接受 DataFrame
- [x] CSV 路径 / DataFrame 双入口（commit a862634，24 passed；偏离计划：去掉 `if national_df.empty: return None` 与 `dropna(Datetime)` 早返回，保留旧 total=0 语义以不破坏 `test_missing_station_levl_column_treated_as_non_national`）

### B4：`run_emergency_response_monitor` 扩容支持 timerange 新链路
- [x] timerange 优先 + 旧 CSV 向下兼容 + 都不传 ValueError（commit b98247b，27 passed；验证 stationProcessMin.py:444 旧调用不传 timerange 走 CSV 链路）

### B5：全链路回归 + 质量流程
- [x] Step 1-2：牵引仓库回归 68 passed（emergency 27 + rainfall_impact 41），无回归
- [x] Step 3：code-review（双代理 CLAUDE.md 合规 + 正确性）- 合规代理 5 项硬约束全 PASS；正确性代理因 volcengine API 订阅错误中断，已自补正确性自审（fetch/compute/run/fixture 均通过，NaT-datatime 为既有非回归）
- [x] Step 4：code-simplifier - hoist HHLY_MIN_COLUMNS 模块常量（commit 42023e9，68 passed）
- [x] Step 5：verification-before-completion - 简化后重跑 68 passed
- [x] Step 6：revise-claude-md - haiheliuyubaoyuagent-master/CLAUDE.md 新增 emergency_response_monitor HHLY 条目
- [x] Step 7：claude-mem - 写入 traction-emergency-hhly-source.md 记忆 + MEMORY.md 索引
- [ ] Step 8：git push

## 计划修正记录
| 修正 | 原因 | 解决 |
|------|------|------|
| B2 `_fake_records` 测试 fixture | 计划原默认元组长度 1，`zip` 截断只产 1 行，与 `len(df)==3` 断言冲突 | 改为长度 3 的默认元组 |
| B3 `compute` 重构早返回 | 计划原版加 `if national_df.empty: return None` + `dropna(subset=["Datetime"])` 会破坏既有 `test_missing_station_levl_column_treated_as_non_national`（期望 total=0 的非 None 结果） | 保留旧语义：不早返回，national_df 空时继续算出 total=0 结果字典 |
| B2 `pd.DataFrame(records, columns=)` | dict records + 显式 columns 会导致 pandas 位置映射，塌缩成 1 行 | 非空路径不传 columns 用 dict 键推断，再补齐缺失列 |

## 关键决策
| 决策 | 结论 | 来源 |
|------|------|------|
| 传播时间口径 | 河流级汇总预计影响时长 | 用户 2026-07-23 确认 |
| 流速来源 | 统一经验流速 2.0 m/s，可配置 | 用户 2026-07-23 确认 |
| 返回位置 | 新增 `river_propagation` 独立字段 | 用户 2026-07-23 确认 |
| 计算层级 | 牵引智能体核心算法层（方案 A） | 用户 2026-07-23 确认 |
| 执行方式 | inline executing-plans（用户授权全流程） | 用户 2026-07-23 确认 |

## 遇到的错误
| 错误 | 尝试次数 | 解决方案 |
|------|---------|---------|
| 暂无 | - | - |
