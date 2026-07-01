$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendScript = Join-Path $RepoRoot "start_backend.ps1"
$ChainlitScript = Join-Path $RepoRoot "start_chainlit.ps1"
$GeoServerScript = Join-Path $RepoRoot "start_geoserver_tomcat.ps1"
$McpServerScript = Join-Path $RepoRoot "server.py"
$McpCondaEnv = if ($env:MCP_CONDA_ENV) { $env:MCP_CONDA_ENV } else { "haihe-gdal" }

if (-not (Test-Path $BackendScript)) {
  throw "未找到脚本: $BackendScript"
}
if (-not (Test-Path $ChainlitScript)) {
  throw "未找到脚本: $ChainlitScript"
}
if (-not (Test-Path $McpServerScript)) {
  throw "未找到 MCP 服务入口: $McpServerScript"
}

Write-Host "[start_all] 启动后端终端..."
Start-Process powershell -ArgumentList @(
  "-NoExit",
  "-ExecutionPolicy", "Bypass",
  "-File", "`"$BackendScript`""
)

Start-Sleep -Seconds 2

if (Test-Path $GeoServerScript) {
  Write-Host "[start_all] 启动 GeoServer(Tomcat) 终端..."
  Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$GeoServerScript`""
  )
  Start-Sleep -Seconds 2
}

Write-Host "[start_all] 启动 MCP(3333) 终端..."
Start-Process powershell -ArgumentList @(
  "-NoExit",
  "-ExecutionPolicy", "Bypass",
  "-Command",
  "& { " +
  '$ErrorActionPreference = "Stop"; ' +
  "Set-Location `"$RepoRoot`"; " +
  '$McpPython = "python"; ' +
  "if (Get-Command conda -ErrorAction SilentlyContinue) { " +
  "  (& conda shell.powershell hook) | Out-String | Invoke-Expression; " +
  "  conda activate $McpCondaEnv; " +
  '  $CondaPython = Join-Path $env:CONDA_PREFIX "python.exe"; ' +
  "  if (Test-Path $CondaPython) { " +
  '    $McpPython = $CondaPython; ' +
  "    Write-Host ""[start_mcp] Runtime=conda env: $McpCondaEnv ($CondaPython)""; " +
  "  } else { " +
  "    Write-Host ""[start_mcp] 已激活 conda 但未找到 python.exe，使用当前 python""; " +
  "  } " +
  "} else { " +
  "  Write-Host ""[start_mcp] conda 不可用，使用当前 Python 环境""; " +
  "} " +
  '& $McpPython server.py --port 3333' +
  " }"
)

Start-Sleep -Seconds 2

Write-Host "[start_all] 启动 Chainlit 终端..."
Start-Process powershell -ArgumentList @(
  "-NoExit",
  "-ExecutionPolicy", "Bypass",
  "-File", "`"$ChainlitScript`""
)

Write-Host "[start_all] 已启动：后端(8080) + GeoServer(Tomcat:8090) + MCP(3333) + Chainlit(8003)"
