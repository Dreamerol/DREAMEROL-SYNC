param(
    [switch]$Force,
    [switch]$DryRun
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

function Wait-ForTunnelUrl {
    param(
        [string]$LogPath,
        [int]$TimeoutSeconds = 40
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $LogPath) {
            $text = Get-Content -LiteralPath $LogPath -Raw -ErrorAction SilentlyContinue
            if ($text) {
                $match = [regex]::Match($text, "https://[a-z0-9-]+\.trycloudflare\.com")
                if ($match.Success) {
                    return $match.Value
                }
            }
        }
        Start-Sleep -Milliseconds 500
    }
    throw "Timed out waiting for the cloudflared tunnel URL."
}

function Wait-ForTunnel {
    param(
        [string]$CallbackUrl,
        [int]$TimeoutSeconds = 40
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            Invoke-WebRequest -Uri $CallbackUrl -UseBasicParsing -TimeoutSec 15 | Out-Null
        }
        catch {
            $response = $_.Exception.Response
            if ($response -and [int]$response.StatusCode -eq 400) {
                return
            }
        }
        Start-Sleep -Seconds 1
    }
    throw "Timed out waiting for the tunnel to reach the Broker."
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

$cloudflared = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $cloudflared) {
    $candidates = @(
        "C:\Program Files (x86)\cloudflared\cloudflared.exe",
        "C:\Program Files\cloudflared\cloudflared.exe"
    )
    $cloudflared = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $cloudflared) {
    throw "cloudflared was not found. Install it with: winget install --id Cloudflare.cloudflared -e"
}

if (-not (Test-Path -LiteralPath $envPath)) {
    throw ".env was not found. Copy .env.example to .env first."
}

$expectedProcesses = @{
    broker = "python"
    dashboard = "python"
    cloudflared = "cloudflared"
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

$originalEnv = [System.IO.File]::ReadAllText($envPath)
if ($originalEnv -notmatch "(?m)^BROKER_CALLBACK_URL=") {
    throw ".env does not contain BROKER_CALLBACK_URL."
}
if ($originalEnv -notmatch "(?m)^BROKER_TRUSTED_PROXIES=.*127\.0\.0\.1") {
    Write-Warning "BROKER_TRUSTED_PROXIES does not include 127.0.0.1; cloudflared clients will share one rate-limit bucket."
}

if ($DryRun) {
    Write-Host "Dry run: prerequisites are available."
    Write-Host "Would update BROKER_CALLBACK_URL in $envPath."
    Write-Host "Would start Broker on http://127.0.0.1:8000, dashboard on http://127.0.0.1:8001, and a cloudflared quick tunnel."
    return
}

New-Item -ItemType Directory -Path $logDir -Force | Out-Null

$brokerOut = Join-Path $logDir "broker.out.log"
$brokerErr = Join-Path $logDir "broker.err.log"
$dashboardOut = Join-Path $logDir "dashboard.out.log"
$dashboardErr = Join-Path $logDir "dashboard.err.log"
$cloudflaredLog = Join-Path $logDir "cloudflared.log"

Remove-Item -LiteralPath $brokerOut, $brokerErr, $dashboardOut, $dashboardErr, $cloudflaredLog -ErrorAction SilentlyContinue

$cloudflaredProcess = $null
$brokerProcess = $null
$dashboardProcess = $null
$callbackUpdated = $false

try {
    Write-Step "Starting cloudflared quick tunnel"
    $cloudflaredArgs = @(
        "tunnel",
        "--url", "http://127.0.0.1:8000",
        "--no-autoupdate",
        "--protocol", "http2",
        "--logfile", "`"$cloudflaredLog`"",
        "--loglevel", "info"
    )
    $cloudflaredProcess = Start-Process -FilePath $cloudflared -ArgumentList $cloudflaredArgs -PassThru -WindowStyle Hidden -WorkingDirectory $logDir
    $tunnelUrl = Wait-ForTunnelUrl -LogPath $cloudflaredLog -TimeoutSeconds 40
    $callbackUrl = "$tunnelUrl/auth/callback"

    Write-Step "Updating BROKER_CALLBACK_URL"
    $updatedEnv = [regex]::Replace($originalEnv, "(?m)^BROKER_CALLBACK_URL=.*$", "BROKER_CALLBACK_URL=$callbackUrl")
    [System.IO.File]::WriteAllText($envPath, $updatedEnv, $utf8NoBom)
    $callbackUpdated = $true

    Write-Step "Starting Broker on port 8000"
    $brokerArgs = @("-m", "uvicorn", "auth_broker.app:create_app", "--factory", "--host", "127.0.0.1", "--port", "8000")
    $brokerProcess = Start-Process -FilePath $python -ArgumentList $brokerArgs -PassThru -WindowStyle Hidden -WorkingDirectory $repoRoot -RedirectStandardOutput $brokerOut -RedirectStandardError $brokerErr
    Wait-ForPort -Port 8000 -TimeoutSeconds 20

    Write-Step "Starting dashboard on port 8001"
    $dashboardArgs = @("-m", "uvicorn", "api.app:app", "--host", "127.0.0.1", "--port", "8001")
    $dashboardProcess = Start-Process -FilePath $python -ArgumentList $dashboardArgs -PassThru -WindowStyle Hidden -WorkingDirectory $repoRoot -RedirectStandardOutput $dashboardOut -RedirectStandardError $dashboardErr
    Wait-ForPort -Port 8001 -TimeoutSeconds 20

    Write-Step "Verifying tunnel and dashboard"
    $dashboardResponse = Invoke-WebRequest -Uri "http://127.0.0.1:8001/" -UseBasicParsing -TimeoutSec 15
    if ($dashboardResponse.StatusCode -ne 200) {
        throw "Dashboard returned HTTP $($dashboardResponse.StatusCode)."
    }

    Wait-ForTunnel -CallbackUrl "$tunnelUrl/auth/callback" -TimeoutSeconds 40

    $state = [ordered]@{
        broker = $brokerProcess.Id
        dashboard = $dashboardProcess.Id
        cloudflared = $cloudflaredProcess.Id
        tunnel_url = $tunnelUrl
        callback_url = $callbackUrl
        started_at = (Get-Date).ToString("o")
    }
    [System.IO.File]::WriteAllText($statePath, ($state | ConvertTo-Json), $utf8NoBom)

    Write-Host ""
    Write-Host "Broker:    http://127.0.0.1:8000 (PID $($brokerProcess.Id))"
    Write-Host "Dashboard: http://127.0.0.1:8001 (PID $($dashboardProcess.Id))"
    Write-Host "Tunnel:    $tunnelUrl (PID $($cloudflaredProcess.Id))"
    Write-Host "Callback:  $callbackUrl"
    Write-Host ""
    Write-Host "Set the callback in the GitHub App settings, then run .\scripts\stop-smoke.ps1 to stop."
}
catch {
    if ($callbackUpdated) {
        [System.IO.File]::WriteAllText($envPath, $originalEnv, $utf8NoBom)
    }
    Stop-StartedProcesses -Processes @($dashboardProcess, $brokerProcess, $cloudflaredProcess)
    $errorLog = Join-Path $logDir "start-smoke.error.log"
    [System.IO.File]::WriteAllText($errorLog, ($_ | Out-String), $utf8NoBom)
    throw
}
