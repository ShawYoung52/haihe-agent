# 天河目录与标黄问题验收记录

日期：2026-08-28  
状态：实现、最终修复波、真实依赖离线回归及最终 scoped re-review 已完成；内网真实接口验收待完成。
实现基线：`58ac85f..836f9ed`，另包含Task4复审fix1的时间窗口守卫。本文是三份原始实施计划的执行结论，历史示例冲突以本记录裁定为准。

## 1. 结论与证据边界

- 已接入新增天河目录 51 题，保留既有 03/04/05 问法；新增匹配仅归一化空白与句末标点，不做宽泛语义扩展。工具参数保留用户原文。
- 5 个标黄问题已有确定性工具路由、空间/时间或 POI 逐日输出离线测试；`ENABLE_FAST_PATHS = False` 未改变。
- 统一河流入口仅支持今天/今日、明天/明日、后天、今天晚上/今晚、未来N自然日（1—99天）的单一窗口。一周/周末、明确日期、其他日内时段、小时窗口、混合时段或未指定时间保留原有Planner/河系工具处理；直接调用新核心时返回invalid_request，不能默默统计今天。此限制不改变原接口的统计范围。
- 未访问天河、生产数据库、MUSIC、GeoServer、真实预报栅格或风险接口。下列 56 题的生产状态全部是“待内网验收”，不得把离线通过写成生产通过。
- Chainlit 全量通过但使用仓库既有离线 stubs；8 个真实 Chainlit 依赖测试仍跳过。MCP 原始全量未通过收集；额外 import-only 测试不代表真实 FastMCP、networkx、GDAL 或服务启动成功。
- 本轮未安装依赖、未改配置、未操作 Git、未修改用户新增归档或 MCP 的既有隐藏配置目录。

## 2. 56 题逐题清单

T 类共同契约：只调用 `query_tianhe_fixed_qa(query=用户原文)`；允许目录规范化匹配，但禁止改写工具参数。成功时透传供应方结果；失败时明确天河服务不可用，不由本地模型代答。每行范围仅说明验收意图，不预填供应方答案。检查原问、空白/标点变体、工具参数、返回状态、最终正文和请求时间。

