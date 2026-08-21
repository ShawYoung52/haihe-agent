# Safe Active Tool Routing and P2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 默认开启安全的主动工具过滤和证据提前收口，并以不可变 WKB 缓存完成河系边界 P2 优化，同时保证任何不确定场景自动回退现有完整 Planner。

**Architecture:** 运行时保留完整 Planner chain，新增 `ActiveToolRouter` 仅为高置信单域问题构造并缓存过滤 chain；首轮工具调用通过可选 `ToolRoundEvidence` 收集结构化结果，证据不足时第二轮永远使用完整 chain。河系边界只缓存数据库返回的 bytes WKB，每次请求重新物化 OGR Geometry。

**Tech Stack:** Python 3.11+、LangChain runnable、Chainlit、pytest、PostgreSQL/PostGIS、GDAL/OGR（可选测试）

**Spec:** `docs/superpowers/specs/2026-08-21-safe-active-tool-routing-p2-design.md`

## Global Constraints

- `ENABLE_ACTIVE_TOOL_FILTER` 和 `ENABLE_EVIDENCE_EARLY_FINALIZE` 默认 `true`，显式 `false` 必须恢复原行为。
- 完整 Planner 自动回退不可关闭；第二轮 Planner 不得继续使用过滤 chain。
- 不开启 `ENABLE_FAST_PATHS`，不改变 fast path 顺序、认证、角色、应急阈值、河网上下游或面雨量规则。
- 不缓存 OGR Geometry、GDAL Dataset、数据库连接、用户会话或工具实例。
- 不访问内网服务做单元测试。
- 工作区已有未提交修改；不创建提交、不覆盖无关文件，每个任务用定向测试和 `git diff --check` 做检查点。

---

### Task 1: 主动工具路由纯策略和黄金问题召回

**Files:**
- Create: `haiheliuyubaoyuagent-master/chainlitexam/tools/active_tool_router.py`
- Modify: `haiheliuyubaoyuagent-master/chainlitexam/tools/tool_candidate_index.py`
- Create: `haiheliuyubaoyuagent-master/chainlitexam/tests/test_active_tool_router.py`
- Modify: `haiheliuyubaoyuagent-master/chainlitexam/tests/test_tool_candidate_index.py`

**Interfaces:**
- Consumes: LangChain 工具对象的 `name`；`ToolCandidateIndex` 仅用于独立 shadow 召回观测，不进入主动路由白名单。
- Produces: `ToolRouteDecision`、`ActiveToolRouter.select()`、`ActiveToolRouter.chain_for()`、`ActiveToolRouter.full_chain`。

- [ ] **Step 1: 写路由红测**

覆盖以下真实行为：

```python
decision = router.select("子牙河现在水位多高")
assert decision.mode == "filtered"
assert "query_water_level" in decision.tool_names
assert "query_rolling_forecast" not in decision.tool_names

assert router.select("暴雨洪水多久到达下游").mode == "full"
assert router.select("海河流域当前防汛应急响应级别是多少").mode == "full"
assert router.select("查水位并分析暴雨影响河流").mode == "full"
```

读取 `meteo_qa_cases.json`，仅对 `decision.mode == "filtered"` 断言 required 全包含、forbidden 全排除。再验证相同候选集合只调用一次 `build_chain`，容量超过上限淘汰最旧 chain。

- [ ] **Step 2: 运行红测**

Run: `python -m pytest tests/test_active_tool_router.py tests/test_tool_candidate_index.py -q`（工作目录 `chainlitexam`）  
Expected: FAIL，原因是 `active_tool_router` 不存在或候选规则不满足黄金问题。

- [ ] **Step 3: 实现最小路由策略**

实现：

```python
@dataclass(frozen=True)
class ToolRouteDecision:
    mode: str
    query_type: str
    tool_names: tuple[str, ...]
    requires_tool: bool
    reason: str
```

