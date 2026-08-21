# 风险分层回答 + 灾害点匹配 + 看图器修复 — 部署说明（2026-08-21 · Arc 3 批次）

本批次三件事，一次部署：

1. **看图器修复**：前端同事 2026-08-21 重新构建 AgentWeb 后，`index.html` 改为引用 `./public/img-zoom-agentweb.js`（public/ 子目录约定），旧文件在 webapp 根级 → 404，看图器静默失效。**纯路径问题，JS 内容本身完好，无需改 JS**。
2. **风险接口 → 灾害点匹配**：`/hhfw/riskWarnNew/findDataListByConfig`（返回经度/纬度/风险等级）在 **MCP 后端工具内**按 haversine 就近匹配三张静态隐患表（地灾/山洪/中小河流），命中附 `hazard_id` 等；并产出各区县隐患点总数 + 逐区县×逐等级风险统计 + 逐级防范建议。**纯后端改动，与前端无关**。
3. **司南分层回答**：地理灾害问答最后一段改为按等级分层（先各区县隐患点总数与本次各级数量，再逐级防范建议），不再是"泛泛防范建议"。

---

## 一、改动清单（拷贝清单）

> 服务器无 git，整文件拷贝覆盖即可。**内部服务地址在代码里一律用 env 占位符**（`RISK_WARN_BASE` 等），不写死 IP。

### MCP 进程（haihe-weather-analyzer-mcp）
| 源（本仓库） | 目标（服务器） | 说明 |
|---|---|---|
| `haihe-weather-analyzer-mcp/custom_tools/risk_warning_tool.py` | 同路径 | 灾害点匹配 + 分层统计 + 逐级防范建议核心改动 |
| `haihe-weather-analyzer-mcp/custom_tools/diagnose_risk_api.py` | 同路径 | 服务器侧诊断脚本：打印 findDataListByConfig 真实 body 锁定字段名（见第四节验证 2） |
| `haihe-weather-analyzer-mcp/tests/test_risk_warning_hazard_match.py` | 同路径（可选） | 15 条测试，部署后可在服务器跑确认 |

### chainlitexam 进程（8003）
| 源（本仓库） | 目标（服务器） | 说明 |
|---|---|---|
| `chainlitexam/prompts.py` | 同路径 | 规则 #12（双份）改为必须按等级分层作答 + 逐字采用 `level_advice` |
| `chainlitexam/fast_paths/risk_warning_fast_paths.py` | 同路径 | `_format` 渲染逐级统计/隐患点总数/逐级防范建议（无风险时自动回退笼统建议） |
| `chainlitexam/tests/test_risk_warning_fast_paths.py` | 同路径（可选） | 4 条测试 |

### AgentWeb 前端（看图器修复 + sim-time 面板；2026-08-21 用户确认放 **webapp 根级**，不建 public/）
| 源（本仓库） | 目标（服务器） | 说明 |
|---|---|---|
| `chainlitexam/AgentWeb/img-zoom-agentweb.js` | `.../webapps/AgentWeb/img-zoom-agentweb.js` | 看图器。webapp 根级 |
| `chainlitexam/AgentWeb/sim-time-agentweb.js` | `.../webapps/AgentWeb/sim-time-agentweb.js` | 时间切换面板。webapp 根级；面板运行时锚在页面"说明"元素旁（找不到回退右下角悬浮） |
| `chainlitexam/AgentWeb/index.html` | `.../webapps/AgentWeb/index.html` | `<head>` 引用两个根级脚本：`./img-zoom-agentweb.js` + `./sim-time-agentweb.js`（2026-08-21 修复：同事构建产物引用 `./public/...` 但文件在根级 → 404，改回根级引用） |

---

## 二、部署步骤

1. **拷贝文件**：按第一节清单拷到服务器两个包目录 + AgentWeb public/。
2. **AgentWeb 前端**（看图器 + sim-time 面板，webapp **根级**）：
   - 把 `img-zoom-agentweb.js`、`sim-time-agentweb.js`、`index.html` 拷到 `webapps/AgentWeb/`（根级）；
   - 验证：浏览器打开 AgentWeb，Network 里 `GET /img-zoom-agentweb.js` 和 `/sim-time-agentweb.js` 必须 **200**（404 = 没放对位置）；点聊天里的图片应弹出滚轮缩放看图器；页面"说明"旁应出现「🕒 系统时间」面板（锚定不到时回退右下角悬浮）。
   - **无需重启 Tomcat**（静态资源即拷即用，必要时清浏览器缓存）。
   - **注意**：若前端同事重新构建把 index.html 改回 `./public/*.js` 引用，404 会复现——把引用改回根级（`./img-zoom-agentweb.js`/`./sim-time-agentweb.js`）即可。
3. **重启两个后端服务**（都必须重启，风险匹配改动才生效）：
   - `systemctl restart haihe-chainlit`（8003）
   - MCP `server.py` 进程（SSE 3333，重启后等就绪）
4. **验证**（见第四节）。

---

## 三、接口与字段说明

`query_risk_warning`（MCP 工具，`risk_kind` = `river`/`mountain`/`geologic`）返回在原有 `_summarize` 基础上新增四个字段：

| 字段 | 含义 |
|---|---|
| `county_totals` | 各区县隐患点静态表全量总数（如 `{"冀州区": 257, "蓟州区": 89}`） |
| `county_risk_summary` | 逐区县×逐等级风险记录数（**严重度优先排序**：一级>二级>三级>四级，同级按数量降序） |
| `level_advice` | 逐级防范文案（一级~四级，按国标起草，**文案待业务确认**） |
| `hazard_match` | 匹配统计：`id_matched_count`/`haversine_matched_count`/`unmatched_count`/`total_records`/`radius_km` |

