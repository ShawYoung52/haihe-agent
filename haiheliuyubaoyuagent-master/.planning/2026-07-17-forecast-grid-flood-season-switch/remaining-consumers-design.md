# Design: 剩余 3 个 EC 消费方接入滚动预报

**创建时间:** 2026-07-17  
**背景:** `emergency_api.py` 和 `emergency_response_interface.py` 已接入 `resolve_forecast_grid_source` 切换。剩余 3 个消费方仍用 EC，需设计接入方案。

## 现状摘要

| 消费方 | EC 发现 | EC 读取 | 输出 |
|--------|---------|---------|------|
| `draw_haihe_precip_product.py` | `_resolve_ec_nested_input_dir` + `_pick_latest_raster`（按文件名 glob） | `gdal.Open(path)` → `RasterData` | PNG 降水产品图 |
| `RainfallAnalyzer.py` | pattern `ec_{time}_rain_total_{N}h.tif` in `EC_OUTPUT_PATH` | `gdal.Open(tif)` + 流域边界 rasterize → zonal stats | dict（分流域 mean/max/min 降水） |
| `forecast_product_queue.py` | `collect_ec_forecast_precip_files(start_time, ec_output_path, hours)` | 调 `draw_haihe_precip_product` 画图 | 队列任务（每时效一张图） |

## 各消费方接入设计

### 1. `draw_haihe_precip_product.py`（降水产品图绘制）

**当前接口**: CLI `--input-dir` 或 `--start-utc` + EC 文件名规范，`_open_raster(path)` 用 GDAL 打开单个栅格。

**接入方案 A（推荐）：直接支持 .nc + hour 参数**
- 新增 CLI 参数 `--rolling-forecast-nc PATH` + `--hour N`
- `_open_raster` 分支：若 path 是 .nc，用 xarray 打开、切片 `time=hour`、转 GDAL 内存栅格（或直接用 xarray + rasterio），返回 `RasterData`
- 渲染管线（theme/配色/出图）完全复用
- **优点**: 无临时文件、单文件覆盖全部时效
- **缺点**: 需改 `_open_raster` 核心函数；GDAL ↔ xarray 转换需处理坐标/投影

**接入方案 B：用 `materialize_rolling_forecast_to_files` 预切片**
- 队列/调用方先用 `materialize_rolling_forecast_to_files` 生成 2D .nc，再当普通栅格传给 `--input`
- **优点**: 不改 `draw_haihe_precip_product`
- **缺点**: 临时文件、调用方需额外步骤

**推荐**: 方案 A。`_open_raster` 改动可控，收益是所有调用方（含队列）都能直接用 .nc。

**复杂度**: 中。核心改动在 `_open_raster`（~50 行），加 xarray→GDAL 内存栅格转换。

### 2. `forecast_product_queue.py`（批处理队列）

**当前接口**: `ForecastProductJob(start_time, ec_output_path, hours)` → worker 调 `collect_ec_forecast_precip_files` 拿 `ec_files` dict → 每时效调 `draw_haihe_precip_product` 出图。

**接入方案：job 加 source 字段，worker 按源分发**
- `ForecastProductJob` 加 `source: "ec" | "rolling_forecast"` + `rolling_nc_path: str | None`
- `enqueue_forecast_product_job` 内部调 `resolve_forecast_grid_source` 决定 source
- worker 分支：
  - EC：原逻辑（`collect_ec_forecast_precip_files` + 每时效 `draw_haihe_precip_product --input tif`）
  - 滚动预报：单 .nc 文件，循环 hour 调 `draw_haihe_precip_product --rolling-forecast-nc nc --hour H`
- **依赖**: 消费方 1（`draw_haihe_precip_product` 支持 .nc）先完成

**复杂度**: 低。队列是薄编排层，改动 ~30 行（job 结构 + worker 分支）。

### 3. `RainfallAnalyzer.py`（面雨量分析）

**当前接口**: `analyze_rainfall(time_range, forecast_hours)` → 按 `ec_{time}_rain_total_{N}h.tif` 模式找 TIF → `gdal.Open` + 流域 shp rasterize → zonal stats。

**接入方案 A：直接用 xarray + rioxarray 做 zonal stats**
- 滚动预报路径：open .nc，切片 `time=hour`，用 `xarray.DataArray.weighted()` 或 rasterio rasterize 做 zonal stats
- 与 EC 路径（GDAL rasterize）完全不同的代码分支
- **优点**: 原生 xarray，无坐标/投影转换
- **缺点**: 两套 zonal stats 逻辑（GDAL for EC, xarray for rolling），维护成本高

**接入方案 B：用 `materialize_rolling_forecast_to_files` 生成 2D .nc，走现有 GDAL 流程**
- `analyze_rainfall` 入口调 `resolve_forecast_grid_source`；滚动预报时先 `materialize_rolling_forecast_to_files` 生成各时效 2D .nc
- 后续 GDAL `gdal.Open(.nc)` + rasterize 流程不变（GDAL netCDF 驱动支持）
- **优点**: zonal stats 逻辑零改动；与 `emergency_response_interface` 同一模式
- **缺点**: 临时文件（但面雨量分析不是高频，可接受）

**推荐**: 方案 B。与 `emergency_response_interface.py` 已验证的 `materialize` 模式一致，最小改动、最低风险。

**复杂度**: 低。改动 ~20 行（入口加 `resolve_forecast_grid_source` + `materialize` 分支，后续流程复用）。

## 实现顺序与依赖

```
1. draw_haihe_precip_product.py（方案 A：_open_raster 支持 .nc）
   ↓ 依赖
2. forecast_product_queue.py（job 加 source 字段，worker 分发）
   ↓ 独立
3. RainfallAnalyzer.py（方案 B：materialize + 现有 GDAL 流程）
```

消费方 1 和 3 可并行；消费方 2 依赖消费方 1。

## 风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| `_open_raster` 改动影响 EC TIF 路径回归 | 产品图渲染失败 | 加 .nc 分支不删 TIF 分支；回归测试 EC 路径 |
| GDAL netCDF 驱动对 2D .nc 的坐标识别 | zonal stats 坐标错位 | 用 `materialize` 生成的 .nc 已验证 xarray 可读；GDAL 打开后验证 geotransform |
| 队列 worker 异常时临时 .nc 文件残留 | 磁盘占用 | `materialize` 用 tempfile.mkdtemp，worker finally 清理 |
| 滚动预报 .nc 缺失某时效（time 坐标无该值） | 该时效产品缺失 | `materialize` 已跳过不存在时效；调用方按返回 dict 判断 |

## 待用户决策

- 是否现在实现全部 3 个？还是先做优先级最高的（建议 `RainfallAnalyzer` 面雨量——问答智能体高频调用）？
- `draw_haihe_precip_product` 方案 A vs B：用户是否接受改 `_open_raster` 核心函数？