| 编号 | 用户问题 | 预期工具 | 范围/检查点 | 离线 / 内网状态 |
| --- | --- | --- | --- | --- |
| T01 | 今年7月蓟州区有多少天超过35℃ | `query_tianhe_fixed_qa` | 当年7月；蓟州；35℃以上日数；原文透传 | 路由通过 / 待内网验收 |
| T02 | 今年以来我市40℃以上高温出现过几次？ | `query_tianhe_fixed_qa` | 当年起至请求时；我市；40℃以上次数；原文透传 | 路由通过 / 待内网验收 |
| T03 | 现在市区风大吗？ | `query_tianhe_fixed_qa` | 当前；市区风实况；原文透传 | 路由通过 / 待内网验收 |
| T04 | 市区现在气温和风的实况 | `query_tianhe_fixed_qa` | 当前；市区气温与风实况；原文透传 | 路由通过 / 待内网验收 |
| T05 | 全市现在下了多少雨 | `query_tianhe_fixed_qa` | 当前；全市降雨实况；原文透传 | 路由通过 / 待内网验收 |
| T06 | 今天雨都下在哪了 | `query_tianhe_fixed_qa` | 当天；降雨分布；原文透传 | 路由通过 / 待内网验收 |
| T07 | 暴雨天气的防范建议 | `query_tianhe_fixed_qa` | 暴雨防范知识；原文透传 | 路由通过 / 待内网验收 |
| T08 | 大风天气的防范建议 | `query_tianhe_fixed_qa` | 大风防范知识；原文透传 | 路由通过 / 待内网验收 |
| T09 | 高温天气的防范建议 | `query_tianhe_fixed_qa` | 高温防范知识；原文透传 | 路由通过 / 待内网验收 |
| T10 | 强对流天气怎么应对 | `query_tianhe_fixed_qa` | 强对流应对知识；原文透传 | 路由通过 / 待内网验收 |
| T11 | 暴雨预警四个等级是什么 | `query_tianhe_fixed_qa` | 暴雨预警等级知识；原文透传 | 路由通过 / 待内网验收 |
| T12 | 高温怎么定义 | `query_tianhe_fixed_qa` | 高温定义；原文透传 | 路由通过 / 待内网验收 |
| T13 | 气温多高算是高温 | `query_tianhe_fixed_qa` | 高温阈值知识；原文透传 | 路由通过 / 待内网验收 |
| T14 | 高温来了公众应该怎么办 | `query_tianhe_fixed_qa` | 公众高温应对；原文透传 | 路由通过 / 待内网验收 |
| T15 | 高温预警信号及应对措施 | `query_tianhe_fixed_qa` | 高温预警知识与应对；原文透传 | 路由通过 / 待内网验收 |
| T16 | 降雨量怎么分等级 | `query_tianhe_fixed_qa` | 降雨量等级知识；原文透传 | 路由通过 / 待内网验收 |
| T17 | 台风等级 | `query_tianhe_fixed_qa` | 台风等级知识；原文透传 | 路由通过 / 待内网验收 |
| T18 | 暴雨预警发出后公众该怎么办 | `query_tianhe_fixed_qa` | 公众暴雨预警应对；原文透传 | 路由通过 / 待内网验收 |
| T19 | 暴雨是如何形成的 | `query_tianhe_fixed_qa` | 暴雨成因知识；原文透传 | 路由通过 / 待内网验收 |
| T20 | 暴雨等级是如何划分的 | `query_tianhe_fixed_qa` | 暴雨等级知识；原文透传 | 路由通过 / 待内网验收 |
| T21 | 暴雨的主要危害有哪些 | `query_tianhe_fixed_qa` | 暴雨危害知识；原文透传 | 路由通过 / 待内网验收 |
| T22 | 当前湿度大不大？ | `query_tianhe_fixed_qa` | 当前湿度；原文透传 | 路由通过 / 待内网验收 |
| T23 | 今日雨情 | `query_tianhe_fixed_qa` | 当天雨情；原文透传 | 路由通过 / 待内网验收 |
| T24 | 今天适合洗车吗？ | `query_tianhe_fixed_qa` | 当天洗车适宜性；原文透传 | 路由通过 / 待内网验收 |
| T25 | 今天穿衣有什么建议？ | `query_tianhe_fixed_qa` | 当天穿衣建议；原文透传 | 路由通过 / 待内网验收 |
| T26 | 今天适不适合晾晒？ | `query_tianhe_fixed_qa` | 当天晾晒适宜性；原文透传 | 路由通过 / 待内网验收 |
| T27 | 什么是短时强降水？ | `query_tianhe_fixed_qa` | 短时强降水定义；原文透传 | 路由通过 / 待内网验收 |
| T28 | 副高代表什么含义？ | `query_tianhe_fixed_qa` | 副高定义；原文透传 | 路由通过 / 待内网验收 |
| T29 | 什么是面雨量？ | `query_tianhe_fixed_qa` | 面雨量定义，不是实况数值查询；原文透传 | 路由通过 / 待内网验收 |
| T30 | 雷电怎么防御？ | `query_tianhe_fixed_qa` | 雷电防御知识；原文透传 | 路由通过 / 待内网验收 |
| T31 | 高温有哪些危害？ | `query_tianhe_fixed_qa` | 高温危害知识；原文透传 | 路由通过 / 待内网验收 |
| T32 | 冰雹产生原理？ | `query_tianhe_fixed_qa` | 冰雹成因知识；原文透传 | 路由通过 / 待内网验收 |
| T33 | 双偏振雷达干什么用？ | `query_tianhe_fixed_qa` | 双偏振雷达用途；原文透传 | 路由通过 / 待内网验收 |
| T34 | 自动气象站如何观测？ | `query_tianhe_fixed_qa` | 自动站观测原理；原文透传 | 路由通过 / 待内网验收 |
| T35 | 气象卫星有什么作用？ | `query_tianhe_fixed_qa` | 气象卫星用途；原文透传 | 路由通过 / 待内网验收 |
| T36 | 雾和霾有什么区别？ | `query_tianhe_fixed_qa` | 雾霾区别；原文透传 | 路由通过 / 待内网验收 |
| T37 | 夏天为何多雨？ | `query_tianhe_fixed_qa` | 季节气候知识；原文透传 | 路由通过 / 待内网验收 |
| T38 | 为什么打雷下雨？ | `query_tianhe_fixed_qa` | 雷雨成因知识；原文透传 | 路由通过 / 待内网验收 |
| T39 | 天津当前的天气情况 | `query_tianhe_fixed_qa` | 当前；天津天气；原文透传 | 路由通过 / 待内网验收 |
| T40 | 预警发布流程是什么？ | `query_tianhe_fixed_qa` | 预警发布流程知识；原文透传 | 路由通过 / 待内网验收 |
| T41 | 天气会商包含哪些内容？ | `query_tianhe_fixed_qa` | 天气会商知识；原文透传 | 路由通过 / 待内网验收 |
| T42 | 面雨量如何计算？ | `query_tianhe_fixed_qa` | 面雨量计算方法；原文透传 | 路由通过 / 待内网验收 |
| T43 | 双偏振雷达产品怎么看？ | `query_tianhe_fixed_qa` | 雷达产品判读知识；原文透传 | 路由通过 / 待内网验收 |
| T44 | MICAPS 产品怎么分析？ | `query_tianhe_fixed_qa` | MICAPS 产品分析知识；原文透传 | 路由通过 / 待内网验收 |
| T45 | 你可以回答哪些问题？ | `query_tianhe_fixed_qa` | 能力说明；原文透传 | 路由通过 / 待内网验收 |
| T46 | 明天出门要不要带伞 | `query_tianhe_fixed_qa` | 下一天出行建议；原文透传 | 路由通过 / 待内网验收 |
| T47 | 哪些问题你无法解答？ | `query_tianhe_fixed_qa` | 能力边界；原文透传 | 路由通过 / 待内网验收 |
| T48 | 你的气象数据来源是什么？ | `query_tianhe_fixed_qa` | 数据来源说明；原文透传 | 路由通过 / 待内网验收 |
| T49 | 预报可以支持多长时效？ | `query_tianhe_fixed_qa` | 预报时效说明；原文透传 | 路由通过 / 待内网验收 |
| T50 | 我该怎么向你提问？ | `query_tianhe_fixed_qa` | 提问方法；原文透传 | 路由通过 / 待内网验收 |
| T51 | 降雨对道路交通会带来什么影响？ | `query_tianhe_fixed_qa` | 降雨交通影响知识；原文透传 | 路由通过 / 待内网验收 |

