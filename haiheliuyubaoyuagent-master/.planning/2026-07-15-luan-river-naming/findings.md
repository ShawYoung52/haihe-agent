# Findings: 滦河系河名显示错误

**PLAN_ID:** 2026-07-15-luan-river-naming  
**日期:** 2026-07-15

## 问题现象
- 滦河系（`is_luan=true`）的受影响河流名字仍是单字缩写（如“朱、兴、闪、潵、洋、东、陡、二、石、沙、青、瀑、老、伊、蚁、武、滦、小、柳”）。
- 用户反馈“南拒马河”出现在滦河系区域，名字“全不对”，且都是“滦河系合并之前的”。

## 数据来源调查

### 1. 当前代码读取的河名来源
- `haihe-weather-analyzer-mcp/config.ini` 中 `graph = E:/tj/line/result/river_directed_v4_asis.pkl`。
- 运行时代码通过 `_resolve_graph_path` 优先使用同目录下的 `river_directed_v6.pkl`。
- pkl 图边属性里，`is_luan=true` 的边 `src_name=""`、`river_name` 为单字缩写（如“兴、闪、洋河”等）。
- `hhlyqyxt-master/utils/rainfall_impact_geojson.py` 默认从 `src_name` 取河名；滦河系行 `src_name` 为空，因此回退到 pkl 图的 `river_name`，得到单字名。

### 2. 数据库 `haihe_river_directed_full_v6` 结构
- 关键字段：`objectid`、`river_name`、`src_name`、`is_luan`、`geom`。
- 同一 `objectid` 同时存在 Haihe 行（`is_luan=false`，全名如“拒马河、南拒马河”）和 Luan 行（`is_luan=true`，单字缩写）。
- 例如 `objectid=13`：Haihe 行为“南拒马河”，Luan 行为“青”。
- 当前下游匹配仅按 `objectid` 关联，未加 `is_luan` 过滤，因此 Luan 的 pkl 边理论上可能匹配到 Haihe 的同名 objectid，导致“南拒马河”出现在滦河区域。

### 3. 滦河系原始数据
- `E:\tj\滦河系\滦河系.shp` 只有 `name` 字段，值就是单字缩写。
- 正确的完整河名来自 `E:\tj\line\result\output_final\luanhe_all.shp`（从 `D:\tj\水系\water_line_haihe\water_line_haihe.shp` 提取）。
- 通过空间匹配可得到 objectid → 全名的映射（如 2→兴州河、3→闪电河、13→青龙河、16→伊逊河、19→滦河等）。

## 根因
1. **数据层**：合并进 `river_directed_v6.pkl` / `haihe_river_directed_full_v6` 的滦河系数据只带了单字缩写，未带入 `luanhe_all.shp` 中的完整河名。
2. **代码层**：
   - 下游匹配 `JOIN tmp_downstream_edges ON p.objectid = e.objectid` 未按 `is_luan` 过滤，可能把 Luan 的 pkl 边匹配到 Haihe 的真实河段。
   - 河名回填逻辑没有滦河系 objectid → 全名的映射。

## 修复实现
1. **`is_luan` 透传：**
   - `_query_direct_rows` 现在返回 `is_luan`。
   - `_create_downstream_temp` 增加 `is_luan` 列。
   - `_save_downstream_edge` 从 pkl 边属性透传 `is_luan`。
   - `_query_downstream_rows` 保留 objectid-only JOIN，通过 `match_priority` 优先选择同 is_luan 的 DB 行；下游要素的 `is_luan` 取自 pkl 边，确保名称校正正确。
   - `_river_feature` 将 `is_luan` 写入 GeoJSON properties。
2. **滦河系全名映射：**
   - 新增内置 `_DEFAULT_LUAN_NAME_MAPPING`（objectid 1-21 → 全名）。
   - 新增 `_load_luan_name_mapping()`：优先加载 graph 同目录的 `{stem}_luan_names.json`，缺失时使用内置默认。
   - 新增 `_apply_luan_names()`：仅对 `is_luan=true` 的要素，先补全单字为“X河”，再按 objectid 替换为全名。
   - 单字规范化 `_normalize_river_name()` 仅对 `is_luan=true` 要素生效，避免误改海河系单字名。
3. **测试覆盖：**
   - `test_luan_river_name_mapping_by_objectid`：验证 objectid=13 的 Luan 要素从“青”映射为“青龙河”。
   - `test_haihe_river_name_not_overwritten_by_luan_mapping`：验证 `is_luan=false` 的同名 objectid 保持“南拒马河”。
   - `test_downstream_edge_carries_is_luan`：验证 `_save_downstream_edge` 透传 `is_luan`。

## 回归与修正
- **问题：** 内网验证发现 `_query_downstream_rows` 使用 `JOIN ON p.objectid = e.objectid AND p.is_luan = e.is_luan` 后，大量下游边因找不到同 is_luan 的 DB 行而回退到 pkl 直线几何，导致重复河段。
- **修正：**
  - 回退为 objectid-only JOIN，恢复 `match_priority` 排序。
  - 下游要素的 `is_luan` 改为取自 pkl 边（`e.is_luan`），即使几何匹配到海河系 DB 行，仍能被 `_apply_luan_names` 校正为滦河全名。

## 验证结果
- 牵引智能体测试：`utils/tests/test_rainfall_impact_geojson.py` 14/14 通过。
- 问答智能体测试：`chainlitexam/tests/` 51/51 通过。
- Fast path 静态检查：`chainlitexam/tests/test_fast_paths.py` 19/19 通过。
- 全部修改文件 `py_compile` 通过。

## 决策
- 不重建上游 shapefile/pkl（超出本次代码范围），而是在 `rainfall_impact_geojson.py` 中通过 `is_luan` + `objectid` 精确匹配，并加入一个可维护的滦河系全名映射表。
- 映射表优先以外部 JSON 文件形式存在；若文件缺失则使用内置默认映射，方便后续人工校正。
- 单字河名扩展仅对 `is_luan=true` 生效，防止海河系单字名被误改为“X河”。
- 下游几何匹配保留 objectid-only JOIN + `match_priority`；名称/河系归属由 pkl 边的 `is_luan` 与映射表共同决定，避免严格 JOIN 导致的回退重复。