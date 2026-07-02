# Run Chainlit frontend on Windows PowerShell.

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Frontend = Join-Path $Root "haiheliuyubaoyuagent-master\chainlitexam"

Push-Location $Frontend
try {
    if (-not (Test-Path ".venv")) {
        python -m venv .venv
    }
    & ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt

    if ((Test-Path ".env.example") -and (-not (Test-Path ".env"))) {
        Copy-Item ".env.example" ".env"
        Write-Host "Created .env. Please edit it before starting the frontend."
        exit 1
    }

    if (-not $env:CHAINLIT_HOST) { $env:CHAINLIT_HOST = "0.0.0.0" }
    if (-not $env:CHAINLIT_PORT) { $env:CHAINLIT_PORT = "8003" }

    & ".\.venv\Scripts\chainlit.exe" run chainlit_app.py --host $env:CHAINLIT_HOST --port $env:CHAINLIT_PORT
}
finally {
    Pop-Location
}
