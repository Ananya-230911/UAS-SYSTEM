<#
.SYNOPSIS
    Windows PowerShell equivalent of scripts/run_local.sh -- starts the API,
    telemetry gateway, simulator, and static UI server, each in its own new
    PowerShell window, in the order that actually matters (API, then
    gateway, then simulator, then UI), waiting for each one's health check
    before starting the next.

.DESCRIPTION
    This exists because bash scripts don't run natively in Windows
    PowerShell, so the README's manual Quick Start has you open 4 terminals
    and type one block into each by hand. That's fine once; it's tedious
    every single test run. This script does the exact same 4 steps for you.

    Single vehicle only (matches the manual single-vehicle Quick Start in
    the root README). For a second vehicle, follow the README's "Want a
    second vehicle (Phase 3 fleet)?" section manually alongside this --
    fleet size isn't parameterized here to keep this script simple.

    If you've set $env:API_KEY before running this script (see
    docs/adr/0011-api-authentication.md), it's inherited by every spawned
    window automatically -- nothing extra to pass.

.PARAMETER HomeLat
    Latitude the simulator starts flying from. Defaults to sim.py's own
    default (Stanford, CA) if not given -- same as running sim.py with
    no --home-lat/--home-lon flags at all.

.PARAMETER HomeLon
    Longitude the simulator starts flying from. See HomeLat.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run_local.ps1

.EXAMPLE
    # Start the vehicle over Mumbai instead of the Stanford default --
    # e.g. lat/lon picked from the GCS UI's map search or "Add a
    # Vehicle" location picker.
    powershell -ExecutionPolicy Bypass -File scripts\run_local.ps1 -HomeLat 19.0760 -HomeLon 72.8777
#>

param(
    [double]$HomeLat,
    [double]$HomeLon
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

# Only pass --home-lat/--home-lon through if the caller actually gave
# both -- omitting them entirely lets sim.py fall back to its own
# defaults, rather than this script silently picking a value for
# whichever one wasn't provided.
$homeArgs = ""
if ($PSBoundParameters.ContainsKey("HomeLat") -and $PSBoundParameters.ContainsKey("HomeLon")) {
    $homeArgs = "--home-lat $HomeLat --home-lon $HomeLon"
} elseif ($PSBoundParameters.ContainsKey("HomeLat") -or $PSBoundParameters.ContainsKey("HomeLon")) {
    throw "Pass both -HomeLat and -HomeLon together, or neither."
}

if (-not (Test-Path $venvPython)) {
    throw "Couldn't find $venvPython -- create the venv first (see README's Quick Start on Windows)."
}

function Wait-Health {
    param([string]$Url, [string]$Label, [int]$TimeoutSec = 30)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            Invoke-RestMethod -Uri $Url -TimeoutSec 2 | Out-Null
            return
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    throw "Timed out waiting for $Label at $Url -- check its window for errors."
}

Write-Host "Starting API..."
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$root\services\api'; & '$venvPython' -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
)
Wait-Health "http://127.0.0.1:8000/health" "API"
Write-Host "API is up."

Write-Host "Starting telemetry gateway..."
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$root\services\telemetry-gateway'; `$env:API_BASE_URL = 'http://127.0.0.1:8000'; & '$venvPython' -m uvicorn gateway.main:app --host 0.0.0.0 --port 8001"
)
Wait-Health "http://127.0.0.1:8001/health" "gateway"
Write-Host "Gateway is up."

Write-Host "Starting simulator..."
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$root'; & '$venvPython' simulation\sitl\sim.py --target-host 127.0.0.1 --target-port 14550 $homeArgs"
)

# Poll the gateway's own health check for mavlink_connected -- that's the
# real signal the simulator actually reached it (a HEARTBEAT was seen),
# not just that the simulator process started.
$deadline = (Get-Date).AddSeconds(20)
$connected = $false
while ((Get-Date) -lt $deadline) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8001/health" -TimeoutSec 2
        if ($health.mavlink_connected -eq $true) { $connected = $true; break }
    } catch { }
    Start-Sleep -Milliseconds 500
}
if ($connected) {
    Write-Host "Gateway sees the simulator (mavlink_connected: true)."
} else {
    Write-Warning "Gateway did not report mavlink_connected: true within 20s -- check the simulator window for errors."
}

Write-Host "Starting UI server..."
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$root\apps\gcs-web'; & '$venvPython' -m http.server 8080"
)

Write-Host ""
Write-Host "All four are running in their own windows (closing this one won't stop them)."
Write-Host "Open: http://localhost:8080/?api=http://localhost:8000"
