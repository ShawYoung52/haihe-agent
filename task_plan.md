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

## 任务 C：牵引智能体全量代码审查（进行中）

用户 2026-07-23 指令：遍历项目代码做全流程审查。本期范围 = `hhlyqyxt-master`（牵引智能体），四维度全审（正确性/业务口径/代码质量/部署合规）；另两个仓库（问答智能体、Chainlit 编排）后续排期。流程：superpowers 方法论（理解->规划->执行->验证）+ code-review（并行代理）+ code-simplifier + verification + revise-claude-md + claude-mem + github。

### 审查范围（生产代码 ~6500 行，按风险/规模排序）
- 高优先（未近期深审）：`utils/MusicTool.py`(1233)、`ScheduledTask/stationProcessMin.py`(908)、`utils/rainstorm_impact_map_service.py`(610)、`main.py`(515)、`Service/monitorservice.py`(493)、`ScheduledTask/stationProcess.py`(428)、`utils/send_wx_message.py`(289)、`utils/river_city_impact_tool.py`(260)、`utils/es_preprocess.py`(249)、`Controller/tool_router.py`(249)
- 低优先（近期已深审，轻审）：`utils/rainfall_impact_geojson.py`(1386, Request 1)、`ScheduledTask/emergency_response_monitor.py`(281, 本次 HHLY 改造)
- ORM/小文件：`Models/*`、`utils/db.py`、`utils/config.py`、`Service/reportservice.py`、`Schemas/*`

### 四维度
- D1 正确性与潜在 bug（边界、异常、数据口径、并发/调度）
- D2 业务逻辑与口径一致性（vs CLAUDE.md/specs/业务阈值，含应急响应、暴雨影响、面雨量、水库超限等）
- D3 代码质量与可简化点（去重、过度防御、YAGNI、死代码）
- D4 安全与部署合规（硬编码 IP/路径、env 覆盖、跨仓库 import、向后兼容、内网部署适配）

### 阶段
- [x] C1 理解：摸清 hhlyqyxt-master 结构与规模（34 py 文件，生产 ~6500 行）
- [x] C2 规划：本任务计划已写入 task_plan.md
- [x] C3 执行：4 维度审查（D1/D2 代理 + D3/D4 内联）-> findings 汇总入 hhlyqyxt-master/findings.md
- [x] C4 修复：只修我们的代码（TDD，6 提交）-- P0-2 HHLY 接入生产、P0-1 river_propagation 出口、P2-1 12h 占比分母、P2-3 时区、P2-9 坏 datetime、P1-9 下游命名
- [x] C5 code-simplifier：提取 _resolve_edge_row 共享 helper（GeoJSON + propagation 命名去重）
- [x] C6 verification：72 passed（emergency 30 + rainfall_impact 42），无回归
- [x] C7 revise-claude-md：CLAUDE.md 更新 emergency（HHLY 接入生产、12h 分母、时区、坏 datetime）+ rainfall_impact（propagation 命名修复、入口输出）
- [x] C8 claude-mem：更新 traction-emergency-hhly-source + 新增 traction-review-scope-rule
- [x] C9 github：提交并 push（00eff8e..e53e337）

## 任务 D：问答智能体 haihe-weather-analyzer-mcp 全量代码审查（进行中）

用户 2026-07-24 指令：牵引智能体审完后，接着审问答智能体。本期范围 = `haihe-weather-analyzer-mcp`（独立 git 仓库，74 py 文件/3 万+ 行），四维度全审。规模是牵引侧的 ~5 倍，聚焦最近改动和高风险文件。

### 审查重点文件（按风险/改动频率）
- 最近改动：`rolling_forecast_grid.py`(363)、`rolling_forecast_service.py`(808)、`server.py`(152)、`tools.py`(4592)、`vector_boundary_api.py`(302)
- CLAUDE.md 核心条目 + Request 1 相关：`fixed_rainfall_impact_tool.py`(330)、`haihe_mcp_tools.py`(3596)、`rainfall_ranking_service.py`(119)、`constants.py`(39)
- 应急响应链：`emergency_response_interface.py`(573)、`emergency_api.py`(571)、`emergency_http_server.py`(4547)、`emergency_event_store.py`(292)、`emergency_intranet_sync.py`(629)、`emergency_management_store.py`(1022)
- 绘图产品：`draw_haihe_precip_product.py`(1999)、`draw_river_network.py`(1696)、`forecast_product_queue.py`(420)、`observation_product_queue.py`(804)
- 分析器：`analyzers/RainfallAnalyzer.py`(523)
- 自定义工具：`custom_tools/*`

### 阶段
- [x] D1 理解：摸清 MCP 结构、规模、最近改动
- [x] D2 审查：综合代理（D1+D2+D3）+ 内联 D4 -> 15 项发现（3 P0 + 4 P1 + 8 P2）
- [x] D3 修复：只修明确 bug/泄漏——P0-1 删所有硬编码密码（6 文件）、P0-2 删僵尸文件（3 个含阈值 30.0 漂移的 haihe_music_api）、P1-5 修 tempfile 泄漏（output_dir 必传）
- [x] D4 simplifier+verify+docs+memory+push：4 修复 2 提交 1 push（gitee a347ca2）
- 其余 P0-3（应急判定三处重复）、P1~P2 均仅报告 findings.md，不修

## 任务 E：Chainlit 编排 chainlitexam 全量代码审查（进行中）

用户 2026-07-24 指令：验证完暴雨影响河流后审最后一个仓库 chainlitexam。43 py 文件、~15000 行、单文件最大 message_orchestrator.py 4241、chain_gzt.py 3670。测试基线 70 passed。

### 审查重点
- 主入口：chain_gzt.py（3670，Chainlit lifecycle + FastAPI + auth）、message_orchestrator.py（4241，消息路由+快路径+planner）
- 快路径：fast_paths/*（rainfall/water_level/rainstorm_impact_time/emergency_response/risk_warning/poi_weather）
- 工具：tools/*（decision_weather*/rolling_forecast_response/warning_workflow/rain_analysis/rainfall_river_impact）
- 提示词：prompts.py（565）
- 其它：send_wechat.py、utils/MusicTool.py、mock_vendor_agents.py、external_skill_tools.py

### 阶段
- [x] E1 理解：43 py 文件 ~15000 行、tests 70 passed 基线
- [ ] E2 审查：综合代理 D1+D2+D3 + 内联 D4（secrets/imports/timeouts）
- [ ] E3 修复：只修明确 bug/泄漏（沿用 traction/MCP 规则）
- [ ] E4 simplifier+verify+docs+memory+push
