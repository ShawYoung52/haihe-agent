# Design: 流域未来天气按河系回答

**Date:** 2026-07-22  
**Scope:** `haihe-weather-analyzer-mcp` + `chainlitexam`  
**Data sources:** `public.haihe_zone_9` (PostgreSQL) + 滚动预报网格 / EC AIFS 降雨栅格

## Problem Statement

当前用户询问“海河流域明天天气怎么样”“大清河流域未来三天天气”等流域/子流域未来天气时，系统按“代表城市”返回结果：

- 全流域：北京、天津、石家庄、保定、唐山、沧州等；
- 子流域：大清河→保定、廊坊；子牙河→石家庄、衡水 等。

领导（台长）提出需要**再设计一种按河系/流域维度回答**的方式，以九大分区河系为主，代表城市作为补充细节。

## Design Principle

**流域未来天气回答以“河系（九分区）”为核心维度，降雨网格直接裁剪九分区边界得到各河系平均/最大/最小雨量；代表城市明细仅作为补充。**

## Components

### 1. 新增 MCP 工具 `get_river_system_rainfall_forecast`

位置：`haihe-weather-analyzer-mcp/tools.py`

职责：
- 读取 `haihe_zone_9` 表中的分区边界；
- 通过 `rolling_forecast_grid.resolve_forecast_grid_source` 选择滚动预报 `.nc` 或 EC AIFS tif；
- 将栅格按河系边界裁剪并统计，返回每个河系的降雨量。

入参：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `river_system` | str | 否 | 河系名称，如“大清河”“海河”“全流域”。不传则返回九大分区全部。 |
| `start_time` | str | 是 | 预报起始时间，格式 `YYYY-MM-DD HH:MM:SS`。 |
| `forecast_hours` | int | 否 | 预报时长，默认 24，支持 1-240（10 天）。 |
| `zone_type` | str | 否 | 分区类型，默认 `"9"`（海河九分区）。 |

出参 schema：

```json
{
  "data_source": "滚动预报网格（cycle=...）",
  "fcst_time": "YYYYMMDDHHMMSS",
  "forecast_hours": 24,
  "zones": [
    {
      "zone_name": "大清河",
      "average_rainfall_mm": 12.5,
      "max_rainfall_mm": 35.0,
      "min_rainfall_mm": 0.0
    }
  ]
}
```

### 2. 边界加载模块

在 `haihe-weather-analyzer-mcp` 中新增辅助函数：

- `load_zone_boundaries(zone_type="9", zone_name=None)`：从 PostgreSQL 读取 `haihe_zone_9` 的 `zone_code`、`zone_name`、`geom`；
- 返回 `list[dict]`，每个元素含名称和 `ogr.Geometry` 多边形。

复用现有 `config.ini` 中的 `postgres` 配置。

### 3. 栅格统计复用

新工具内部直接复用 `RainfallAnalyzer.get_city_rainfall_time_range` 的栅格打开、掩膜、统计逻辑，但将“城市要素”替换为“河系要素”。为减少重复，可将核心统计逻辑抽取为 `_compute_rainfall_stats_for_geometry(geometry, start_time, forecast_hours)`。

### 4. Chainlit 侧集成

- `chain_gzt.py`：注册新 MCP 工具，加入 `WEATHER_ASSISTANT_PROMPT` 可见工具列表；
- `prompts.py`：
  - 更新“子流域未来天气查询规范”，明确优先调用 `get_river_system_rainfall_forecast`；
  - 更新“流域预报/明天天气”规则，禁止对流域问题调用 `query_rolling_forecast`；
  - 给出回答格式：核心结论 + 河系主表 + 可选城市补充 + 数据来源。

### 5. Fast Path

`ENABLE_FAST_PATHS=false` 为当前默认，因此 fast path 不做重点改造。保留现有 `_try_basin_weather_fast_path` / `_try_subbasin_forecast_fast_path` 代码，仅确保常量命名一致，避免与新工具冲突。

## Data-Flow Diagram