| H01 | 明天泃河有雨吗？ | `query_river_rainfall_forecast` | 泃河真实河道两侧约5公里；下一自然日00:00—24:00；不可用≠无雨 | 离线通过 / 待内网验收 |
| H02 | 今天晚上滦河有雨吗？ | `query_river_rainfall_forecast` | 滦河属于九分区，裸名称直接按滦河分区统计；今晚18:00—24:00，18时后从当前可用整点起 | 离线通过 / 待内网验收 |
| H03 | 今天蓟州可能有哪些风险？ | `query_region_weather_risks` | 蓟州代表点及返回半径；三类风险分别显示；最近可用起报真实覆盖未知时不得写完整自然日 | 离线通过 / 待内网验收 |
| H04 | 未来三天于桥水库降雨预报？ | `query_decision_weather_for_poi` | 天津于桥水库；三天独立展示，不重复总雨量；核对水情/风险提醒事实依据 | 离线通过 / 待内网验收 |
| H05 | 盘山景区未来两天天气？ | `query_decision_weather_for_poi` | 天津盘山景区；两天独立展示；仅真实天气触发对应提醒 | 离线通过 / 待内网验收 |

标黄题应额外保存：实际 POI 名称/坐标、匹配河名与 scope_type、查询起止时间、数据来源、每时段像元数/状态、三灾种独立风险状态和最终回答。全流域、天津与某条河流不能互相替代。

## 3. 必须保留的执行裁定

