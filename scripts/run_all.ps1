<#
.SYNOPSIS
    Starts everything the GCS page needs, in one command, and checks it.

.DESCRIPTION
    Runs the two existing launchers -- nothing new is started:
      1. scripts\run_fleet.ps1  -> flight API :8000 (the header badge),
         telemetry gateways, MAVLink simulators, and the page on :8080.
      2. scripts\run_cbba.ps1   -> fleet-agent CBBA server :8020 (the
         Multi-drone panel).
    Any part that is already running is skipped, so running this twice
    is harmless. At the end it checks all three addresses, says which
    one (if any) is not answering, and opens the page.

    One-time setup (same shared venv every service uses):
        .venv\Scripts\pip.exe install -r services\api\requirements.txt -r services\telemetry-gateway\requirements.txt -r simulation\sitl\requirements.txt -r services\fleet-agent\requirements.txt

.PARAMETER Webcam
    Also start the perception service (YOLOv8) with this computer's
    webcam, so the quick survey's drones take a real picture at each
    place. Needs the one-time perception install:
        .venv\Scripts\pip.exe install -r services\perception\requirements.txt

.PARAMETER Count
.PARAMETER HomeLat
.PARAMETER HomeLon
    Passed through to run_fleet.ps1 (number of simulated drones and
    sim-1's start position).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run_all.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run_all.ps1 -Count 5

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run_all.ps1 -Webcam
#>

param(
    [switch]$Webcam,
    [int]$Count = 3,
    [double]$HomeLat = 37.4275,
    [double]$HomeLon = -122.1697
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

function Test-Up([string]$Url) {
    try {
        Invoke-WebRequest -Uri $Url -TimeoutSec 2 -UseBasicParsing | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Test-PortBusy([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect("127.0.0.1", $Port)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

# --- 1. Flight stack: API :8000 (header badge), gateways, simulators, page :8080
if (Test-Up "http://127.0.0.1:8000/health") {
    Write-Host "Flight API already running on :8000 -- not starting a second copy."
} else {
    if (Test-PortBusy 8080) {
        Write-Warning ("Port 8080 is already in use (probably an older 'http.server 8080' window). " +
            "That's fine if it serves apps\gcs-web; the extra page window this starts will just show an error.")
    }
    try {
        & (Join-Path $PSScriptRoot "run_fleet.ps1") -Count $Count -HomeLat $HomeLat -HomeLon $HomeLon
    } catch {
        Write-Warning "The flight stack did not start: $($_.Exception.Message)"
        Write-Warning "If an API window opened, its red error says why. Most often the one-time pip install (see this script's header) was not run."
    }
}

# --- 2. CBBA server :8020 (Multi-drone panel)
if (Test-Up "http://127.0.0.1:8020/health") {
    Write-Host "CBBA server already running on :8020 -- not starting a second copy."
} else {
    & (Join-Path $PSScriptRoot "run_cbba.ps1")
}

# --- 2b. Camera (perception service :8010), only with -Webcam
if ($Webcam) {
    if (Test-Up "http://127.0.0.1:8010/health") {
        Write-Warning "Perception service already running on :8010 -- close its window first if it was not started with -Webcam."
    } else {
        try {
            & (Join-Path $PSScriptRoot "run_perception.ps1") -Webcam
        } catch {
            Write-Warning "The camera service did not start: $($_.Exception.Message)"
        }
    }
}

# --- 3. The page itself, if nothing serves it yet (e.g. the API was already up)
if (-not (Test-PortBusy 8080)) {
    Write-Host "Starting UI server on :8080..."
    Start-Process powershell -ArgumentList @(
        "-NoExit", "-Command",
        "cd '$root\apps\gcs-web'; & '$venvPython' -m http.server 8080"
    )
    Start-Sleep -Seconds 2
}

# --- 4. Check all three and say exactly what is missing
Write-Host ""
Write-Host "Checking..."
$checks = @(
    @{ Name = "Flight API  :8000  (header badge 'live')"; Url = "http://127.0.0.1:8000/health"; Fix = "scripts\run_fleet.ps1 -- check its API window for errors" },
    @{ Name = "CBBA server :8020  (Multi-drone panel)  "; Url = "http://127.0.0.1:8020/health"; Fix = "scripts\run_cbba.ps1 -- check its window for errors" },
    @{ Name = "Web page    :8080                       "; Url = "http://127.0.0.1:8080/"; Fix = "cd apps\gcs-web; ..\..\.venv\Scripts\python.exe -m http.server 8080" }
)
if ($Webcam) {
    $checks += @{ Name = "Camera      :8010  (webcam + YOLOv8)     "; Url = "http://127.0.0.1:8010/health"; Fix = "scripts\run_perception.ps1 -Webcam -- check its window; one-time: .venv\Scripts\pip.exe install -r services\perception\requirements.txt" }
}
$allUp = $true
foreach ($c in $checks) {
    if (Test-Up $c.Url) {
        Write-Host "  OK       $($c.Name)" -ForegroundColor Green
    } else {
        $allUp = $false
        Write-Host "  MISSING  $($c.Name)  -> start: $($c.Fix)" -ForegroundColor Red
    }
}

Write-Host ""
if ($allUp) {
    Write-Host "All running. Open http://localhost:8080/ and press Ctrl+Shift+R once to load the latest page." -ForegroundColor Green
    try { Start-Process "http://localhost:8080/" } catch { }
} else {
    Write-Warning "Not everything is running yet (see MISSING above). Fix that, then run this script again -- it skips what is already up."
}
