<#
.SYNOPSIS
    Starts an N-vehicle fleet (API + N gateway/simulator pairs + UI) in
    one command, instead of manually running run_local.ps1's steps once
    per vehicle with hand-computed ports (see the README's "Want a
    second vehicle" section).

.DESCRIPTION
    Builds on exactly the same pieces run_local.ps1 and the README's
    manual multi-vehicle walkthrough already use -- same port scheme
    (gateway 8001/8002/..., MAVLink 14550/14551/...), same
    GATEWAY_URLS_JSON mechanism (docs/adr/0009-multi-vehicle-fleet.md).
    No gateway/API/simulator code changes needed for this; it only
    automates starting N copies of what already works for one vehicle.

    Vehicles are spaced out in a small grid around the home location so
    they don't all start on top of each other on the map -- cosmetic
    only, not a real formation.

.PARAMETER Count
    Number of simulated vehicles to start. Defaults to 3.

.PARAMETER HomeLat
.PARAMETER HomeLon
    Starting location for sim-1; later vehicles are offset from it.
    Defaults to sim.py's own default (Stanford, CA) if not given.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run_fleet.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run_fleet.ps1 -Count 5
#>

param(
    [int]$Count = 3,
    [double]$HomeLat = 37.4275,
    [double]$HomeLon = -122.1697
)

$ErrorActionPreference = "Stop"

if ($Count -lt 1) {
    throw "-Count must be at least 1."
}

$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

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

# Compute every vehicle's ports/home position up front (same scheme the
# README's manual multi-vehicle walkthrough already uses), and the
# GATEWAY_URLS_JSON map for everything past sim-1 (which uses the API's
# default GATEWAY_BASE_URL, same as single-vehicle run_local.ps1) -- so
# the API only has to start once, already knowing about every vehicle,
# instead of needing a restart partway through like the manual README
# flow does.
$vehicles = @()
$gatewayUrls = @{}
for ($i = 0; $i -lt $Count; $i++) {
    $vehicleId = "sim-$($i + 1)"
    $gatewayPort = 8001 + $i
    $mavlinkPort = 14550 + $i
    $lat = $HomeLat + (0.0013 * [math]::Floor($i / 3))
    $lon = $HomeLon + (0.0013 * ($i % 3))
    $vehicles += [PSCustomObject]@{
        Id = $vehicleId
        GatewayPort = $gatewayPort
        MavlinkPort = $mavlinkPort
        Lat = $lat
        Lon = $lon
    }
    if ($i -gt 0) {
        $gatewayUrls[$vehicleId] = "http://127.0.0.1:$gatewayPort"
    }
}

$gatewayUrlsJson = ($gatewayUrls | ConvertTo-Json -Compress)
if ($gatewayUrls.Count -eq 0) { $gatewayUrlsJson = "{}" }

# Set GATEWAY_URLS_JSON on THIS script's own process rather than
# embedding it as literal text inside the child's -Command string --
# the JSON value's own double quotes get mangled when a string
# containing embedded double quotes is nested inside the single-quoted
# wrapper inside the double-quoted -Command argument inside
# Start-Process's -ArgumentList (three layers of requoting is one too
# many). Start-Process's child inherits the parent's environment by
# default, so setting it here reaches the API process just as reliably
# and far more simply.
$env:GATEWAY_URLS_JSON = $gatewayUrlsJson

Write-Host "Starting API for a $Count-vehicle fleet..."
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$root\services\api'; & '$venvPython' -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
)
Wait-Health "http://127.0.0.1:8000/health" "API"
Write-Host "API is up, routing: $gatewayUrlsJson"

foreach ($v in $vehicles) {
    Write-Host "Starting gateway + simulator for $($v.Id) (gateway :$($v.GatewayPort), MAVLink :$($v.MavlinkPort))..."
    Start-Process powershell -ArgumentList @(
        "-NoExit", "-Command",
        "cd '$root\services\telemetry-gateway'; `$env:API_BASE_URL = 'http://127.0.0.1:8000'; `$env:VEHICLE_ID = '$($v.Id)'; `$env:GATEWAY_MAVLINK_PORT = '$($v.MavlinkPort)'; & '$venvPython' -m uvicorn gateway.main:app --host 0.0.0.0 --port $($v.GatewayPort)"
    )
    Wait-Health "http://127.0.0.1:$($v.GatewayPort)/health" "$($v.Id) gateway"

    Start-Process powershell -ArgumentList @(
        "-NoExit", "-Command",
        "cd '$root'; & '$venvPython' simulation\sitl\sim.py --target-host 127.0.0.1 --target-port $($v.MavlinkPort) --home-lat $($v.Lat) --home-lon $($v.Lon)"
    )
}

Write-Host "Waiting for every gateway to see its simulator..."
foreach ($v in $vehicles) {
    $deadline = (Get-Date).AddSeconds(20)
    $connected = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$($v.GatewayPort)/health" -TimeoutSec 2
            if ($health.mavlink_connected -eq $true) { $connected = $true; break }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    if ($connected) {
        Write-Host "$($v.Id): gateway sees its simulator."
    } else {
        Write-Warning "$($v.Id): gateway did not report mavlink_connected: true within 20s -- check its simulator window."
    }
}

Write-Host "Starting UI server..."
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$root\apps\gcs-web'; & '$venvPython' -m http.server 8080"
)

Write-Host ""
Write-Host "$Count vehicles running in their own windows (closing this one won't stop them)."
Write-Host "Open: http://localhost:8080/?api=http://localhost:8000"
