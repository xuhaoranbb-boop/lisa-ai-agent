param(
    [string]$CapturePath = 'artifacts\jarvis-visual-qa.png',
    [int]$CaptureMs = 6000
)

$projectRoot = Split-Path -Parent $PSScriptRoot
$ashleyRoot = Join-Path $projectRoot 'ashley-upstream'
$nodeDir = Join-Path $env:USERPROFILE '.local\node-v22.23.2-win-x64'
$electron = Join-Path $ashleyRoot 'node_modules\electron\dist\electron.exe'
$capture = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $CapturePath))

$env:PATH = "$nodeDir;$env:PATH"
$env:JARVIS_VISUAL_QA = '1'
$env:JARVIS_QA_CAPTURE_PATH = $capture
$env:JARVIS_QA_CAPTURE_MS = [string]$CaptureMs

Push-Location $ashleyRoot
try {
    & $electron .
}
finally {
    Pop-Location
}

Write-Output $capture
