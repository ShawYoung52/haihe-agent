# 客户交付包整理设计（qa-agent-delivery）

> 日期：2026-08-07
> 目标：从杂乱的开发仓库中提取问答智能体相关代码，整理成一个干净、可浏览、能讲清"问答为什么慢"的客户交付包。
> 原始仓库：`D:\PythonProject\haiheliuyubaoyuagent-master`（源代码**不做任何改动**）。

## 1. 背景与目标

客户要求提供智能体项目代码，核心目的是**通过代码理解问答为什么慢**。现状是开发目录很乱：

- 根目录混有无关项目（`hhlyqyxt-master` 牵引预报系统、`Wechat*` 微信相关代码、`chainlit.md`、`task_plan.md` 等开发残留）。
- 问答智能体代码散在三处：
  - `forecast_evaluate 2/forecast_evaluate/`（预报检验评估工具包，目录名带空格 + `__MACOSX/` 垃圾）
  - `haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/`（MCP 后端）
  - `haiheliuyubaoyuagent-master/chainlitexam/`（Chainlit 前端问答智能体）

## 2. 用户已确认的决策

| 决策点 | 选择 |
|--------|------|
| 交付目的 | 客户看代码、理解问答为什么慢 |
| 性能说明 | 新增性能分析文档（推荐） |
| 目录组织 | 重新规划目录（扁平化） |
| 无关目录 | 全部排除（hhlyqyxt-master、Wechat*、根目录零散文件） |
| 敏感信息 | 全部保留（不脱敏） |
| 交付形式 | 新文件夹 + zip 打包 |

## 3. 交付包目录结构

新文件夹：`qa-agent-delivery/`（放在原始仓库根目录下）

```
qa-agent-delivery/
├── README.md                       # 交付总览：模块关系、如何看代码、如何定位性能
├── docs/
│   ├── architecture.md             # 三层架构：Chainlit 前端 / MCP 后端 / 检验评估
│   └── performance/
│       ├── 01-why-qa-is-slow.md    # ★ 核心：性能分析（回答"为什么慢"）
│       ├── 02-optimizations-done.md# 已做优化清单（LLM预热/缓存/Prompt拆分/并行工具/快速路径）
│       ├── 03-call-chain.md        # 迁移现有 current-qa-call-chain.md
│       └── 04-perf-observability.md# timing_logger 埋点 + perf_stats 怎么跑
├── chainlitexam/                   # 前端问答智能体（复制源码）
├── haihe-weather-analyzer-mcp/     # MCP 后端（复制源码）
└── forecast_evaluate/              # 预报检验评估包（去掉 " 2" 和 __MACOSX）
                                    #   注意：其下保持原始内部结构 forecast_evaluate/scripts/
```

## 4. 复制/排除清单

### 4.1 复制进交付包（源码）

- `forecast_evaluate 2/forecast_evaluate/**` → `forecast_evaluate/**`（排除 `__MACOSX/`）
- `haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/**` → `haihe-weather-analyzer-mcp/**`
- `haiheliuyubaoyuagent-master/chainlitexam/**` → `chainlitexam/**`

### 4.2 排除清单（不进入交付包）

| 目录/文件 | 原因 |
|-----------|------|
| `hhlyqyxt-master/` | 另一个项目（牵引预报系统） |
| `WechatGatewayClient/ WechatLinkTest/ WechatPipeline/ WechatRPA/` | 微信相关，非核心 |
| `forecast_evaluate 2/__MACOSX/` | macOS 垃圾文件 |
| 所有 `__pycache__/`、`.pytest_cache/` | 缓存 |
| 所有 `.idea/` | IDE 配置 |
| `.claude/worktrees/` | git worktree 工作区 |
| `.venv/`、`.git/` | 环境/版本库 |
| `chainlitexam/.files/` | Chainlit 运行时会话文件（134K 临时文件） |
| `chainlitexam/` 下的 diff 文件（`current_fix.diff`、`luan_review.diff`、`msg_orchestrator.diff`、`review_diff.txt`） | 开发残留 |
| `chainlitexam/code-review-findings.json` | 内部评审记录 |
| 根目录 `task_plan.md`、`progress.md`、`findings.md`、`current-progress.md`、`AGENTS.md`、`PRODUCT.md`、`DESIGN.md`、`plugin-list.json` | 开发过程文件 |
| `.chainlit/`、`.planning/`、`.superpowers/`、`.agents/`、`.claude/`（外层） | 开发工具配置 |

