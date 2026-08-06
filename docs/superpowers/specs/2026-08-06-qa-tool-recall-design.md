# 候选工具召回增强（Top-5/8/12 Recall）设计

**日期**：2026-08-06
**状态**：设计（待用户审阅）
**范围**：`chainlitexam/tools/tool_candidate_index.py` + `message_orchestrator.py` + 新 `scripts/recall_stats.py`

## 背景

现有 `[TOOL_CAND]` 影子日志（`message_orchestrator.py:4620-4626`）已记录 `actual`/`recalled`/`candidates`，但：
- 缺 `query_type`（无法按问题类型统计召回率）
- 缺 Top-5/8/12 分层召回（`candidates_for` 默认 limit=12，看不出更小候选集的表现）
- 无离线统计脚本（无法从日志算 Recall、漏召回列表）

GPT 方案阶段六要求完善影子模式，为将来 `ENABLE_ACTIVE_TOOL_FILTER=true` 启用做准备。

## 硬约束（GPT 原则）

- **纯影子观测**：不改 Planner 绑定（`bind_tools(tools)` 保持完整工具集）。
- **默认关闭**：`ENABLE_ACTIVE_TOOL_FILTER=false`。
- **候选失败 → 完整工具集合重试**（未来启用时：候选路由失败切回完整工具集，最多重试一次）。本期只记录。
- **外部回答不暴露候选工具信息**。

## 设计

### 1. `ToolCandidateIndex` 增强

在 `tools/tool_candidate_index.py` 增加 `candidates_for_top_n(user_text, n)`：

```python
def candidates_for_top_n(self, user_text: str, n: int) -> list[str]:
    """取候选工具前 n 个（按关键词命中顺序）。"""
    matched: list[str] = []
    for kw, names in self._by_keyword.items():
        if kw in (user_text or ""):
            for name in names:
                if name not in matched:
                    matched.append(name)
    for name in self._default_candidates:
        if name not in matched:
            matched.append(name)
    return matched[:n]
```

同时 `candidates_for(user_text, limit)` 内部复用 `candidates_for_top_n`（避免重复逻辑）。

### 2. `[TOOL_CAND]` 日志补 query_type + Top-N recall

`message_orchestrator.py` 的 `[TOOL_CAND]` 块扩展：

```python
    if planner_msg.tool_calls and callbacks.get("tool_candidate_index"):
        try:
            idx = callbacks["tool_candidate_index"]
            actual = [tc["name"] for tc in planner_msg.tool_calls]
            qtype = _evidence_query_type_from_tool_names(planner_msg)  # 复用 evidence 的 helper
            top5 = idx.candidates_for_top_n(message.content, 5)
            top8 = idx.candidates_for_top_n(message.content, 8)
            top12 = idx.candidates_for(message.content, limit=12)
            def _recall(top: list) -> str:
                hit = [t for t in actual if t in top]
                return f"{len(hit)}/{len(actual)}"
            print(f"[TOOL_CAND] request={session_id} query_type={qtype} actual={actual} "
                  f"recall_5={_recall(top5)} recall_8={_recall(top8)} recall_12={_recall(top12)} "
                  f"candidates_12={top12}")
        except Exception:
            pass
```

**安全性**：纯日志增强，不改绑定。try/except 兜底。

### 3. `scripts/recall_stats.py` 统计脚本

新增脚本，读 `[TOOL_CAND]` JSON Lines（需先让日志输出 JSON Lines——`[TOOL_CAND]` 现在是文本格式，改为 `[TOOL_CAND] {json}`）：

输出：
- 请求总数
- 按 `query_type` 的请求数
- Top-5/8/12 平均 Recall（recall_5/8/12 的分子和/分母和）
- 关键工具漏召回列表（某工具在 actual 中出现但不在 top12 候选）

**日志格式改 JSON Lines**：`print(f"[TOOL_CAND] {json.dumps({...}, ensure_ascii=False)}")`。

## 载体

| 文件 | 改动 |
|------|------|
| `chainlitexam/tools/tool_candidate_index.py` | 加 `candidates_for_top_n`；`candidates_for` 复用 |
| `chainlitexam/message_orchestrator.py` | `[TOOL_CAND]` 改 JSON Lines + 补 query_type/Top-N recall |
| `chainlitexam/scripts/recall_stats.py` | 新增统计脚本 |

## 测试

- `candidates_for_top_n`：天气/预警/水位问法各自召回对应工具；n=5/8/12 分层正确。
- `[TOOL_CAND]` JSON 可解析、含 query_type/recall_5/8/12。
- `recall_stats.py`：对样例日志输出正确 recall。
- 全量测试回归。

## 风险

- **纯影子**：不改绑定，不改回答。日志增强 try/except 兜底。
- `_evidence_query_type_from_tool_names` 复用（阶段五已实现），避免重复。