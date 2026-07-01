---
name: vendor_alpha_water
description: 合作方 Alpha 水文分析智能体（演示用 mock，对接后替换为真实 HTTP）
---

# Alpha 水文智能体

## 何时使用

- 用户明确提到 **Alpha / 阿尔法 / 合作方A** 并要求水文专项分析。
- 用户要求调用「第三方水文智能体」「外协水文模型」等，且语境指向本合作方。

## 行为约定

- 运行时由主应用通过 LangChain 工具 `invoke_partner_skill_alpha_hydro(query)` 调用；`query` 为用户原问或提炼后的任务描述。
- 当前返回为 **本地 mock JSON**（`mock: true`），对接厂商后仅在 `mock_vendor_agents.call_vendor_alpha_hydro_api` 中改为 HTTP 即可。

## 输出解读

- `summary`：对方模型摘要（demo 为占位文案）。
- `indicators`：结构化指标（demo）。
- 必须在面向用户的回复中说明数据来源为「合作方演示接口」直至接入生产。