- **匹配方式（2026-08-21 用户确认接口返回 id/name）**：接口每条记录已带隐患点 `id`/`name`（样本：`{"name":"石界滑坡","lon":113.75,"id":68,"lat":36.25,"level":5}`）。**有 id 的记录按 id 直连静态隐患表**取 county/city（`match_method="id"`，不靠经纬度）；记录本身无 id 才按经纬度就近匹配兜底（`match_method="haversine"`，`RISK_WARNING_MATCH_RADIUS_KM` 默认 1.0，钳 0-10）；有 id 但静态表查不到时不就近猜测（避免挂错隐患点）。每条记录附 `hazard_id`/`hazard_name`/`county_name`/`city_name`。**健壮性（code-review 2026-08-21 修正）**：id 做类型归一（int 68 / float 68.0 / str "68" 均视为 68，JSON 序列化把数字打成 68.0 也能直连）；`id=0` 视为无 id 走就近兜底；`RISK_WARNING_MATCH_RADIUS_KM` 非法值回退 1.0，绝不击穿工具导入；记录无 county/area 时县级汇总记 `未知区域`，**绝不拿隐患点名（如"石界滑坡"）当区县名**。
- **等级归一（数字越大风险越高）**：接口返回中文 一~四级 / 红橙黄蓝 / 数字（样本 `level:5`）→ 统一归一到「一级(红)最重 ~ 四级(蓝)最轻」：`5→一级、4→二级、3→三级、2→四级`；`1≈无/极低风险` 不列入本次风险。**方向为假设，需服务器真实等级分布确认**（若相反改 `_NUMERIC_LEVEL_MAP` 一个常量）。每条记录附 `level_norm`（展示用归一等级，记录表与统计表口径一致）；**`level_advice` 只含本次实际出现的等级**（本次仅四级时不再刷一级"立即转移"的最高级文案）。
- **表查询失败静默降级**：DB 不可用时 `county_totals={}`、`county_risk_summary=[]`、`hazard_match` 的 `enabled=false`，`level_advice` 仍按类型给出，天气/风险主体回答不受影响。

---

## 四、验证

1. **看图器 + 时间面板**：AgentWeb 页面 → Network 确认 `/img-zoom-agentweb.js` 与 `/sim-time-agentweb.js` 均 200 → 点一张聊天图片 → 滚轮缩放/拖拽/双击复位/背景点击关闭；页面"说明"旁应出现「🕒 系统时间」面板（输入时间→设置=锚定全局"现在"，恢复=还原真实时间）。
2. **风险工具直测**（服务器上，确认真实等级分布与 id 匹配）：
   ```bash
   cd haihe-weather-analyzer-mcp
   python custom_tools/diagnose_risk_api.py        # 打印 findDataListByConfig 真实 body
   ```
   - 核对：① 等级字段取值分布（中文/颜色/数字，方向是否"数字越大风险越高"）；② 接口返回的 `id` 是否都能命中静态隐患点表（看 `hazard_match` 的 `id_matched_count` vs `unmatched_count`）；③ 经纬度/名称字段名。发现不符 → 改 `risk_warning_tool.py`（`_NUMERIC_LEVEL_MAP` 方向 或 `_normalize_record` 候选键）后重启 MCP。
3. **分层回答端到端**（经 8003）：
   ```bash
   curl -X POST http://localhost:8003/api/v1/qa/ask -H 'Content-Type: application/json' \
     -d '{"question":"冀州区地质灾害风险怎么样？"}'
   ```
   期望回答形态：先「冀州区共 X 个地灾隐患点，本次一级 A 处、二级 B 处、三级 C 处、四级 D 处」，再逐级给出防范建议（**一级>二级>三级>四级，一级最重**）。不再只给泛泛防范建议。
4. **无风险场景**：若某时段无风险记录，回答应为「当前未发现明显地质灾害风险」+ 隐患点总数背景，**不刷四级防范文案**。

---

## 五、已知缺口 / 待确认

1. **数字等级方向未锁定**（最大风险）：接口样本为 `level=5`，代码按「数字越大风险越高（5=最高/红色）→ 5=一级」处理。`diagnose_risk_api.py` 打印真实等级**分布**后确认方向；若相反，改 `risk_warning_tool.py` 的 `_NUMERIC_LEVEL_MAP`（1 与 5 互换）即可。
2. **接口字段名部分已知**：`id`/`name`/`lon`/`lat`/`level`/`area_246` 已从样本确认；其余字段（时间/描述）用诊断脚本核对后补充到 `_normalize_record` 候选键。
3. **静态表 id 与接口 id 是否同源**：接口返回 id（如 68/69）假设与静态隐患点表 `id` 主键一致（同甲方数据源）；若不一致（id 对不上导致 `unmatched_count` 高），改用经纬度匹配或确认 id 映射。
4. **逐级防范文案按国标起草**，待业务确认用词。

---

## 六、回滚

- **看图器/时间面板**：删掉 `webapps/AgentWeb/` 根级的 `img-zoom-agentweb.js`/`sim-time-agentweb.js`，并从 index.html 移除对应 `<script src>` 行 → 回无查看器/无面板状态。
- **风险匹配/分层回答**：用改动前的整文件覆盖回两个包（`risk_warning_tool.py` / `prompts.py` / `risk_warning_fast_paths.py`）+ 重启两服务。
- 若 `level_advice` 文案被业务否决：只改 `risk_warning_tool.py` 的 `_LEVEL_ADVICE` 字典重启 MCP 即可，prompts/fast_path 无需动。
