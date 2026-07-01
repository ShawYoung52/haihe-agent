param(
    [string]$BaseUrl = "http://127.0.0.1:8080"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-True {
    param(
        [bool]$Condition,
        [string]$Message
    )
    if (-not $Condition) {
        throw "ASSERT FAILED: $Message"
    }
}

function To-BoolStrict {
    param(
        [object]$Value
    )
    if ($Value -is [bool]) { return [bool]$Value }
    $txt = [string]$Value
    if ([string]::IsNullOrWhiteSpace($txt)) { return $false }
    $v = $txt.Trim().ToLowerInvariant()
    if ($v -in @("1","true","yes","y","on")) { return $true }
    if ($v -in @("0","false","no","n","off")) { return $false }
    return $false
}

function Invoke-Json {
    param(
        [string]$Url,
        [string]$Method = "GET",
        [hashtable]$Headers = @{},
        [object]$BodyObj = $null
    )
    $args = @{
        Uri = $Url
        Method = $Method
        Headers = $Headers
        UseBasicParsing = $true
    }
    if ($null -ne $BodyObj) {
        $args["ContentType"] = "application/json"
        $args["Body"] = ($BodyObj | ConvertTo-Json -Depth 10 -Compress)
    }
    $resp = Invoke-WebRequest @args
    return @{
        Response = $resp
        Body = ($resp.Content | ConvertFrom-Json)
    }
}

Write-Host "== Regression logic checks ==" -ForegroundColor Cyan
Write-Host "BaseUrl: $BaseUrl"

# Check 1: bool parsing for queue paused should treat "false" as false
$q1 = Invoke-Json -Url "$BaseUrl/emergency/forecast/products/queue" -Method "POST" -BodyObj @{ paused = "false" }
Assert-True ($q1.Response.StatusCode -eq 200) "queue pause POST should return 200"
$pausedVal = To-BoolStrict $q1.Body.paused
Assert-True (-not $pausedVal) "paused='false' should NOT pause queue"
Write-Host "[OK] bool parsing for paused" -ForegroundColor Green

# Check 2: response-board GET should be read-only (trigger_forecast ignored)
$rb = Invoke-Json -Url "$BaseUrl/emergency/management/response-board?trigger_forecast=true&start_time=2026010100" -Method "GET"
Assert-True ($rb.Response.StatusCode -eq 200) "response-board GET should return 200"
$hasTriggerMeta = $null -ne $rb.Body.PSObject.Properties["trigger_forecast"]
Assert-True (-not $hasTriggerMeta) "GET response-board should not execute trigger_forecast"
Write-Host "[OK] response-board GET read-only" -ForegroundColor Green

# Check 3: trace query on existing events endpoint
$traceId = "trace-regression-" + [guid]::NewGuid().ToString("N")
$tc = Invoke-Json -Url "$BaseUrl/emergency/events?trace_id=$traceId&limit=5" -Method "GET"
Assert-True ($tc.Response.StatusCode -eq 200) "events trace query should return 200"
Assert-True ([string]$tc.Body.trace_id -eq $traceId) "events trace query response trace_id mismatch"
Assert-True ($null -ne $tc.Body.list) "events trace query should include list"
Write-Host "[OK] trace query on /emergency/events" -ForegroundColor Green

Write-Host ""
Write-Host "ALL REGRESSION CHECKS PASSED" -ForegroundColor Green
