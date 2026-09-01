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

## 2026-08-31（R9：表格断行真 bug 定位修复）

用户复测"天津当前天气实况"（本地工具 query_current_weather_observation），表格又断行（`|区域|...||:---|...`）——
**这是当前代码的真 bug，不是部署问题**（此前 R5 判断为天河接口所致已关闭，现被本地工具打脸，R5 改判）。

- **根因**：`chain_gzt.astream_answer_chain_to_message` 主路径在函数内 `_repair_markdown_layout(_sanitize_display_text(full_text))`
  把 `stream_msg.content` 修好（`||`→`|\n|`），但 **`return _sanitize_display_text(full_text)` 返回的是未修复原文**；
  调用方（`_finalize_complete_tool_evidence` 等 6 处）拿**返回值**再 `stream_msg.content = text`（evidence 路径 :2653、
  首轮 :5736）覆盖 → 修复被冲掉，模型偶发压成一行的表格原样渲染。两个 ainvoke 兜底返回同样未修复。
  旁证：`_sanitize_display_text` 自带的 `_concat_row_pattern` `||` 拆行修复对**多于 2 行粘连**失效
  （贪心 group2 吞多行 → 行内 | 数不等 → 守卫拒绝），实测留 `||`；`_repair_markdown_layout` 的简单
  `.replace("||","|\n|")` 对任意行数都有效，故两链叠加后输出正确——问题只在返回值不带修复。
- **修**：返回值与展示一致（主路径 `return final_text`；两个 ainvoke 兜底 `text` 也过 `_repair_markdown_layout`）。
  副作用：历史消息存修复版（与展示一致，answer LLM 后续仿写格式更稳）。
- **测试**：`test_execution_mode.py::test_answer_return_value_has_repaired_table`（贴的表格场景：断言返回值与
  stream_msg.content 都无 `||`、行已拆、两者相等）。**红绿闭环**：stash 修复后测试 FAIL、恢复后 PASS。
- **全量回归**：chainlitexam 1081 passed / 5 skipped / 0 failed。
- CLAUDE.md A4 补返回值契约说明。

## 2026-08-31（R10：实况问法前缀误标"预报数据"）

用户复测"天津当前天气实况"，表格已正常（R9 修复生效），但回答前缀仍是
"已结合预报数据完成分析"——实况查询声称"预报数据"，是错的前缀。

- **根因**：`_build_thinking_summary` 关键词分支顺序——预报分支 `["未来","预报","明天","后天","周末","天气"]`
  在观察分支 `["实况","现在","当前","刚才","今天","今日"]` **之前**；"天津当前天气实况"同时含"天气"和"当前/实况"，
  "天气"先命中 → 实况问法被标成"预报"。R2 修重复摘要时还把错误标签锁进了测试
  （test_prepend_strips_llm_imitated_summary_prefix 断言"已结合预报数据"），这次一起纠正。
- **修**：把强观察词（实况/现在/当前/刚才）拆到预报分支**之前**；"今天/今日"保持在预报分支之后
  （避免"今天天津天气"这类预报问法被误标为实况）。变更 = 4 行分支重排，无其它逻辑改动。
- **测试**：新增 `test_current_observation_query_gets_observation_prefix`（4 条问法锁定实况前缀）；
  原 4 个锁"预报"标签的测试改锁正确"实况观测"标签（去重断言改用 `out.count("完成分析")==1`）。
  红绿闭环：修复前 4 failed → 修复后全过。
- **全量回归**：chainlitexam 1082 passed / 5 skipped / 0 failed。
- **code-review（独立 agent）**：分支重排无确认缺陷，但发现 1 条可疑 + nits，已全部落实：
  1. （中）`qa_http_api._LEAD_IN_ONLY` 缺"已结合实况观测数据…"前缀 → HTTP 出口纯引导语过滤对实况问法失效。
     补入清单（`_LEAD_IN_ONLY_NORMALIZED` rstrip 冒号归一化只处理冒号、不含逗号变体），测试
     `test_merge_answers_drops_lead_in_only_with_fallback_text` 参数化覆盖 预报/实况/半角冒号 3 变体。
  2. （低）"当前天气预报/现在天气预报" 从"预报"标签翻转为"实况"——与路由口径（当前/现在→实况工具）一致，可接受；
     已在分支上方加注释说明排序是承重约束（勿合并调序）。
  3. （nit）"今天X天气→预报"守卫无测试锁 → 新增 `test_today_with_weather_stays_forecast_prefix`。
  4. （nit）补"实时"到强观察词（"天津实时天气"同族缺漏，现也正确标实况）。
