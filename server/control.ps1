# VoxCraft ASR server local control script (Windows / PowerShell).
# IMPORTANT: Keep this file ASCII-only. Windows PowerShell 5.1 reads UTF-8 files
# without a BOM as the system code page; Japanese text can then corrupt quotes and
# cause parser errors. User-facing Japanese documentation belongs in README.md.
#
# Usage:
#   .\control.ps1 status
#   .\control.ps1 start
#   .\control.ps1 stop
#   .\control.ps1 restart
#
# MAINTAINER / AI NOTE:
# - This script is deliberately local-only. Do not turn these operations into an
#   unauthenticated HTTP endpoint: VoxCraft currently listens on 0.0.0.0 and has
#   no API authentication.
# - A stopped server cannot receive a remote "start" request. Full remote control
#   requires a separate, always-on, authenticated supervisor (preferably reachable
#   only through Tailscale or another private network).
# - Stop-Process is not graceful on Windows. Do not stop the server while a live
#   transcription is recording. A future graceful design should make the launcher
#   own the uvicorn process or add a local-only authenticated shutdown channel.
# - Before stopping anything, this script verifies both the listening port and the
#   uvicorn command line. Keep this fail-closed check if the launcher changes.
# - This is manual control, not a watchdog. Crash/hang auto-recovery belongs in a
#   Windows service, Scheduled Task, or separate supervisor.

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action = "status",

    [ValidateRange(1, 600)]
    [int]$TimeoutSec = 120
)

$ErrorActionPreference = "Stop"

$voxcraftServerDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$voxcraftLauncher = Join-Path $voxcraftServerDir "autostart.vbs"
$voxcraftPort = if ($env:VOXCRAFT_PORT) { [int]$env:VOXCRAFT_PORT } else { 8760 }
$voxcraftHealthUrl = "http://127.0.0.1:$voxcraftPort/health"

function Get-VoxCraftListener {
    $listeners = @(
        Get-NetTCPConnection -State Listen -LocalPort $voxcraftPort -ErrorAction SilentlyContinue
    )
    if ($listeners.Count -eq 0) {
        return $null
    }

    $processIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($processIds.Count -ne 1) {
        throw "Multiple processes are listening on port $voxcraftPort. Refusing to continue."
    }
    return $listeners[0]
}

function Get-VerifiedVoxCraftProcess {
    param([Parameter(Mandatory = $true)]$Listener)

    $processId = [int]$Listener.OwningProcess
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction Stop
    if ($null -eq $process) {
        throw "Could not inspect the process listening on port $voxcraftPort."
    }

    $commandLine = [string]$process.CommandLine
    $escapedPort = [regex]::Escape([string]$voxcraftPort)
    $isUvicornApp = $commandLine -match '(?i)(?:^|\s)-m\s+uvicorn\s+main:app(?:\s|$)'
    $usesExpectedPort = $commandLine -match "(?i)--port(?:\s+|=)$escapedPort(?:\s|$)"
    if (-not ($isUvicornApp -and $usesExpectedPort)) {
        throw (
            "Port $voxcraftPort is used by a process that cannot be verified as VoxCraft. " +
            "Refusing to stop it. (PID=$processId, command=$commandLine)"
        )
    }
    return $process
}

function Get-VoxCraftHealth {
    try {
        $health = Invoke-RestMethod -Uri $voxcraftHealthUrl -TimeoutSec 3
        $propertyNames = @($health.PSObject.Properties.Name)
        if ($propertyNames -contains "model" -and
            $propertyNames -contains "ready" -and
            $propertyNames -contains "transcribeModel") {
            return $health
        }
    } catch {
        # A listener can exist while model startup is still in progress or the
        # server is hung. Process verification remains the stop safety boundary.
    }
    return $null
}

function Wait-VoxCraftPort {
    param(
        [Parameter(Mandatory = $true)][bool]$WantListening,
        [Parameter(Mandatory = $true)][string]$Operation
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSec)
    do {
        $isListening = $null -ne (Get-VoxCraftListener)
        if ($isListening -eq $WantListening) {
            return
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)

    $expected = if ($WantListening) { "start" } else { "stop" }
    throw "VoxCraft did not $expected within $TimeoutSec seconds after $Operation."
}

function Show-VoxCraftStatus {
    $listener = Get-VoxCraftListener
    if ($null -eq $listener) {
        Write-Host "VoxCraft: stopped (port $voxcraftPort)"
        return
    }

    $process = Get-VerifiedVoxCraftProcess -Listener $listener
    $health = Get-VoxCraftHealth
    if ($null -ne $health) {
        Write-Host (
            "VoxCraft: running (PID {0}, port {1}, model {2}, device {3}, ready {4})" -f
            $process.ProcessId, $voxcraftPort, $health.model, $health.device, $health.ready
        )
    } else {
        Write-Host (
            "VoxCraft: process is running but /health is not responding " +
            "(PID $($process.ProcessId), port $voxcraftPort)"
        )
    }
}

function Start-VoxCraftServer {
    $listener = Get-VoxCraftListener
    if ($null -ne $listener) {
        $process = Get-VerifiedVoxCraftProcess -Listener $listener
        Write-Host "VoxCraft is already running. (PID $($process.ProcessId), port $voxcraftPort)"
        return
    }
    if (-not (Test-Path -LiteralPath $voxcraftLauncher)) {
        throw "Launcher not found: $voxcraftLauncher"
    }

    Start-Process -FilePath "wscript.exe" `
        -ArgumentList ('"{0}"' -f $voxcraftLauncher) `
        -WorkingDirectory $voxcraftServerDir `
        -WindowStyle Hidden
    Wait-VoxCraftPort -WantListening $true -Operation "start"
    $listener = Get-VoxCraftListener
    $process = Get-VerifiedVoxCraftProcess -Listener $listener
    Write-Host "VoxCraft started. (PID $($process.ProcessId), port $voxcraftPort)"
}

function Stop-VoxCraftServer {
    $listener = Get-VoxCraftListener
    if ($null -eq $listener) {
        Write-Host "VoxCraft is already stopped. (port $voxcraftPort)"
        return
    }

    $process = Get-VerifiedVoxCraftProcess -Listener $listener
    $processId = [int]$process.ProcessId
    Stop-Process -Id $processId -ErrorAction Stop
    Wait-VoxCraftPort -WantListening $false -Operation "stop"
    Write-Host "VoxCraft stopped. (PID $processId)"
}

switch ($Action.ToLowerInvariant()) {
    "status" {
        Show-VoxCraftStatus
    }
    "start" {
        Start-VoxCraftServer
    }
    "stop" {
        Stop-VoxCraftServer
    }
    "restart" {
        Stop-VoxCraftServer
        Start-VoxCraftServer
    }
}
