---
name: tianhe-qa-tool
description: 天河 Fixed QA 问答接口接入——query_tianhe_fixed_qa 工具供 planner 调用
metadata:
  type: project
---

天河平台（`10.226.188.156:8001`）除报告接口外，还提供问答接口 `POST /api/qa`（含 Fixed QA 固定问答 + 普通问答）。已接入我们的问答智能体。

**接入方式**：`chainlitexam/external_skill_tools.py` 新增两个函数：
- `query_tianhe_fixed_qa(query)` — LangChain `@tool`，planner 命中天河 Fixed QA 时调用
- `call_tianhe_qa_api(query)` — 真实 HTTP 调用，body `{"question", "history": [], "stream": false}`，timeout `(5,120)`
- 响应 `data["answer"]` 直接透传；失败返回中文提示不抛异常

**业务口径**（用户确认）：
- 角色：新增 MCP 工具供 planner 调用（不改 planner 主流程）
- 触发：天河 Fixed QA 覆盖的问题（已知示例：今天雨下了多长时间/全市现在下了多少雨/市区实况/暴雨防范建议）
- 单轮 `history=[]`，不透传多轮
- answer 直接透传，不本地二次加工

**关键坑**：
- 天河 Fixed QA 是整句精确匹配（NFKC + 去空白 + 去句末标点），工具描述明确"不要改写或提炼问题"，否则不命中
- 必须显式传 `"stream": false`（天河默认 true 会变流式）
- `TIANHE_QA_API_URL` 环境变量可覆盖部署地址
- 降级正文（"智能体服务暂时不可用"）原样透传，不自动重试

**Why:** 天河有自己的 Fixed QA 目录（固定模板/知识库），某些问题由天河回答更标准。

**How to apply:** planner 判断命中天河 Fixed QA 时调用 `query_tianhe_fixed_qa`；不命中走本地工具。天河失败时返回提示，planner 兜底本地。

链接：[[traction-report-api]] [[qa-http-api]]
