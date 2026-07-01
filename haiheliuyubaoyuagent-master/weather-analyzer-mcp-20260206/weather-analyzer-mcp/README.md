# weather-analyzer-mcp

气象分析 MCP 工具服务。将 MICAPS 数据的气象图绘制功能封装为 MCP (Model Context Protocol) 工具，供 Claude Desktop / Cline / 其他 MCP 客户端自动调用。

## 支持的图表

| 层面 | 位势高度场 | 风羽图 | 露点温度 |
|------|-----------|--------|---------|
| 500hPa | ✓ | ✓ | - |
| 700hPa | ✓ | ✓ | ✓ |
| 850hPa | ✓ | ✓ | ✓ |

## 目录结构

```
weather/
├── weather_mcp_service.py   # MCP 服务入口（启动这个）
├── weather_analyzer.py      # 核心绘图逻辑
├── config.ini               # 路径配置（部署时必须改）
├── test_local.py            # 本地测试脚本
├── analyze_weather.py       # 独立命令行工具
├── 气象分析词.txt            # AI 分析时使用的 prompt
├── Util/
│   ├── MDFS.py              # MICAPS 二进制数据读取
│   ├── draw.py              # shapefile 绘制
│   └── table.py             # 气象要素编码表
├── Common/                  # 原始绘图脚本（参考用）
├── pyproject.toml           # 打包配置
└── requirements.txt         # 依赖清单
```

## 安装部署（在目标服务器上）

### 1. 安装 Python 依赖

```bash
cd weather
pip install -r requirements.txt
```

> 如果 cartopy 安装失败，推荐用 conda：`conda install -c conda-forge cartopy`

### 2. 修改 config.ini

```ini
[common]
rootDir=D:\micaps_data          # MICAPS 数据根目录（改成你的）
provincePath=D:\shp\province.shp # 省界 shapefile 路径（改成你的）
saveDir=D:\output                # 图片输出目录（改成你的）
lonMin=85
lonMax=147
latMin=15
latMax=61.5

[paths]
# 位势高度场数据路径（MICAPS 14 类）
height_field_500=UPPER_AIR\MANUAL_ANALYSIS\HGT\500
height_field_700=UPPER_AIR\MANUAL_ANALYSIS\HGT\700
height_field_850=UPPER_AIR\MANUAL_ANALYSIS\HGT\850

# 站点观测数据路径（风场、露点）
station_data_500=UPPER_AIR\PLOT\500
station_data_700=UPPER_AIR\PLOT\700
station_data_850=UPPER_AIR\PLOT\850
```

数据目录结构应为：
```
rootDir/
└── YYYYMMDD/
    ├── UPPER_AIR/MANUAL_ANALYSIS/HGT/500/YYYYMMDDHHMISS.000
    ├── UPPER_AIR/MANUAL_ANALYSIS/HGT/700/...
    ├── UPPER_AIR/MANUAL_ANALYSIS/HGT/850/...
    ├── UPPER_AIR/PLOT/500/YYYYMMDDHHMISS.000
    ├── UPPER_AIR/PLOT/700/...
    └── UPPER_AIR/PLOT/850/...
```

### 3. 本地测试

```bash
python test_local.py
```

全部显示 ✅ 即可。

### 4. 启动 MCP 服务

**本地模式（stdio）** — 用于 Claude Desktop / Cline 等本地客户端：

```bash
python weather_mcp_service.py
```

**远程模式（SSE）** — 用于服务器部署，供远程 MCP 客户端连接：

```bash
python weather_mcp_service.py --transport sse --host 0.0.0.0 --port 8000
```

启动后 SSE 端点为 `http://服务器IP:8000/sse`。

## 接入 MCP 客户端

### Claude Desktop（本地 stdio 模式）

编辑 `%APPDATA%\Claude\claude_desktop_config.json`（Windows）或 `~/Library/Application Support/Claude/claude_desktop_config.json`（Mac）：

```json
{
  "mcpServers": {
    "weather-analyzer": {
      "command": "python",
      "args": ["D:\\weather\\weather_mcp_service.py"],
      "env": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

### Cline (VSCode)（本地 stdio 模式）

在 MCP Servers 设置中添加相同配置，重启 VSCode。

### 远程 SSE 模式

在服务器上以 SSE 模式启动服务后，客户端配置 SSE 端点：

```json
{
  "mcpServers": {
    "weather-analyzer": {
      "url": "http://服务器IP:8000/sse"
    }
  }
}
```

> 任何支持 MCP stdio 或 SSE 传输的客户端都可以接入。

## MCP 工具列表

| 工具名 | 功能 | 耗时 |
|-------|------|------|
| `get_latest_analysis_time` | 获取最新可用数据时间 | <1s |
| `draw_height_field(time_str, level)` | 生成位势高度场图 | ~3s |
| `draw_wind_barb(time_str, level)` | 生成风羽图 | ~3s |
| `draw_dew_point(time_str, level)` | 生成露点温度图 | ~3s |
| `generate_level_charts(time_str, level)` | 生成某层面全部图 | ~10s |
| `get_analysis_prompt` | 获取气象分析词 | <1s |

### 参数说明

- `time_str`：`YYYYMMDDHHMISS` 格式，如 `20250820080000`
- `level`：`500hPa` / `700hPa` / `850hPa`

### 推荐调用流程

为避免超时，建议逐个调用单图工具：

```
1. get_latest_analysis_time()              → 拿到 time_str
2. get_analysis_prompt()                   → 拿到分析词（AI 读图规则）
3. draw_height_field(time_str, "500hPa")   → 500hPa 位势高度场
4. draw_wind_barb(time_str, "500hPa")      → 500hPa 风羽图
5. draw_height_field(time_str, "700hPa")   → 700hPa 位势高度场
6. draw_wind_barb(time_str, "700hPa")      → 700hPa 风羽图
7. draw_dew_point(time_str, "700hPa")      → 700hPa 露点
8. draw_height_field(time_str, "850hPa")   → 850hPa 位势高度场
9. draw_wind_barb(time_str, "850hPa")      → 850hPa 风羽图
10. draw_dew_point(time_str, "850hPa")     → 850hPa 露点
11. AI 根据分析词 + 图片输出专业分析报告
```

> `get_analysis_prompt()` 返回完整的气象分析词，包含风向识别规则、位势高度图识别规则、露点图识别规则、切变线识别规则、以及四部分输出结构。AI 应按此规则读图并输出分析报告。

## 独立命令行使用（不走 MCP）

```bash
# 使用最新数据生成所有层面
python analyze_weather.py

# 指定时间 + 保存报告
python analyze_weather.py -t 20250820200000 -s

# 只生成 500hPa
python analyze_weather.py -l 500hPa

# 查看帮助
python analyze_weather.py -h
```

## 气象分析词

分析词已集成到 MCP 服务中（`get_analysis_prompt` 工具），AI 在生成图片后可自动获取。

也可查看 `气象分析词.txt` 原文件。分析词包含：
- 风向/风羽识别规则（风向杆、风羽符号详解）
- 位势高度图识别规则（等高线、槽线、高低压标识）
- 露点图识别规则（插值面 + 图例颜色识别）
- 切变线识别规则（基于风矢气旋性突变判断）
- 四部分输出结构：500hPa / 700hPa / 850hPa / 高低空综合分析
- 区域限定：京津冀及其东南部流域区域（112°E–120°E, 34°N–40°N）
