$ErrorActionPreference = "Stop"

$statePath = Join-Path $env:TEMP "dreamerol-smoke-state.json"

if (-not (Test-Path -LiteralPath $statePath)) {
    Write-Host "No smoke-test state found."
    return
}

$state = [System.IO.File]::ReadAllText($statePath) | ConvertFrom-Json

if ($state.watchdog) {
    $watchdog = Get-Process -Id ([int]$state.watchdog) -ErrorAction SilentlyContinue
    if ($watchdog -and $watchdog.ProcessName -in @("powershell", "pwsh")) {
        Stop-Process -Id $watchdog.Id -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
        Write-Host "Stopped watchdog (PID $($watchdog.Id))."
    }
    elseif ($watchdog) {
        Write-Warning "Skipping watchdog PID $($state.watchdog) because it is $($watchdog.ProcessName)."
    }
}

$expectedProcesses = [ordered]@{
    dashboard = "python"
    broker = "python"
}

foreach ($name in $expectedProcesses.Keys) {
    $processId = $state.$name
    if (-not $processId) {
        continue
    }
    $process = Get-Process -Id ([int]$processId) -ErrorAction SilentlyContinue
    if (-not $process) {
        continue
    }
    if ($process.ProcessName -ne $expectedProcesses[$name]) {
        Write-Warning "Skipping $name PID $processId because it is $($process.ProcessName), not $($expectedProcesses[$name])."
        continue
    }
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    Write-Host "Stopped $name (PID $processId)."
}

Remove-Item -LiteralPath $statePath -ErrorAction SilentlyContinue
Write-Host "Smoke-test services stopped."
