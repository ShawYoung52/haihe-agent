# 会话日志

## 2026-08-31

- 开工：用户确认上轮遗留（POI 大模型注意事项未做、能见度假阳性未修），给新任务：docx 标黄 5 条 + 天河新问题接入。
- 读取 docx（python 解 word/document.xml，highlight 识别标黄）→ 50 条清单、7 大类、5 条标黄。
- 路由探针 50 条：天河固定目录已含全部粘贴问题（需求 2 已满足）；标黄 5 条中泃河/滦河/蓟州风险走 PLANNER 待验证，于桥水库/盘山走 decision_weather。
- **P1 泃河/滦河**（已完成）：`_BASIN_RIVER_NAMES` 补"泃河"（防误路由天津区域）；`river_system_forecast` 新增 `_TRIBUTARY_TO_ZONE` 支流→九分区归并（泃河/潮白河/蓟运河/北运河/州河/还乡河→北三河，滹沱河/滏阳河→子牙河，漳河/卫河→漳卫南运河）+ `scope_note` 注明口径；`river_query_forecast` 走廊未命中时支流回退所属分区（river_name 保留用户所问河名）。顺带修复：潮白河/蓟运河/北运河原本 zone 匹配也落空。
- **P2 蓟州风险**（验证通过，无需改代码）：`query_region_weather_risks` 工具已存在，主动路由 `is_conservative_region_risk_query("今天蓟州可能有哪些风险")`=True，prompt 规则 12 引导，测试 test_region_weather_risks.py 通过。
- **P3 于桥水库/盘山**（路由验证通过）：SIMPLE→query_decision_weather_for_poi，槽位抽取 于桥水库(reservoir)/盘山景区(scenic) 正确；POI 命中需内网验证。
- **额外修复 1（决策天气槽位）**："本周末适合去泰达航母主题公园游玩吗" 规则抽槽曾得 "末适合去泰达航母主题公园"（"本周"切走留"末"、"适合去"未剥）。现 `本/这/下周` 时段词整体含"末"切分 + 前导出行引导词（适合去/想去等）剥除。
- **额外修复 2（天津港路由）**：裸"天津港"（无港口/港区后缀）曾落到天津市区代表点。三处同口径补"天津港"：MCP POI_PLACE_KEYWORDS、chainlitexam DECISION_WEATHER_PREFILTER_SUFFIXES、_DECISION_WEATHER_SUFFIXES（自命名后缀 head 为空直接取"天津港"）。
- 全量测试：MCP 595 passed / chainlitexam 835+159 passed，0 失败。
- **code-review（独立 agent，3 条确认缺陷，已全部 TDD 修复）**：
  1. （中）决策天气槽位：出行引导词剥离原排在尾端"去"剥离之后 → "能去/可以去/应该去/建议去"的"去"被先吃掉，残留"能/可以/应该/建议"拼进位置名（"明天能去天津港"→"能天津港"）。修：前导出行词剥离移到尾端剥离之前。回归测试 test_rule_based_slots_strip_modal_activity_prefix（6 句）。
  2. （中低）支流归并在剥后缀之前 → "泃河流域/潮白河河系"映射失败。修：`_tributary_zone_lookup` 容忍"流域/河系"后缀，`_match_zone_name`/`tributary_zone_for` 共用。新增 `_norm_zone_name` 统一归一化（消除 rstrip 链重复）。
  3. （低）scope_note 原用裸等值 `zone_name == parent_zone`，与 `_match_zone_name` 的归一化+别名包含口径不一致（zone_name 带"区"后缀时缺失）。修：复用匹配结果——`parent_zone and zones`（zones 已被过滤到所属分区）。
- 修复后全量：MCP 597 passed / chainlitexam 835+160 passed，0 失败。
- **code-simplifier**：结论"已经足够简单"，2 条可选清理——采纳 1 条（river_query_forecast 三态分支拆开，去掉 `zone_target != target` 守卫）；另 1 条（合并 tributary_zone_for/_tributary_zone_lookup）保留 public/private 分层不动。
- **提交并推送**：ee7bc17（领导问题清单20260826）已 push 到 origin/main（58ac85f..ee7bc17，含本地累计 26 个提交）。
- **遗留 1（能见度假阳性）**：查证已修复（rain_only 占位值守卫 + min_vis<1.0 门控 + 测试锁定），无需改。
- **遗留 2（POI 丰富注意事项）**：按用户选定的推荐方案实现——扩受控 action 词表（dress/car_wash/drying/exercise 9 动作 + fine/mild 派生条件），保持零编造；prompt 加生活指数引导。测试 TestLifestyleAdviceActions 8 条。全量 chainlitexam 835+168 passed。
- 下一步：提交生活指数建议并推送。

