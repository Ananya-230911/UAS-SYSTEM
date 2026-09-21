<#
.SYNOPSIS
    Stops every UAS-SYSTEM process started by run_local.ps1 (or a manual
    multi-terminal session) -- run this before a fresh start (e.g. right
    before a demo) so leftover processes from an earlier session don't
    cause "port already in use" errors.

.DESCRIPTION
    Earlier troubleshooting used "Get-Process python | Stop-Process
    -Force" -- effective, but blunt: it stops every Python process on
    the machine, including anything unrelated you might have running
    (another course's script, a Jupyter kernel, etc). This script
    instead looks up which process actually owns each of this
    project's known ports (API 8000, gateway 8001, UI 8080, plus any
    extra fleet ports you've used) and stops only those.

.PARAMETER ExtraPorts
    Additional gateway ports to also check and stop, e.g. from using
    the "Add a Vehicle" panel to spawn extra simulated vehicles
    (8002, 8003, ...).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\stop_local.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\stop_local.ps1 -ExtraPorts 8002,8003
#>

param(
    [int[]]$ExtraPorts = @()
)

$ports = @(8000, 8001, 8080) + $ExtraPorts
$stoppedPids = @{}

foreach ($port in $ports) {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($conn in $conns) {
        $procId = $conn.OwningProcess
        if ($stoppedPids.ContainsKey($procId)) { continue }
        try {
            $proc = Get-Process -Id $procId -ErrorAction Stop
            Write-Host "Stopping $($proc.ProcessName) (PID $procId) on port $port..."
            Stop-Process -Id $procId -Force
            $stoppedPids[$procId] = $true
        } catch {
            # Process already gone between listing and stopping -- fine.
        }
    }
}

if ($stoppedPids.Count -eq 0) {
    Write-Host "Nothing was listening on ports $($ports -join ', ') -- already stopped."
} else {
    Write-Host "Stopped $($stoppedPids.Count) process(es). Safe to run run_local.ps1 again."
}
