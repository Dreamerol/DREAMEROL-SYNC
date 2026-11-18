param(
    [switch]$Force,
    [switch]$DryRun,
    [switch]$Watchdog
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $repoRoot ".env"
$statePath = Join-Path $env:TEMP "dreamerol-smoke-state.json"
$logDir = Join-Path $env:TEMP "dreamerol-smoke"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message"
}

function Get-PortListener {
    param([int]$Port)
    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        return Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    }
    return $null
}

function Wait-ForPort {
    param(
        [int]$Port,
        [int]$TimeoutSeconds = 20
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $client.Connect("127.0.0.1", $Port)
            if ($client.Connected) {
                return
            }
        }
        catch {
        }
        finally {
            $client.Close()
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Timed out waiting for port $Port."
}

function Wait-ForBrokerHealth {
    param(
        [string]$Url,
        [int]$TimeoutSeconds = 20
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-RestMethod -Uri $Url -TimeoutSec 5
            if ($response.status -eq "ok") {
                return
            }
        }
        catch {
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Timed out waiting for Broker health at $Url."
}

function Stop-StartedProcesses {
    param($Processes)
    foreach ($process in $Processes) {
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    throw "python was not found in PATH."
}

if (-not (Test-Path -LiteralPath $envPath)) {
    throw ".env was not found. Copy .env.example to .env first."
}

$envText = [System.IO.File]::ReadAllText($envPath)
$callbackMatch = [regex]::Match($envText, "(?m)^BROKER_CALLBACK_URL=(.+)$")
if (-not $callbackMatch.Success) {
    throw ".env does not contain BROKER_CALLBACK_URL."
}
$callbackUrl = $callbackMatch.Groups[1].Value.Trim()
if (-not $callbackUrl) {
    throw "BROKER_CALLBACK_URL is empty."
}

$expectedProcesses = @{
    broker = "python"
    dashboard = "python"
}

if (Test-Path -LiteralPath $statePath) {
    $existingState = [System.IO.File]::ReadAllText($statePath) | ConvertFrom-Json
    $running = @()
    foreach ($name in $expectedProcesses.Keys) {
        $process = Get-Process -Id $existingState.$name -ErrorAction SilentlyContinue
        if ($process) {
            $running += "$name (PID $($process.Id))"
        }
    }
    if ($running.Count -gt 0) {
        if (-not $Force) {
            throw "Smoke test is already running: $($running -join ", "). Use -Force to restart."
        }
        & (Join-Path $PSScriptRoot "stop-smoke.ps1")
    }
}

foreach ($port in 8000, 8001) {
    $listener = Get-PortListener -Port $port
    if ($listener) {
        throw "Port $port is already in use by PID $($listener.OwningProcess)."
    }
}

if ($DryRun) {
    Write-Host "Dry run: prerequisites are available."
    Write-Host "Would start Broker on http://127.0.0.1:8000 and dashboard on http://127.0.0.1:8001."
    Write-Host "Callback: $callbackUrl"
    if ($Watchdog) {
        Write-Host "Would also start the Broker/dashboard watchdog."
    }
    return
}

New-Item -ItemType Directory -Path $logDir -Force | Out-Null

$brokerOut = Join-Path $logDir "broker.out.log"
$brokerErr = Join-Path $logDir "broker.err.log"
$dashboardOut = Join-Path $logDir "dashboard.out.log"
$dashboardErr = Join-Path $logDir "dashboard.err.log"

Remove-Item -LiteralPath $brokerOut, $brokerErr, $dashboardOut, $dashboardErr -ErrorAction SilentlyContinue

$brokerProcess = $null
$dashboardProcess = $null
$watchdogProcess = $null

try {
    Write-Step "Starting Broker on port 8000"
    $brokerArgs = @("-m", "uvicorn", "auth_broker.app:create_app", "--factory", "--host", "127.0.0.1", "--port", "8000")
    $brokerProcess = Start-Process -FilePath $python -ArgumentList $brokerArgs -PassThru -WindowStyle Hidden -WorkingDirectory $repoRoot -RedirectStandardOutput $brokerOut -RedirectStandardError $brokerErr
    Wait-ForPort -Port 8000 -TimeoutSeconds 20
    Wait-ForBrokerHealth -Url "http://127.0.0.1:8000/healthz" -TimeoutSeconds 20

    Write-Step "Starting dashboard on port 8001"
    $dashboardArgs = @("-m", "uvicorn", "api.app:app", "--host", "127.0.0.1", "--port", "8001")
    $dashboardProcess = Start-Process -FilePath $python -ArgumentList $dashboardArgs -PassThru -WindowStyle Hidden -WorkingDirectory $repoRoot -RedirectStandardOutput $dashboardOut -RedirectStandardError $dashboardErr
    Wait-ForPort -Port 8001 -TimeoutSeconds 20

    Write-Step "Verifying dashboard"
    $dashboardResponse = Invoke-WebRequest -Uri "http://127.0.0.1:8001/" -UseBasicParsing -TimeoutSec 15
    if ($dashboardResponse.StatusCode -ne 200) {
        throw "Dashboard returned HTTP $($dashboardResponse.StatusCode)."
    }

    $state = [ordered]@{
        broker = $brokerProcess.Id
        dashboard = $dashboardProcess.Id
        callback_url = $callbackUrl
        started_at = (Get-Date).ToString("o")
    }
    [System.IO.File]::WriteAllText($statePath, ($state | ConvertTo-Json), $utf8NoBom)

    if ($Watchdog) {
        $watchdogScript = Join-Path $PSScriptRoot "watch-smoke.ps1"
        $watchdogOut = Join-Path $logDir "watchdog.out.log"
        $watchdogErr = Join-Path $logDir "watchdog.err.log"
        Remove-Item -LiteralPath $watchdogOut, $watchdogErr -ErrorAction SilentlyContinue
        $watchdogProcess = Start-Process -FilePath "powershell.exe" -ArgumentList @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", "`"$watchdogScript`"",
            "-IntervalSeconds", "5",
            "-Quiet"
        ) -PassThru -WindowStyle Hidden -WorkingDirectory $repoRoot -RedirectStandardOutput $watchdogOut -RedirectStandardError $watchdogErr
        $state["watchdog"] = $watchdogProcess.Id
        [System.IO.File]::WriteAllText($statePath, ($state | ConvertTo-Json), $utf8NoBom)
    }

    Write-Host ""
    Write-Host "Broker:    http://127.0.0.1:8000 (PID $($brokerProcess.Id))"
    Write-Host "Dashboard: http://127.0.0.1:8001 (PID $($dashboardProcess.Id))"
    if ($Watchdog) {
        Write-Host "Watchdog:  PID $($watchdogProcess.Id)"
    }
    Write-Host "Callback:  $callbackUrl"
    Write-Host ""
    Write-Host "Open http://127.0.0.1:8001 and sign in with GitHub."
    Write-Host "The GitHub App callback must match the callback above; no tunnel is started."
}
catch {
    Stop-StartedProcesses -Processes @($watchdogProcess, $dashboardProcess, $brokerProcess)
    $errorLog = Join-Path $logDir "start-smoke.error.log"
    [System.IO.File]::WriteAllText($errorLog, ($_ | Out-String), $utf8NoBom)
    throw
}
