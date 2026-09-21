<#
.SYNOPSIS
    Starts the Perception & AI Decision service
    (docs/adr/0015-perception-and-decision.md) in its own new
    PowerShell window.

.DESCRIPTION
    Kept separate from run_local.ps1 on purpose: this service's
    ultralytics/torch dependency is large (several hundred MB) and the
    feature is entirely optional -- the core GCS stack (telemetry,
    commands, missions, fleet, geofencing) works fully without it, and
    starting it is a deliberate extra step so a core demo never depends
    on this heavier, newer piece.

    One-time setup (installs the dependency into the same shared venv
    every other service already uses):
        .venv\Scripts\pip.exe install -r services\perception\requirements.txt

    First run after that needs network access once to download the
    ~6MB YOLOv8n model weights (cached afterward in
    services\perception\.models\, gitignored -- a build artifact).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run_perception.ps1
#>

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    throw "Couldn't find $venvPython -- create the venv first (see README's Quick Start on Windows)."
}

Write-Host "Starting Perception & AI Decision service..."
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$root\services\perception'; `$env:PYTHONPATH = '.'; & '$venvPython' -m uvicorn app.main:app --host 0.0.0.0 --port 8010"
)

$deadline = (Get-Date).AddSeconds(30)
$up = $false
while ((Get-Date) -lt $deadline) {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:8010/health" -TimeoutSec 2 | Out-Null
        $up = $true
        break
    } catch {
        Start-Sleep -Milliseconds 500
    }
}

if ($up) {
    Write-Host "Perception service is up on http://localhost:8010."
    Write-Host "The GCS UI already defaults to that address -- no URL change needed."
} else {
    Write-Warning "Perception service did not respond within 30s -- check its window for errors. First run downloads model weights and needs network access; if requirements.txt isn't installed yet, run the one-time setup command in this script's header first."
}
