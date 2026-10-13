param(
    [string]$ReadImageConfig = "$env:USERPROFILE\.codex\read-image.json",
    [string]$EnvPath = ".env"
)

$config = Get-Content -Raw -LiteralPath $ReadImageConfig | ConvertFrom-Json
$model = [string]$config.model
$baseUrl = [string]$config.baseUrl
$apiKey = [string]$config.apiKey

if ([string]::IsNullOrWhiteSpace($model)) {
    throw "Vision model is missing from $ReadImageConfig"
}
if ([string]::IsNullOrWhiteSpace($baseUrl)) {
    throw "Vision base URL is missing from $ReadImageConfig"
}
if ([string]::IsNullOrWhiteSpace($apiKey)) {
    throw "Vision API key is missing from $ReadImageConfig"
}

$updates = [ordered]@{
    JARVIS_VISION_MODEL = $model
    JARVIS_VISION_BASE_URL = $baseUrl
    JARVIS_VISION_API_KEY = $apiKey
}
$lines = @(Get-Content -LiteralPath $EnvPath -ErrorAction SilentlyContinue)
$seen = @{}
$result = foreach ($line in $lines) {
    $matched = $false
    foreach ($name in $updates.Keys) {
        if ($line -match "^\s*$([regex]::Escape($name))\s*=") {
            $seen[$name] = $true
            $line = "$name=$($updates[$name])"
            $matched = $true
            break
        }
    }
    if (-not $matched) {
        $line
    }
}
foreach ($name in $updates.Keys) {
    if (-not $seen.ContainsKey($name)) {
        $result += "$name=$($updates[$name])"
    }
}
Set-Content -LiteralPath $EnvPath -Value $result -Encoding utf8
Write-Output "Synced vision configuration to $EnvPath"