域识别顺序必须从高风险到低风险：unsafe/GIS → warning → water_level → rain → current → forecast → rag → unknown。命中两个安全域视为 mixed，返回 full。每个安全单域只绑定固定最小工具白名单；索引评分不合入首轮 Planner，最低工具缺失返回 full。

`ActiveToolRouter.chain_for()` 对 full 直接返回完整 chain；filtered 使用按名称排序后的 tuple 作 LRU key，调用注入的 `build_chain(list[tool])`。

- [ ] **Step 4: 运行绿测并检查 diff**

Run: `python -m pytest tests/test_active_tool_router.py tests/test_tool_candidate_index.py -q`  
Run: `git diff --check -- haiheliuyubaoyuagent-master/chainlitexam/tools/active_tool_router.py haiheliuyubaoyuagent-master/chainlitexam/tools/tool_candidate_index.py`

---

### Task 2: 结构化工具证据和完整性策略

**Files:**
- Create: `haiheliuyubaoyuagent-master/chainlitexam/tools/tool_round_evidence.py`
- Modify: `haiheliuyubaoyuagent-master/chainlitexam/tools/meteo_evidence.py`
- Create: `haiheliuyubaoyuagent-master/chainlitexam/tests/test_tool_round_evidence.py`
- Modify: `haiheliuyubaoyuagent-master/chainlitexam/tests/test_meteo_evidence.py`

**Interfaces:**
- Consumes: 工具名、`ok/error/missing` 状态、`_unwrap_tool_result` 后的 payload。
- Produces: `ToolEvidenceItem`、`ToolRoundEvidence.record/items_for/has_errors_for`、兼容两种输入的 `is_evidence_complete()`。

- [ ] **Step 1: 写证据红测**

```python
evidence = ToolRoundEvidence()
evidence.record("query_water_level", "ok", {
    "records": [{"water_level_m": 3.2}], "count": 1,
})
assert is_evidence_complete("water_level", evidence.items) is True

evidence.record("query_basin_areal_rainfall", "error", {"error": "timeout"})
assert evidence.has_errors_for("rain") is True
assert is_evidence_complete("rain", evidence.items_for("rain")) is False
```

另测 current 有时次且有统计为完整、rain 非空无 error 为完整、空 records/错误/unsafe/unknown 为 False，并保持旧 bundle 测试不变。

- [ ] **Step 2: 运行红测**

Run: `python -m pytest tests/test_tool_round_evidence.py tests/test_meteo_evidence.py -q`  
Expected: FAIL，原因是新类型不存在或 `meteo_evidence` 不识别 payload。

- [ ] **Step 3: 实现证据对象和兼容适配**

`ToolRoundEvidence` 内部只保存 list；`record()` 把 payload 原样保存但不保存工具实例。工具到 query_type 的映射集中在新模块并保持 warning 优先级。`is_evidence_complete` 先读取 `payload`，没有时兼容旧 `bundle`。

- [ ] **Step 4: 运行绿测**

Run: `python -m pytest tests/test_tool_round_evidence.py tests/test_meteo_evidence.py -q`

---

### Task 3: 运行时双 Planner 接线和完整回退

**Files:**
- Modify: `haiheliuyubaoyuagent-master/chainlitexam/chain_gzt.py`
- Modify: `haiheliuyubaoyuagent-master/chainlitexam/message_orchestrator.py`
- Create: `haiheliuyubaoyuagent-master/chainlitexam/tests/test_active_tool_orchestration.py`
- Modify: `haiheliuyubaoyuagent-master/chainlitexam/tests/test_http_perf_opt.py`

**Interfaces:**
- Consumes: `ActiveToolRouter`、现有完整 `planner_chain`、`ToolRoundEvidence`。
- Produces: runtime/callbacks 中的 `active_tool_router`；首轮 filtered、第二轮 full 的执行契约。

- [ ] **Step 1: 写接线红测**

把接线决策提取为可单测辅助函数：

```python
assert _active_tool_filter_enabled({}) is True
assert _evidence_early_finalize_enabled({}) is True
assert _needs_full_planner_fallback(decision, planner_msg_without_tools, None) == "missing_tool_call"
assert _planner_chain_after_tool_round(router, decision) is router.full_chain
```

