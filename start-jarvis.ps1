$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$env:JARVIS_VOICE_PROVIDER = "local"
$env:JARVIS_BRIDGE_URL = "http://127.0.0.1:8765"
$env:PYTHONPATH = Join-Path $root "src"
$env:JARVIS_ENVIRONMENT = "production"

function Test-JarvisBridge {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 2
        return [bool]$health.ok
    } catch {
        return $false
    }
}

if (-not (Test-JarvisBridge)) {
    $python = Join-Path $root ".venv\Scripts\python.exe"
    Start-Process -FilePath $python `
        -ArgumentList "-m", "jarvis", "bridge", "--port", "8765" `
        -WorkingDirectory $root `
        -WindowStyle Hidden

    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline -and -not (Test-JarvisBridge)) {
        Start-Sleep -Milliseconds 400
    }
}

$localModelScript = Join-Path $root "training\start_local_model.ps1"
if (
    $env:JARVIS_LOCAL_MODEL_AUTOSTART -ne "0" -and
    (Test-Path -LiteralPath $localModelScript)
) {
    try {
        & $localModelScript
    } catch {
        Write-Warning "Local model fallback is unavailable: $($_.Exception.Message)"
    }
}

$electron = Join-Path $root "ashley-upstream\node_modules\electron\dist\electron.exe"
$appPath = Join-Path $root "ashley-upstream"
Start-Process -FilePath $electron -ArgumentList ".", "--show" -WorkingDirectory $appPath
