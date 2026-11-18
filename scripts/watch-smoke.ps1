param(
    [int]$IntervalSeconds = 5,
    [int]$MaxChecks = 0,
    [switch]$Once,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$statePath = Join-Path $env:TEMP "dreamerol-smoke-state.json"
$logDir = Join-Path $env:TEMP "dreamerol-smoke"
$watchdogLog = Join-Path $logDir "watchdog.log"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-WatchdogLog {
    param(
        [string]$Message,
        [string]$Level = "INFO"
    )
    $line = "[{0}] {1} {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    if (-not $Quiet) {
        Write-Host $line
    }
    try {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        Add-Content -LiteralPath $watchdogLog -Value $line -Encoding UTF8
    }
    catch {
    }
}

function Read-SmokeState {
    if (-not (Test-Path -LiteralPath $statePath)) {
        return $null
    }
    try {
        return [System.IO.File]::ReadAllText($statePath) | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Write-SmokeState {
    param($State)
    [System.IO.File]::WriteAllText($statePath, ($State | ConvertTo-Json), $utf8NoBom)
}

function Test-HttpOk {
    param([string]$Url)
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
        return ($response.StatusCode -eq 200)
    }
    catch {
        return $false
    }
}

function Test-BrokerHealth {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:8000/healthz" -TimeoutSec 5
        return ($response.status -eq "ok")
    }
    catch {
        return $false
    }
}

function Test-DashboardHealth {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:8001/healthz" -TimeoutSec 5
        return ($response.status -eq "ok")
    }
    catch {
        return $false
    }
}

function Get-PortOwner {
    param([int]$Port)
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        return [int]$listener.OwningProcess
    }
    return $null
}

function Stop-UnhealthyProcess {
    param(
        $State,
        [int]$Port,
        [string]$StateKey,
        [string]$ProcessName,
        [string]$Label
    )
    $owner = Get-PortOwner -Port $Port
    if ($null -eq $owner) {
        return
    }
    $statePid = $State.$StateKey
    if (-not $statePid -or [int]$statePid -ne $owner) {
        return
    }
    $process = Get-Process -Id $owner -ErrorAction SilentlyContinue
    if ($process -and $process.ProcessName -eq $ProcessName) {
        Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
        Write-WatchdogLog "Stopped unhealthy $Label PID $owner." "WARN"
    }
}

function Start-Broker {
    param($State)
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot ".env"))) {
        Write-WatchdogLog ".env was not found; cannot restart Broker." "ERROR"
        return $false
    }
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) {
        Write-WatchdogLog "python was not found in PATH; cannot restart Broker." "ERROR"
        return $false
    }
    $owner = Get-PortOwner -Port 8000
    if ($null -ne $owner) {
        Write-WatchdogLog "Port 8000 is owned by PID $owner but health check failed; refusing to kill an unknown process." "ERROR"
        return $false
    }

    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $out = Join-Path $logDir "broker-watchdog.out.log"
    $err = Join-Path $logDir "broker-watchdog.err.log"
    Remove-Item -LiteralPath $out, $err -ErrorAction SilentlyContinue

    $process = Start-Process -FilePath $python -ArgumentList @(
        "-m",
        "uvicorn",
        "auth_broker.app:create_app",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        "8000"
    ) -PassThru -WindowStyle Hidden -WorkingDirectory $repoRoot -RedirectStandardOutput $out -RedirectStandardError $err

    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        if (Test-BrokerHealth) {
            $State.broker = $process.Id
            Write-SmokeState -State $State
            Write-WatchdogLog "Broker restarted with PID $($process.Id)."
            return $true
        }
        if ($process.HasExited) {
            break
        }
        Start-Sleep -Milliseconds 500
    }

    Write-WatchdogLog "Broker restart failed; see $err." "ERROR"
    return $false
}

function Start-Dashboard {
    param($State)
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) {
        Write-WatchdogLog "python was not found in PATH; cannot restart dashboard." "ERROR"
        return $false
    }
    $owner = Get-PortOwner -Port 8001
    if ($null -ne $owner) {
        Write-WatchdogLog "Port 8001 is owned by PID $owner but health check failed; refusing to kill an unknown process." "ERROR"
        return $false
    }

    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $out = Join-Path $logDir "dashboard-watchdog.out.log"
    $err = Join-Path $logDir "dashboard-watchdog.err.log"
    Remove-Item -LiteralPath $out, $err -ErrorAction SilentlyContinue

    $process = Start-Process -FilePath $python -ArgumentList @(
        "-m",
        "uvicorn",
        "api.app:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8001"
    ) -PassThru -WindowStyle Hidden -WorkingDirectory $repoRoot -RedirectStandardOutput $out -RedirectStandardError $err

    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        if (Test-DashboardHealth) {
            $State.dashboard = $process.Id
            Write-SmokeState -State $State
            Write-WatchdogLog "Dashboard restarted with PID $($process.Id)."
            return $true
        }
        if ($process.HasExited) {
            break
        }
        Start-Sleep -Milliseconds 500
    }

    Write-WatchdogLog "Dashboard restart failed; see $err." "ERROR"
    return $false
}

function Invoke-WatchdogCheck {
    $state = Read-SmokeState
    if (-not $state) {
        Write-WatchdogLog "No smoke-test state found at $statePath. Start the smoke test first." "ERROR"
        return $false
    }

    if (Test-BrokerHealth) {
        $owner = Get-PortOwner -Port 8000
        if ($owner -and (-not $state.broker -or [int]$state.broker -ne $owner)) {
            Write-WatchdogLog "Broker is healthy but state PID $($state.broker) differs from listener PID $owner; updating state." "WARN"
            $state.broker = $owner
            Write-SmokeState -State $state
        }
        Write-WatchdogLog "Broker is healthy (PID $($state.broker))."
    }
    else {
        Write-WatchdogLog "Broker health check failed; attempting restart." "WARN"
        Stop-UnhealthyProcess -State $state -Port 8000 -StateKey "broker" -ProcessName "python" -Label "Broker"
        if (-not (Start-Broker -State $state)) {
            return $false
        }
    }

    $dashboardOk = $true
    if (Test-DashboardHealth) {
        $owner = Get-PortOwner -Port 8001
        if ($owner -and (-not $state.dashboard -or [int]$state.dashboard -ne $owner)) {
            Write-WatchdogLog "Dashboard is healthy but state PID $($state.dashboard) differs from listener PID $owner; updating state." "WARN"
            $state.dashboard = $owner
            Write-SmokeState -State $state
        }
        Write-WatchdogLog "Dashboard is healthy (PID $($state.dashboard))."
    }
    else {
        Write-WatchdogLog "Dashboard health check failed; attempting restart." "WARN"
        Stop-UnhealthyProcess -State $state -Port 8001 -StateKey "dashboard" -ProcessName "python" -Label "dashboard"
        if (-not (Start-Dashboard -State $state)) {
            $dashboardOk = $false
        }
    }

    return $dashboardOk
}

$lastOk = $false
$checks = 0
while ($true) {
    $lastOk = Invoke-WatchdogCheck
    $checks += 1
    if ($Once -or ($MaxChecks -gt 0 -and $checks -ge $MaxChecks)) {
        break
    }
    Start-Sleep -Seconds $IntervalSeconds
}

if ($lastOk) {
    exit 0
}
exit 1