静态契约测试还需确认 `_run_tool_round(..., evidence_sink=round_evidence)`、第二轮调用使用 `full_planner_chain`、显式 false 时首轮仍用传入的完整 `planner_chain`。

- [ ] **Step 2: 运行红测**

Run: `python -m pytest tests/test_active_tool_orchestration.py tests/test_http_perf_opt.py -q`  
Expected: FAIL，新辅助函数/runtime 字段不存在。

- [ ] **Step 3: 构建共享 ActiveToolRouter**

在 `_build_orchestrator_runtime()` 保留：

```python
full_planner_chain = planner_template | planner_llm.bind_tools(tools)
active_tool_router = ActiveToolRouter(
    tools=tools,
    full_chain=full_planner_chain,
    build_chain=lambda selected: planner_template | planner_llm.bind_tools(selected),
    candidate_index=tool_candidate_index,
)
```

runtime 返回 `planner_chain`（兼容，指向 full）和 `active_tool_router`；callbacks 注入 router。运行时缓存键补充三个 active filter 配置项和 evidence flag。

- [ ] **Step 4: 接入首轮和硬回退**

`process_message` 在通用 Planner 首轮前选择 decision/chain。规则强制路由不使用 router。若 filtered 且需要工具但首轮无调用，立刻用 full chain 重跑首轮；router 异常捕获后使用 full。

工具循环的第二次 Planner 固定：

```python
full_planner_chain = getattr(router, "full_chain", planner_chain)
planner_msg = await callback(full_planner_chain, {"messages": messages}, reasoning)
```

- [ ] **Step 5: 运行绿测和既有 HTTP/LLM 测试**

Run: `python -m pytest tests/test_active_tool_orchestration.py tests/test_http_perf_opt.py tests/test_build_chat_llm.py -q`

---

### Task 4: 默认证据提前收口和观测字段

**Files:**
- Modify: `haiheliuyubaoyuagent-master/chainlitexam/message_orchestrator.py`
- Modify: `haiheliuyubaoyuagent-master/chainlitexam/timing_logger.py`
- Modify: `haiheliuyubaoyuagent-master/chainlitexam/tests/test_timing_logger.py`
- Modify: `haiheliuyubaoyuagent-master/chainlitexam/tests/test_active_tool_orchestration.py`

**Interfaces:**
- Consumes: `round_evidence`、query_type、现有 Answer callbacks 和 `_assemble_tool_observations_fallback`。
- Produces: 安全域提前 Answer；`TimingContext` 七个路由/回退字段。

- [ ] **Step 1: 写提前收口红测**

测试纯决策函数：

```python
assert _should_early_finalize("water_level", complete_water_evidence, emergency=False) is True
assert _should_early_finalize("rain", failed_rain_evidence, emergency=False) is False
assert _should_early_finalize("water_level", complete_water_evidence, emergency=True) is False
assert _should_early_finalize("unknown", complete_water_evidence, emergency=False) is False
```

验证 env 未设置时默认 true，显式 false 时只写 evidence timing，不进入提前 Answer。

- [ ] **Step 2: 运行红测**

Run: `python -m pytest tests/test_active_tool_orchestration.py tests/test_timing_logger.py -q`

- [ ] **Step 3: 实现提前 Answer 的单一辅助函数**

提取 `_finalize_from_complete_evidence(...)`，复用现有 `_compress_messages`、Answer timeout、输出清洗、followup、thinking summary 和消息落库。Answer 异常时调用 `_assemble_tool_observations_fallback`；无有效 fallback 才恢复完整 Planner，不吞错误。

warning、decision_poi、rolling Fix A 和 forced final 分支保持在新逻辑之前。

- [ ] **Step 4: 增加 TimingContext 字段并绿测**

`as_dict()` 输出 `tool_filter_mode/tool_candidates_count/tool_filter_reason/full_planner_fallback/full_planner_fallback_reason/evidence_early_finalize/planner_rounds_saved`，默认值不影响旧日志消费者。

