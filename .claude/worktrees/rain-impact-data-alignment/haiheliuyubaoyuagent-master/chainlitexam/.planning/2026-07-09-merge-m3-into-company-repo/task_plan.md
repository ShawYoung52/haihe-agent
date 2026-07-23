# Task Plan: 将 M3 决策天气 POI 工具合并到公司仓库

**PLAN_ID:** 2026-07-09-merge-m3-into-company-repo  
**Goal:** 把开发分支（C:\...\haiheliuyubaoyuagent-master）上的 M3 `query_decision_weather_for_poi` 改动与同事的清理改动合并到公司仓库（E:\python\haiheliuyubaoyuagent），保持现有 fast path 行为并避免冲突。

## Current Phase
Phase 5 — Delivery（complete）

## Phases

### Phase 1: Requirements & Discovery
- [x] 确认公司仓库路径 `E:\python\haiheliuyubaoyuagent` 及当前分支 `master`
- [x] 确认同事未提交改动：删除 `.chainlit/config.toml`+翻译文件、删除 `chainlitexam/bf.py` 和 `chainlitexam/testchain20260303.py`
- [x] 确认公司仓库没有 `ENABLE_FAST_PATHS` 开关，仍保留原 `DecisionWeatherQAService` fast path
- **Status:** complete

### Phase 2: Planning & Structure
- [x] 决定合并策略：不照搬 `decision_weather_core.py` 重构，而是直接新增 `chainlitexam/tools/decision_weather.py`，复用公司仓库 `message_orchestrator.py` 已有 helper
- [x] 需要同步改动的文件：`chain_gzt.py`、`prompts.py`、`message_orchestrator.py`
- **Status:** complete

### Phase 3: Implementation
- [x] 新增 `chainlitexam/tools/decision_weather.py`（支持 general_weather / rain_now / rain_next_hours）
- [x] 修改 `chainlitexam/chain_gzt.py` 注册工具
- [x] 修改 `chainlitexam/prompts.py` 增加 POI 路由规范
- [x] 修改 `chainlitexam/message_orchestrator.py` 修复 `_decision_weather_prefilter`
- **Status:** complete

### Phase 4: Testing & Verification
- [x] 对公司仓库改动文件运行 `python -m py_compile`，无语法错误
- [x] 运行 `superpowers:verification-before-completion` 要求的新鲜验证
- **Status:** complete

### Phase 5: Delivery
- [x] 先提交同事清理改动（2 个 chore commit）
- [x] 再提交 M3 合并改动（1 个 feat commit）
- [x] 使用 claude-mem 更新项目记忆
- **Status:** complete

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| 公司仓库不引入 `decision_weather_core.py` | 公司仓库 `message_orchestrator.py` 已有所需 helper，引入 core 会额外改动并可能与同事代码冲突 |
| 工具内复用 `_decision_hourly_window` / `_build_decision_hourly_facts` | 保持与现有 `DecisionWeatherQAService` 对 rain_now / rain_next_hours 的行为一致 |
| 从 `institution_suffixes` 中剔除“区/县/市/省” | 避免“西青区天气”“天津市天气”等宽泛区域查询被误判为 POI |
| 先提交同事清理再提交 M3 功能 | 区分两类变更，便于回滚和 review |

## Errors Encountered
| Error | Resolution |
|-------|------------|
| `langchain_core` 在公司 venv 未安装，无法直接 import 验证 | 改用 `py_compile` 做语法级验证；运行时依赖由部署环境保证 |
| 初始 prefilter 会把“西青区天气”等宽泛区域当作 POI | 剔除行政区域后缀；同时保留“地点+时间”类查询 |
| 新工具缺少 rain_now / rain_next_hours  specialization | 复用公司仓库已有的 `_decision_hourly_window` 和 `_build_decision_hourly_facts` |