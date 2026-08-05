# 预警查询路由 LLM 规则化 设计

**日期**：2026-08-05
**状态**：设计（待用户审阅）
**范围**：`chainlitexam/tools/warning_workflow.py`

## 背景与用户反馈

生产日志显示预警查询仍慢：
- "暴雨蓝色预警解除了吗？" — 32.26s
- "现在有什么预警？" — 57.04s（`answer=43732ms`）

**根因**：预警专用链路内部有 **2 次独立 LLM 调用**，未纳入之前的优化：
1. `_route_warning_tools`（`warning_workflow.py:452`）— 判断调用哪个预警接口
2. `_generate_warning_core_and_advice`（`warning_workflow.py:576`）— 生成核心结论+防范建议

其中 `_route_warning_tools` 是**纯规则可替代的 LLM 调用**——它只是按用户问题关键词选择接口（类似决策天气槽位规则抽取）。

## 现状

`WARNING_ROUTE_PROMPT`（`prompts.py:1`）要求 LLM 输出：
```json
{"tool_names": ["get_effective_warning_info"], "national_keywords": "天津", "reason": "..."}
```

路由选择规则（纯关键词判断）：
- **国家/中央/周边/华北/京津冀/北京/河北** → `get_national_warning_info` + `national_keywords`
- **解除/已解除/过去/此前** → `get_history_warning_info`（通常+`get_effective_warning_info`）
- **今天/今日/新发/动态** → `get_today_warning_summary`
- **高温预警+多少度** → `get_effective_warning_info`
- **默认** → `get_effective_warning_info`

现有规则函数可直接复用：`_is_warning_fact_query`、`_is_high_temperature_warning_value_query`、`_infer_national_warning_keywords`、`_normalize_warning_route`。

## 设计

### 新增 `_route_warning_tools_rule_based(user_text) -> dict | None`

纯规则选择工具（与 `_normalize_warning_route` 返回格式兼容）：

```python
def _route_warning_tools_rule_based(user_text: str) -> dict | None:
    t = (user_text or "").strip()
    if not t or "预警" not in t:
        return None
    tool_names = ["get_effective_warning_info"]
    # 国家/周边/华北/京津冀/北京/河北
    national = any(k in t for k in ("国家局", "中央气象台", "中央台", "全国", "周边", "华北", "京津冀", "北京", "河北"))
    # 历史/解除
    history = any(k in t for k in ("解除了吗", "解除预警", "已解除", "解除的", "历史预警", "过去", "此前"))
    # 今日动态
    today = any(k in t for k in ("今天新发", "今日新发", "今日发布", "今天发布", "今日预警", "今天预警", "新发", "动态"))
    if national:
        tool_names = ["get_national_warning_info"] if "天津" not in t and not any(local in t for local in ("天津", "我市", "全市")) else ["get_effective_warning_info", "get_national_warning_info"]
    if history:
        if "get_history_warning_info" not in tool_names:
            tool_names.append("get_history_warning_info")
    if today:
        if "get_today_warning_summary" not in tool_names:
            tool_names.append("get_today_warning_summary")
    if not tool_names:
        tool_names = ["get_effective_warning_info"]
    return _normalize_warning_route({
        "tool_names": list(dict.fromkeys(tool_names)),
        "national_keywords": _infer_national_warning_keywords(t, None),
        "reason": "规则路由",
    })
```

### 修改 `_route_warning_tools`：规则优先，LLM 兜底

```python
async def _route_warning_tools(answer_chain, user_text, callbacks):
    rule_route = _route_warning_tools_rule_based(user_text)
    if rule_route:
        print(f"[WarningWorkflow] rule route={json.dumps(rule_route, ensure_ascii=False)}")
        return rule_route
    # 回退：现有 LLM 路由（保底，不改变行为）
    ...现有代码...
```

**安全性**：规则抽不出（无"预警"关键词）→ 回退 LLM。规则选错接口时，`_filter_warning_records_for_user`/`_warning_records_from_payload` 等下游仍会按问法过滤，接口多查一般无害（如查了 effective + history，空接口返回空记录）。

## 载体

| 文件 | 改动 |
|------|------|
| `chainlitexam/tools/warning_workflow.py` | 新增 `_route_warning_tools_rule_based`；`_route_warning_tools` 规则优先 |

## 测试

- 规则路由：验证"现在有什么预警"→ `get_effective_warning_info`；"暴雨预警解除了吗"→ `get_effective_warning_info` + `get_history_warning_info`；"今天发布了哪些预警"→ 含 `get_today_warning_summary`；"中央气象台和天津预警"→ 含 `get_national_warning_info` + `get_effective_warning_info`。
- 规则失败（无"预警"关键词）→ None → LLM 兜底。
- 全量测试回归。

## 风险

- **规则路由 vs LLM 路由**：现有 LLM 路由可能在某些边缘问法上选更多接口（如同时查多个）。规则路由保守，通常选 1-2 个接口。多接口冗余查询会浪费一点时间（但接口本身快，如 `get_effective_warning_info` 2.3s），不影响回答正确性。
- 不改 `_generate_warning_core_and_advice`（那是必要的结论 LLM，不能省）。