<#
.SYNOPSIS
    Starts the fleet-agent CBBA simulation server
    (docs/CBBA_PLAN.md, docs/adr/0018-cbba-core-allocation.md) in its own
    new PowerShell window, on port 8020.

.DESCRIPTION
    Read-only: it runs the CBBA multi-drone allocation simulation and
    returns the result for the GCS page's "CBBA task allocation" panel.
    It sends no commands and does not talk to any drone, gateway or LLM.
    Independent of run_local.ps1 / run_fleet.ps1 -- the CBBA panel works
    even when no simulated drone is running.

    One-time setup (same shared venv every other service uses):
        .venv\Scripts\pip.exe install -r services\fleet-agent\requirements.txt

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run_cbba.ps1
#>

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    throw "Couldn't find $venvPython -- create the venv first (see README's Quick Start on Windows)."
}

Write-Host "Starting CBBA simulation server..."
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$root\services\fleet-agent'; `$env:PYTHONPATH = '.'; & '$venvPython' -m uvicorn app.server:app --host 127.0.0.1 --port 8020"
)

$deadline = (Get-Date).AddSeconds(30)
$up = $false
while ((Get-Date) -lt $deadline) {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:8020/health" -TimeoutSec 2 | Out-Null
        $up = $true
        break
    } catch {
        Start-Sleep -Milliseconds 500
    }
}

if ($up) {
    Write-Host "CBBA server is up on http://localhost:8020."
    Write-Host "In the GCS page, scroll the right-hand panel to 'CBBA task allocation' and click 'Run CBBA'."
} else {
    Write-Warning "CBBA server did not respond within 30s -- check its window for errors. If fastapi/uvicorn aren't installed, run the one-time setup command in this script's header."
}