## 2026-08-31（内网实测反馈三条）

用户在内网测了标黄 5 条，粘贴答案+日志，给三条整改口径：

- **① 风险显示"只显示有数据的时刻"**：点位路径（于桥水库"未来三天"）原按目标日逐日请求未来起报时次，未来时次未发布必回无资料 → 整表刷"风险预报资料不可用"。改：点位统一用最近起报时次（`fcst_times=None`，无资料回退前一周期），不再逐日请求未来时次；MCP 透传 `point_risk_window`（数据时段 start_label/end_label，最近 08/20 起报 +24h）与 `point_risk_beyond_from`（所问窗口超资料覆盖时的"之后暂无资料"起点）；前端 `_build_point_risk_level_section` 表头标注"（资料时段：X—Y）"、超窗附注"Y 之后暂无风险预报资料"。no_data 渲染文案 "风险预报资料不可用"→"本时次暂无预报资料"（与"接口暂不可用"=失败、"本次无风险"=可达零风险三态区分）。区域路径（`_query_region_hazards` 逐日）不动。新增 MCP `_latest_risk_cycle_window`/`_risk_window_beyond_label` + 测试 6 条；chainlitexam 测试更新 2 处旧文案 + 新增 5 条。
- **② 水库 0 不显示**：`蓄水量`/`出库流量` 接口对未监测项回 0 占位（于桥水库"蓄水量约0/出库流量约0"）。改 `_build_poi_reminder_section` 蓄水量/出库流量仅当 `_to_positive_number`（正数才返回，0/负/None/NaN/布尔/非法→None）才显示；库上水位（真实测量）照常。与 VISMIN=0 缺测同口径。新增 `_to_positive_number` + 测试 2 条 + helper 测试 1 条。
- **③ 回答专业化**：根因是结论被严格压成一句话、数字全剥到表格，只剩空泛评价（"气温适宜""天气不错"）。两条结论 prompt 各加"专业表述"约束（用规范天气术语写天气过程与时段、气温可给整数区间、降水/大风点明量级与时段、避免空泛评价），仍守零编造、不与表格重复。改 `rolling_forecast_response.rolling_forecast_llm_instruction` + `decision_weather_core._generate_decision_weather_answer` prompt。
- 全量：MCP 602 passed / chainlitexam 835+174 passed，0 失败。
- 待确认：区域路径多日窗口（蓟州类）目前仍按 8-24 口径渲染（no_data→"无风险"、超 24h 移除列），是否要与点位路径统一为"标数据时段+超窗说明"。

## 2026-08-31（内网实测第二轮：区域风险话术 + 河流预报太简）

用户复测后两条新反馈：

- **A 蓟州风险"接口暂不可用（无对应预报数据）"（用户："这能给业务人员看吗"）**：诊断 = **我们这边渲染口径问题，不是接口问题**。日志显示 08 时与 20 时（回退）两类都调了——08 时若真失败会 break 不再调 20 时；现 20 时也被调 → 08 时回的是"该时次无数据"（业务正常应答，走 continue），地质(SCMOC)/中小河流(EC type1) 20 时仍无数据 → `unavailable_reason="risk_forecast_no_data"`；山洪(EC type2) 20 时有数据（零风险）→ 正确"无风险"。接口一直通。但 prompt 把所有 unavailable 一律写"接口暂不可用"，模型还自行加"（无对应预报数据）"。
  - 修：`_normalize_region_risk` 新增**确定性 `status_text`**（面向业务用户）：no_data/risk_window_incomplete→"暂无风险预报资料"、真失败（risk_service_unavailable/risk_kind_unavailable/malformed）→"风险数据查询暂时不可用"、no_risk→"无风险"、risk→"有风险"。prompts.py 双轨规则改为**逐字采用 `status_text`**，禁止改写为"接口暂不可用/无对应预报数据/暂无对应时次风险资料"等技术化措辞、禁止自加括号说明。测试 `TestRegionRiskStatusText` 4 条（含"status_text 绝不含'接口'/'预报数据'"反向锁）。
- **B 泃河/滦河回答太简（用户："怎么回答的这么少了"）**：河流走廊/九分区预报走通用 planner→answer 纯 LLM 路径，**没有代码生成的表格**，只出一句话（与本次改动无关，是既有路径）。
  - 修：新增 `chainlitexam/tools/river_forecast_response.py` `build_river_forecast_answer`——确定性组装【核心结论】（无明显降雨/有降雨含时段最大雨量/多日分述）+【逐时段降雨预报】表（时段×平均雨量×最大雨量×降雨判断）+数据来源，零编造只引用工具返回降雨字段；`_run_tool_round` 对 `query_river_rainfall_forecast` 且纯河流查询（`is_conservative_river_forecast_query`）设 `forced_final_text` 直接收口（镜像 query_decision_weather_for_poi），非 ok/混合查询交回原 LLM 路径。测试 `test_river_forecast_response.py` 7 条。
