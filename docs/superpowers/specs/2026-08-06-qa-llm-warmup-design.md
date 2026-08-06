# LLM 冷启动预热 设计

**日期**：2026-08-06
**状态**：设计（待用户审阅）
**范围**：`chainlitexam/chain_gzt.py`

## 背景

现有 `_warmup_qa`（`chain_gzt.py:540`）只调用 `_get_runtime()`，**仅构建运行时和连接工具**，不触发 Planner/Answer 真实推理。首个真实请求仍需等 LLM 冷启动（模型加载、连接池、推理管线初始化），造成首请求尾延迟。

GPT 方案阶段七要求增加**可选真实预热**：`ENABLE_LLM_WARMUP=false` 默认关，分别对 Planner 和 Answer 发最小非敏感请求，让模型完成一次真实推理。

## 硬约束（GPT 原则）

- **默认关闭**：`ENABLE_LLM_WARMUP=false`。关闭时行为不变（只构建运行时）。
- **最小非敏感请求**：预热请求不含真实用户数据、内网地址、工具结果。
- **独立短超时**：预热失败不阻断服务启动（现有 try/except 兜底）。
- **不写数据库**：预热不触发 Chainlit 数据层。
- **不进入外部接口缓存**：预热结果不入 `_response_cache`。
- **不记录真实用户数据**。

## 设计

### `ENABLE_LLM_WARMUP` 开关

`chain_gzt.py` `_warmup_qa` 中，构建运行时后，若开启则发预热请求：

```python
@cl.on_app_startup
async def _warmup_qa():
    if not qa_http_api.runtime.configured:
        qa_http_api.runtime.configure(_build_qa_runtime)
    try:
        print("[QA-API] warming up...")
        runtime = await qa_http_api.runtime._get_runtime()
        if os.environ.get("ENABLE_LLM_WARMUP", "false").strip().lower() in ("1", "true", "yes"):
            await _llm_warmup(runtime)
            print("[QA-API] LLM warmup done")
        else:
            print("[QA-API] ready (LLM warmup disabled)")
    except Exception as e:
        print(f"[QA-API] warmup failed (will lazy-load): {type(e).__name__}")
```

### `_llm_warmup(runtime)` 真实推理预热

```python
async def _llm_warmup(runtime: dict) -> None:
    """对 Planner 和 Answer 发最小非敏感请求，触发一次真实推理预热。"""
    warmup_msg = [HumanMessage(content="请回复一个字：好")]
    planner_chain = runtime["planner_chain"]
    answer_chain = runtime["answer_chain"]
    try:
        # Planner 预热（bind_tools 后发最小请求）
        await asyncio.wait_for(planner_chain.ainvoke({"messages": warmup_msg}), timeout=30)
        print("[LLM-WARMUP] planner done")
    except Exception as e:
        print(f"[LLM-WARMUP] planner failed: {type(e).__name__}")
    try:
        # Answer 预热
        await asyncio.wait_for(answer_chain.ainvoke({"messages": warmup_msg}), timeout=30)
        print("[LLM-WARMUP] answer done")
    except Exception as e:
        print(f"[LLM-WARMUP] answer failed: {type(e).__name__}")
```

> 注意：`planner_chain.ainvoke({"messages": [...]})` 会触发 `bind_tools` 的工具 Schema 组装 + 一次推理，但**不会调用工具**（消息只有一条 HumanMessage，无 tool_calls）。`answer_chain.ainvoke` 同理。预热请求内容"请回复一个字：好"非敏感。

### 安全点

- 预热在 `_warmup_qa` 内，`_get_runtime()` 已成功（运行时就绪）才执行。失败被 try/except 兜底。
- 预热不写 Chainlit 数据层（只是 `ainvoke`，不经过 emitter）。
- 预热不进 `_response_cache`（不调 `QARuntime.ask`）。
- 预热用 `asyncio.wait_for` 30s 短超时，防止卡死启动。

## 载体

| 文件 | 改动 |
|------|------|
| `chainlitexam/chain_gzt.py` | `_warmup_qa` 加 `ENABLE_LLM_WARMUP` 开关 + `_llm_warmup` 函数 |

## 测试

- `ENABLE_LLM_WARMUP=false` 时 `_warmup_qa` 只构建运行时，不调 LLM。
- `ENABLE_LLM_WARMUP=true` 时调用 `_llm_warmup`，用 fake chain 验证 planner/answer 各 ainvoke 一次。
- 预热失败（fake chain 抛错）不阻断，打印日志。
- 全量测试回归。

## 风险

- **默认关闭**：`ENABLE_LLM_WARMUP=false` 行为不变。
- **失败兜底**：预热失败只打日志，不影响启动。
- **非敏感**：预热请求不含真实数据。
- 预热是可选优化，内网验证确认能降低首请求延迟后再启用。