- **全量回归（收尾门禁）**：chainlitexam **1085 passed / 5 skipped / 0 failed**。CLAUDE.md 补排序承重约束说明。

## 2026-08-31（R8 关闭确认）

- 用户确认 R8 其余 03 实况问法（全市现在下了多少雨/市区现在气温和风的实况/今天雨下了多长时间/昨天雨下得怎么样）
  走天河即可："这些你提到的走天河就行"。R8 关闭，保持现状不动，无代码改动、无回归。
- task_plan.md R8 勾选关闭。

## 2026-08-31（R11：表格渲染成原始字符 —— 规则 3 空行守卫）

用户复测"天津当前天气实况"（前缀已对 R10、表格文本多行正常），仍报"压根就没表格，全是字符"。
经 AskUserQuestion 澄清 = **UI 里 markdown 表格渲染成原始 `|` 字符**。这是独立于 R9（`||` 粘行）的新根因。

- **根因**：`message_orchestrator._sanitize_display_text` 规则 3（首提交即存在）在"正文后接表格"插空行，
  但字符类含 `\s`——**无空格紧凑表格**（`|区域|...`，管道后紧跟非空白）的每个行首 `|`（前一字符 `\n`）都命中，
  表头/分隔行/数据行之间全被插空行。GFM 表格要求表头行与分隔行**相邻**，空行把整张表拆成带 `|` 的普通段落，
  remark-gfm 不再识别为表格 → UI 显示原始字符。逐层排查：chainlit 2.9.6 bundle `remarkPlugins=[YEn,K2n,Ufn,GEn]`
  中 K2n=remark-gfm（其 `Y2n` = gfm 五扩展：autolink/footnote/strikethrough/table/tasklist）、Ufn=remark-math——**gfm 已启用**，
  排除渲染层配置问题；用精确 MSG#5 内容跑 `_sanitize_display_text` 实锤插空行（表头↔分隔行不再相邻）。
  带空格风格（`| 日期 |`，`_markdown_table` 生成）被规则 3 负向前瞻 `(?![\s|])` 天然豁免、一直正常——
  这解释了为何滚动预报表格好而当前实况表格坏。
- **修**：规则 3 改回调 `_blank_before_table_row`——上一行已是表格行（strip 后 `|` 开头 `|` 结尾且 `|` 数≥2）
  时保持相邻（不插空行），只在"正文后接表格"时插空行。规则 3c/5/6 对紧凑表格已安全（3c 要求行首 `|` 前一字符非 `|`，
  表行间前一字符恰是 `|`；5 只在表格后接非 `|` 文本时插行；6 与表格无关），无需改。性能：200 行表 0.41ms、
  10 张×200 行 6.6ms，可忽略。
- **测试（TDD 红→绿）**：新增 `tests/test_sanitize_display_text.py`（5 条：紧凑表行相邻/生产场景全答案/
  正文后接表格仍插空行/带空格表完整/行内竖线不变；修复前 3 条红）+ `test_execution_mode.py` 新增
  `test_answer_keeps_multiline_table_rows_adjacent`（answer 链端到端多行表相邻 + 返回值=展示）。
- **全量回归**：chainlitexam **1091 passed / 5 skipped / 0 failed**。diff 敏感扫描 CLEAN。
- **待办**：code-review（独立 agent 进行中）→ 提交推送（显式路径 git add，不提交 AgentWeb.zip）。

