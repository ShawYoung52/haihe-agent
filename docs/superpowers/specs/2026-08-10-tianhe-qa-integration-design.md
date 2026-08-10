# 问答智能体 · 接入天河 Fixed QA 问答接口设计

- **状态**：草案（brainstorming 已完成，待用户 review）
- **日期**：2026-08-10
- **作用域**：`chainlitexam/external_skill_tools.py`（新增工具 + HTTP 调用函数）
- **契约类别**：向后兼容（纯新增工具，不改 planner 主流程）
- **模型分工**：DeepSeek v4 Flash = 主力执行；DeepSeek v4 Pro = 架构师 / 高级审查
- **相关记忆**：`[[deepseek-model-constraint]]`、`[[user-full-process-workflow]]`、`[[traction-report-api]]`

---

## 1. 目标

天河平台（`10.226.188.156:8001`）提供了问答接口 `POST /api/qa`（含 Fixed QA 固定问答 + 普通问答）。本功能把该接口接入我们的问答智能体：新增一个 MCP 工具供 planner 大模型调用，当问题命中天河 Fixed QA 目录时，由天河返回 `answer` 作为最终回答。

**非目标**：
- 不改 planner 主流程（`process_message` / `message_orchestrator`）
- 不透传多轮 history（单轮 `history=[]`）
- 不在本地二次加工 answer（直接透传）
- 不做鉴权（天河接口本身未鉴权，仅限内网）

## 2. 背景

天河 `/api/qa` 接口（对接文档 `qa-api-integration-guide.md`）：
- `POST http://10.226.188.156:8001/api/qa`
- body: `{"question": "...", "history": [], "stream": false}`
- 响应: `{"answer": "完整回答正文"}`（UTF-8，可能含 Markdown）
- Fixed QA 整句精确匹配（NFKC 规范化 + 去空白 + 去句末标点），未命中继续走普通问答
- 已知 Fixed QA 示例：`今天雨下了多长时间`、`全市现在下了多少雨`、`市区现在气温和风的实况`、`暴雨天气的防范建议`
- 连接超时 5s，响应超时 120s
- 错误：400（空问题）、422（结构错）、5xx（服务端）、200 但降级正文

现有合作方路由工具（`invoke_partner_skill_*`）全部是 **mock**（`mock_vendor_agents.py`）。天河接口是**真实 HTTP 调用**，实现必须真实请求，不能 mock。

## 3. 设计

### 3.1 新增 `chainlitexam/external_skill_tools.py` 内两个函数

#### `async def call_tianhe_qa_api(query: str) -> str`

真实 HTTP 调用天河接口，返回 `answer` 字符串：

- `query` 去除首尾空白，为空则返回中文提示
- `httpx.post(TIANHE_QA_API_URL, json={"question": query, "history": [], "stream": false}, timeout=(5, 120))`
- 成功（2xx）且 `answer` 是字符串 → 返回 `answer`
- 失败分支全部返回中文提示（供 planner 兜底），**不抛异常**：

| 场景 | 返回 |
|---|---|
| 空 query | `"问题不能为空。"` |
| 连接超时 / 连接失败 | `"天河问答服务连接超时，请稍后重试或换一种问法。"` |
| HTTP 非 2xx | `"天河问答服务暂时不可用，请稍后重试。"` |
| 响应非 JSON / 缺 answer | `"天河问答服务返回格式异常，请稍后重试。"` |
| answer 是降级正文（含"暂时不可用"） | 原样透传（文档 9.4 要求展示降级文本，不自动重试） |

**脱敏**：错误信息不含内网 IP/路径；日志不记录完整问题正文（遵循对接文档 §10"调用日志没有记录不必要的完整敏感问题"）。

#### `async def query_tianhe_fixed_qa(query: str) -> str`

LangChain `@tool` 包装，供 planner 调用：

```python
@tool
async def query_tianhe_fixed_qa(query: str) -> str:
    """调用天河平台 Fixed QA 固定问答接口，获取模板化回答。

    适用于天河已配置固定问答目录的问题，命中后由天河返回标准回答。
    当前已知的 Fixed QA 示例（整句精确匹配，会做去空白/去句末标点规范化）：
    - 今天雨下了多长时间
    - 全市现在下了多少雨
    - 市区现在气温和风的实况
    - 暴雨天气的防范建议

    参数 query：用户问题原文（中文）。不要自行改写或提炼——Fixed QA 是整句匹配。
    返回：天河生成的完整回答正文（UTF-8 字符串，可能含 Markdown 表格）。
    接口失败时返回中文提示，planner 应改用其他本地工具回答。
    """
    return await call_tianhe_qa_api(query)
```

### 3.2 配置

```python
TIANHE_QA_API_URL = os.getenv("TIANHE_QA_API_URL", "http://10.226.188.156:8001/api/qa")
```

模块级常量，部署地址变化时改环境变量，不硬编码到文档。

### 3.3 注册

在 `build_external_skill_tools()` 返回列表中加入 `query_tianhe_fixed_qa`，与现有合作方工具一起注入 planner。

### 3.4 数据流

```
用户问题
  → planner LLM（判断命中天河 Fixed QA 示例关键词）
      → query_tianhe_fixed_qa(query)
          → call_tianhe_qa_api(query)
              → POST {TIANHE_QA_API_URL} {"question": query, "history": [], "stream": false}
              → data["answer"]
  → 直接透传 answer（经 _sanitize_display_text）给用户
```

## 4. 错误处理

| 场景 | 处理 |
|---|---|
| 空 query | 返回"问题不能为空。"，不调用 HTTP |
| 连接超时（5s）/ 连接失败 | 返回提示，planner 兜底 |
| HTTP 非 2xx | 返回提示，planner 兜底 |
| 响应非 JSON / 缺 answer | 返回提示，planner 兜底 |
| 200 但降级正文 | 原样透传（文档要求展示，不自动重试） |
| **任何异常** | 不抛，返回中文提示，不阻塞 planner |

## 5. 测试策略

`chainlitexam/tests/test_tianhe_qa.py`，mock httpx，不依赖内网：

1. `call_tianhe_qa_api` 正常返回 answer
2. 空 query 返回提示，不调用 HTTP
3. 连接超时（httpx.ConnectTimeout）→ 返回提示
4. 连接失败（httpx.ConnectError）→ 返回提示
5. HTTP 500 → 返回提示
6. 响应非 JSON / 缺 answer → 返回提示
7. 降级正文（"智能体服务暂时不可用"）→ 原样透传
8. 工具描述包含已知 Fixed QA 示例
9. 环境变量 `TIANHE_QA_API_URL` 覆盖生效

## 6. 执行编排

| Phase | 内容 | 模型 |
|---|---|---|
| 0 | 设计（本文档） | Pro |
| 1 | TDD 实现 `call_tianhe_qa_api` + `query_tianhe_fixed_qa` + 测试 | Flash |
| 2 | code-review → code-simplifier → 全量回归 | Pro 审查 |
| 3 | 更新 CLAUDE.md + 写记忆 + 提交推送 | Flash |

## 7. 交付物

- `chainlitexam/external_skill_tools.py`（新增 2 个函数，加入注册列表）
- `chainlitexam/tests/test_tianhe_qa.py`（新增）
- 对接文档更新（README 或 `docs/` 说明天河接口接入）
- CLAUDE.md 更新 + 记忆写入