### 4.3 需要保留的关键内容

- `chainlitexam/tests/`（30+ 测试，含性能相关）
- `chainlitexam/scripts/perf_stats.py`、`recall_stats.py`、`_stats_common.py`（客户可自己跑性能统计）
- `chainlitexam/timing_logger.py`（延迟埋点）
- `chainlitexam/public/`（Chainlit 前端定制资源，2.1MB）
- `chainlitexam/skills/`（合作方能力 SKILL）
- `haihe-weather-analyzer-mcp/pyproject.toml`、`uv.lock`（依赖清单）
- `chainlitexam/docs/performance/`（现有性能文档，迁移引用）

## 5. 必须的代码修正（仅限交付包内）

`forecast_evaluate_tool.py:27` 存在跨目录相对路径引用：

```python
_EVALUATE_SCRIPTS = Path(__file__).resolve().parents[2] / "forecast_evaluate 2" / "forecast_evaluate" / "scripts"
```

交付包中目录名变为 `forecast_evaluate/`，且层级从"离根 2 层"变为"离根 1 层"，此引用必须同步改为：

```python
_EVALUATE_SCRIPTS = Path(__file__).resolve().parents[1] / "forecast_evaluate" / "forecast_evaluate" / "scripts"
```

> **层级说明**：原仓库该文件在 `<root>/<inner>/haihe-weather-analyzer-mcp/`（离根 2 层），故 `parents[2]` 回到根再找 `forecast_evaluate 2`；交付包中它在 `<root>/qa-agent-delivery/haihe-weather-analyzer-mcp/`（离根 1 层），`forecast_evaluate/` 也在 `qa-agent-delivery/` 下，故需用 `parents[1]`。

**注意**：这只改交付包内的副本，原始仓库源码不动。

## 6. 依赖清单

`chainlitexam/` 缺少 `requirements.txt`/`pyproject.toml`。整理时从以下来源生成 `chainlitexam/requirements.txt`：
- `chainlitexam/chain_gzt.py` 的 import（chainlit、langchain_core、langchain_mcp_adapters、langchain_openai、httpx、psycopg2、matplotlib、fastapi 等）
- `haihe-weather-analyzer-mcp/pyproject.toml` 的 dependencies

## 7. 性能分析文档内容大纲（核心交付物）

`docs/performance/01-why-qa-is-slow.md`，主线 = 回答"为什么慢"：

1. **总览调用链**：一次问答从 HTTP 进来经过哪些节点（参考现有 `current-qa-call-chain.md`）
2. **慢在哪里**：逐节点剖析
   - Planner 2 次 LLM 调用（默认 60s 超时/次，`PLANNER_MODEL=Qwen3.6-27B`）
   - 并行纯数据工具 + 60s 容错重试（`_PARALLEL_TOOL_SEMAPHORE` 并发 4）
   - 串行副作用工具（`_invoke_tool_with_tolerance`）
   - 内网 MCP 连接（`load_sse_tools`，首次初始化可能长时间挂住）
   - 滚动预报专用路径 `is_current_rolling_weather_query`（LLM summary 超时 130s）
3. **已做的优化**：LLM 预热、响应缓存（TTL 300s）、简单天气规则路由（省 5-10s）、Prompt 拆分、工具候选召回、evidence-finalize、timing_logger 埋点
4. **如何观测**：`perf_stats.py`、`recall_stats.py`、各节点超时配置项
5. **后续可优化方向**

## 8. 验收标准

1. 交付包目录结构符合 §3，无垃圾文件（__pycache__、.idea、worktrees、__MACOSX、diff 文件）
2. `forecast_evaluate_tool.py` 路径引用在交付包内正确指向 `forecast_evaluate/`
3. 三个模块源码内容与原始仓库一致（除必要的路径修正）
4. `docs/performance/` 有完整性能分析文档，客户能据此理解"为什么慢"
5. README.md 说明模块关系、如何看代码、如何定位性能
6. 原始仓库 `git status` 干净（源码未被改动）
7. 打包 `qa-agent-delivery.zip`
