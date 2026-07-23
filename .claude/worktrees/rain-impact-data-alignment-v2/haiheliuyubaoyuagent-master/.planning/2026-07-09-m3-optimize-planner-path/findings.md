# Findings & Decisions: M3 优化 Planner-Only 路径

**PLAN_ID:** 2026-07-09-m3-optimize-planner-path  
**日期:** 2026-07-09

## Requirements
- 在 `ENABLE_FAST_PATHS=false`（默认）时，决策天气 POI 查询（如“梅江会展中心明天天气怎么样”）仍能高质量回答。
- 把原 fast path 中抽槽、POI 定位、代表站匹配、滚动预报、格式化回答封装成单一工具 `query_decision_weather_for_poi`。
- 增强 `WEATHER_ASSISTANT_PROMPT`，明确 planner 何时应调用新工具、何时不应调用。
- 新增工具需可独立测试、可回滚。

## Research Findings
- 设计文档位置：`docs/superpowers/specs/2026-07-09-m3-optimize-planner-path-design.md`
- 实施计划位置：`docs/superpowers/plans/2026-07-09-m3-optimize-planner-path-plan.md`
- 骨架文件已提交：`chainlitexam/tools/decision_weather.py`（268 行），包含 `_extract_slots`、`_normalize_slots`、`_generate_answer` 与 `build_decision_weather_tools`。
- 骨架文件依赖 `message_orchestrator.py` 中的多个 helper（`_find_tool`、`_unwrap_tool_observation`、`_decision_weather_prefilter` 等）。
- 当前 git 状态干净，无未提交改动；上一次提交即为骨架文件。

## Technical Decisions
| Decision | Rationale |
|----------|-----------|
| 在 `chain_gzt.py` 的 `on_chat_start` 中，等 `answer_chain` 与 `callbacks` 创建完成后再构建 decision-weather 工具 | 工具工厂需要这些运行时依赖 |
| 在 `prompts.py` 的 `### 4. 知识库类问题回答规范` 之前插入 `### 5. 决策天气 POI 查询规范` | 保持原有章节顺序，新增规范紧跟工具使用规范 |
| 测试使用 `chainlitexam/tests/stubs.ensure_stubs()` | 与现有测试一致，避免 Chainlit 导入问题 |

## Issues Encountered
| Issue | Resolution |
|-------|------------|
| planning-with-files 默认 catchup 脚本路径在 Windows 下不存在 | 改用插件缓存目录中的实际路径 |

## Resources
- `docs/superpowers/specs/2026-07-09-m3-optimize-planner-path-design.md`
- `docs/superpowers/plans/2026-07-09-m3-optimize-planner-path-plan.md`
- `chainlitexam/tools/decision_weather.py`
- `chainlitexam/chain_gzt.py`
- `chainlitexam/prompts.py`
- `chainlitexam/tests/stubs.py`
