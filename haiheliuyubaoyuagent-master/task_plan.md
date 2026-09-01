# 领导问题清单接入（2026-08-26 docx + 天河补充问题）

来源：`问题分类列表20260826.docx`（领导测试要求）+ 用户粘贴的天河新增问题列表。

## 验收口径（用户原话）

1. 流域不同河系、水库的天气、山区风险类问题都要做出来（**文档中标黄的先做**）
2. 将天河做好的新问题接上（天河已有答案的问题，确定性路由到 `query_tianhe_fixed_qa`）
3. 全流程保证质量：code-review、code-simplifier、superpowers、测试

## 标黄先做（5 条）

| 问题 | 现状路由 | 目标 |
|---|---|---|
| 明天泃河有雨吗？ | PLANNER（未验证质量） | 河系降雨预报，确定性走对工具 |
| 今天晚上滦河有雨吗？ | PLANNER | 同上 |
| 今天蓟州可能有哪些风险？ | PLANNER | 风险预警工具（地灾/山洪/中小河流） |
| 未来三天于桥水库降雨预报？ | SIMPLE→decision_weather | 验证答案质量（水库点位降雨） |
| 盘山景区未来两天天气？ | SIMPLE→decision_weather | 验证答案质量（景区） |

## 天河侧

- 天河固定目录 48 条已全部接入（`tools/tianhe_fixed_qa_catalog.py`，含用户粘贴的全部新增问题）✅
- 待办：为目录问法的**近似变体**评估是否需要扩展（如"天津当前天气实况"vs目录"天津当前的天气情况"）——谨慎，防误伤决策天气。

## 阶段

- [x] P0 探针：50 条问题路由摸底（findings.md）
- [x] P1 标黄 5 条：河系天气（泃河/滦河）路由与答案质量 —— 支流→九分区归并 + 泃河入 basin guard
- [x] P2 标黄：蓟州山区风险问法路由 —— `query_region_weather_risks` 已存在，路由判定确认
- [x] P3 标黄：于桥水库/盘山 答案质量验证 —— 槽位干净（reservoir/scenic），POI 命中待内网验证
- [x] P4 剩余 docx 条目摸底 —— 天河目录已全覆盖
- [x] P5 测试 + code-review（3 条确认缺陷已修）+ code-simplifier + CLAUDE.md 更新
- [ ] P6 提交（显式路径）+ github（待用户确认是否推送/PR）

## 内网实测第三轮（2026-08-31 下午，本轮）

用户复测报疑似回归两条，并补"冲突先走我们智能体"原则 + 天河已完成清单对照。

- [x] R1 今日雨情走天河没图 → 改走本地组合长图（冲突归我们）。路由选择层覆盖，目录清单不动。
- [x] R2 天津当前天气实况重复思考摘要 → `_prepend_thinking_summary` 幂等剥离。
- [x] R3 docx 43 条全量路由回归测试 `test_docx_question_routing.py`（天河→天河、其余→本地、无冲突）。
- [x] R6 天津当前的天气情况 → 本地实况（用户"肯定归我们"）。`_route_tianhe_fixed_catalog_query` 对
      `_TIANHE_SERVED_LOCALLY`（长图 2 条 + 实况 1 条）返回 None，冲突问法确定性强制本地工具
      （长图→generate_haihe_composite_longimg、实况→query_current_weather_observation），不靠 planner 善意。
- [x] R4 全流程：code-review（无确认缺陷，1 PLAUSIBLE 加固 + 半角冒号 nit）+ code-simplifier（提取共享 helper）
      + 验证（1080 passed/5 skipped/0 failed，diff 敏感信息 CLEAN）+ github（无 PR）+ context7（不适用）。
- [x] R7 提交推送：完成（显式路径 git add，不含 AgentWeb.zip）。
- [x] R5 表格换行异常：**已关闭**。用户判断该次是走了天河接口所致；代码侧确认无问题，不追。
- [x] R9 天津当前天气实况表格断行（**当前代码真 bug，非部署**）：`astream_answer_chain_to_message` 内部
      `_repair_markdown_layout` 修好 `stream_msg.content`，但**返回值是未修复原文**；调用方
      （`_finalize_complete_tool_evidence` 等）拿返回值覆盖 `stream_msg.content` → 修复被冲掉，
      模型偶发压成一行的表格 `|...||:---|` 原样渲染。修：返回值与展示一致（主路径 + ainvoke 兜底都过
      `_repair_markdown_layout`）。回归测试 `test_answer_return_value_has_repaired_table`（红绿闭环验证）。