- **code-review（独立 agent，无确认缺陷）+ 加固**：守卫从「上一行 strip 后 |开头|结尾 且 |≥2」简化为「`|` 所在行以 `|` 开头」——后者同时修复**行中标点结尾单元格**（`结论：|`/`（中雨）|`）被拆行的既有缺陷（紧凑表单元格分隔符，改动前同病），且不误伤（普通段落不以 `|` 开头，已实测核对）；另采纳 rfind 免整前缀拷贝（性能 nit）。全量 chainlitexam **1093 passed / 5 skipped / 0 failed**。

## 2026-09-01（R12：天津当前天气实况内容口径 —— 列天津各区县、不带海河流域）

用户复测"天津当前天气实况"（R11 修复后表格已正常渲染），但内容口径不对：
"问的不是天津的吗，为什么会出现海河流域，而不把天津各区的列出来呢"——回答给了
天津市/中心城区/蓟州区/海河流域 4 个汇总行，没有天津各区县明细，还多了海河流域行。

- **根因**：① 粒度——`query_current_weather_observation` 只聚合 6 个固定桶
  （tianjin/tianjin_central/jizhou/beijing/hebei/haihe_basin），无天津区县明细
  （数据层 `Cnty` 字段完全支持按区县聚合）；② 口径——该问法不含"滚动" → 不走
  `is_current_rolling_weather_query` 确定性格式化器（那路径按设计就带海河流域行），
  走 planner→`query_current_weather_observation`→answer LLM 从 JSON 自由挑行，
  prompt :124 的"不得将天津市与海河流域混为一谈"不足以约束。
- **修（两层，与"司南分层回答"同模式：代码供数据 + prompt 引导展示）**：
  ① MCP `current_weather_observation_service._group_tianjin_districts(tianjin_records)`：
  按 `Cnty` 分组、逐区县复用 `_calculate_area_stats`，`regions` 新增 `tianjin_districts`
  （list：name + 同口径统计字段；按 max_pre_mm 降序、None 无数据排最后、名称兜底；
  缺 Cnty 归"未分区"不丢数据；展示名用原始 Cnty 零编造）。确定性"滚动实况"路径
  （current_weather_observation_response.build_*）只读 REGION_LABELS 固定键 +
  `if key in REGION_LABELS` 过滤，新增键不影响该路径。
  ② prompt 双轨（PLANNER :302-304 / WEATHER :987-989 同段）加"展示范围"规则：
  只问"天津/全市/我市"当前实况时用 `regions.tianjin_districts` 逐区县列表，
  不把海河流域/北京/河北列入明细（除非用户明确问到）。haihe_mcp_tools docstring 同步。
  注：PLANNER 轨用半角引号、WEATHER 轨用全角引号（两轨既有差异，非字节级一致），分别 edit。
- **测试（TDD 红→绿）**：MCP `tests/test_current_weather_districts.py`（6 条：按 Cnty 分组/
  复用 area_stats 同区聚合/按雨量降序/缺 Cnty 归未分区/既有 6 桶不变/累计缺测但小时有雨不排最后）
  ——修复前 4 failed。chainlitexam `test_prompts.py::test_current_weather_observation_tianjin_districts_scope_rule`
  双轨锁 1 条。
- **code-review（独立 agent，无确认缺陷）**：采纳 1 条加固——排序键原只用 `max_pre_mm`，
  "累计 PRE 缺测但小时 PRE_1h 有雨"的区县会被当无数据排到最后；改为与 `rainfall_judgement`
  的 `rain_basis` 同口径（累计缺测回退小时），新增 `TestTianjinDistrictsSortEdge` 红绿锁定。
  另修正 CLAUDE.md 测试 venv 路径（旧路径本机不存在）。**未采纳（出范围/既有设计）**：
  ① "滚动气象信息实况"确定性路径按设计带海河流域 6 行（流域级运行报告，非本问法）；
  ② 拆分 prompt（ENABLE_NEW_ANSWER_PROMPT，非默认）的 METEO_ANSWER 本就不含当前实况块，
  属既有结构缺口。
- **全量回归**：MCP **612 passed / 20 skipped**（+6）；chainlitexam **1094 passed / 5 skipped / 0 failed**。
- diff 敏感信息扫描 CLEAN。待办：提交推送。

## 2026-09-01（R13：河流预报时段标签"未来第N天"→明天/后天/日期）