- 全量：MCP 606 passed / chainlitexam 842+174 passed，0 失败。

## 2026-08-31（内网实测第三轮：今日雨情走天河没图 + 实况重复摘要）

用户复测报两条疑似回归，并自己发现"今日雨情走了天河接口"。先以 `git show --stat` 举证：本轮前两笔提交
（8e26c32 / fb39b9c）只碰 河流预报/区域风险话术/水库0/专业化，**未碰实况观测、任何出图/长图工具、思考摘要逻辑**。

- **A 今日雨情走天河、没图（用户选定"改走本地长图"）**：根因 = "今日雨情"在天河固定目录
  `tools/tianhe_fixed_qa_catalog.py:29`（甲方 48 条第 29 条）里，`_route_tianhe_fixed_catalog_query`
  整句精确命中→强制 `query_tianhe_fixed_qa`；天河纯文本 QA 对这条的成品答案是"长图已生成，请查看图片"
  （全仓库 grep 无此句，证实是天河原文透传），图字节不经文本接口传过来 → 只见文案不见图。
  该问法本是 **2026-08-21 验收 #4 既定的"降水专题组合长图"触发问法**（prompts 组合长图规则 +
  `test_prompts.py::test_longimg_trigger_covers_today_rain` 锁定），2026-08-24 天河目录接入把它截走造成退化。
  - 修（只改路由选择层，不动目录清单/底层识别）：`message_orchestrator` 新增
    `_TIANHE_LOCAL_LONGIMG_QUESTIONS={"今日雨情","今天雨情"}` + `_route_local_longimg_catalog_query`，
    在 `_select_pre_planner_route` 顶部、天河固定目录判断**之前**优先路由到 `generate_haihe_composite_longimg`
    （无参=今天长图），确定性出图并跳过 planner/天河。`_route_tianhe_fixed_catalog_query` 与 48 条清单保持不变。
  - 测试 `test_tianhe_knowledge_route.py::TestLocalLongimgOverride`（7 条：今日/今天雨情+带标点→组合长图、
    目录清单仍含今日雨情、底层识别不变、其它目录问法仍走天河）。
- **B 天津当前天气实况 重复思考摘要**：该问法**不走天河**（与目录"天津当前的天气情况"字面不同、
  03 实况规则缺"风大/多长时间/昨天"等词不命中），走本地 planner→实况工具。重复摘要"已结合预报数据…：
  已结合实况观测数据…："根因 = answer LLM **仿写历史**（历史 AIMessage 连摘要前缀一起存入，
  `messages.append(AIMessage(content=text))`），开头再产出一句"已…如下："，代码 `_prepend_thinking_summary`
  又前置正确摘要 → 摘要套摘要。既有设计，非本轮提交。
  - 修：`_prepend_thinking_summary` 幂等——新增 `_strip_leading_thinking_summary`
    （`_THINKING_SUMMARY_LEAD_RE=^已[^\n]*?如下：\s*`，匹配全部摘要分支含 has_chart 变体），前置正确摘要前
    剥离开头已有摘要行。预警"当前无生效…"守卫、正常无前缀回答不受影响。
  - 测试 `test_thinking_summary.py` +6 条（仿写剥离/多句叠加剥离/has_chart 变体/正常不变/非摘要不误剥/预警守卫不变）。
- **全量回归**：chainlitexam 1028 passed / 5 skipped / 0 failed。
- **待用户内网复测**：① 今日雨情应出本地组合长图（依赖服务器装有 Playwright+Chromium，否则降级网址文案）；
  ② 天津当前天气实况重复摘要应消失。**表格换行异常**（"|区域|平均雨量…"表头单元格被拆开）未能在代码侧复现，
  疑为粘贴换行塌陷或 answer LLM 表格生成抖动——若复测仍在，请给原始答案（非粘贴）定位。

## 2026-08-31（第三轮续：天津当前的天气情况 → 本地 + 冲突原则推广）

用户给天河已完成 51 问清单，并定口径：**"假如我们的问题跟天河冲突了，那肯定先走我们的智能体"**。
问"天津当前的天气情况"是否归我们时用户不耐烦："肯定归我们啊，我不是说过了吗，还要问我"——按冲突原则直接收归，不再逐条问。

