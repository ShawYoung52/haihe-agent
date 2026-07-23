---
name: vendor_shortterm
description: 短临预报智能体（风廓线/低空急流/短时强降水/雷暴等0-6小时预报）
---

# 短临预报智能体

## 何时使用

- 用户询问**风廓线、低空急流、短时强降水、雷暴、未来0-6小时天气**等短临预报问题。
- 用户明确要求对接「短临智能体」「短临预报」「短时预报」等第三方短临服务。

## 行为约定

- 运行时由主应用通过 LangChain 工具 `invoke_partner_skill_shortterm(query, history)` 调用。
- `query` 为用户原问，`history` 为可选历史消息。
- 调用 `mock_vendor_agents.call_vendor_shortterm_api(query, history)` 执行 HTTP 请求。

## 接口说明

- 请求地址：`POST http://10.226.107.134:12582/chat_completions`
- 请求格式：`{ message_id, session_id, user_id, content, history }`
- 响应格式：SSE 流式，含 `delta(text/chart/table/image)`、`end`、`error` 块
- 输出为结构化数据（文本、图表、表格），主模型需据此组织回答，说明数据来源为「短临智能体」

## 注意事项

- 工具本身聚合所有流式块，返回 `{ full_text, content_blocks, charts, tables, images }`
- 主模型必须基于 `full_text` 和 `content_blocks` 回答，不可自行编造
- 若返回 `error` 字段，须如实告知用户