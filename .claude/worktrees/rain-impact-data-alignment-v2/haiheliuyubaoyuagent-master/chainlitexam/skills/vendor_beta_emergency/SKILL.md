---
name: vendor_beta_emergency
description: 合作方 Beta 应急联动辅助智能体（演示用 mock，对接后替换为真实 HTTP）
---

# Beta 应急智能体

## 何时使用

- 用户明确提到 **Beta / 贝塔 / 合作方B** 并要求应急、联动、处置建议类分析。
- 用户要求调用「第三方应急智能体」且语境指向本合作方。

## 行为约定

- 运行时由主应用通过 LangChain 工具 `invoke_partner_skill_beta_emergency(query)` 调用。
- 当前返回为 **本地 mock JSON**（`mock: true`），对接时在 `mock_vendor_agents.call_vendor_beta_emergency_api` 中实现真实请求。

## 输出解读

- `alerts`：提示级信息列表（demo）。
- `actions`：建议动作列表（demo）。
- 面向用户时需标注为演示数据，直至生产接入。
