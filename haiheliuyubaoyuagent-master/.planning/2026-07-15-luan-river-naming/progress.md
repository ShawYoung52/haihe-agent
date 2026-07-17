# Progress Log: 滦河系河名显示错误修复

**PLAN_ID:** 2026-07-15-luan-river-naming  
**会话日期:** 2026-07-15

## Session: 2026-07-15 (continued)

### Current Status
- **Phase:** 8 - Regression Fix（已关闭）
- **Started:** 2026-07-15
- **Continued:** 2026-07-16
- **Regression Reported:** 2026-07-16
- **Completed:** 2026-07-16

### Actions Taken
- 读取 `haihe-weather-analyzer-mcp/config.ini`，确认 graph 路径和 river_table。
- 加载 `E:/tj/line/result/river_directed_v6.pkl`，确认 Luan 边属性 `is_luan=true`、`src_name=""`、`river_name` 为单字缩写。
- 连接 PostgreSQL 查询 `haihe_river_directed_full_v6`：
  - 发现 `is_luan` 字段存在。
  - 确认同一 `objectid` 同时有 Haihe/Luan 两行（如 objectid=13：南拒马河 vs 青）。
- 读取 `E:\tj\滦河系\滦河系.shp`，确认其 `name` 字段只有单字缩写。
- 读取 `E:\tj\line\result\output_final\luanhe_all.shp`（raw UTF-8 解码），获取完整河名。
- 通过空间邻近分析生成候选映射，并整理出 objectid → 全名映射表。
- 创建新 plan 目录：`2026-07-15-luan-river-naming`。
- 在 `hhlyqyxt-master/utils/rainfall_impact_geojson.py` 中实现修复：
  - `_query_direct_rows` 返回 `is_luan`。
  - `_create_downstream_temp` 增加 `is_luan` 列。
  - `_save_downstream_edge` 从 pkl 边透传 `is_luan`。
  - `_query_downstream_rows` JOIN 增加 `p.is_luan = e.is_luan` 条件。
  - `_river_feature` 写入 `properties.is_luan`。
  - 新增 `_DEFAULT_LUAN_NAME_MAPPING`、`_load_luan_name_mapping()`、`_apply_luan_names()`，按 objectid 替换 Luan 要素河名为全名，并支持外部 `{stem}_luan_names.json` 覆盖/扩展。
  - 单字规范化 `_normalize_river_name()` 仅在 `is_luan=true` 时生效。
- 新增/更新 `hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py` 测试：
  - `test_luan_river_name_mapping_by_objectid`
  - `test_haihe_river_name_not_overwritten_by_luan_mapping`
  - `test_downstream_edge_carries_is_luan`
- 使用 `code-review` 技能发现两个问题并修复：
  - 单字规范化未按 `is_luan` 过滤 → 改为仅对 `is_luan=true` 要素调用。
  - 下游 JOIN 仅通过 `match_priority` 排序，未强制 `is_luan` 匹配 → 改为 JOIN 条件 `ON p.objectid = e.objectid AND p.is_luan = e.is_luan`。
- 使用 `code-simplifier` 简化代码：合并 `_apply_luan_names` 中的单字扩展与 objectid 映射，清理冗余 SQL 别名与注释。
- 更新 `CLAUDE.md`：补充 Luan River naming 段落，说明 `is_luan` 透传、JOIN 过滤、外部 JSON 映射。
- 更新 `claude-mem`：新增 `luan-river-naming.md` 并更新 `MEMORY.md` 索引。

### Phase 8: Regression Fix
- **Status:** in_progress
- **User feedback:** 内网验证发现严格 `is_luan` JOIN 后重复河段反而增多，滦河系修复未达预期。
- **Root cause:** `JOIN ON p.objectid = e.objectid AND p.is_luan = e.is_luan` 过于严格；当 pkl 边的 `is_luan` 在 full_v6 中找不到对应行时，下游边回退到 pkl 直线几何，与 direct_buffer 的真实河段重复。
- **Fix:**
  - 回退为 objectid-only JOIN，恢复 `match_priority`（优先同 is_luan 的 DB 行）。
  - 下游要素的 `is_luan` 属性改为取自 pkl 边（`e.is_luan`）而非 DB 行（`p.is_luan`），确保即使几何匹配到海河系 DB 行，仍能被 `_apply_luan_names` 校正为滦河全名。
  - 同步更新测试，确保下游行携带 pkl 边的 `is_luan`。
- 重新运行 code-review / code-simplifier / verification 全流程。
- 更新 CLAUDE.md 与 claude-mem，修正 is_luan 语义说明。

### Verification
- 牵引智能体测试：`utils/tests/test_rainfall_impact_geojson.py` 17/17 通过。
- 问答智能体测试：`chainlitexam/tests/` 51/51 通过。
- Fast path 静态检查：`chainlitexam/tests/test_fast_paths.py` 19/19 通过。
- 全部修改文件 `py_compile` 语法检查通过。

### Findings Snapshot
- Luan 边 objectid 1-21 对应单字缩写，映射为：
  - 1→滦河、2→兴州河、3→闪电河、4/5→洒河、6/7→洋河、8→东河、9→陡河、10→二滦河、11→大石河、12→冷口沙河、13→青龙河、14→瀑河、15→老牛河、16→伊逊河、17→蚁蚂吐河、18→武烈河、19→滦河、20→小滦河、21→柳河。
- 下游要素 `is_luan` 取自 pkl 边，几何匹配保留 objectid-only JOIN + `match_priority` 排序；名称由 `_apply_luan_names` 校正。

### Next
- Phase 8 完成后重新内网验证。

### Errors
| Error | Resolution |
|-------|------------|
| pyshp/dbfread 默认解码出现乱码 | 使用 `raw=True` 读取字节后手动 `decode('utf-8')` |
| 单字规范化未按 `is_luan` 过滤 | code-review 发现后，在 `_apply_luan_names` 内仅对 `is_luan=true` 要素调用 |
| 下游 JOIN 未强制 `is_luan` 匹配 | code-review 发现后，JOIN 条件增加 `AND p.is_luan = e.is_luan` |
| 严格 `is_luan` JOIN 导致重复河段 | 回退为 objectid-only JOIN + `match_priority`；下游要素 `is_luan` 改用 pkl 边值 |