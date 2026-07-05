# 暴雨影响河流 GeoJSON / 专题图服务配置说明

本文档说明 `hhlyqyxt-master/utils/rainstorm_impact_map_service.py` 需要配置哪些内容，以及同事如何调用。

## 1. 服务产物说明

本服务有两个对外入口：

```python
from utils import create_rainstorm_impact_geojson_file
from utils import create_rainstorm_impact_map
```

### 1.1 只要暴雨影响河流 GeoJSON

调用：

```python
result = create_rainstorm_impact_geojson_file(
    start_time="2026-07-02 15:00:00",
    end_time="2026-07-03 15:00:00",
    graph_path=r"E:\tj\line\result\river_directed_v5.pkl",
)

geojson_url = result["geojson_url"]
```

返回主文件：

```text
river_impact.geojson
```

用途：给十四所或其他系统直接加载河流影响图层。

### 1.2 要完整专题图文件包

调用：

```python
result = create_rainstorm_impact_map(
    start_time="2026-07-02 15:00:00",
    end_time="2026-07-03 15:00:00",
    graph_path=r"E:\tj\line\result\river_directed_v5.pkl",
)

map_url = result["map_package_url"]
```

返回主文件：

```text
rainstorm_impact_map.json
```

用途：给前端按“专题图”方式渲染，文件包中包含 GeoJSON 地址、样式、图例和摘要。

## 2. 必须配置项

### 2.1 文件落盘目录

```powershell
$env:RAINSTORM_IMPACT_OUTPUT_DIR="D:\rainstorm_impact_output"
```

说明：服务生成的 `river_impact.geojson`、`impact_stations.geojson`、`summary.json`、`style.json`、`rainstorm_impact_map.json` 都会写到这个目录下。

### 2.2 HTTP 发布根地址

```powershell
$env:RAINSTORM_IMPACT_PUBLIC_BASE_URL="http://你的服务IP:端口/rainstorm_impact_output"
```

说明：这个地址必须能访问到 `RAINSTORM_IMPACT_OUTPUT_DIR` 对应的文件目录。

例如文件实际落到：

```text
D:\rainstorm_impact_output\rainstorm_impact_202607021500_202607031500_abcd1234\river_impact.geojson
```

则返回给十四所的地址会是：

```text
http://你的服务IP:端口/rainstorm_impact_output/rainstorm_impact_202607021500_202607031500_abcd1234/river_impact.geojson
```

如果没有配置 `RAINSTORM_IMPACT_PUBLIC_BASE_URL`，服务会直接报错，不会返回本地路径或挂载盘符。

## 3. MUSIC 实况降雨接口配置

服务会调用 MUSIC 接口查询海河流域实况降雨。正式环境建议显式配置：

```powershell
$env:MUSIC_SERVICE_IP="10.226.90.120"
$env:MUSIC_SERVICE_NODE_ID="NMIC_MUSIC_CMADAAS"
$env:MUSIC_USER_ID="你的MUSIC用户"
$env:MUSIC_PASSWORD="你的MUSIC密码"
$env:MUSIC_CONNECT_TIMEOUT="5"
$env:MUSIC_READ_TIMEOUT="120"
$env:MUSIC_API_TIME_SHIFT_HOURS="-8"
```

说明：

```text
MUSIC_SERVICE_IP              MUSIC 服务地址
MUSIC_SERVICE_NODE_ID         服务节点 ID
MUSIC_USER_ID                 MUSIC 用户名
MUSIC_PASSWORD                MUSIC 密码
MUSIC_CONNECT_TIMEOUT         连接超时，秒
MUSIC_READ_TIMEOUT            读取超时，秒
MUSIC_API_TIME_SHIFT_HOURS    调 MUSIC 时的时间偏移，当前默认 -8
```

## 4. 河流影响计算配置

### 4.1 河网拓扑 pkl

调用时传入：

```python
graph_path=r"E:\tj\line\result\river_directed_v5.pkl"
```

这是下游 50km 追踪使用的有向河网拓扑。

### 4.2 PostGIS 河流表

核心算法默认使用：

```text
schema      = public
river_table = haihe_river_directed_full_v5
geom字段    = geom
ID字段      = objectid
河名字段    = src_name
```

需要保证服务运行环境的数据库连接能访问这个表。

当前连接来源：

```text
hhlyqyxt-master/utils/db.py
```

后续建议把 `utils/db.py` 里的数据库连接也改成环境变量或配置文件，不要长期写死在代码里。

## 5. 最小运行示例

### 5.1 PowerShell 环境变量

```powershell
$env:RAINSTORM_IMPACT_OUTPUT_DIR="D:\rainstorm_impact_output"
$env:RAINSTORM_IMPACT_PUBLIC_BASE_URL="http://你的服务IP:端口/rainstorm_impact_output"

$env:MUSIC_SERVICE_IP="10.226.90.120"
$env:MUSIC_SERVICE_NODE_ID="NMIC_MUSIC_CMADAAS"
$env:MUSIC_USER_ID="你的MUSIC用户"
$env:MUSIC_PASSWORD="你的MUSIC密码"
$env:MUSIC_CONNECT_TIMEOUT="5"
$env:MUSIC_READ_TIMEOUT="120"
$env:MUSIC_API_TIME_SHIFT_HOURS="-8"
```

### 5.2 Python 调用

```python
from utils import create_rainstorm_impact_geojson_file

result = create_rainstorm_impact_geojson_file(
    start_time="2026-07-02 15:00:00",
    end_time="2026-07-03 15:00:00",
    graph_path=r"E:\tj\line\result\river_directed_v5.pkl",
)

print(result["geojson_url"])
print(result["delivery"]["main_file"]["address"])
```

## 6. 返回字段说明

只生成 GeoJSON 时，重点字段是：

```text
geojson_url                         暴雨影响河流 GeoJSON 的 HTTP 地址
delivery.main_file.address          同 geojson_url
delivery.files.river_impact_geojson 暴雨影响河流 GeoJSON
delivery.files.impact_stations_geojson 暴雨触发站 GeoJSON
delivery.files.summary_json         摘要文件
delivery.files.style_json           专题图样式文件
summary                             影响河流数量、触发站数量等摘要
rainfall_source                     实况降雨来源信息
```

专题图文件包时，重点字段是：

```text
map_package_url                     专题图文件包 HTTP 地址
geojson_url                         专题图内使用的河流 GeoJSON HTTP 地址
delivery.main_file.address          同 map_package_url
```

## 7. 对接建议

给十四所只传一个地址时，优先传：

```python
result["geojson_url"]
```

前端需要专题图样式、图例、摘要时，传：

```python
result["map_package_url"]
```

不要把 Python 内存对象直接给对方，也不要返回本地盘符。对外交付统一使用 HTTP 地址。
