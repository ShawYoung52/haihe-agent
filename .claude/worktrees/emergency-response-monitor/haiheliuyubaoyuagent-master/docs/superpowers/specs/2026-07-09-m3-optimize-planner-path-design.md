# 设计文档：M3 优化 Planner-Only 路径

**日期:** 2026-07-09  
**状态:** 待审批  
**依赖:** M1（ENABLE_FAST_PATHS feature flag 已关闭 fast path）  
**相关文件:** `chainlitexam/prompts.py`, `chainlitexam/tools/decision_weather.py`, `chainlitexam/message_orchestrator.py`, `chainlitexam/chain_gzt.py`

## 1. 背景

M1 已经通过 `ENABLE_FAST_PATHS=false` 关闭了所有 fast path 前置拦截，用户查询全部走 planner LLM + 工具循环。当前 planner 使用的工具来自两部分：
- MCP SSE 服务器提供的天气/河流/预警等工具
- 本地工具 `chainlitexam/tools/rain_analysis.py` 提供的 `local_analyze_rainfall_by_time`

在关闭 fast path 后，最容易出现质量下降的场景是**决策天气 POI 查询**（如"梅江会展中心明天天气怎么样"）。原 fast path 需要执行：
1. LLM 抽槽识别地点和时间
2. POI 定位获取经纬度
3. 匹配最近代表站
4. 查询滚动预报
5. 按特定格式生成回答

这种多步复合查询很难通过 planner 直接选择多个工具并自行组合格式化，因此需要封装成一个单一工具。

## 2. 目标

- 把最容易误判的决策天气 POI 查询封装成 planner 可调用的单一工具
- 增强 `WEATHER_ASSISTANT_PROMPT` 中的工具选择指引
- 让 planner-only 模式在决策天气场景下至少达到原 fast path 的准确率和输出质量
- 保持新增工具可独立测试、可回滚

## 3. 方案

采用"方案 B：新增一个决策天气 POI 工具 + 增强提示词"。

### 3.1 新增工具 `query_decision_weather_for_poi`

**位置：** `chainlitexam/tools/decision_weather.py`

**职责：**
- 接收用户原始问题文本
- 使用 LLM 抽取地点、时间、问题类型等槽位
- 调用 `search_poi` 获取经纬度
- 匹配最近滚动预报代表站
- 调用 `query_rolling_forecast` 获取预报数据
- 返回格式化好的 Markdown 文本

**接口：**
```python
@tool
def query_decision_weather_for_poi(user_text: str) -> str:
    """
    回答关于具体地点、场馆、学校、医院、设施附近的未来天气或当前天气决策服务问题。
    适用于"XX地方明天天气怎么样""XX场馆未来24小时有雨吗""XX学校适合户外活动吗"等查询。
    内部会自动完成 POI 定位、代表站匹配、滚动预报查询和格式化回答。
    参数 user_text：用户原始问题文本。
    返回：已经格式化好的 Markdown 文本，可直接展示给用户。
    """
```

**实现策略：**
- 复用现有 `DecisionWeatherQAService` 中的 `_extract_slots`、`_normalize_slots`、POI 定位、代表站匹配、滚动预报调用、`_generate_answer` 逻辑
- 将原来依赖 `cl.Message`/`cl.Step` 的 UI 副作用剥离，工具只返回文本
- 错误情况返回业务化提示文本，不抛异常中断 planner

**注册：**
在 `chainlitexam/chain_gzt.py` 的 `on_chat_start` 中，把新工具合并进 tools 列表：
```python
from tools.decision_weather import build_decision_weather_tools

tools = await load_sse_tools()
tools = tools + build_external_skill_tools() + build_rain_analysis_tools() + build_decision_weather_tools()
```

### 3.2 增强 `WEATHER_ASSISTANT_PROMPT`

在 `chainlitexam/prompts.py` 的 `WEATHER_ASSISTANT_PROMPT` 中增加以下段落（放在"核心职责"部分）：

```markdown
### 5. 决策天气 POI 查询规范
- 当用户询问具体地点、场馆、学校、医院、设施、单位附近的未来天气或当前天气时，调用 `query_decision_weather_for_poi`。
- 典型问法："梅江会展中心明天天气怎么样""天津大学未来24小时会下雨吗""XX公园适合周末露营吗""XX机场现在能见度如何"。
- 该工具会自动完成 POI 定位、代表站匹配、滚动预报查询和格式化回答，不要自行拆分调用 search_poi 和 query_rolling_forecast。
- 如果用户问的是"天津天气""海河流域天气""西青区天气"等宽泛区域，不调用此工具，优先使用天津滚动预报或降雨工具。
```

