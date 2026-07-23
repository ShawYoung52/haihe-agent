# 风险预警问答链路补全计划（2026-07-21）

## 背景

同事确认 `http://10.226.107.35:8070/hhfw/riskWarnNew/findDataListByConfig` 可用：
- 中小河流洪水：`model=EC, type=1`
- 山洪：`model=EC, type=2`
- 地质灾害：`model=SCMOC, type=3`

## 现状审计

| 组件 | 文件 | 状态 |
| --- | --- | --- |
| MCP 工具 | `haihe-weather-analyzer-mcp/custom_tools/risk_warning_tool.py` | ✅ model/type 与同事代码一致，base 默认 `10.226.107.35:8070` |
| MCP 注册 | `haihe-weather-analyzer-mcp/server.py:38,84` | ✅ 已注册并列入工具目录 |
| Fast path | `chainlitexam/fast_paths/risk_warning_fast_paths.py` | ✅ 关键词检测 + 表格输出 + reasoning 步骤 |
| Fast path 装载 | `chainlitexam/fast_paths/__init__.py` | ✅ 已 install |
| LangChain 工具加载 | `chainlitexam/chain_gzt.py:load_sse_tools` | ✅ MCP SSE 自动发现，planner 可见 |
| **系统提示引导** | `chainlitexam/prompts.py` | ❌ **无 `query_risk_warning` 引导** |

## 缺口

`ENABLE_FAST_PATHS` 默认 `false`（见 CLAUDE.md "Feature Flags"），主路径是 planner LLM。
planner 只能靠 MCP 工具描述自行发现 `query_risk_warning`，路由可靠性不足：
用户问"有没有山洪风险？""哪些区域有地质灾害？"时可能不调用工具而编造答案，违反
prompts.py 的"有工具必须用工具"核心规则。

## 改动范围

### 1. `chainlitexam/prompts.py`（已提交）
在"重要提醒"区新增第 12 条规则，引导 planner LLM 路由风险问题到 `query_risk_warning`。
明确 `risk_kind` 只能传英文值，移除 model/type 等技术细节。

### 2. `haihe-weather-analyzer-mcp/custom_tools/risk_warning_tool.py`
- `RISK_ALIASES` 补 `崩塌`/`泥石流` 别名，与 fast path 的 `_detect_risk_kind` 对齐。
  否则 planner 按用户原文传 `risk_kind="崩塌"` 会被 `_normalize_risk_kind` raise ValueError。
- 移除 `region` 参数的后端转发 + 移除 `DEFAULT_PAGE_NUM`/`DEFAULT_PAGE_SIZE` 默认值。
  后端 `/hhfw/riskWarnNew/findDataListByConfig` 只认 `model` + `type`，其他参数均导致
  HTTP 500（线上日志三次证实）。Region 过滤留给 LLM 侧基于返回结果筛选。
- `urllib` → `requests` 重写 HTTP 层（与同项目 `haihe_mcp_tools.py` 一致），自动带
  `User-Agent` 等标准 header。

### 3. `chainlitexam/tests/test_risk_warning_fast_paths.py`（新文件）
4 个典型问法的路由行为测试 + 非风险文本的拒绝测试，防止 `_detect_risk_kind` / 
`_is_risk_question` 改动回归。

### 4. `CLAUDE.md`
新增 `risk_warning_tool.py` key file 条目，记录别名同步约束 + HTTP 层约束。

### 5. memory
新增 `risk-warning-tool.md` + MEMORY.md 索引，记录全部三坑（region/pageNum/pageSize → 500）。

### 3. `chainlitexam/tests/test_risk_warning_fast_paths.py`（新文件）
4 个典型问法的路由行为测试 + 非风险文本的拒绝测试，防止 `_detect_risk_kind` / 
`_is_risk_question` 改动回归。

### 4. `CLAUDE.md`
新增 `risk_warning_tool.py` key file 条目，记录别名同步约束。

### 5. memory
新增 `risk-warning-tool.md` + MEMORY.md 索引。

## 验收标准

- [x] `chainlitexam/tests/test_fast_paths.py` 通过（19/19）
- [x] `python -m pytest chainlitexam/tests/ -v` 全套通过（53/53 含新测试）
- [x] prompts.py 新增内容与同事前端代码的 model/type 一致
- [x] 不引入新的内部地址到用户可见输出
- [ ] /code-review 与 /simplify 无高置信度问题
