param(
    [switch]$SkipTests,
    [switch]$SkipNode
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$failures = New-Object "System.Collections.Generic.List[string]"

function Invoke-Check {
    param(
        [string]$Name,
        [scriptblock]$Command
    )
    Write-Host "==> $Name"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        $failures.Add($Name)
    }
}

Push-Location $repoRoot
try {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) {
        throw "python was not found in PATH."
    }

    Invoke-Check "Python version" {
        & python -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11 or newer is required'"
    }

    Invoke-Check "Python syntax" {
        & python -m compileall -q api auth_broker backend fork_sync main.py tests
    }

    if (-not $SkipNode) {
        $node = (Get-Command node -ErrorAction SilentlyContinue).Source
        if (-not $node) {
            Write-Host "Node.js was not found in PATH."
            $failures.Add("JavaScript syntax")
        }
        else {
            Invoke-Check "JavaScript syntax" {
                & node --check web/app.js
            }
        }
    }

    Write-Host "==> PowerShell syntax"
    $parseErrors = @()
    foreach ($file in Get-ChildItem -Path (Join-Path $repoRoot "scripts") -Filter "*.ps1") {
        $tokens = $null
        $errors = $null
        [System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors) | Out-Null
        foreach ($error in @($errors)) {
            $parseErrors += "$($file.Name): $($error.Message)"
        }
    }
    if ($parseErrors.Count -gt 0) {
        $parseErrors | ForEach-Object { Write-Host $_ }
        $failures.Add("PowerShell syntax")
    }

    Invoke-Check "Installed packages" {
        & python -m pip check
    }

    if (-not $SkipTests) {
        Invoke-Check "pytest" {
            & python -m pytest -q
        }
    }
}
finally {
    Pop-Location
}

if ($failures.Count -gt 0) {
    Write-Host ""
    Write-Host "FAILED: $($failures -join ', ')" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "All checks passed." -ForegroundColor Green