- [x] R8（关闭，用户确认 2026-08-31）prompt 0.5 段逐字枚举的其它 03 实况问法
      （全市现在下了多少雨/市区现在气温和风的实况/今天雨下了多长时间/昨天雨下得怎么样）
      仍走天河——用户确认"这些走天河就行"，保持现状不动，不改 prompts.py。
- [x] R11 天津当前天气实况**表格渲染成原始 `|` 字符**（UI"压根就没表格"）：`_sanitize_display_text` 规则 3
      （首提交即存在）字符类含 `\s`，无空格紧凑表格的每个行首 `|`（前一字符 `\n`）都被插空行——
      表头/分隔行/数据行不再相邻，GFM 表格被拆成带 `|` 的普通段落。修：规则 3 改回调守卫
      `_blank_before_table_row`（`|` 所在行以 `|` 开头则保持原样，code-review 加固后同时覆盖
      行中标点/括号结尾单元格分隔）。测试 test_sanitize_display_text.py（7 条）
      + test_execution_mode.py 端到端多行表相邻。全量 1093 passed/5 skipped/0 failed。
- [x] R12 天津当前天气实况**内容口径**（表格已渲染，但出现海河流域行、不列天津各区县）：
      "问的不是天津的吗，为什么会出现海河流域，而不把天津各区的列出来呢"。两层修复：
      ① MCP `current_weather_observation_service._group_tianjin_districts` 按 Cnty 分组、
      逐区县复用 `_calculate_area_stats`，`regions` 新增 `tianjin_districts`（按最大雨量降序、
      无数据排最后、缺 Cnty 归"未分区"不丢数据、展示名用原始 Cnty 零编造）；确定性"滚动实况"
      路径只读 REGION_LABELS 固定键不受影响。② prompt 双轨（PLANNER/WEATHER）加"展示范围"规则：
      只问天津/全市/我市时用 `regions.tianjin_districts` 逐区县列表、不带海河流域/北京/河北
      （除非用户明确问到）；haihe_mcp_tools docstring 同步。测试 test_current_weather_districts.py（6 条，
      含 code-review 加固：累计缺测回退小时雨量排序）+ test_prompts.py 双轨锁 1 条。
      全量 MCP 612 passed / chainlitexam 1094 passed/5 skipped/0 failed。
- [x] R13 河流预报时段标签"未来第N天"→明天/后天/具体日期（用户："什么未来第1天都是错的，要像天气怎么样
      那样回答"）。`resolve_river_forecast_periods` future_days 分支改用 `_relative_day_label`
      （明天/后天/M月D日，同滚动预报 _time_of_day_label 口径）。测试 1 条红绿锁定。
      全量 MCP 613 passed / chainlitexam 1094 passed/5 skipped/0 failed。
- [x] R14 **问实况却出预报**（测试人员开会反馈，"可能"=非确定性）：systematic-debugging 定位根因 =
      `_route_simple_weather_query` 不检查观测词，"今天/今日+天气词+实况"被确定性强制到 `query_rolling_forecast`
      （预报）；`has_mixed_current_future_scope` 拦不住因 `FUTURE_TIME_MARKERS` 无"今天"（今天=当天非未来）。
      "现在/当前/实时 实况"因不是时间词落回 planner 通常答对 → 表现为"有时/可能"。修：最终返回前加观测词守卫
      （`CURRENT_TIME_MARKERS` 并入"实测"），纯实况问法无点位→`query_current_weather_observation`、
      有点位→planner。测试 TestRouteObservationQuery 14 条红绿锁定。全量 chainlitexam 1108 passed/5 skipped/0 failed。
- [x] R15 河流预报时段标签带具体日期（用户确认"带个具体日期也是比较好的"）：`_relative_day_label`
      明天→明天（M月D日）、后天→后天（M月D日）、≥3天仍 M月D日；单日分支保留短标签。测试断言同步。
      全量 MCP 613 passed。
- [ ] R16 提交推送（显式路径，不含 AgentWeb.zip）。

## 遗留（上一批未完成 → 本轮已处理）

- [x] POI 决策天气"丰富注意事项/游玩建议"——按推荐方案**扩受控 action 词表**（新增 dress/car_wash/drying/exercise 9 个动作 + fine/mild 条件），保持零编造。已提交。
- [x] "能见度较低"晴天假阳性——**查证已修复**（`_poi_weather_conditions` 仅非 rain-only 解析能见度、动态从句 `min_vis<1.0` 门控 + rain_only 占位值守卫；test_decision_weather_tool.py:408-434/1241-1268/1553 锁定）。
