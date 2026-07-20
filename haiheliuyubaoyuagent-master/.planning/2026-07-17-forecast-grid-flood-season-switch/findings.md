# Findings: 预报网格数据汛期切换

## 现有数据源核查

### 滚动预报（query_rolling_forecast）
- **文件**：`haihe-weather-analyzer-mcp/haihe_mcp_tools.py:2800`
- **配置**：`ROLLING_FORECAST_API_URL`（env）、`ROLLING_FORECAST_COORDS`（11 个天津区县坐标）
- **要素**：`ROLLING_FORECAST_ELEMENTS` = WEA/TMAX/TMIN/EDA/RHMAX/RHMIN/TP1H
- **调用方**：
  - `chainlitexam/message_orchestrator.py` — weekly forecast fast path、decision weather
  - `chainlitexam/tools/decision_weather.py` — decision weather tool
  - `chainlitexam/tools/decision_weather_core.py` — 自带 COORDS（同样 11 点）
  - `chainlitexam/prompts.py:318` — LLM prompt 指定天津预报必须调用此工具
- **范围**：仅天津 11 区县，点-based，非网格
- **结论**：**不是项目经理所指的"预报网格数据"接口**

### EC（ECMWF AIFS）
- **文件**：
  - `haihe-weather-analyzer-mcp/haihe_mcp_tools.py` — `collect_ec_forecast_precip_files`、`ec_forecast_precip_files_by_horizon`
  - `haihe-weather-analyzer-mcp/analyzers/RainfallAnalyzer.py` — 面雨量分析
  - `haihe-weather-analyzer-mcp/draw_haihe_precip_product.py` — 降水产品图绘制
  - `haihe-weather-analyzer-mcp/emergency_api.py` — 应急响应
- **配置**：`EC_OUTPUT_PATH`、`EC_AIFS_ROOT`（env）
- **数据**：GRIB2/TIF 网格，12/24/36/48/60/72h 累计降水
- **范围**：海河流域网格
- **文件路径**：`/home/ev/data/ec/EC_AIFS/{年}/{YYYYMMDD}/*.grib2`

## 汛期判定（已确认）
- 规则：6 月 1 日 00:00 - 9 月 30 日 23:59 为汛期
- 切换方式：按当前系统日期自动判断，无需环境变量
- 实现建议：`_is_flood_season(now: datetime | None = None) -> bool`，可注入时间方便单测

## 待澄清问题（阻塞）
1. 项目经理所指的"滚动预报网格接口"是哪个？需接口文档
2. 该接口数据类型（网格/站点）、覆盖范围（海河/天津/其他）
3. 切换层级：数据采集层（文件读取前选择）还是产品生成层（分析器内选择）
4. 汛期内 EC 是否完全停用
5. 要素/时效/格式是否与 EC 对齐，是否需要适配层

## 风险
- 若新接口要素与 EC 不一致（如只有降水、无其他要素），切换后部分产品功能可能缺失
- 若新接口是站点数据而非网格，面雨量/产品图等依赖网格的功能需要插值或降级
- 汛期切换时机：若汛期开始日 EC 已缓存但需切滚动预报，缓存失效策略需设计