同时，修正工具描述中的不准确之处：
- `query_rolling_forecast`：明确说明用于天津及其区级区域的未来综合天气（气温、风力、降水、能见度等），不是 POI 级查询
- `search_poi`：明确说明仅用于需要精确经纬度的场景，常规天气查询不要调用

### 3.3 剥离 UI 副作用

原 `DecisionWeatherQAService.try_handle` 中包含以下 UI 副作用：
- 发送/更新 `status_msg`
- 创建 `cl.Step(name="点位天气查询进度", type="tool")`
- 调用 `_emit_fast_path_result`

新工具内部不应有这些副作用。回答生成后，由 planner 路径的 `answer_chain` 或 `stream_text_to_message` 统一输出。

如果需要在思考过程中展示阶段，可以返回一个结构化结果（包含 `reasoning_stages` 和 `answer`），但目前 M3 只要求返回可直接展示的 Markdown 文本。

### 3.4 错误处理

新工具内部捕获所有异常，返回业务化文本：
- POI 未找到："未检索到"XX"的可用位置信息，请换一个更明确的地点名称。"
- 时间无法确定："请补充具体的查询日期或时段。"
- 滚动预报失败："当前无法获取该点位的滚动预报数据，请稍后重试。"
- 其他异常："点位天气查询遇到异常，请稍后重试。"

## 4. 数据流

```
User: "梅江会展中心明天天气怎么样"
    │
    ▼
Planner LLM
    │
    ├── 看到 WEATHER_ASSISTANT_PROMPT 中决策天气 POI 规范
    │
    ▼
调用 query_decision_weather_for_poi("梅江会展中心明天天气怎么样")
    │
    ├── LLM 抽槽 → location=梅江会展中心, target_start=..., target_end=...
    ├── search_poi → lon/lat
    ├── _nearest_decision_station → 代表站
    ├── query_rolling_forecast → 预报数据
    └── _generate_answer → Markdown 文本
    │
    ▼
返回 Markdown 给 planner
    │
    ▼
answer_chain / stream_text_to_message 输出给用户
```

## 5. 与现有 fast path 的关系

- `ENABLE_FAST_PATHS=false` 时，原 `_try_decision_weather_fast_path` 不再被调用
- 新工具 `query_decision_weather_for_poi` 由 planner 按需调用
- 原 `DecisionWeatherQAService` 可以保留一段时间作为参考实现，但不删除
- 如果新工具表现不佳，可以通过 `ENABLE_FAST_PATHS=true` 快速回退到原 fast path

## 6. 测试策略

1. **工具加载测试**：验证 `build_decision_weather_tools()` 返回的工具能被正确合并到 `tools` 列表
2. **单元测试**：用 mock 的 `search_poi` 和 `query_rolling_forecast` 测试 `query_decision_weather_for_poi` 的完整流程
3. **集成测试**：准备 10-20 条历史决策天气查询，验证 planner 选择 `query_decision_weather_for_poi` 的准确率
4. **对比测试**：与原 fast path 输出对比，确保格式化质量不下降

## 7. 风险与应对

| 风险 | 影响 | 应对措施 |
|------|------|----------|
| 工具内部 LLM 抽槽失败 | 回答质量下降 | 复用现有 `_extract_slots` 提示词，保持逻辑不变 |
| 工具返回 Markdown 过长 | 超出模型上下文 | 保留精简版格式化逻辑，必要时截断 |
| planner 仍然不选新工具 | 回到通用路径，效果差 | 在提示词中明确场景映射，必要时调整工具名/description |
| 与原 fast path 输出不一致 | 用户感知差异 | 做对比测试，调整格式化逻辑 |

## 8. 范围

M3 只包含：
- 新增 `chainlitexam/tools/decision_weather.py`
- 修改 `chainlitexam/prompts.py` 增强提示词
- 修改 `chainlitexam/chain_gzt.py` 注册新工具
- 新增/更新测试

M3 不包含：
- 删除原 `DecisionWeatherQAService` 或 `_try_decision_weather_fast_path`
- 封装其他 fast path 场景（降雨分布图、预警摘要等）
- 改动 MCP 服务器工具

## 9. 决策点

本设计文档审批后，下一步将使用 `superpowers:writing-plans` 制定详细实施计划。
