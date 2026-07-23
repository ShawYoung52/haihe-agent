# 设计文档：用 Planner-Only 模式替换 Fast Path 关键词拦截

**日期:** 2026-07-09  
**状态:** 待审批  
**相关文件:** `chainlitexam/message_orchestrator.py`, `chainlitexam/fast_paths/`, `.chainlit/config.toml`

## 1. 背景与问题

当前 `message_orchestrator.py` 中维护了 19 个硬编码 fast path，覆盖降雨、河流、预警、决策天气、应急响应等场景。这些 fast path 通过关键词匹配前置拦截用户查询，绕过 planner LLM 直接生成回答。

**主要问题：**
- 关键词匹配粗糙，导致大量误判（如"本周末天气如何"被 DecisionWeather 拦截、"今天天气怎么样"被 POI 天气拦截）
- 19 个并行拦截器互相竞争，顺序和排除逻辑难以维护
- 新增查询类型需要新增 fast path，扩展性差
- 与 planner LLM 双轨并行，调试困难

## 2. 目标

- 消除关键词前置拦截导致的误判
- 简化消息处理流程
- 在保持回答质量的前提下，逐步减少对硬编码 fast path 的依赖
- 保留回滚能力，降低变更风险

## 3. 推荐方案

**总体策略：** 阶段 1 先关闭 fast path 调用，全部走 planner LLM + 工具循环；阶段 2 根据真实运行数据，把高频/复杂场景的格式化能力封装成 planner 可调用的技能/工具。

### 3.1 阶段 1：Feature Flag 关闭 Fast Path

采用环境变量 + 模块级常量控制，避免依赖 Chainlit 配置解析细节：

```python
# message_orchestrator.py 顶部
ENABLE_FAST_PATHS = os.environ.get("ENABLE_FAST_PATHS", "false").lower() in ("1", "true", "yes")
```

在 `process_message()` 中读取该标志：

```python
if ENABLE_FAST_PATHS:
    if await _try_rainfall_img_fast_path(...):
        return
    # ... 其他 fast path
```

当 `ENABLE_FAST_PATHS=false`（默认）时，跳过所有 `_try_*_fast_path()` 调用，直接进入 planner LLM 路径。

**代码改动范围：**
- 不删除 fast path 函数定义，只禁用其调用
- 保留 `fast_paths/` 目录及安装逻辑，但默认不安装
- 保留 `_show_business_reasoning`、`generate_fast_path_thinking` 等可被复用的组件
- 启动脚本默认不带 `ENABLE_FAST_PATHS=true`，便于回滚时临时开启

### 3.2 阶段 2：观测与收集问题 Case

关闭 fast path 后，运行 1-2 周，重点观察：

1. **响应延迟**：planner-only 是否明显变慢
2. **工具选择准确率**：planner 是否会选错工具或漏选工具
3. **输出稳定性**：图表、表格、特定格式化回答是否仍然稳定
4. **用户反馈**：哪些查询类型回答质量下降

收集方式：
- 增加结构化日志，记录每个查询的：用户输入、planner 选择的工具、最终回答长度、耗时、是否有用户反馈
- 定期抽样人工 review

### 3.3 阶段 3：把高频场景封装为 Planner 可调用的技能

对于阶段 2 中发现的问题，不恢复 fast path 前置拦截，而是把原 fast path 的格式化逻辑封装成：

1. **MCP 工具**：如果逻辑适合作为工具（如生成特定图表、查询特定数据集）
2. **专用函数工具**：通过 function calling 暴露给 planner
3. **增强的工具描述**：帮助 planner 更准确选择

例如：
- 原 `_try_rainfall_img_fast_path` → 增强 `query_rainfall_distribution_image` 工具描述
- 原 `_try_decision_weather_fast_path` → 新增 `query_decision_weather_for_poi` 工具
- 原 `_try_warning_fact_fast_path` → 增强 `get_effective_warning_info` 工具描述

这样 planner 仍然自主决策，但工具集更贴合业务场景。

## 4. 数据流

```
User Query
    │
    ▼
process_message()
    │
    ├── (旧) Fast Path 拦截链 [Feature Flag 关闭时跳过]
    │
    ▼
Planner LLM (Qwen3.6-27B)
    │
    ├── 选择工具 → _run_tool_round()
    │       ├── MCP 工具（天气、河流、预警等）
    │       └── 本地工具（ rainfall、river、warning 等）
    │
    ▼
生成最终回答
```

## 5. 兼容性 / 回滚

- `enable_fast_paths = true` 时，完全恢复现有行为
- fast path 函数和文件保留不动
- 删除 fast path 调用点只通过 `if enable_fast_paths:` 包裹，不物理删除

## 6. 测试策略

1. **回归测试**：运行现有 `tests/test_fast_paths.py`（fast path 启用时）、`test_thinking.py`、`test_reasoning_step.py` 等
2. **Feature Flag 测试**：分别测试 `enable_fast_paths = true` 和 `false` 两种模式
3. **真实查询样本测试**：准备 50-100 条历史真实查询，对比 fast path 开启/关闭时的输出差异
4. **A/B 测试**：对内部用户分组开启/关闭，收集反馈

## 7. 风险与应对

| 风险 | 影响 | 应对措施 |
|------|------|----------|
| Planner 选错工具 | 回答质量下降 | 阶段 2 收集 case，阶段 3 增强工具描述或新增工具 |
| 响应延迟增加 | 用户体验下降 | 监控耗时，必要时增加缓存或异步处理 |
| 特定格式化输出不稳定 | 图表/表格展示异常 | 把格式化逻辑封装为工具，让 planner 调用 |
| 同事不习惯新流程 | 协作摩擦 | 保留 feature flag，逐步推广 |

## 8. 实施里程碑

1. **M1**：添加 feature flag，默认关闭 fast path，保留回滚开关
2. **M2**：内部跑通回归测试 + 样本查询对比
3. **M3**：小范围灰度（1-2 个用户），收集反馈
4. **M4**：根据反馈增强工具/技能，决定是否物理删除 fast path 代码
5. **M5**：全面上线，文档更新

## 9. 决策点

本设计文档审批后，下一步将使用 `superpowers:writing-plans` 制定 M1 的详细实施计划。