Run: `python -m pytest tests/test_active_tool_orchestration.py tests/test_timing_logger.py tests/test_meteo_evidence.py -q`

---

### Task 5: 河系边界不可变 WKB 缓存

**Files:**
- Modify: `haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/river_system_forecast.py`
- Modify: `haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/tests/test_river_system_rainfall_forecast.py`

**Interfaces:**
- Consumes: PostgreSQL boundary rows。
- Produces: `_query_zone_boundary_rows()`、`_materialize_zone_boundaries()`；保持 `_load_zone_boundaries_from_db()` 原签名和返回结构。

- [ ] **Step 1: 写缓存红测**

测试同键查询数据库一次、返回的 `geometry` 不是同一对象；不同 zone/config 不共享；空结果/异常不缓存；TTL=0 重查；容量 2 时第三键淘汰第一键。GDAL 不可用时用注入的 `geometry_factory` 或 monkeypatch `_materialize_zone_boundaries` 测缓存，不把整个缓存测试 skip。

- [ ] **Step 2: 运行红测**

Run: `python -m pytest tests/test_river_system_rainfall_forecast.py -q`（工作目录 MCP）  
Expected: 新缓存断言失败。

- [ ] **Step 3: 实现成功结果 TTL/LRU 缓存**

使用 `OrderedDict + threading.Lock`，缓存 tuple 中的 bytes WKB；`RIVER_SYSTEM_BOUNDARY_CACHE_TTL=3600`、`RIVER_SYSTEM_BOUNDARY_CACHE_MAX_SIZE=32` 安全解析。TTL=0 不读不写。物化阶段每次调用 `ogr.CreateGeometryFromWkb(bytes(wkb))`。

- [ ] **Step 4: 运行绿测**

Run: `python -m pytest tests/test_river_system_rainfall_forecast.py -q`

---

### Task 6: 文档与合并回归

**Files:**
- Modify: `haiheliuyubaoyuagent-master/CLAUDE.md`
- Modify: `docs/performance/baseline-before-optimization.md`

**Interfaces:**
- Consumes: 完成后的默认值、环境变量、测试数字。
- Produces: 当前行为、回滚方式和生产验证命令。

- [ ] **Step 1: 更新当前状态文档**

记录两个默认 true 开关、不可关闭的完整回退、P2 WKB 缓存、`[PERF]` 新字段；明确 fast paths 仍关闭。

- [ ] **Step 2: 运行定向合并回归**

Chainlit 工作目录：

```powershell
python -m pytest tests/test_active_tool_router.py tests/test_tool_candidate_index.py tests/test_tool_round_evidence.py tests/test_meteo_evidence.py tests/test_active_tool_orchestration.py tests/test_timing_logger.py tests/test_http_perf_opt.py tests/test_build_chat_llm.py tests/test_forecast_evaluate_fast_path.py tests/test_forecast_evaluate_full.py -q
```

MCP 工作目录：

```powershell
python -m pytest tests/test_river_system_rainfall_forecast.py tests/test_forecast_evaluate_cache_order.py tests/test_emergency_static_metadata_cache.py tests/test_ttl_cache_helper.py tests/test_last_month_static_mapping_cache.py -q
```

- [ ] **Step 3: 语法、格式和工作区检查**

Run: `python -m py_compile` 覆盖所有新增/修改 Python 文件。  
Run: `git diff --check`。  
Run: `git status --short`，确认用户已有 `.claude/` 未改动。

- [ ] **Step 4: 生产 smoke test 指引**

不在当前环境访问内网。交付时提供五类问题：一般未来天气、水位、面雨量、应急响应、混合问法；用 `[PERF]` 对比候选数、fallback、planner rounds、p50/p95，并用 `ENABLE_ACTIVE_TOOL_FILTER=false` / `ENABLE_EVIDENCE_EARLY_FINALIZE=false` 验证回滚。
