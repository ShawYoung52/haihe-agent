# 安全主动工具路由与 P2 架构优化设计

**日期：** 2026-08-21  
**范围：** `haiheliuyubaoyuagent-master/chainlitexam/`、`haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/river_system_forecast.py`  
**状态：** 已完成自审，待用户书面确认后进入实施计划

## 1. 背景

当前 Planner 在运行时绑定全部 MCP、本地和外部工具。`ToolCandidateIndex` 只记录 Top-5/8/12 影子召回，未参与真实 Planner；`is_evidence_complete` 也只写 shadow 日志，除滚动预报 Fix A 和预警专用收口外，证据完整后仍可能调用第二轮 Planner。

本设计默认开启主动工具过滤和证据提前收口，但把“不破坏既有问答”置于性能收益之前：任何不确定、失败、混合或复杂场景必须自动回到现有完整 Planner 链路。完整链路回退是硬编码安全护栏，不提供关闭开关。

## 2. 目标

1. 高置信单域问题的首轮 Planner 只绑定最多 12 个候选工具，减少工具描述 token 和工具选择耗时。
2. 工具证据完整时跳过第二轮 Planner，直接进入现有 Answer 链路。
3. 候选召回不确定、首轮未选工具、工具失败或证据不完整时，自动使用完整 Planner 补查。
4. 保持认证、角色权限、Fast Path 顺序、应急响应阈值、河网上下游语义、子流域天气规则、面雨量规则和现有回答装配器不变。
5. 把主动路由和证据收集从 `message_orchestrator.py` 中拆成独立、可测试的纯策略组件。
6. 缓存河系分区边界的不可变 WKB 数据，避免每次预报查询都访问 PostGIS；每次使用时重新构造 OGR Geometry，杜绝跨线程共享可变 GDAL/OGR 对象。

## 3. 非目标

- 不把系统拆成天气、河流、预警等多个独立 Agent。
- 不开启全局 `ENABLE_FAST_PATHS`，不改变既有 Fast Path 命中顺序。
- 不修改 Prompt 中的业务规则、应急阈值或工具参数语义。
- 不缓存 Raster、GDAL Dataset、OGR Geometry、数据库连接或用户会话对象。
- 不对未知查询强行分类；未知和混合意图直接使用完整 Planner。

## 4. 总体架构

```text
用户问题
  ↓
ActiveToolRouter.select
  ├─ unsafe / mixed / unknown ─────────────→ 完整 Planner
  └─ safe single-domain
        ↓
     候选 Planner（≤ ACTIVE_TOOL_LIMIT）
        ↓
     首轮工具执行 + ToolRoundEvidence
        ├─ 首轮无工具/工具失败/证据不完整 ─→ 完整 Planner 补查
        └─ 证据完整
              ↓
           Answer 链路（跳过第二轮 Planner）
```

现有规则强制路由、滚动预报 Fix A、预警专用回答、点位决策天气强制成品回答和应急综合逻辑的优先级高于本设计。本设计只处理原本会进入通用 Planner 循环的请求。

## 5. 主动工具路由

### 5.1 新模块和接口

新增 `chainlitexam/tools/active_tool_router.py`：

```python
@dataclass(frozen=True)
class ToolRouteDecision:
    mode: str                    # "full" | "filtered"
    query_type: str              # forecast/current/warning/water_level/rain/unknown
    tool_names: tuple[str, ...]
    requires_tool: bool
    reason: str

class ActiveToolRouter:
    def select(self, user_text: str, limit: int = 12) -> ToolRouteDecision: ...
    def chain_for(self, decision: ToolRouteDecision): ...
```

构造函数接收完整工具列表、完整 Planner chain 和 `build_chain(candidate_tools)` 回调。过滤链按规范化工具名元组放入容量 64 的进程级 LRU；同一候选集合不重复 `bind_tools`。

### 5.2 安全域与绕过域

允许过滤的高置信单域：