用户复测"未来三天泃河有雨吗"：答案数据没问题，但时段标签写"未来第1天/第2天/第3天"——
"意思是你没按照天气怎么样那些问题回答啊，什么未来第1天都是错的啊，不要这样描述啊"。

- **根因**：非数据问题，是标签文案。`resolve_river_forecast_periods`（river_query_forecast.py:243-247）
  把"未来N天"分支每天标成 `f"未来第{offset}天"`；前端 `build_river_forecast_answer` 直接渲染
  `period.label`，于是结论与【逐时段降雨预报】表都出现"未来第N天"。项目里"天气怎么样"类问题的
  惯例是 `rolling_forecast_service._time_of_day_label`：今天/明天/后天/M月D日。
- **修**：新增 `_relative_day_label(day, today)`（明天/后天/M月D日），future_days 分支改用它。
  纯函数 offset→标签映射，不触碰取数/窗口逻辑。前端无需改（渲染 label 原样）。
  修复后"未来三天泃河有雨吗"结论 = "预计明天、后天、9月4日泃河河道两侧约5公里沿线范围无明显降雨"，
  表格行 = 明天/后天/9月4日。
- **测试（TDD 红→绿）**：`test_river_query_forecast.py::test_future_days_labels_are_friendly_dates_not_future_day_n`
  （未来3天→明天/后天/9月4日；未来2天→明天/后天；未来5天→明天/后天/9月4/5/6日；断言不含"未来第"）。
  修复前红、修复后绿；既有"未来三天"测试只锁窗口数量/日期连续性，不锁标签，不受影响。
- **全量回归**：MCP **613 passed / 20 skipped**；chainlitexam **1094 passed / 5 skipped / 0 failed**。
- 纯标签微改（1 纯函数 + 1 分支），范围太轻未启动独立 review agent，自行复核无副作用面。

## 2026-09-01（R14：问实况却出预报 —— 路由观测词守卫；R15：河流标签带具体日期）

用户转述测试人员开会反馈两个问题：① **问"实况"有时会出"预报"**（"可能"=非确定性，要好好排查）；
② 河流预报时段标签**带个具体日期比较好**（确认采纳 R13 末尾提议）。

### R14 实况→预报 根因排查与修复（systematic-debugging）

- **取证**：`_select_pre_planner_route` 顺序 = 长图覆盖 → 实况目录（天津当前的天气情况）→ 天河固定目录 →
  `_route_simple_weather_query` → 天河知识路由。嫌疑锁定第 4 步 `_route_simple_weather_query`——
  它把"时间词（今天/明天…)+ 天气词"**确定性强制到 `query_rolling_forecast`（预报）**，跳过 planner，
  但**全程不检查"实况/实时/实测"等观测词**。
- **根因**：`has_mixed_current_future_scope` 要求同时含"当前词（现在/当前/目前/实时/实况）"和"未来词
  （明天/后天/未来/预报…)"才算混合，而 `FUTURE_TIME_MARKERS` **没有"今天"**（今天=当天非未来）。
  于是"今天天气实况"：有观测词"实况"但无未来词 → 不算混合 → 不拦截 → 命中 `query_rolling_forecast`（预报）。
  而"现在/当前/实时 实况"因这些**不是** `_SIMPLE_WEATHER_TIME_WORDS` 时间词、`has_time=False` 落回 planner
  通常答对。**是否出预报取决于措辞 → 表现为"有时/可能"**。现有 21 条路由测试无任何含"实况"样本，洞一直没被锁。
- **修**：`_route_simple_weather_query` 最终返回前加观测词守卫——含 `CURRENT_TIME_MARKERS`
  （现在/当前/目前/实时/实况，**并入"实测"**——实况同义族，测试人员会换着用；has_mixed/active_tool_router/
  新守卫三消费方语义都正确）且无未来词的纯实况问法：无点位→`query_current_weather_observation`（实况），
  有点位（`_decision_weather_prefilter` 命中）→返回 None 交回 planner（区域实况工具粒度不对，planner 用
  `query_poi_nearest_observation`）。"观测词+未来词"（明天实况）仍被 `has_mixed_current_future_scope` 提前拦到 planner。
  端到端验证：5 条实况问法→实况工具；"现在天气实况"/"明天天气实况"→None 交 planner；"今天天气/今天天气预报"→预报不变。
  误伤自查：天河 03 实况目录问法（全市/市区/现在/昨天+雨量/气温风）不含"今天/今日+天气关键词"组合、到不了本守卫，仍走天河（R8 不破）。
