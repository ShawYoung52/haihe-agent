param(
    [string]$BaseUrl = "http://127.0.0.1:8080",
    [string]$SessionId = "selfcheck-session-001",
    [string]$TraceId = "trace-selfcheck-001"
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

function Invoke-JsonGet {
    param(
        [string]$Url,
        [hashtable]$Headers = @{},
        [Microsoft.PowerShell.Commands.WebRequestSession]$Session = $null
    )
    $resp = Invoke-WebRequest -Uri $Url -Method Get -Headers $Headers -WebSession $Session -UseBasicParsing
    $obj = $resp.Content | ConvertFrom-Json
    return @{
        Response = $resp
        Body = $obj
    }
}

function Get-PropOrNull {
    param(
        [object]$Obj,
        [string]$Name
    )
    if ($null -eq $Obj) { return $null }
    $p = $Obj.PSObject.Properties[$Name]
    if ($null -eq $p) { return $null }
    return $p.Value
}

Write-Host "== Emergency HTTP self-check start ==" -ForegroundColor Cyan
Write-Host "BaseUrl: $BaseUrl"

# 1) /health 基础检查
$health = Invoke-JsonGet -Url "$BaseUrl/health"
Assert-True ($health.Response.StatusCode -eq 200) "/health 状态码应为 200"
$healthOk = Get-PropOrNull -Obj $health.Body -Name "ok"
$healthTrace = [string](Get-PropOrNull -Obj $health.Body -Name "trace_id")
Assert-True ([bool]$healthOk) "/health body.ok 应为 true"
Assert-True (-not [string]::IsNullOrWhiteSpace($healthTrace)) "/health body.trace_id 不应为空（请确认服务已重启到最新代码）"
Assert-True (-not [string]::IsNullOrWhiteSpace($health.Response.Headers["X-Trace-Id"])) "/health 响应头 X-Trace-Id 不应为空"
Write-Host "[OK] /health" -ForegroundColor Green

# 2) /emergency/flow/self-check 轻量流程检查
$flow = Invoke-JsonGet -Url "$BaseUrl/emergency/flow/self-check"
Assert-True ($flow.Response.StatusCode -eq 200) "/emergency/flow/self-check 状态码应为 200"
$flowOk = Get-PropOrNull -Obj $flow.Body -Name "ok"
$checks = Get-PropOrNull -Obj $flow.Body -Name "checks"
$mgmt = Get-PropOrNull -Obj $checks -Name "management_store"
$fqq = Get-PropOrNull -Obj $checks -Name "forecast_queue"
$oqq = Get-PropOrNull -Obj $checks -Name "observation_queue"
Assert-True ([bool]$flowOk) "flow self-check 总体 ok 应为 true"
Assert-True ([bool](Get-PropOrNull -Obj $mgmt -Name "ok")) "management_store.ok 应为 true"
Assert-True ([bool](Get-PropOrNull -Obj $fqq -Name "ok")) "forecast_queue.ok 应为 true"
Assert-True ([bool](Get-PropOrNull -Obj $oqq -Name "ok")) "observation_queue.ok 应为 true"
Write-Host "[OK] /emergency/flow/self-check" -ForegroundColor Green

# 3) response-board 会话连续性检查（Cookie + X-Session-Id）
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$rbHeaders = @{
    "X-Session-Id" = $SessionId
}
$rb1 = Invoke-JsonGet -Url "$BaseUrl/emergency/management/response-board" -Headers $rbHeaders -Session $session
Start-Sleep -Seconds 2
$rb2 = Invoke-JsonGet -Url "$BaseUrl/emergency/management/response-board" -Headers $rbHeaders -Session $session
Assert-True ($rb1.Response.StatusCode -eq 200) "response-board 第一次状态码应为 200"
Assert-True ($rb2.Response.StatusCode -eq 200) "response-board 第二次状态码应为 200"
$rb1Trace = [string](Get-PropOrNull -Obj $rb1.Body -Name "trace_id")
$rb2Trace = [string](Get-PropOrNull -Obj $rb2.Body -Name "trace_id")
Assert-True (-not [string]::IsNullOrWhiteSpace($rb1Trace)) "response-board 第一次 trace_id 不应为空"
Assert-True (-not [string]::IsNullOrWhiteSpace($rb2Trace)) "response-board 第二次 trace_id 不应为空"
Write-Host "[OK] /emergency/management/response-board (会话连续访问)" -ForegroundColor Green

# 4) trace_id 透传检查（自定义 X-Trace-Id）
$traceHeaders = @{
    "X-Trace-Id" = $TraceId
}
$traceCheck = Invoke-JsonGet -Url "$BaseUrl/health" -Headers $traceHeaders
$respTraceHeader = [string]$traceCheck.Response.Headers["X-Trace-Id"]
$respTraceBody = [string](Get-PropOrNull -Obj $traceCheck.Body -Name "trace_id")
Assert-True ($respTraceHeader -eq $TraceId) "X-Trace-Id 响应头应透传为 $TraceId"
Assert-True ($respTraceBody -eq $TraceId) "body.trace_id 应透传为 $TraceId"
Write-Host "[OK] trace_id 透传" -ForegroundColor Green

Write-Host ""
Write-Host "ALL CHECKS PASSED" -ForegroundColor Green
Write-Host "Tip: add this script to your pre-release checklist."