- 一般未来天气/气温/风/能见度：滚动预报及点位决策天气候选。
- 当前天气实况：当前实况候选。
- 当前/历史/国家预警：对应预警候选。
- 河道、水库、闸坝水位：水位候选。
- 流域面雨量：面雨量候选。
- 知识库明确问法：RAG 候选。

强制完整 Planner：

- 防汛应急响应、暴雨影响河流、河网上下游、河网绘图、行政区/GIS/图片生成。
- 同时命中两个及以上业务域的混合问题。
- 无法高置信分类的问题。
- 候选集中缺少该域的最低必需工具。

过滤工具采用**域级固定最小白名单**，不再把 `ToolCandidateIndex` 的评分结果合入首轮 Planner。候选索引只保留 shadow 召回观测，用于评估工具描述和黄金问题覆盖率；这项安全简化避免同域宽泛候选改变专用工具语义。不能把 `query_rolling_forecast` 无条件加入所有查询，否则会破坏面雨量、点位天气和水位问题的 forbidden 契约。

### 5.3 完整链路回退

过滤 Planner 后满足任一条件，立即调用完整 Planner：

1. `requires_tool=True` 但 Planner 没有产生 tool call。
2. Planner 产生候选集合之外的工具名或工具不存在。
3. 首轮目标域工具全部失败。
4. 结构化证据不完整，需要补充查询。
5. 运行时路由组件发生任何异常。

第二轮 Planner 一律使用完整 Planner chain，不继续使用过滤 chain。这样首轮过滤最多影响性能，不会永久限制后续补查能力。

### 5.4 配置

- `ENABLE_ACTIVE_TOOL_FILTER=true`：默认开启；显式 `false` 完全恢复当前完整 Planner 行为。
- `ACTIVE_TOOL_LIMIT=12`：保留为兼容的安全上限（范围 5～20，非法值回落 12）；当前固定最小白名单均小于该上限，不用于扩展候选。
- `ACTIVE_TOOL_CHAIN_CACHE_MAX_SIZE=64`：过滤 chain LRU 容量，非法值回落 64。

完整链路回退没有环境变量，始终启用。

## 6. 结构化工具证据

新增 `chainlitexam/tools/tool_round_evidence.py`：

```python
@dataclass(frozen=True)
class ToolEvidenceItem:
    tool_name: str
    status: str                 # "ok" | "error" | "missing"
    payload: object

class ToolRoundEvidence:
    def record(self, tool_name: str, status: str, payload: object) -> None: ...
    def items_for(self, query_type: str) -> list[ToolEvidenceItem]: ...
    def has_errors_for(self, query_type: str) -> bool: ...
```

`_run_tool_round` 新增可选参数 `evidence_sink=None`，既有五元组返回值保持不变，避免破坏现有调用和测试。工具成功后记录 `_unwrap_tool_result(observation)`；工具不存在或异常时记录失败。证据对象不保存 Chainlit Step、连接、工具实例或用户会话。

## 7. 证据提前收口

`tools/meteo_evidence.py` 扩展为读取 `ToolEvidenceItem`，同时兼容旧的 `{"tool_name", "bundle"}` shadow 输入。

安全完整性规则：

- `forecast`：现有 rolling bundle 含非空 `code_section`；继续复用 Fix A。
- `current`：payload 为 dict、无 `error`，且含实况时间和有效统计/records。
- `water_level`：payload 为 dict、无 `error`，`records` 为非空列表且至少一项存在水位字段。
- `rain`：payload 为非空列表，且不存在顶层 `error` 项。
- `warning`：继续走现有 `warning_workflow.finalize_warning_answer`，不改为空预警的业务语义。
- `decision_poi`：继续使用现有 `forced_final_text`，不进入通用证据提前收口。
- 应急、河网、影响分析、混合和未知类型：始终不提前收口。

提前收口仍调用现有 Answer chain，不用模板拼接替代最终业务回答；Answer 失败时复用 `_assemble_tool_observations_fallback`。因此节省的是第二轮 Planner，不改变 Answer Prompt、历史上下文和输出清洗逻辑。

