# 客户交付包整理（qa-agent-delivery）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从杂乱的开发仓库中提取问答智能体相关代码（chainlitexam 前端 + haihe-weather-analyzer-mcp 后端 + forecast_evaluate 检验评估），整理成一个干净、可浏览、能讲清"问答为什么慢"的客户交付包 `qa-agent-delivery/`，并打包为 zip。

**Architecture:** 交付包由三个互不嵌套的模块目录 + 一个 `docs/` 目录组成。`chainlitexam/`（Chainlit 前端）与 `haihe-weather-analyzer-mcp/`（MCP 后端）保持原内部结构原样复制；`forecast_evaluate 2/forecast_evaluate/` 去空格改名复制为 `forecast_evaluate/forecast_evaluate/`。`docs/performance/` 存放回答"为什么慢"的性能分析文档。唯一的代码修正是在交付包内把 `forecast_evaluate_tool.py` 的跨目录路径引用从 `"forecast_evaluate 2"` 改为 `"forecast_evaluate"`。

**Tech Stack:** 纯文件复制/组织 + Markdown 文档撰写 + PowerShell/robocopy 复制 + Compress-Archive 打包。不改动 Python 源码逻辑。

## Global Constraints

- **原始仓库源码不动**：`D:\PythonProject\haiheliuyubaoyuagent-master` 下任何源码文件**禁止修改**。所有修正只发生在交付包副本内。
- **交付包位置**：`D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\`（原始仓库根目录下）。
- **目录重命名**：`forecast_evaluate 2/forecast_evaluate` → 交付包内 `forecast_evaluate/forecast_evaluate`（保留内部子目录，只去掉外层空格目录名）。
- **排除清单**：`__pycache__/`、`.pytest_cache/`、`.idea/`、`.git/`、`.venv/`、`.claude/worktrees/`、`__MACOSX/`、`chainlitexam/.files/`、`*.diff`、`chainlitexam/code-review-findings.json`、根目录非核心文件（`hhlyqyxt-master/`、`Wechat*`、`task_plan.md`、`progress.md`、`findings.md`、`current-progress.md`、`AGENTS.md`、`PRODUCT.md`、`DESIGN.md`、`plugin-list.json`、`chainlit.md`、`.chainlit/`、`.planning/`、`.superpowers/`、`.agents/`、`.claude/`、`.files/`）。
- **敏感信息全保留**：不脱敏，不改 config。但交付 README 需注明"内网地址/凭据需替换"。
- **交付形式**：`qa-agent-delivery/` 文件夹 + `qa-agent-delivery.zip`。
- **性能文档核心**：回答"客户的问题——为什么问答慢"。

---

### Task 1: 创建交付包骨架目录

**Files:**
- Create: `qa-agent-delivery/`（目录树）

**Interfaces:**
- Consumes: 无（首个任务）
- Produces: 目录骨架，后续任务在其内填充内容

- [ ] **Step 1: 创建交付包顶层目录结构**

```powershell
$root = "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery"
$dirs = @(
  "$root\chainlitexam",
  "$root\haihe-weather-analyzer-mcp",
  "$root\forecast_evaluate\forecast_evaluate",
  "$root\docs\performance"
)
foreach ($d in $dirs) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
Write-Output "created:"
Get-ChildItem $root -Recurse -Directory | Select-Object -ExpandProperty FullName
```

- [ ] **Step 2: 验证目录结构**

Run: `Test-Path "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\docs\performance"`
Expected: `True`

- [ ] **Step 3: Commit**

```bash
cd "D:/PythonProject/haiheliuyubaoyuagent-master"
git status --short   # 确认交付包目录被 .gitignore 忽略，或为空（不被跟踪）
```

> 注：`qa-agent-delivery/` 是交付产物，不应被 git 跟踪。若未在 .gitignore 中，本任务不提交它；仅在 Task 8 结束时决定是否加入 .gitignore。交付包内文件不进入 git。

---

### Task 2: 复制 chainlitexam 前端模块

**Files:**
- Copy: `haiheliuyubaoyuagent-master/chainlitexam/**` → `qa-agent-delivery/chainlitexam/`

**Interfaces:**
- Consumes: Task 1 的 `qa-agent-delivery/chainlitexam/` 目录
- Produces: 完整的 chainlitexam 前端代码副本（排除垃圾文件）

- [ ] **Step 1: 用 robocopy 复制 chainlitexam（排除缓存/IDE/会话/diff）**

```powershell
$src = "D:\PythonProject\haiheliuyubaoyuagent-master\haiheliuyubaoyuagent-master\chainlitexam"
$dst = "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\chainlitexam"
$exclude = @("__pycache__", ".pytest_cache", ".idea", ".files", "*.diff", "code-review-findings.json")
$args = @($src, $dst, "/E", "/COPY:DAT", "/R:1", "/W:1") + ($exclude | ForEach-Object { "/XD", $_ })
# robocopy 不支持同时用 /XD 排除文件和 /XF 排除文件，分开处理：
# 先用 /XD 排除目录，再用 /XF 排除 diff 文件
robocopy $src $dst /E /XD __pycache__ .pytest_cache .idea .files /XF *.diff code-review-findings.json /R:1 /W:1
Write-Output "robocopy exit code: $LASTEXITCODE"  # 0-7 均视为成功
```

- [ ] **Step 2: 验证复制结果**

```powershell
$dst = "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\chainlitexam"
# 核心文件存在
@("chain_gzt.py","message_orchestrator.py","prompts.py","timing_logger.py","qa_http_api.py","README.md","tests","public","skills","scripts\perf_stats.py") | ForEach-Object {
  $p = Join-Path $dst $_
  if (Test-Path $p) { Write-Output "OK: $_" } else { Write-Output "MISSING: $_" }
}
# 垃圾文件不存在
@("__pycache__","code-review-findings.json","current_fix.diff") | ForEach-Object {
  $p = Join-Path $dst $_
  if (Test-Path $p) { Write-Output "SHOULD-BE-EXCLUDED: $_" } else { Write-Output "CLEAN: $_" }
}
```

- [ ] **Step 3: 验证无 diff 文件进入交付包**

```powershell
Get-ChildItem "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\chainlitexam" -Recurse -Filter "*.diff" | Select-Object -ExpandProperty FullName
# 期望无输出
```

---

### Task 3: 复制 haihe-weather-analyzer-mcp 后端模块

**Files:**
- Copy: `haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/**` → `qa-agent-delivery/haihe-weather-analyzer-mcp/`

**Interfaces:**
- Consumes: Task 1 的 `qa-agent-delivery/haihe-weather-analyzer-mcp/` 目录
- Produces: MCP 后端代码副本（排除垃圾文件）

- [ ] **Step 1: 用 robocopy 复制 MCP 后端**

```powershell
$src = "D:\PythonProject\haiheliuyubaoyuagent-master\haiheliuyubaoyuagent-master\haihe-weather-analyzer-mcp"
$dst = "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\haihe-weather-analyzer-mcp"
robocopy $src $dst /E /XD __pycache__ .pytest_cache .idea /XF *.diff /R:1 /W:1
Write-Output "robocopy exit code: $LASTEXITCODE"  # 0-7 均视为成功
```

- [ ] **Step 2: 验证复制结果**

```powershell
$dst = "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\haihe-weather-analyzer-mcp"
@("server.py","tools.py","haihe_mcp_tools.py","forecast_evaluate_tool.py","main.py","config.ini","pyproject.toml","uv.lock","README.md","custom_tools","utils","wms_vector_service") | ForEach-Object {
  $p = Join-Path $dst $_
  if (Test-Path $p) { Write-Output "OK: $_" } else { Write-Output "MISSING: $_" }
}
if (Test-Path "$dst\__pycache__") { Write-Output "SHOULD-BE-EXCLUDED: __pycache__" } else { Write-Output "CLEAN: no __pycache__" }
```

---

### Task 4: 复制 forecast_evaluate 检验评估包（去空格改名）

**Files:**
- Copy: `forecast_evaluate 2/forecast_evaluate/**` → `qa-agent-delivery/forecast_evaluate/forecast_evaluate/`

**Interfaces:**
- Consumes: Task 1 的 `qa-agent-delivery/forecast_evaluate/forecast_evaluate/` 目录
- Produces: 检验评估包副本（去掉 `__MACOSX/`，目录名去空格）

- [ ] **Step 1: 复制（源目录名含空格，用引号包裹）**

```powershell
$src = "D:\PythonProject\haiheliuyubaoyuagent-master\forecast_evaluate 2\forecast_evaluate"
$dst = "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\forecast_evaluate\forecast_evaluate"
robocopy $src $dst /E /XD __MACOSX __pycache__ /R:1 /W:1
Write-Output "robocopy exit code: $LASTEXITCODE"
```

- [ ] **Step 2: 验证复制结果（含 __MACOSX 排除）**

```powershell
$dst = "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\forecast_evaluate\forecast_evaluate"
@("scripts\forecast_evaluate.py","scripts\analyzer.py","scripts\config.py","scripts\batch_download.py","SKILL.md","SKILL_ANA.md","docs\01-quickstart.md") | ForEach-Object {
  $p = Join-Path $dst $_
  if (Test-Path $p) { Write-Output "OK: $_" } else { Write-Output "MISSING: $_" }
}
# 确认 __MACOSX 未进来（源里有它）
if (Test-Path "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\forecast_evaluate\__MACOSX") { Write-Output "SHOULD-BE-EXCLUDED: __MACOSX" } else { Write-Output "CLEAN: no __MACOSX" }
# 确认源顶层没有名为 forecast_evaluate 的孤儿文件
Get-ChildItem "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\forecast_evaluate" | Select-Object Name
# 期望输出只有: forecast_evaluate 子目录
```

---

### Task 5: 修正交付包内 forecast_evaluate_tool.py 路径引用

**Files:**
- Modify: `qa-agent-delivery/haihe-weather-analyzer-mcp/forecast_evaluate_tool.py:27`（仅交付包副本）

**Interfaces:**
- Consumes: Task 3 复制的 `haihe-weather-analyzer-mcp/forecast_evaluate_tool.py`
- Produces: 路径引用正确的交付包副本。后续 Task 6 的文档引用此修正作为"交付包内唯一代码变更"示例。

- [ ] **Step 1: 读取交付包内的目标行，确认当前内容**

```powershell
$f = "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\haihe-weather-analyzer-mcp\forecast_evaluate_tool.py"
(Get-Content $f | Select-Object -Index 26)
# 期望: _EVALUATE_SCRIPTS = Path(__file__).resolve().parents[2] / "forecast_evaluate 2" / "forecast_evaluate" / "scripts"
```

> **关键层级说明**：原仓库 `forecast_evaluate_tool.py` 在 `<root>/<inner>/haihe-weather-analyzer-mcp/`（离根 2 层），故原代码用 `parents[2]` 回到仓库根再找 `forecast_evaluate 2`。交付包中该文件在 `<root>/qa-agent-delivery/haihe-weather-analyzer-mcp/`（离根 1 层），`forecast_evaluate/` 也在 `qa-agent-delivery/` 下，**相对层级变为 1 层** → 必须用 `parents[1]`。

- [ ] **Step 2: 修改路径引用（parents 索引 + 目录名去空格）**

```powershell
$f = "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\haihe-weather-analyzer-mcp\forecast_evaluate_tool.py"
$content = Get-Content $f -Raw -Encoding UTF8
# 原: parents[2] / "forecast_evaluate 2" / "forecast_evaluate" / "scripts"
# 改: parents[1] / "forecast_evaluate" / "forecast_evaluate" / "scripts"
$c1 = $content -replace 'parents\[2\] / "forecast_evaluate 2"', 'parents[1] / "forecast_evaluate"'
if ($c1 -eq $content) {
  Write-Output "PATTERN NOT FOUND - 检查源文件第27行实际内容"
} else {
  [System.IO.File]::WriteAllText($f, $c1, (New-Object System.Text.UTF8Encoding $false))
  Write-Output "REPLACED"
}
```

- [ ] **Step 3: 验证修改结果**

```powershell
$f = "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\haihe-weather-analyzer-mcp\forecast_evaluate_tool.py"
Select-String -Path $f -Pattern 'parents\[1\]' | Select-Object LineNumber, Line
# 期望: 第27行现在是 parents[1] / "forecast_evaluate" / "forecast_evaluate" / "scripts"
# 第4行 docstring 仍保留 "forecast_evaluate/scripts/"（注释，无需改）
```

- [ ] **Step 4: 验证原始仓库源码未被动过**

```bash
cd "D:/PythonProject/haiheliuyubaoyuagent-master"
git status --short
# 期望只有 .claude/settings.local.json 的既有改动，无源码文件被修改
```

---

### Task 6: 撰写交付包 README.md

**Files:**
- Create: `qa-agent-delivery/README.md`

**Interfaces:**
- Consumes: Task 2-4 复制的模块、Task 5 的路径修正
- Produces: 交付包入口文档。Task 7 的 docs 与之互补（README 讲"怎么用/怎么看"，docs 讲"为什么慢"）。

- [ ] **Step 1: 写 README.md**

```powershell
$readme = @'
# 海河流域暴雨洪水预报智能体 — 客户交付包

> 本包为海河流域气象问答智能体的源码交付，面向**代码审阅**场景。
> 核心问题定位：**智能体问答为什么慢？** → 见 `docs/performance/01-why-qa-is-slow.md`。

## 模块结构

| 目录 | 作用 | 入口 |
|------|------|------|
| `chainlitexam/` | 前端问答智能体（Chainlit 对话、FastAPI HTTP 接口、消息编排、快速路径） | `chain_gzt.py` |
| `haihe-weather-analyzer-mcp/` | 后端 MCP 服务（天气/河网/应急响应/面雨量/预警/检验评估工具） | `server.py` / `main.py` |
| `forecast_evaluate/` | 预报检验评估工具包（TS/PC/BIAS/MAE 等指标，被 MCP 的 `forecast_evaluate_tool.py` 调用） | `forecast_evaluate/scripts/forecast_evaluate.py` |
| `docs/` | 架构说明 + 性能分析文档 | `docs/architecture.md`、`docs/performance/` |

## 三层依赖关系

```
chainlitexam (前端问答)
    │  http / 消息编排
    ▼
haihe-weather-analyzer-mcp (后端工具 MCP)
    │  import sys.path
    ▼
forecast_evaluate (预报检验评估)
```

问答主链路：`POST /api/v1/qa/ask` → `chainlitexam/qa_http_api.py` → `message_orchestrator.process_message` → 调 MCP 工具 / LLM Planner → Answer。

## 如何看代码定位"为什么慢"

1. 先读 `docs/performance/01-why-qa-is-slow.md`（性能分析主线）。
2. 再读 `docs/performance/03-call-chain.md`（逐节点调用链，标注每个节点是否调 LLM/超时/重试）。
3. 对照代码：
   - 编排：`chainlitexam/message_orchestrator.py`（`process_message`、`_run_tool_round`、`_invoke_tools_in_parallel`）
   - HTTP 入口：`chainlitexam/qa_http_api.py`（`QARuntime.ask`、信号量、响应缓存）
   - Planner/Answer LLM：`chainlitexam/chain_gzt.py`（`astream_planner_think`、`astream_answer_chain_to_message`）
   - 延迟埋点：`chainlitexam/timing_logger.py`（输出 `[PERF]` JSON Lines）
   - 性能统计脚本：`chainlitexam/scripts/perf_stats.py`、`recall_stats.py`
4. 已做优化：`docs/performance/02-optimizations-done.md`。

## 部署与运行说明（给客户环境）

- **依赖**：`haihe-weather-analyzer-mcp/pyproject.toml` + `chainlitexam/requirements.txt`（见下）。
- **⚠️ 内网凭据**：`haihe-weather-analyzer-mcp/config.ini` 与 `chainlitexam/utils/config.py` 含内网地址与数据库/接口凭据。客户部署时**必须替换**为自有环境的地址与凭据。
- **数据库**：PostgreSQL（`utils/config.py` / `config.ini` 中 `DB_HOST`/`DB_PASSWORD`）。
- **MCP 内网接口**：`load_sse_tools()` 连接的内网 MCP 地址需替换。
- **LLM 配置**：`PLANNER_MODEL`/Answer 模型及 API key 需按客户环境配置（见 `chain_gzt.py` 环境变量）。

## 交付包内代码变更清单

- 唯一代码修正：`haihe-weather-analyzer-mcp/forecast_evaluate_tool.py` 第 27 行路径引用
  `"forecast_evaluate 2"` → `"forecast_evaluate"`（因交付包目录名去空格）。
- 其余源码与原始仓库一致。
'@
$readme | Out-File -FilePath "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\README.md" -Encoding utf8
Write-Output "README.md written"
```

- [ ] **Step 2: 验证 README 存在且包含关键章节**

```powershell
$f = "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\README.md"
if (Test-Path $f) {
  $c = Get-Content $f -Raw
  @("为什么慢","chainlitexam","haihe-weather-analyzer-mcp","forecast_evaluate","01-why-qa-is-slow","config.ini") | ForEach-Object {
    if ($c -match [regex]::Escape($_)) { Write-Output "OK contains: $_" } else { Write-Output "MISSING: $_" }
  }
} else { Write-Output "README NOT CREATED" }
```

---

### Task 7: 撰写性能分析文档（docs/performance/，核心交付物）

**Files:**
- Create: `qa-agent-delivery/docs/architecture.md`
- Create: `qa-agent-delivery/docs/performance/01-why-qa-is-slow.md`
- Create: `qa-agent-delivery/docs/performance/02-optimizations-done.md`
- Create: `qa-agent-delivery/docs/performance/03-call-chain.md`（迁移现有 `chainlitexam/docs/performance/current-qa-call-chain.md`）
- Create: `qa-agent-delivery/docs/performance/04-perf-observability.md`

**Interfaces:**
- Consumes: Task 5 路径修正、Task 6 README、`chainlitexam/docs/performance/current-qa-call-chain.md`（源文档）
- Produces: 回答"为什么慢"的完整文档集。README 引用这些文档。

- [ ] **Step 1: 迁移现有调用链文档为 03-call-chain.md**

```powershell
$src = "D:\PythonProject\haiheliuyubaoyuagent-master\haiheliuyubaoyuagent-master\chainlitexam\docs\performance\current-qa-call-chain.md"
$dst = "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\docs\performance\03-call-chain.md"
Copy-Item $src $dst -Force
Write-Output "migrated"
```

- [ ] **Step 2: 写 docs/architecture.md**

```powershell
$arch = @'
# 架构说明

## 总体结构

本智能体为三层架构：

```
┌─────────────────────────────────────────────────┐
│  1. chainlitexam/  — 前端问答智能体              │
│      Chainlit 对话界面 / FastAPI HTTP 接口       │
│      message_orchestrator 消息编排               │
└──────────────────┬──────────────────────────────┘
                   │ 调用工具 / 获取数据
┌──────────────────▼──────────────────────────────┐
│  2. haihe-weather-analyzer-mcp/ — 后端 MCP 服务  │
│      天气 / 河网 / 应急响应 / 面雨量 / 预警       │
│      检验评估（调 forecast_evaluate）            │
└──────────────────┬──────────────────────────────┘
                   │ sys.path 引用
┌──────────────────▼──────────────────────────────┐
│  3. forecast_evaluate/ — 预报检验评估包          │
│      TS/PC/BIAS/MAE 指标、降水/温度检验          │
└─────────────────────────────────────────────────┘
```

## 各层职责

### chainlitexam/（前端）
- `chain_gzt.py`：Chainlit 会话入口、FastAPI 服务、工具绑定、LLM（Planner/Answer）调用。
- `message_orchestrator.py`：核心编排——规则路由、快速路径、多轮工具执行、回退。
- `qa_http_api.py`：HTTP 问答接口（`POST /api/v1/qa/ask`），信号量并发控制、响应缓存。
- `timing_logger.py`：延迟埋点，输出 `[PERF]` JSON Lines。
- `tools/`：决策天气、降雨河流影响、预警工作流等业务工具。
- `fast_paths/`：降雨/水位/风险预警等快速路径（免 Planner LLM）。
- `skills/`：合作方能力封装（Alpha 水文 / Beta 应急 / 短临预报）。

### haihe-weather-analyzer-mcp/（后端）
- `server.py` / `main.py`：MCP 服务装配，注册工具。
- `tools.py` / `haihe_mcp_tools.py`：天气/河网/面雨量/预警等核心工具实现。
- `emergency_*.py`：应急响应判定、事件存储、HTTP 服务、内网同步。
- `forecast_evaluate_tool.py`：检验评估 MCP 工具（调 forecast_evaluate）。
- `custom_tools/`：历史同期降雨、POI 最近观测站、风险预警等定制工具。
- `wms_vector_service/`：河网/边界矢量 WMS 服务。
- `config.ini`：内网地址、数据库、GeoServer 配置。

### forecast_evaluate/（检验评估）
- `forecast_evaluate/scripts/`：`forecast_evaluate.py`（核心）、`analyzer.py`、`batch_download.py`、`config.py`。
- 通过检验 API（内网 10.226.107.74:31002）获取 TS/PC/BIAS/MAE/ME 等指标。
- 被 MCP 的 `forecast_evaluate_tool.py` 以 `sys.path` 方式引用。

## 关键数据流

### 一次问答的请求路径
```
用户提问
  → POST /api/v1/qa/ask (qa_http_api.py)
  → QARuntime.ask（信号量排队 + 会话锁）
  → process_message (message_orchestrator.py)
      → 简单天气规则路由？（免 LLM）
      → 滚动预报专用路径？（LLM summary）
      → Planner 第1次（LLM 规划）
      → 工具轮（并行纯数据 + 串行副作用）
      → Planner 第2次（如需）
      → Answer（生成回答）
  → 返回 HTTP 响应
```
详见 `docs/performance/01-why-qa-is-slow.md` 与 `03-call-chain.md`。
'@
$arch | Out-File -FilePath "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\docs\architecture.md" -Encoding utf8
Write-Output "architecture.md written"
```

- [ ] **Step 3: 写 docs/performance/01-why-qa-is-slow.md（核心）**

```powershell
$why = @'
# 问答为什么慢？— 性能分析

> 面向客户审阅。回答核心问题：**一次问答的延迟从哪里来？**
> 完整逐节点调用链见 `03-call-chain.md`；已做优化见 `02-optimizations-done.md`。

## TL;DR

一次典型问答的耗时大头是 **LLM 调用**（Planner 规划 + Answer 回答）与**内网数据接口往返**。
在非快速路径下，问答要经历：

1. **Planner 第 1 次**：LLM 规划工具调用 —— 默认超时 60s，是单点耗时上限。
2. **工具轮**：多个纯数据工具并行执行（并发 4），每个工具 60s 容错重试；串行副作用工具逐个执行。
3. **Planner 第 2 次**：需要更多信息时再次 LLM 规划（至多 5 轮）。
4. **Answer**：LLM 生成回答 —— 60s 超时。

在配置了内网 MCP 时，首次 `load_sse_tools()` 连接内网地址可能长时间挂住（超时兜底 180s）。

## 耗时来源逐项拆解

| 阶段 | 是否调 LLM | 默认超时 | 说明 |
|------|-----------|---------|------|
| 排队 | 否 | 整体 180s | `QARuntime.ask` 信号量（并发 4）+ 会话锁 |
| 运行时获取 | 否 | 180s | `load_sse_tools()` 连内网 MCP，首次可能挂住 |
| 简单天气路由 | 否 | — | 命中则**跳过 Planner**，省 5-10s |
| 滚动预报路径 | 是（1次 summary） | 130s | `is_current_rolling_weather_query` |
| Planner 第1次 | 是 | 60s | `PLANNER_MODEL`（Qwen3.6-27B），连接错误重试 |
| 并行工具轮 | 否（纯数据） | 每个 60s | 并发 4，`_PARALLEL_TOOL_SEMAPHORE` |
| 串行副作用工具 | 否 | 每个 60s | 单个 `_invoke_tool_with_tolerance` |
| Planner 第2次 | 是 | 60s | 数据不足时；Fix A 数据完整则跳过 |
| Answer | 是 | 60s | `astream_answer_chain_to_message` |

## 为什么"慢"是必然的

- **LLM 是硬耗时**：Planner + Answer 至少 2 次 LLM 调用，每次秒级到几十秒。
- **内网接口慢**：工具调天擎/检验 API，内网延迟与抖动直接进链路。
- **串行依赖**：串行副作用工具不能并行，逐个等待。
- **容错重试**：60s 超时 + 连接错误重试，网络抖动时放大耗时。
- **无快速路径命中时**：走完整 Planner 流程（2 次 LLM），是最慢的通用路径。

## 哪些问题不在"慢"的范围内

- 响应缓存（TTL 300s）命中时**不慢**——单轮重复问题直接返回。
- 简单天气规则路由命中时**不慢**——跳过 Planner。
- 快速路径（降雨/水位/风险预警）命中时**不慢**——免 Planner。

## 如何量化

运行 `chainlitexam/scripts/perf_stats.py` 读取 `[PERF]` JSON Lines 日志，得到 P50/P90/P95/P99 与工具耗时 Top：
```bash
python scripts/perf_stats.py perf.jsonl
```
详见 `04-perf-observability.md`。
'@
$why | Out-File -FilePath "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\docs\performance\01-why-qa-is-slow.md" -Encoding utf8
Write-Output "01-why-qa-is-slow.md written"
```

- [ ] **Step 4: 写 docs/performance/02-optimizations-done.md**

```powershell
$opt = @'
# 已做优化清单

> 这些优化已进入代码，部分为默认开启。理解"为什么慢"时，先知道哪些慢点已被消除。

| 优化 | 作用 | 默认 |
|------|------|------|
| 简单天气规则路由 `_route_simple_weather_query` | 明确时间+天气词时跳过 Planner LLM，省 5-10s | 开 |
| 响应缓存 `_response_cache`（TTL 300s） | 单轮重复问题直接返回，不跑链路 | 开 |
| LLM 预热 `ENABLE_LLM_WARMUP` | 启动时预热 LLM 连接，降首个请求延迟 | 关（按需开） |
| 并行纯数据工具 `_invoke_tools_in_parallel` | 纯数据工具并发 4，省串行等待 | 开 |
| 工具候选召回 `tool_candidate_index` | 减少候选工具，提升 Planner 规划效率 | 开 |
| Prompt 拆分 | 规划/回答分离，降低单次 LLM 输入与响应 | 开 |
| 证据完整性 `is_evidence_complete` | 数据完整时提前结束（shadow） | 开 |
| 历史压缩 `_compress_messages` | 旧轮次 Tool/AI 消息截断，降输入 token | 开 |
| 定时埋点 `TimingContext` | `[PERF]` JSON Lines，可量化各阶段 | 开 |

## 关键配置项（影响延迟）

| 配置 | 默认 | 说明 |
|------|------|------|
| `PLANNER_TIMEOUT_SECONDS` | 60 | Planner LLM 超时 |
| `PLANNER_MAX_RETRIES` | 2 | 连接错误重试次数 |
| `MAX_CONCURRENCY` | 4 | HTTP 并发信号量 |
| `TIMEOUT_SECONDS` | 180 | 整体问答超时 |
| `_PARALLEL_TOOL_SEMAPHORE` | 4 | 并行工具并发 |
| `QA_API_RESPONSE_CACHE_TTL` | 300 | 响应缓存 TTL |
| `MAX_PLANNER_ROUNDS` | 5 | Planner 最大轮数 |
| `ENABLE_FAST_PATHS` | false | 快速路径总开关 |

> 详细优化过程文档（内部）见 `chainlitexam/docs/performance/` 与各 plan/spec 记录；本包仅保留结论性清单。
'@
$opt | Out-File -FilePath "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\docs\performance\02-optimizations-done.md" -Encoding utf8
Write-Output "02-optimizations-done.md written"
```

- [ ] **Step 5: 写 docs/performance/04-perf-observability.md**

```powershell
$obs = @'
# 性能可观测性

## 埋点方式

`chainlitexam/timing_logger.py` 的 `TimingContext` 结构化记录一次问答的各阶段耗时，输出一行 `[PERF]` JSON：

```json
{"request_id":"...","stages":{"queue":12,"planner1":3400,"tools":[123,890],"answer":2100},"planner_rounds":2,"total_ms":6800,"status":"ok"}
```

要点：
- **不记录**用户问题、工具原始结果、内网地址、绝对路径（隐私与安全）。
- `http_queue_wait_ms` / `tool_queue_wait_ms`：区分「信号量排队」与「工具执行」。
- `planner_input_chars` / `planner_output_chars`：观察 Prompt 规模。

## 统计脚本

### perf_stats.py — P50/P90/P95/P99 与工具耗时 Top

```bash
cd chainlitexam
python scripts/perf_stats.py < perf.jsonl   # 或 python scripts/perf_stats.py perf.jsonl
```

输出：`total_requests`、`total_ms`（P50/P90/P95/P99）、`planner_rounds_dist`、`top_tools_by_ms`。

### recall_stats.py — 工具候选召回统计

读取召回 shadow 日志，统计候选命中率。详见脚本 docstring。

## 从日志到结论

1. 抓 `[PERF]` 行：应用运行时 stdout/stderr 或日志文件。
2. 跑 `perf_stats.py` 得 P95/P99 与工具 Top。
3. 定位最慢工具/阶段（`stages` 或 `tools` 数组）。
4. 对照 `01-why-qa-is-slow.md` 判断：是 Planner LLM 慢、工具慢，还是排队慢。

## 相关代码入口

- 埋点：`chainlitexam/timing_logger.py`（`TimingContext.mark`、`finalize`）
- 统计：`chainlitexam/scripts/perf_stats.py`、`recall_stats.py`、`_stats_common.py`
- 编排埋点位置：`message_orchestrator.py` 的 `process_message` 各阶段
'@
$obs | Out-File -FilePath "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\docs\performance\04-perf-observability.md" -Encoding utf8
Write-Output "04-perf-observability.md written"
```

- [ ] **Step 6: 验证 4 个性能/架构文档均存在**

```powershell
$base = "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\docs"
@("architecture.md","performance\01-why-qa-is-slow.md","performance\02-optimizations-done.md","performance\03-call-chain.md","performance\04-perf-observability.md") | ForEach-Object {
  $p = Join-Path $base $_
  if (Test-Path $p) { Write-Output "OK: $_" } else { Write-Output "MISSING: $_" }
}
```

- [ ] **Step 7: 验证 03-call-chain.md 内容与源一致（迁移完整性）**

```powershell
$a = Get-Content "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\docs\performance\03-call-chain.md" -Raw
$b = Get-Content "D:\PythonProject\haiheliuyubaoyuagent-master\haiheliuyubaoyuagent-master\chainlitexam\docs\performance\current-qa-call-chain.md" -Raw
if ($a -eq $b) { Write-Output "CALL-CHAIN IDENTICAL" } else { Write-Output "DIFFERS - 检查迁移" }
```

---

### Task 8: 生成 chainlitexam/requirements.txt + 最终打包

**Files:**
- Create: `qa-agent-delivery/chainlitexam/requirements.txt`
- Create: `qa-agent-delivery/qa-agent-delivery.zip`
- Modify（可选）: `.gitignore`（将 `qa-agent-delivery/` 加入忽略）

**Interfaces:**
- Consumes: Task 2-7 的所有产物
- Produces: 最终交付物（文件夹 + zip）

- [ ] **Step 1: 从 mcp pyproject.toml 提取依赖，生成 chainlitexam/requirements.txt**

```powershell
$mcpPy = "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\haihe-weather-analyzer-mcp\pyproject.toml"
$req = "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\chainlitexam\requirements.txt"
# 读取 pyproject dependencies 段
$deps = (Get-Content $mcpPy -Raw)
$lines = $deps -split "`n" | Where-Object { $_ -match '^\s+"' } | ForEach-Object { ($_ -replace '^\s+','' -replace ',$','').Trim('"') }
# 补充 chainlitexam 特有依赖（从 chain_gzt.py import 观察）
$extra = @(
  "chainlit>=2.0.0",
  "langchain-core",
  "langchain-openai",
  "langchain-mcp-adapters",
  "httpx",
  "psycopg2-binary",
  "jieba",
  "fastapi",
  "uvicorn"
)
$all = @("# chainlitexam + haihe-weather-analyzer-mcp 依赖（由交付整理自动生成）") + $lines + $extra
$all | Out-File -FilePath $req -Encoding utf8
Write-Output "requirements.txt written ($($all.Count) lines)"
```

- [ ] **Step 2: 验证 requirements.txt 包含关键依赖**

```powershell
$req = "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery\chainlitexam\requirements.txt"
$c = Get-Content $req -Raw
@("chainlit","langchain-openai","psycopg2","geopandas","fastapi") | ForEach-Object {
  if ($c -match [regex]::Escape($_)) { Write-Output "OK: $_" } else { Write-Output "MISSING: $_" }
}
```

- [ ] **Step 3: 全面清理扫描（交付包内无垃圾文件）**

```powershell
$pkg = "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery"
# 应排除项扫描
$bad = @()
$bad += Get-ChildItem $pkg -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue
$bad += Get-ChildItem $pkg -Recurse -Directory -Filter ".pytest_cache" -ErrorAction SilentlyContinue
$bad += Get-ChildItem $pkg -Recurse -Directory -Filter ".idea" -ErrorAction SilentlyContinue
$bad += Get-ChildItem $pkg -Recurse -Directory -Filter "__MACOSX" -ErrorAction SilentlyContinue
$bad += Get-ChildItem $pkg -Recurse -File -Filter "*.diff" -ErrorAction SilentlyContinue
if ($bad.Count -eq 0) { Write-Output "CLEAN: no __pycache__/.pytest_cache/.idea/__MACOSX/*.diff" }
else { $bad | Select-Object -ExpandProperty FullName }
# 不应出现的文件
if (Test-Path "$pkg\chainlitexam\code-review-findings.json") { Write-Output "LEAK: code-review-findings.json" }
if (Test-Path "$pkg\chainlitexam\.files") { Write-Output "LEAK: .files" }
if (Test-Path "$pkg\hhlyqyxt-master") { Write-Output "LEAK: hhlyqyxt-master" }
if (Test-Path "$pkg\WechatGatewayClient") { Write-Output "LEAK: Wechat*" }
```

- [ ] **Step 4: 验证交付包顶层只有预期目录/文件**

```powershell
Get-ChildItem "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery" | Select-Object Name
# 期望: README.md, docs, chainlitexam, haihe-weather-analyzer-mcp, forecast_evaluate
```

- [ ] **Step 5: 打包 zip**

```powershell
$src = "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery"
$zip = "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery.zip"
# 删除旧的 zip（若存在）
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $src -DestinationPath $zip -CompressionLevel Optimal
Write-Output "zip created"
```

- [ ] **Step 6: 验证 zip 存在且非空，检查其内容**

```powershell
$zip = "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery.zip"
if (Test-Path $zip) {
  $size = (Get-Item $zip).Length
  Write-Output "zip size: $size bytes"
  # 用 .NET 列出 zip 内顶层条目（验证无垃圾）
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $z = [System.IO.Compression.ZipFile]::OpenRead($zip)
  try {
    $z.Entries | Where-Object { $_.FullName -notmatch '/' -or $_.FullName -match '^[^/]+/$' } |
      Select-Object -First 20 -ExpandProperty FullName
  } finally { $z.Dispose() }
} else { Write-Output "ZIP NOT CREATED" }
```

- [ ] **Step 7: 验证原始仓库源码未被改动**

```bash
cd "D:/PythonProject/haiheliuyubaoyuagent-master"
git status --short
# 期望只有 .claude/settings.local.json 既有改动，无源码文件被修改
```

- [ ] **Step 8: 决定是否将交付包加入 .gitignore（保留）**

若 `qa-agent-delivery/` 与 `qa-agent-delivery.zip` 出现在 `git status` 中（未被忽略），追加到 `.gitignore`：

```powershell
$gi = "D:\PythonProject\haiheliuyubaoyuagent-master\.gitignore"
$c = Get-Content $gi -Raw
if ($c -notmatch 'qa-agent-delivery') {
  Add-Content -Path $gi -Value "`n# 客户交付包（整理产物，不入库）`nqa-agent-delivery/`nqa-agent-delivery.zip" -Encoding utf8
  Write-Output ".gitignore updated"
} else { Write-Output ".gitignore already covers qa-agent-delivery" }
```

- [ ] **Step 9: 最终验收——对照 spec §8**

```powershell
$pkg = "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery"
Write-Output "=== 交付包顶层 ==="
Get-ChildItem $pkg | Select-Object Name
Write-Output "=== 性能文档 ==="
Get-ChildItem "$pkg\docs\performance" | Select-Object Name
Write-Output "=== 路径修正确认 ==="
Select-String -Path "$pkg\haihe-weather-analyzer-mcp\forecast_evaluate_tool.py" -Pattern 'parents\[2\]' | Select-Object Line
Write-Output "=== zip ==="
Test-Path "D:\PythonProject\haiheliuyubaoyuagent-master\qa-agent-delivery.zip"
```
