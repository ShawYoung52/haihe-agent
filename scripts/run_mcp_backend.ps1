# Run MCP backend on Windows PowerShell.

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Backend = Join-Path $Root "haiheliuyubaoyuagent-master\haihe-weather-analyzer-mcp"

Push-Location $Backend
try {
    if (-not (Test-Path ".venv")) {
        python -m venv .venv
    }
    & ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt

    if (-not $env:MCP_HOST) { $env:MCP_HOST = "0.0.0.0" }
    if (-not $env:MCP_PORT) { $env:MCP_PORT = "3333" }

    & ".\.venv\Scripts\python.exe" main.py --host $env:MCP_HOST --port $env:MCP_PORT
}
finally {
    Pop-Location
}