配置：

- `ENABLE_EVIDENCE_EARLY_FINALIZE=true`：默认开启；显式 `false` 只记录 shadow，不改变 Planner 循环。

## 8. 河系边界缓存

`river_system_forecast.py` 把数据库读取和 OGR 构造拆开：

```python
def _query_zone_boundary_rows(...) -> tuple[dict, ...]: ...
def _materialize_zone_boundaries(rows: tuple[dict, ...]) -> list[dict]: ...
```

缓存对象只包含 `zone_name`、`zone_code`、`srid` 和 `bytes(geom_wkb)`。缓存键包含数据库 host/port/dbname/schema、分区表、zone name；仅成功非空结果写缓存。每次调用 `_load_zone_boundaries_from_db` 都从 WKB 新建 OGR Geometry，调用方拿不到共享可变对象。

- `RIVER_SYSTEM_BOUNDARY_CACHE_TTL=3600`
- `RIVER_SYSTEM_BOUNDARY_CACHE_MAX_SIZE=32`

TTL 设为 0 时禁用缓存；数据库失败和空边界不缓存。

## 9. 可观测性

`TimingContext` 增加：

- `tool_filter_mode`
- `tool_candidates_count`
- `tool_filter_reason`
- `full_planner_fallback`
- `full_planner_fallback_reason`
- `evidence_early_finalize`
- `planner_rounds_saved`

日志继续使用 `[PERF]` JSON Lines，不记录 Prompt、工具参数、内网地址或用户敏感内容。

## 10. 测试与验收

### 10.1 纯策略测试

- 安全单域返回 filtered；应急、河网、影响、混合、未知返回 full。
- limit 非法时回落 12；候选 chain LRU 有界。
- 路由异常返回 full，不向请求抛错。

### 10.2 黄金问答召回

读取 `tests/fixtures/meteo_qa_cases.json`：

- 对进入 filtered 模式的 case，`required` 工具必须全部在候选中。
- 对进入 filtered 模式的 case，`forbidden` 工具不得出现在候选中；full 模式仍由既有 Planner 约束，不把完整工具表误判为路由新增。
- required 为空或复杂域允许直接 full，不强制过滤。

### 10.3 回退与提前收口

- 过滤 Planner 无 tool call → 完整 Planner 被调用。
- 工具失败/证据不足 → 完整 Planner 被调用。
- 水位、面雨量有效 payload → 第二轮 Planner 不调用，Answer 正常调用。
- 应急/多工具 → 即使某一结果完整也不提前收口。
- 两个开关显式 false → 行为与修改前一致。

### 10.4 GIS 缓存

- 同键两次查询只访问一次数据库，但返回两个不同 OGR Geometry 实例。
- 配置、表、zone name 不同不共享。
- 空结果、异常、TTL=0 不缓存。
- 容量超过上限淘汰最旧项。

### 10.5 回归门槛

- 现有 forecast evaluate、timing、HTTP、fast/full forecast、预警、决策天气、河系预报测试无新增失败。
- `ENABLE_ACTIVE_TOOL_FILTER=false` 且 `ENABLE_EVIDENCE_EARLY_FINALIZE=false` 的回滚测试必须通过。
- 不连接内网的单元测试全部通过后，才允许进行内网 smoke test。

## 11. 上线与回滚

上线默认开启两个功能，但保留每请求自动完整回退。若生产出现异常：

1. 先设置 `ENABLE_ACTIVE_TOOL_FILTER=false`，恢复完整工具绑定。
2. 若仍有答案提前结束问题，设置 `ENABLE_EVIDENCE_EARLY_FINALIZE=false`。
3. GIS 缓存可设置 `RIVER_SYSTEM_BOUNDARY_CACHE_TTL=0` 独立关闭。

回滚均不需要修改数据库、清理缓存文件或改变 MCP 配置；进程重启后立即生效。
