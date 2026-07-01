$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $RepoRoot
$ChainlitDir = Join-Path $ProjectRoot "chainlitexam-gis"
$VenvActivate = Join-Path $ChainlitDir ".venv_new\Scripts\Activate.ps1"
$ChainlitEntry = Join-Path $ChainlitDir "chain_gzt.py"

if (-not (Test-Path $ChainlitDir)) {
  throw "未找到 chainlit 目录: $ChainlitDir"
}
if (-not (Test-Path $VenvActivate)) {
  throw "未找到 chainlit 虚拟环境激活脚本: $VenvActivate"
}
if (-not (Test-Path $ChainlitEntry)) {
  throw "未找到 chainlit 入口文件: $ChainlitEntry"
}

# 方案 C：统一 Python UTF-8 输出，避免终端中文乱码
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 默认监测配置（可按需改）
if (-not $env:EMERGENCY_MONITOR_MODE) { $env:EMERGENCY_MONITOR_MODE = "http_or_tool" }
if (-not $env:EMERGENCY_MONITOR_BASE_URL) { $env:EMERGENCY_MONITOR_BASE_URL = "http://127.0.0.1:8080" }
if (-not $env:EMERGENCY_MONITOR_TRIGGER_PATH) { $env:EMERGENCY_MONITOR_TRIGGER_PATH = "/emergency/forecast" }
if (-not $env:EMERGENCY_MONITOR_POLL_PATH) { $env:EMERGENCY_MONITOR_POLL_PATH = "/emergency/management/response-board" }
if (-not $env:EMERGENCY_MONITOR_POLL_INTERVAL_SEC) { $env:EMERGENCY_MONITOR_POLL_INTERVAL_SEC = "60" }
if (-not $env:EMERGENCY_MONITOR_ENABLE_REGIONS) { $env:EMERGENCY_MONITOR_ENABLE_REGIONS = "0" }
if (-not $env:EMERGENCY_MONITOR_VERBOSE_UPDATES) { $env:EMERGENCY_MONITOR_VERBOSE_UPDATES = "0" }
# GIS WMS SQL 联动默认配置（避免前端收到空的 wms.base_url）
if (-not $env:GIS_WMS_BASE_URL) { $env:GIS_WMS_BASE_URL = "http://127.0.0.1:8090/geoserver/wms" }
if (-not $env:GIS_WMS_SQL_ID_PARAM) { $env:GIS_WMS_SQL_ID_PARAM = "sql_id" }
# 若前端按固定图层读取，可按需覆盖此值（默认留空）
if (-not $env:GIS_WMS_LAYER_NAME) { $env:GIS_WMS_LAYER_NAME = "" }

# 默认触发参数（仅当未设置时才填充）
if (-not $env:EMERGENCY_MONITOR_TRIGGER_PAYLOAD) {
  $env:EMERGENCY_MONITOR_TRIGGER_PAYLOAD = '{"start_time":"2023073000","scope":"haihe","basin_codes":"HHLY_JUECE","ec_output_path":"C:\\Users\\gaozr\\Desktop\\fsdownload\\2023_mock_grib2_rate","local_station_json_path":"C:\\Users\\gaozr\\Desktop\\fsdownload\\station_rain_api_20230730000000_20230730000000.json","include_evidence":true}'
}

Set-Location $ChainlitDir
. $VenvActivate

Write-Host "[start_chainlit] ChainlitDir: $ChainlitDir"
Write-Host "[start_chainlit] EMERGENCY_MONITOR_BASE_URL=$($env:EMERGENCY_MONITOR_BASE_URL)"
Write-Host "[start_chainlit] EMERGENCY_MONITOR_POLL_INTERVAL_SEC=$($env:EMERGENCY_MONITOR_POLL_INTERVAL_SEC)"
Write-Host "[start_chainlit] GIS_WMS_BASE_URL=$($env:GIS_WMS_BASE_URL)"
Write-Host "[start_chainlit] GIS_WMS_SQL_ID_PARAM=$($env:GIS_WMS_SQL_ID_PARAM)"
Write-Host "[start_chainlit] PYTHONUTF8=$($env:PYTHONUTF8), PYTHONIOENCODING=$($env:PYTHONIOENCODING)"
Write-Host "[start_chainlit] Starting chainlit on 0.0.0.0:8003 ..."

chainlit run chain_gzt.py --host 0.0.0.0 --port 8003
