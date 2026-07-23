# 任务计划：建立项目上下文文档

## 目标
为 `haiheliuyubaoyuagent-master` 项目建立清晰的上下文规则，输出/更新 4 份非技术人员可读懂的文档：
1. `PRODUCT.md` — 产品定位、用户、范围、不做的事
2. `DESIGN.md` — 业务工具形态、PC/手机使用方式、设计约束
3. `AGENTS.md` — 写给 Codex 的项目工作手册
4. `current-progress.md` — 当前进度、已完成、下一步、风险

## 阶段

### 阶段 1：盘点现有文档与项目现状
- [x] 读取已有的 PRODUCT.md、DESIGN.md、AGENTS.md、current-progress.md
- [x] 读取项目 README.md 了解整体结构
- [x] 核对 AGENTS.md 中引用的文件是否存在
- [x] 查看最近 git 提交，确认实际开发进度

### 阶段 2：修正 AGENTS.md 中的过时/错误引用
- [x] 删除或修正不存在的 `USER_API.md`、`USER_AND_FRONTEND_INTEGRATION.md`、`REST_API_README.md`、`RIVER_API_README.md` 引用
- [x] 补充 `fast_paths/` 目录说明
- [x] 确认 `message_orchestrator.py` 与 `prompts.py` 的当前关系

### 阶段 3：更新 current-progress.md
- [x] 反映近期代码提交（rainfall impact river features、emergency response 调整等）
- [x] 更新文件结构线索
- [x] 调整下一步任务和风险

### 阶段 4：确认 PRODUCT.md 与 DESIGN.md
- [x] 检查是否仍符合当前项目意图
- [x] 无需大幅调整，保持原内容

### 阶段 5：输出与汇报
- [x] 汇总 4 份文档路径和关键结论
- [x] 向用户说明改动点和待决策事项

## 遇到的错误
| 错误 | 尝试次数 | 解决方案 |
|------|---------|---------|
| 无 | - | - |
