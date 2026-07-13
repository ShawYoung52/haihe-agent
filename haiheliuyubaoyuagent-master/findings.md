# Findings: 实时思考不可见根因

**日期:** 2026-07-08

## Requirements
- 用户在 Chainlit 前端发送“测试实时思考”后，应能看到类似 DeepSeek 的实时思考/推理过程
- 当前现象：前端卡住，然后直接输出最终答案；后端日志显示 thinking stream 成功（content_len=242）

## Research Findings
- `chainlitexam/message_orchestrator.py:40` 定义 `ReasoningStep`，使用 `cl.Step(name="🤔 思考过程", type="llm")`
- `.chainlit/config.toml:128` 已配置 `cot = "full"`，理论上应展示所有 step
- Chainlit 1.1.300+ 后 Chain of Thought 被重构为“仅一层深”，UI 主要渲染父 step 的 `output`
- `ReasoningStep.append()` 逻辑：当存在 `_current_stage` 时，只把 token 追加到子 stage 的 `output`，不更新父 step 的 `output`
- `process_message()` 在思考生成后会调用 `reasoning.stage("🔍 理解问题", ...)`，导致后续 planner stream 的 token 全部进入子 stage
- 父 step output 在流式过程中保持为空；`close()` 时若 `_buffer` 为空则设为 "思考完成"，用户因此看不到任何实时思考内容
- **第二个根因**：`ReasoningStep` 创建 `cl.Step` 时未指定 `parent_id`，Chainlit 会把它当成 root step，默认 CoT 视图不渲染 root step；需要挂到 `cl.context.current_run` 下
- **第三个问题**：`ReasoningStep` 里用的是 `collapsed` 而非 Chainlit 识别的 `default_open`，思考块默认折叠/不醒目；改为 `default_open=True`
- **第四个问题**：Planner 返回的 `tool_calls` 可能包含 `id=None` 或 `name=""` 的畸形调用，直接构造 `ToolMessage` 会触发 Pydantic `ValidationError`
- **第五个问题**：`stage()` 既把标题写入父 output，又创建同名的嵌套子 stage，导致 Chainlit 前端把同一内容渲染两次；改为只在父 output 中写 Markdown 标题，不再创建子 stage
| Decision | Rationale |
|----------|-----------|
| 父 step output 作为唯一真实展示内容 | Chainlit UI 的 CoT 视图以父 step 的 output 为展示主体 |
| 子 stage 不再承载流式 token | 避免内容被隐藏；子 stage 如需保留可仅作调试/数据层用途 |
| `stage()` 不再创建嵌套子 stage | 避免父 output 和子 stage 重复渲染同一内容；只保留父 step 中的 Markdown 阶段标题 |
| `__aenter__()` 获取 `cl.context.current_run.id` 作为 `parent_id` | 无 parent_id 的 step 是 root step，Chainlit CoT 不渲染；挂到 run 下才可见 |
| `default_open=True` 替代无意义的 `collapsed` | Chainlit 只识别 `default_open`，确保思考块默认展开 |
| `_ensure_tool_calls_from_content()` 规范化 tool_calls | 补全缺失 id、过滤空 name，防止 `ToolMessage` 构造崩溃 |

## Issues Encountered
| Issue | Resolution |
|-------|------------|
| 现有 `test_append_updates_current_stage` 断言与新行为冲突 | 同步更新测试 |
| `test_step_initially_expanded` / `test_step_collapses_on_close` 使用已废弃的 `collapsed` | 改为断言 `default_open` |
| Planner 返回空 name / None id 的 tool_call 导致 `ToolMessage` 校验失败 | 在 `_ensure_tool_calls_from_content` 中规范化过滤 |

## 新发现：思考过程未在回答结束后自动折叠（2026-07-10）

### 现象
- 后端 TypeError（`Step.__init__() got an unexpected keyword argument 'auto_collapse'`）已修复
- 但用户反馈：思考过程在最终答案发送后仍然展开，没有自动收起

### 根因分析
1. `.chainlit/config.toml:180` 显示 `generated_by = "2.9.6"`，说明生产/测试环境运行 Chainlit 2.9.6
2. `auto_collapse` 参数是 Chainlit 2.10.0（PR #2818）才加入后端 `Step.__init__` 和前端 `Step.tsx` 的
3. Chainlit 2.9.6 的标准前端 `Step.tsx` 不识别 `autoCollapse` 字段，因此不会自动折叠
4. `ReasoningStep` 当前实现：
   - 新版本：传 `auto_collapse=True`，依赖前端自动折叠
   - 旧版本：回退到 `close()` 时设置 `default_open=False`
5. 但 `default_open` 只在 Step 组件初始渲染时决定 `openValue`，对已展开的 step 调用 `update()` 设置 `default_open=False` 不会触发前端重新折叠
6. 因此旧版本 Chainlit 上思考过程会一直保持展开

### 可选方案
| 方案 | 说明 | 优点 | 缺点 |
|------|------|------|------|
| A. 升级 Chainlit 到 >= 2.10.0 | 生产/开发环境都升级 | 原生支持 `auto_collapse`，无需额外代码 | 需要运维配合，可能涉及数据库迁移（2.9.4 需要 `modes` 列） |
| B. 自定义前端实现折叠 | 在 `frontend/` 或 `frontendAgent/` 中覆盖 Step 组件 | 不依赖 Chainlit 版本 | 需要维护 custom build，可能复杂 |
| C. 后端在最终答案中折叠思考 | 例如发送一个特殊 step update 让前端重新挂载 | 不升级前端 | 需要改变 React key 或类似技巧，不可靠 |

### 推荐方案
- **首选方案 A**：升级 Chainlit 到 >= 2.10.0（当前开发环境已是 2.11.1，说明代码已为新版本准备）
- 同时保留后端对旧版本的兼容（不崩溃），但明确文档说明：自动折叠需要 Chainlit >= 2.10.0

### 资源
- Chainlit PR #2818: https://github.com/Chainlit/chainlit/pull/2818
- Chainlit 2.10.0 release: https://github.com/Chainlit/chainlit/releases/tag/2.10.0
- 本地 Chainlit 2.11.1 源码确认 `Step.__init__` 支持 `auto_collapse`
- 生产 config.toml 显示 `generated_by = "2.9.6"`
