$ErrorActionPreference = 'Stop'

$ashleyRoot = $PSScriptRoot
$projectRoot = Split-Path -Parent $ashleyRoot
$nodeDir = Join-Path $env:USERPROFILE '.local\node-v22.23.2-win-x64'

$env:PATH = "$nodeDir;$env:PATH"
$env:JARVIS_VOICE_PROVIDER = 'local'
$env:JARVIS_BRIDGE_URL = 'http://127.0.0.1:8765'
if (-not $env:JARVIS_ENVIRONMENT) {
    $env:JARVIS_ENVIRONMENT = 'production'
}

try {
    Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' -TimeoutSec 2 | Out-Null
} catch {
    Start-Process -FilePath 'python' `
        -ArgumentList '-m','uv','run','jarvis','bridge','--port','8765' `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden | Out-Null

    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        try {
            Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' -TimeoutSec 2 | Out-Null
            break
        } catch {
            if ($attempt -eq 29) {
                throw 'Jarvis local bridge did not start on port 8765.'
            }
        }
    }
}

Set-Location -LiteralPath $ashleyRoot
& (Join-Path $nodeDir 'pnpm.cmd') run build
$electron = Join-Path $ashleyRoot 'node_modules\electron\dist\electron.exe'
Start-Process -FilePath $electron -ArgumentList '.' -WorkingDirectory $ashleyRoot -WindowStyle Normal