- **测试（TDD 红→绿）**：`test_simple_weather_route.py::TestRouteObservationQuery` 新增 14 条
  （8 实况→实况工具 / 2 点位实况→planner / 4 预报与混合不变：今天天气、今天天气预报、今天会下雨吗→预报、明天天气实况→None）。
  修复前 9 failed → 修复后全过。
- **全量回归**：chainlitexam **1108 passed / 5 skipped / 0 failed**（1094+14）。改动小且测试充分，按工作流对微小改动自行复核。

### R15 河流预报时段标签带具体日期

- 用户确认采纳 R13 末尾提议：明天/后天也带具体日期，多天窗口相对词与绝对日期混排时避免歧义。
- **修**：`_relative_day_label` 改 明天→`明天（M月D日）`、后天→`后天（M月D日）`、≥3 天仍 `M月D日`。
  单日分支（今天/明天/后天）保留短标签不变。前端渲染 label 原样，无需改。
- **测试**：`test_future_days_labels_are_friendly_dates_not_future_day_n` 断言同步改带日期形式
  （未来三天→明天（9月2日）/后天（9月3日）/9月4日 等）。
- **全量回归**：MCP **613 passed / 20 skipped**（改既有测试，数不变）；chainlitexam 不受影响。
- 待办：提交推送（显式路径，不含 AgentWeb.zip）。

### R17/R18 河流预报数据来源去括号 + 走廊灾害风险表

- 用户复测"未来三天泃河有雨吗"输出，提两条口径：
  ① 数据来源行不要"滚动预报网格"后面的 `（cycle=…）` 括号内容；
  ② 河流预报回答不像"天气怎么样"——风险内容（灾害风险表）没加进来。
- **① 去括号（前端展示层）**：`river_forecast_response._strip_data_source_cycle` 用
  `_CYCLE_SUFFIX_RE`（`（cycle=[^）]*）`）剥起报时次括号，只留数据源名。MCP data_source 契约
  保留 cycle 不动（`test_final_river_source_contract.py` 锁定必须含 cycle），所以只能在展示层剥。
- **② 走廊灾害风险表**：MCP `river_query_forecast._attach_corridor_region_hazards`——走廊几何
  `Centroid()` 取代表点（4326，免改 SQL）→ `rolling_forecast_service._risk_fcst_times_from_window(
  _river_risk_calendar_window(periods), now)` + `_query_region_hazards`，结果以
  `region_display="X沿线"` 附 `result["region_hazards"]`。`_load_rolling_forecast_service` 惰性
  import（防模块顶层重依赖/循环，供测试 monkeypatch）；osgeo 在测试 venv 不可用，测试用 dummy
  几何（object()）走优雅降级返回 None。任何异常/空结果静默降级，绝不阻断降雨回答。
  前端 `build_river_forecast_answer` 复用 `rolling_forecast_response._region_hazard_table`
  渲染【沿线灾害风险】表（灾害类型×隐患点数量×本次风险等级×风险研判×防范建议），排在数据来源之前。
  九分区路径（大区域单点代表性弱）暂不附着，留待后续。
- **测试（TDD 红→绿）**：前端 `test_river_forecast_response.py` +4（去括号/无括号不变/风险表渲染
  且排数据来源前/无 region_hazards 不出表）；MCP `test_river_query_forecast.py` +4（附着含
  region_display 与 categories/失败静默降级/空结果不附着/dummy 几何降级）。
- **全量回归**：MCP **617 passed / 20 skipped**（613+4）；chainlitexam **938+174 passed /
  5 skipped / 0 failed**（1108+4）。
- 待办：提交推送（显式路径，不含 AgentWeb.zip）。
