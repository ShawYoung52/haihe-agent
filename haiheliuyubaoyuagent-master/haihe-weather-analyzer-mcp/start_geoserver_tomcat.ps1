$ErrorActionPreference = "Stop"

$TomcatCandidates = @(
  "D:\tj\apache-tomcat-9.0.107",
  "D:\tj\apache-tomcat-9.0.106"
)

$TomcatHome = $env:TOMCAT_HOME
if (-not $TomcatHome) {
  $TomcatHome = ($TomcatCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1)
}
if (-not $TomcatHome) {
  throw "Tomcat not found. Set TOMCAT_HOME. Candidates: $($TomcatCandidates -join ', ')"
}

$StartupBat = Join-Path $TomcatHome "bin\startup.bat"
$ShutdownBat = Join-Path $TomcatHome "bin\shutdown.bat"
$ServerXml = Join-Path $TomcatHome "conf\server.xml"
$WebappsDir = Join-Path $TomcatHome "webapps"

if (-not (Test-Path $StartupBat)) { throw "Missing: $StartupBat" }
if (-not (Test-Path $ServerXml)) { throw "Missing: $ServerXml" }
if (-not (Test-Path $WebappsDir)) { throw "Missing: $WebappsDir" }

$DesiredPort = if ($env:GEOSERVER_TOMCAT_PORT) { [int]$env:GEOSERVER_TOMCAT_PORT } else { 8090 }
$GeoServerBase = "http://127.0.0.1:$DesiredPort/geoserver"

# 1) Patch Tomcat HTTP port from 8080 to desired port.
$raw = Get-Content -Path $ServerXml -Raw
$patched = $raw -replace '(<Connector\s+port=")8080(")', "`${1}$DesiredPort`${2}"
if ($patched -ne $raw) {
  Set-Content -Path $ServerXml -Value $patched -Encoding UTF8
  Write-Host "[geoserver] Tomcat HTTP port updated to $DesiredPort"
} else {
  Write-Host "[geoserver] Tomcat port already set or 8080 connector not found"
}

# 2) Deploy geoserver.war if not present under webapps.
$DeployedWar = Join-Path $WebappsDir "geoserver.war"
if (-not (Test-Path $DeployedWar)) {
  $WarCandidates = Get-ChildItem -Path "D:\tj" -Filter "*.war" -File -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match "geoserver" } |
    Select-Object -ExpandProperty FullName

  $WarPath = $WarCandidates | Select-Object -First 1
  if (-not $WarPath) {
    Write-Warning "[geoserver] geoserver*.war not found under D:\tj. Copy it manually to $DeployedWar"
  } else {
    Copy-Item -Path $WarPath -Destination $DeployedWar -Force
    Write-Host "[geoserver] WAR deployed: $WarPath -> $DeployedWar"
  }
} else {
  Write-Host "[geoserver] $DeployedWar already exists"
}

# 3) Restart Tomcat.
if (Test-Path $ShutdownBat) {
  & $ShutdownBat | Out-Null
  Start-Sleep -Seconds 2
}
& $StartupBat | Out-Null

Write-Host "[geoserver] Tomcat started. GeoServer: $GeoServerBase"
Write-Host "[geoserver] First boot may take 20-60 seconds."
