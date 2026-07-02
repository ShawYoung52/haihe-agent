# Windows bootstrap script for local development.
# Run from the repository root:
# powershell -ExecutionPolicy Bypass -File scripts/bootstrap_windows.ps1

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Frontend = Join-Path $Root "haiheliuyubaoyuagent-master\chainlitexam"
$Backend = Join-Path $Root "haiheliuyubaoyuagent-master\haihe-weather-analyzer-mcp"

function Ensure-VenvAndDeps($ProjectDir) {
    Push-Location $ProjectDir
    try {
        if (-not (Test-Path ".venv")) {
            python -m venv .venv
        }
        & ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
        & ".\.venv\Scripts\pip.exe" install -r requirements.txt
        if ((Test-Path ".env.example") -and (-not (Test-Path ".env"))) {
            Copy-Item ".env.example" ".env"
            Write-Host "Created $ProjectDir\.env. Please edit it before production use."
        }
    }
    finally {
        Pop-Location
    }
}

Write-Host "Bootstrapping backend..."
Ensure-VenvAndDeps $Backend

Write-Host "Bootstrapping frontend..."
Ensure-VenvAndDeps $Frontend

Write-Host "Running repository checks..."
Push-Location $Root
try {
    python scripts/check_repository.py
    python -m unittest discover -s tests
}
finally {
    Pop-Location
}

Write-Host "Bootstrap complete."
Write-Host "Start backend:  powershell -ExecutionPolicy Bypass -File scripts/run_mcp_backend.ps1"
Write-Host "Start frontend: powershell -ExecutionPolicy Bypass -File scripts/run_chainlit_frontend.ps1"
