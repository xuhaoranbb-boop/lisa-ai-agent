$ErrorActionPreference = "Stop"

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = $env:JARVIS_LOCAL_MODEL_PYTHON
if (-not $python) {
    $python = "D:\jarvis-training\venv\Scripts\python.exe"
}
$adapter = $env:JARVIS_LOCAL_MODEL_ADAPTER
if (-not $adapter) {
    $adapter = "D:\jarvis-training\outputs\qwen25-15b-lora-v4"
}
$model = if ($env:JARVIS_LOCAL_MODEL_NAME) {
    $env:JARVIS_LOCAL_MODEL_NAME
} else {
    "Qwen/Qwen2.5-1.5B-Instruct"
}
$port = if ($env:JARVIS_LOCAL_MODEL_PORT) {
    [int]$env:JARVIS_LOCAL_MODEL_PORT
} else {
    8001
}

function Test-LocalModel {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/health" -TimeoutSec 2
        return [bool]$health.ok
    } catch {
        return $false
    }
}

if (Test-LocalModel) {
    return
}
if (-not (Test-Path -LiteralPath $python)) {
    throw "Local model Python was not found: $python"
}
if (-not (Test-Path -LiteralPath $adapter)) {
    throw "Local model adapter was not found: $adapter"
}

$env:HF_ENDPOINT = "https://hf-mirror.com"
$env:HF_HOME = if ($env:HF_HOME) { $env:HF_HOME } else { "D:\jarvis-training\hf-cache" }
$env:HF_HUB_DISABLE_XET = "1"
$env:HF_HUB_OFFLINE = "1"
$env:TRANSFORMERS_OFFLINE = "1"
$logDir = Join-Path $root "training\logs"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$stdout = Join-Path $logDir "local-model.out.log"
$stderr = Join-Path $logDir "local-model.err.log"
$server = Join-Path $root "training\serve_lora.py"
$arguments = @(
    "`"$server`"",
    "--model", "`"$model`"",
    "--adapter", "`"$adapter`"",
    "--served-model-name", "jarvis-lora",
    "--host", "127.0.0.1",
    "--port", "$port",
    "--use-4bit"
) -join " "

Start-Process -FilePath $python `
    -ArgumentList $arguments `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr

$deadline = (Get-Date).AddMinutes(4)
while ((Get-Date) -lt $deadline -and -not (Test-LocalModel)) {
    Start-Sleep -Milliseconds 500
}
if (-not (Test-LocalModel)) {
    throw "Local model did not become healthy. See $stderr"
}