```
用户提问：海河流域明天天气如何
        │
        ▼
Chainlit process_message()
        │
        ▼
Planner LLM (prompts.py 规则)
        │
        ▼
调用 get_river_system_rainfall_forecast(
        river_system="全流域",
        start_time="2026-07-23 02:00:00",
        forecast_hours=24)
        │
        ▼
MCP 工具
  ├── 读取 haihe_zone_9 边界
  ├── resolve_forecast_grid_source() 选滚动预报/EC
  ├── 栅格裁剪 + 统计（每河系 avg/max/min）
  └── 返回 zones + data_source
        │
        ▼
Planner LLM 生成回答
  ├── 核心结论
  ├── 河系主表
  ├── 可选：代表城市补充
  └── 数据来源：天津市气象台滚动预报 / ECMWF AIFS
        │
        ▼
返回给用户
```

## Interfaces

### 新增公开函数

- `get_river_system_rainfall_forecast(river_system, start_time, forecast_hours, zone_type) -> dict`

### 新增内部辅助函数

- `_load_zone_boundaries_from_db(zone_type, zone_name, config) -> list[dict]`
- `_compute_rainfall_stats_for_geometry(geometry, forecast_file, forecast_hours, data_source_label) -> dict`

### 修改现有函数

- `RainfallAnalyzer.get_city_rainfall_time_range`：可将其栅格统计部分抽取为共享函数，供城市和河系工具复用（可选，视实现时重复程度决定）。

## Error Handling

| 场景 | 内部行为 | 用户侧表现 |
|------|----------|------------|
| 数据库连接失败或 `haihe_zone_9` 无数据 | 记录异常日志 | “暂时无法获取河系预报数据，请稍后重试。” |
| 无可用预报文件（滚动预报/EC 均缺失） | 返回 `zones: []`，`data_source` 标注“无可用预报文件” | “当前暂无可用降雨预报数据。” |
| 部分河系统计失败 | 该河系返回 `null`，其余正常返回 | 不阻断，正常展示成功河系 |
| 参数格式错误 | 返回明确错误信息 | “查询参数有误，请确认时间格式。” |

## Testing Plan

### MCP 层单元测试

文件：`haihe-weather-analyzer-mcp/tests/test_river_system_rainfall_forecast.py`

1. 工具 schema/名称注册检查；
2. `haihe_zone_9` 边界加载 mock（无数据库时）；
3. 栅格统计逻辑：构造内存小栅格 + 简单多边形，验证 avg/max/min 计算；
4. 无数据路径：空边界/空栅格返回空 `zones`；
5. 错误路径：数据库异常返回友好提示。

### Chainlit 层测试

1. 更新 `chainlitexam/tests/test_thinking.py` 或新增测试，验证 `prompts.py` 中：
   - 包含 `get_river_system_rainfall_forecast` 工具名；
   - 流域/子流域未来天气规则指向新工具。

### 手动验证

1. 启动 MCP server + Chainlit；
2. 验证问题：
   - “海河流域明天天气怎么样” → 返回九大分区河系主表；
   - “大清河流域未来三天天气” → 返回大清河河系数据（九大分区中仅展示大清河，或按子流域口径展示相关分区）；
   - “海河流域未来一周天气” → 返回逐天河系表。

## Migration / Rollback

- 纯新增工具，不影响现有 `get_city_rainfall_time_range`、`query_basin_areal_rainfall`；
- `prompts.py` 为增量更新，旧的城市级回答能力保留；
- 回滚：移除新工具注册、恢复 `prompts.py` 相关段落即可。

## Decisions

| Decision | Rationale |
|----------|-----------|
| 新增独立 MCP 工具 | 与 `get_city_rainfall_time_range` 架构对称，planner 可直接调用，复用滚动预报/EC 切换逻辑 |
| 使用 `haihe_zone_9` 数据库边界 | 已确认现有九分区边界在数据库中，可直接裁剪降雨网格，结果比城市聚合更精确 |
| 九大分区为默认展示维度 | 与现有面雨量九分区口径一致，业务认知统一 |
| 城市明细作为补充 | 满足领导“按河系回答”的同时保留原有城市细节，便于用户定位 |
| 不重点改造 fast path | `ENABLE_FAST_PATHS=false` 为默认，planner-only 路径为当前主路径 |
| 用户侧只输出业务口径 | 不暴露数据库表名、工具参数、文件名等后端细节 |

## Out of Scope

- 修改实况降雨、气象预警、应急响应等非未来预报类问答的维度；
- 新增气温、风力等未获取的预报指标；
- 重新生成或修改 `haihe_zone_9` 表结构；
- 删除或废弃现有 `get_city_rainfall_time_range` 工具。
