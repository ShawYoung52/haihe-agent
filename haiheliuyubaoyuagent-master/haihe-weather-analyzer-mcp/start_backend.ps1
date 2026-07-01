$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvActivate = Join-Path $RepoRoot ".venv\Scripts\Activate.ps1"
$ServerScript = Join-Path $RepoRoot "emergency_http_server.py"
$CondaEnvName = if ($env:BACKEND_CONDA_ENV) { $env:BACKEND_CONDA_ENV } else { "haihe-gdal" }
$UseCondaRaw = if ($null -ne $env:BACKEND_USE_CONDA) { $env:BACKEND_USE_CONDA } else { "0" }
$UseConda = ($UseCondaRaw.ToString().Trim().ToLower() -in @("1","true","yes","on"))
$RuntimeLabel = ""
$PythonExe = "python"

if (-not (Test-Path $ServerScript)) {
  throw "未找到后端启动脚本: $ServerScript"
}

# 方案 C：统一 Python UTF-8 输出，避免终端中文乱码
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 可选：开启后端会话基线，避免页面未提问时直接看到历史联调事件
if (-not $env:EMERGENCY_RESPONSE_BOARD_SESSION_BASELINE) {
  $env:EMERGENCY_RESPONSE_BOARD_SESSION_BASELINE = "1"
}

Set-Location $RepoRoot

$CondaReady = $false
if ($UseConda -and (Get-Command conda -ErrorAction SilentlyContinue)) {
  try {
    (& conda "shell.powershell" "hook") | Out-String | Invoke-Expression
    conda activate $CondaEnvName
    $CondaPython = Join-Path $env:CONDA_PREFIX "python.exe"
    if (-not (Test-Path $CondaPython)) {
      throw "Conda Python 不存在: $CondaPython"
    }
    & $CondaPython -c "import sys;mods=['osgeo','psycopg2'];missing=[]`nfor m in mods:`n    try:`n        __import__(m)`n    except Exception:`n        missing.append(m)`nprint('missing=',missing)`nsys.exit(1 if missing else 0)"
    if ($LASTEXITCODE -eq 0) {
      $CondaReady = $true
      $RuntimeLabel = "conda env: $CondaEnvName ($CondaPython)"
      $PythonExe = $CondaPython
    } else {
      Write-Host "[start_backend] Conda env '$CondaEnvName' 缺少必要依赖(osgeo/psycopg2)，将回退到 .venv"
      conda deactivate
    }
  } catch {
    Write-Host "[start_backend] Conda env '$CondaEnvName' 初始化失败，将回退到 .venv：$($_.Exception.Message)"
  }
} elseif (-not $UseConda) {
  Write-Host "[start_backend] BACKEND_USE_CONDA 未开启，默认使用 .venv"
}

if (-not $CondaReady) {
  if (-not (Test-Path $VenvActivate)) {
    throw "未找到虚拟环境激活脚本: $VenvActivate"
  }
  # 避免沿用外层终端中的 conda GDAL 变量，导致 .venv 误加载错误插件目录
  Remove-Item Env:GDAL_DRIVER_PATH -ErrorAction SilentlyContinue
  Remove-Item Env:GDAL_DATA -ErrorAction SilentlyContinue
  Remove-Item Env:PROJ_LIB -ErrorAction SilentlyContinue
  Remove-Item Env:PROJ_DATA -ErrorAction SilentlyContinue
  . $VenvActivate
  $RuntimeLabel = ".venv"
  $PythonExe = (Get-Command python).Source
}

Write-Host "[start_backend] RepoRoot: $RepoRoot"
Write-Host "[start_backend] Runtime=$RuntimeLabel"
Write-Host "[start_backend] EMERGENCY_RESPONSE_BOARD_SESSION_BASELINE=$($env:EMERGENCY_RESPONSE_BOARD_SESSION_BASELINE)"
Write-Host "[start_backend] PYTHONUTF8=$($env:PYTHONUTF8), PYTHONIOENCODING=$($env:PYTHONIOENCODING)"
Write-Host "[start_backend] Starting emergency_http_server on 0.0.0.0:8080 ..."

& $PythonExe emergency_http_server.py --host 0.0.0.0 --port 8080