| 裁定 | 理由与执行方式 | 判断错误的代价 |
| --- | --- | --- |
| 精确目录先于简单天气及旧天河规则 | `process_message` 使用经过行为测试的 `_select_pre_planner_route(user_text)`；不启用快速路径 | 增加一个内部选择接口；源码检查测试需同步维护 |
| Chainlit/MCP 分目录、分进程测试 | 两个项目均有顶层 `tools`；使用系统 Python，并把 TEMP/TMP 指向仓库可写临时目录 | 同进程可能测试了错误模块；错误临时目录会产生权限失败 |
| 九分区与下一级河流使用确定范围 | 九分区九个名称（含裸名称）直接按 `haihe_zone_9` 面范围统计，不查询河道走廊；下一级具体河流先查 `river_table_full` 两侧5公里，只有 `RiverNotFoundError` 才回退所属九分区 | 统一大清河与滦河等九分区回答口径；下一级河流仍保持精细沿线范围 |
| 使用真实米制5公里缓冲 | `ST_Buffer(…::geography, 5000)::geometry` 输出 WGS84；不采用 EPSG:3857 平面5000单位冒充地面5公里。依据控制器核对的 [PostGIS ST_Buffer 文档](https://postgis.net/docs/ST_Buffer.html) | SQL 离线断言需要更新；真实几何和投影效果仍须 PostGIS 内网验证 |
| 首轮与补充 Planner 都执行河流工具边界 | 防止后续全工具开放时改查天津；保留已取得工具结果后的完整答案，禁止重复调用；POI、混合、河网、水位等排除语义不变 | 识别条件过宽会阻挡混合请求，必须保留排除用例 |
| 支持窗口内的明确河系/流域使用统一 MCP 入口并复用既有核心 | 仅今天/明天/后天/今晚/未来N天单一窗口外层选 `query_river_rainfall_forecast`，内部调用 `river_system_forecast.get_river_system_rainfall_forecast`，保留九分区与逐日时间解析；不加载走廊 | 支持窗口的外层工具名与旧实现不同；其他时间形式仍由旧Planner/河系工具处理 |
| 统一入口只强制其已支持的单一窗口 | 不把一周、周末、明确日期、其他日内时段或混合时段强制到新解析器；主动路由和首轮/补充编排保留原调用，核心直接调用返回invalid_request；不新增周日期解析功能 | 部分周预报/无时间问题保留旧工具名和Planner参数处理；后续扩大统一日期支持需单独补测试实现 |
| 新区域风险 `no_data` 是 unavailable | `risk_forecast_no_data` 单独保留；仅服务有效且结果确实空才是 no_risk；不使用禁用的缺时次文案 | 缺预报周期会明确显示不可用，不能得到虚假的“无风险”结论 |
| POI 显式日期/星期标题做最小生产修复 | 日期类请求按实际月日显示，不随运行当天变成今天/明天；相对日期请求保留原格式 | 显式日期标题改变，必须同时测试相对“明天”标签 |
| 缺水情且无雨时不预测水库水位 | 标黄于桥也必须满足事实门控；缺水位或汛限资料时说明无法判断，不宣称水位平稳或当次山洪风险；有数据的提醒和表格保留 | 缺事实时一条水库提醒改变；不新增查询，不替换水位工具 |

原计划的 known-system-first、EPSG:3857 缓冲、no_data→no_risk、跨项目单进程测试命令属于已被替代的提案示例，不是最终契约。新区域风险与既有 POI 风险展示不是同一个状态机，不得以“简化”为由合并。

原计划“所有河流未来降雨问题统一入口”的概括亦已被上述时间窗口限制取代；例如“海河流域一周天气”“海河流域未来一周天气”仍可选择既有get_river_system_rainfall_forecast，不能将其视为漏路由后强改成今天。

## 4. 本轮自动化验证（2026-08-28）

### 2026-08-31 最终审查修复波

独立终审的六项 Important finding 已完成一次合并修复波；最终 scoped re-review 已逐项确认六项均关闭，未发现直接相关的 Critical、Important 或 Minor 回归，结论为 `Ready to merge: Yes`。业务契约更新如下：

- 河流/河系统一入口把 aware 北京时间转换为既有 resolver 使用的北京本地 naive 时间；公开 `start_time/end_time` 仍带 `+08:00`。严格模式只接受与请求起止完全一致且逐小时齐全的滚动网格；旧 resolver 默认钳制语义不变。
- 今天 00:00—次日00:00 若当前08时周期不能覆盖，会尝试可发现的前一20时周期；没有完整来源时返回不可用，不再把08—次日08统计标为自然日。
- 旧 POI 风险的 `no_data` 独立显示“风险预报资料不可用”，有效空集合仍显示“本次无风险”，不使用禁用文案。
- 区域三灾种聚合新增可选的逐灾种、逐起报覆盖元数据；只有所有请求周期均成功且为空才能报全窗无风险。部分空/缺失、风险+缺失、混合失败及全缺失分别保留 partial/risk/unavailable 事实。旧调用不请求覆盖元数据时返回结构不变。
- 支持地域与显式未知行政范围混合时整体拒绝并披露未知名称；可选 `regions` 不得遮蔽原问题冲突；多个支持地域仍逐个查询。
- 河流降雨与“风大/大风/风速”混合问题在首轮和补充轮均保留完整 Planner；纯降雨、既有“风力”及 POI 边界保持。

真实依赖环境最新全量：Chainlit **992 passed / 5 skipped**；MCP **579 passed / 20 skipped**。MCP 使用已有 `.venv-test` 的真实 FastMCP/networkx/rich，未使用 import stub；20 skip 为缺 scipy、GDAL 或样本 `.nc`。Chainlit 5 skip 为测试 UI 边界隔离，另有一条第三方 Pydantic 弃用 warning。这些结果不替代 GDAL/NetCDF、内网或 PC/手机验收。

| 检查 | 实际结果 | 说明 |
| --- | --- | --- |
| 修复前契约回归（主动路由＋提示词＋黄金路由） | 6 failed, 89 passed | 三个黄金ID缺路由模式；四项旧工具名断言；今日流域示例缺失 |
| 新双轨提示词契约修复前 | 2 failed, 58 passed | 两个实际提示词均缺今日流域示例，确认可重现 |
| 提示词＋黄金路由修复后 | 60 passed | 检查统一入口、原文、九分区、无天津替代和真实 data_source |
| 水库缺水情提醒 RED → GREEN | 5 failed → 5 passed | 无水位、空水位字典、仅有水位但无汛限三种缺事实输入；更新两项旧契约断言 |
| POI/路由/滚动回答回归 | 257 passed | 保留有雨/有水位风险建议、原表格与日期格式 |
| fix1未支持时段守卫 RED → GREEN | Chainlit 14 failed / 112 passed → 定向全绿；MCP 8 failed / 56 passed → 定向全绿 | 周/周末、明确日期、日内、混合、小时窗口；另补“今天起3天/大后天”两项失败后修复 |
| Chainlit 定向七文件（fix1后） | 515 passed | 天河目录/边界、主动路由、POI、编排、提示词、黄金路由 |
| Chainlit 全量（fix1后） | 874 passed, 8 skipped | 无失败；8项均因真实 Chainlit 包缺失的既有 skip |
| MCP 河流核心（fix1后） | 67 passed | 含今日流域核心复用、五类支持窗口日期、未支持时段不查替代数据 |
| MCP 定向六文件（fix1后） | 120 passed, 4 skipped | 河道/河系、区域风险、注册与滚动风险；4项既有 GDAL skip |
| MCP 原始全量 | 20 collection errors | 缺少 fastmcp/networkx；未执行完整测试主体 |
| MCP 额外 import-only 全量（fix1后） | 562 passed, 17 skipped, 2 failed | 仅导入替身；真实分析器缺 rich、真实 FastMCP 实例化均仍失败，未伪造框架 |

MCP 17 个既有 skip：9 项缺 GDAL，8 项缺样本 .nc 文件；本轮没有把失败改成 skip。两个剩余失败分别为 `TestForecastEvaluateCacheOrder.test_analyzer_can_build_report_without_charts`（rich）和 `test_poi_hazard_reminder_tool.py::test_register_exposes_tool`（真实 FastMCP 实例化）。注册单测中的 FakeMCP 只能证明本项目注册边界，不能替代这项真实框架测试。

### 可复现命令

在仓库根目录先设置环境；以下只写测试临时数据，不访问真实内外网服务：

```powershell
$acceptanceRoot = (Resolve-Path .).Path
$acceptancePython = 'D:\Python\Python313\python.exe'
$env:PYTHONUTF8 = '1'
$env:TEMP = Join-Path $acceptanceRoot '.tmp\pytest-river'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null

Set-Location (Join-Path $acceptanceRoot 'haiheliuyubaoyuagent-master\chainlitexam')
& $acceptancePython -m pytest tests -q -rs
& $acceptancePython -m pytest tests/test_tianhe_fixed_qa_catalog.py tests/test_tianhe_knowledge_route.py tests/test_active_tool_router.py tests/test_decision_weather_tool.py tests/test_message_orchestrator.py tests/test_prompts.py tests/test_meteo_qa_routing_contract.py -q

Set-Location (Join-Path $acceptanceRoot 'haiheliuyubaoyuagent-master\haihe-weather-analyzer-mcp')
& $acceptancePython -m pytest tests -q --tb=short
& $acceptancePython -m pytest tests/test_river_query_forecast.py tests/test_river_query_tool_registration.py tests/test_region_weather_risks.py tests/test_region_risk_tool_registration.py tests/test_river_system_rainfall_forecast.py tests/test_rolling_forecast_region_hazards.py -q -rs
```

只有为观察可独立运行的离线测试才执行以下额外命令。它在新进程内将 FastMCP 设为 `object`、networkx 设为空模块；没有任何框架功能替身，不修改代码或磁盘模块，不设置 PYTHONPATH，进程退出后失效。预期仍保留真实框架和 rich 两项失败：

```powershell
& $acceptancePython -c "import sys, types, pytest; fastmcp = types.ModuleType('fastmcp'); fastmcp.FastMCP = object; sys.modules['fastmcp'] = fastmcp; sys.modules['networkx'] = types.ModuleType('networkx'); raise SystemExit(pytest.main(['tests', '-q', '--tb=short', '-rs']))"
```

## 5. 简化复核与验收门禁

已复核三阶段20个改动文件中的生产逻辑、注册、fixture和测试；本轮不强行重构：

- 天河运行目录只有一个；测试中的51题独立期望快照必须保留，避免测试与实现同时误改却仍通过。
- 编排与主动路由共用公开河流谓词；MCP 的河名解析有名称提取职责，与保守路由判定不同，不能直接合并正则或跨进程引入依赖。
- 现有河流异常类别、覆盖率状态与区域风险三态分别承担不同业务语义，不合并。双轨提示词仍分别服务两个入口，不作结构性抽取。
- SQL 值参数化、表标识符安全组合、全量河网、geography 5000米缓冲、连接关闭、逐日统计和 data_source 透传已有离线契约检查；真实几何与栅格计算待依赖齐备环境验收。
- POI 复用既有表格和事实门控；无水位/汛限且无雨时“水位预计平稳”的遗留断言已按控制器裁定做最小修复。旧 POI `no_data` 已独立显示风险预报资料不可用，有效空结果仍为无风险；没有与新区域风险归一化器合并。

上线前仍需完成：

- [x] 独立全改动终审及六项修复后的限定复审；限定复审结论为 `Ready to merge: Yes`。
- [ ] 在真实 Chainlit、FastMCP、networkx、rich、GDAL 和样本齐全的环境重跑两项目全量；无新增失败。
- [ ] 逐题执行 T01—T51/H01—H05，记录接口响应、参数、时间、来源和最终正文。
- [ ] 断开/超时/缺资料/零像元/数据库错误等故障场景；不能输出无雨或无风险。
- [ ] 对照地图检查九分区名称直接按分区统计、泃河等下一级河流匹配 full_v6 两侧5公里及 fallback；人工核对逐日栅格无重复累计。
- [ ] 验证周/周末/明确日期/日内或混合时段保留原时间参数；统一核心不支持时明确invalid_request，不返回当天整日替代结果。
- [ ] 核对蓟州三灾种、静态隐患数量与动态风险、实际起报周期；未知覆盖不得宣称完整当天。
- [ ] 核对天津于桥水库/盘山景区定位、逐日雨量、注意事项来源；缺气象/水位事实不得作事实断言。
- [ ] PC/手机端核对56题代表性输出、长表、长文本、错误提示和可操作性；此轮未做浏览器/UI验收。