- **R6 天津当前的天气情况 → 本地实况**：该问法在天河固定目录（catalog 第 45 条），但天河成品是静态文本、
  本地实况观测是实时数据更准。修复方式重构：把 R1 的"长图覆盖"扩为统一的"冲突问法收归本地"集合——
  `_TIANHE_LOCAL_LONGIMG_QUESTIONS={"今日雨情","今天雨情"}`（走本地组合长图）+
  `_TIANHE_LOCAL_PLANNER_QUESTIONS={"天津当前的天气情况"}`（落本地 planner 当前天气），
  并集 `_TIANHE_SERVED_LOCALLY`；`_route_tianhe_fixed_catalog_query` 对该并集返回 None（不再走天河），
  由 `_select_pre_planner_route` 顶部的长图覆盖先接住今日雨情，天津当前的天气情况则穿透到 planner 实况。
  **目录清单 TIANHE_FIXED_QA_QUESTIONS 保持不动**（是天河"已备好答案"的事实记录），只改路由策略层。
  安全性：prompts.py 未逐字枚举"天津当前的天气情况"（0.5 段只列全市雨量/市区气温风等其它 03 实况问法），
  planner 不会被推向天河；`_enforce_tianhe_catalog_boundary` 因 `_route_tianhe_knowledge_query` 对它返回 None
  会正确拦截 planner 万一误选天河 → 回退本地。probe 实证：fixed/knowledge/simple 全 None → select=None 交 planner。
- **测试**：test_tianhe_knowledge_route.py `test_all_new_questions_route_verbatim` 排除 _TIANHE_SERVED_LOCALLY
  + 新增 `test_served_locally_questions_skip_tianhe_catalog`；`test_override_keeps_question_in_tianhe_catalog`
  改为"目录仍含但路由返回 None"；新增 `TestLocalPlannerOverride`（5 条）。docx 路由测试把"天津当前的天气情况"
  加入"不被天河截走"清单。test_tianhe_knowledge_route 199 passed。
- **code-simplifier 微调**：`_strip_leading_thinking_summary` 循环内 `.lstrip()` 冗余（正则 `\s*` 已吞尾随空白），删除。
- **code-review（独立 agent，完整当前 diff）**：**无确认缺陷**。1 条 PLAUSIBLE 已加固 + 2 个 nit 采纳 1 个：
  - （低）`天津当前的天气情况` 原靠 planner 善意——prompts.py:255 告诉 planner"51 题优先按固定目录精确命中"，
    万一 planner 误选天河 → 边界拦截 → 空工具调用落"无法获取"。修：新增 `_route_local_current_weather_catalog_query`
    确定性强制 `("query_current_weather_observation", {})`（hours_back 默认 6，与通用实况同口径），
    与长图覆盖对称，三条冲突问法全部确定性收归本地。
  - （nit）strip 正则只认全角`：`——answer LLM 若用半角冒号仿写前缀则剥不掉。改 `如下[:：]`。
  - （nit）strip 吃掉 `已…如下：` 开头的合法引导句——正文保留、代码前置自己的摘要，属方法固有、可接受，不改。
- **code-simplifier（第二轮，R6+review 加固后）**：采纳提取共享 helper `_route_local_catalog_query`
  （归一化→成员检查→无参工具元组 共性与两个包装函数各留专属 docstring 的"为何本地赢天河"），
  冲突问法家族 0→2 后模式明确会复现，集中惯用法为真净收益。行为逐字不变。
- **github 环节**：仓库直推 main 无 PR；唯一 PR #1（2026-07 chore）已关闭未合并，无待处理评审意见。
- **context7**：本轮纯路由逻辑+测试改动，无三方库/框架/API 用法变化，不适用。
- **全量回归（最终门禁，含 test_decision_weather_tool.py——CLAUDE.md 记录的既有导入失败已不成立）**：
  chainlitexam **1080 passed / 5 skipped / 0 failed**（无 --ignore）。diff 敏感信息扫描 CLEAN（无内网 IP/凭据）。
- **待办**：提交推送（显式路径 git add，不提交 AgentWeb.zip）。

## 2026-08-31（收尾确认）

- 提交推送完成：`fb39b9c..7ee5284 main -> main`（fix(routing): 冲突问法先走本地）。
- **R5 表格换行异常 关闭**：用户判断该次是走了天河接口所致，代码侧确认无问题，不追。
- **R8（待定）**：prompt 0.5 段逐字枚举的其它 03 实况问法（全市现在下了多少雨/市区现在气温和风的实况等）
  仍走天河，用户说"再说"，保持现状不动。
