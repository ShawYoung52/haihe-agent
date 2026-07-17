# Task Plan: 滦河系河名显示错误修复

**PLAN_ID:** 2026-07-15-luan-river-naming  
**创建时间:** 2026-07-15  
**Goal:** 修复暴雨影响河流工具中滦河系（is_luan=true）河名为单字缩写、且可能误匹配到海河系同名 objectid 的问题。

## Current Phase
All phases complete.

## Phases

### Phase 1: Requirements & Discovery
- [x] 确认滦河系名字来源（pkl 图 + full_v6 表）
- [x] 确认同一 objectid 同时存在 Haihe/Luan 两行，当前未按 is_luan 过滤
- [x] 从 `luanhe_all.shp` 得到 objectid → 全名映射
- **Status:** complete

### Phase 2: Design Fix
- [x] 决定映射表存储方式（内置默认 + 可选外部 JSON）
- [x] 设计 `is_luan` 透传路径：pkl 边 → downstream_temp → SQL JOIN → GeoJSON properties
- [x] 设计 `is_luan` 过滤：直接河段查询也返回 `is_luan`，下游 JOIN 增加 `is_luan` 条件
- **Status:** complete

### Phase 3: Implement Traction Agent Fix
- [x] 在 `rainfall_impact_geojson.py` 中：
  - `_query_direct_rows` 返回 `is_luan`
  - `_create_downstream_temp` 增加 `is_luan` 列
  - `_save_downstream_edge` / `_collect_downstream_edges` 透传 `is_luan`
  - `_query_downstream_rows` JOIN 增加 `is_luan` 匹配
  - `_river_feature` 写入 `properties.is_luan`
  - 新增 `_apply_luan_names`：对 `is_luan=true` 的要素按 objectid 映射为全名
  - 提供内置默认映射和外部 JSON 加载路径
- **Status:** complete

### Phase 4: Tests
- [x] 更新/新增 `test_rainfall_impact_geojson.py`：
  - 验证 Luan objectid 名称被映射为全名
  - 验证 Haihe objectid 名称不被误改
  - 验证 is_luan 透传到下游边和 GeoJSON
- **Status:** complete

### Phase 5: Code Review & Simplification
- [x] 使用 `code-review` 技能扫描改动
- [x] 使用 `code-simplifier` 简化代码
- **Status:** complete

### Phase 6: Verification
- [x] 运行牵引智能体测试 14/14 通过
- [x] 运行问答智能体测试 51/51 通过
- [x] 运行 fast path 18/18 通过
- [x] `py_compile` 通过
- **Status:** complete

### Phase 7: Documentation & Memory
- [x] 更新 `CLAUDE.md` 关于滦河系命名和 `is_luan` 过滤的说明
- [x] 使用 `claude-mem` 记录映射表决策与 `is_luan` 过滤
- **Status:** complete

### Phase 8: Regression Fix ( downstream duplicates )
- [x] 回退导致重复河段的严格 `is_luan` JOIN
- [x] 改用 pkl 边 `is_luan` 作为下游要素属性，保留 objectid-only JOIN + `match_priority` 排序
- [x] 修复下游几何裁剪锚点误从 `to_frac` 改为 `from_frac` 的回归
- [x] 运行 code-review / code-simplifier / verification 全流程
- [x] 更新 CLAUDE.md 与 claude-mem 关于修正后的 is_luan 语义
- **Status:** complete

## Key Questions
1. 是否接受内置默认映射 + 可选外部 JSON 的方案？
2. 对于 objectid=1（朱）和 objectid=8（东）等空间匹配不明确的，采用什么全名？
3. 是否需要同步修改上游 shapefile 生成脚本，避免未来再次丢失全名？（本次仅代码层修复）

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| 在代码层增加 `is_luan` 过滤 + 全名映射 | 上游数据重建成本高，代码层可快速修复并便于后续调优 |
| 映射表优先外部 JSON、内置默认兜底 | 便于业务人员人工校正，不重新部署代码即可更新 |
| 单字河名扩展仅对 `is_luan=true` 生效 | 避免误改海河系等非滦河系的单字河名 |
| 下游 JOIN 仅按 `objectid` 匹配，按 `match_priority` 优先同 is_luan | 严格 JOIN 会导致无匹配边回退到 pkl 直线，产生重复河段 |
| 下游要素 `is_luan` 取自 pkl 边而非 DB 行 | 即使几何匹配到海河系 DB 行，仍能用滦河映射表校正名称 |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| 单字规范化未按 `is_luan` 过滤 | code-review 发现 `_normalize_river_name` 对所有要素生效 | 改为在 `_apply_luan_names` 内仅对 `is_luan=true` 要素调用 |
| 下游 JOIN 未强制 `is_luan` 匹配 | code-review 发现仅通过 `match_priority` 排序，可能误匹配 | 改为 JOIN 条件 `ON p.objectid = e.objectid AND p.is_luan = e.is_luan` |
| 严格 `is_luan` JOIN 导致重复河段 | 用户内网验证发现下游边无法匹配同 objectid 的 DB 行，回退到 pkl 直线几何，与 direct_buffer 重复 | 回退为 objectid-only JOIN + `match_priority`，并将下游要素 `is_luan` 改为取自 pkl 边 |