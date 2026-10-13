param(
    [string]$CertificateThumbprint = '',
    [string]$PfxPath = '',
    [string]$PfxPassword = '',
    [switch]$GenerateDevelopmentCertificate,
    [string]$TimestampServer = 'http://timestamp.digicert.com',
    [string]$ReportPath = ''
)

$ErrorActionPreference = 'Stop'
$ashleyRoot = Split-Path -Parent $PSScriptRoot
$releaseRoot = Join-Path $ashleyRoot 'release'
$artifacts = @(
    (Join-Path $releaseRoot 'win-unpacked\GCAI JARVIS.exe'),
    (Join-Path $releaseRoot 'GCAI-JARVIS-1.0.0-x64.exe')
)

foreach ($artifact in $artifacts) {
    if (-not (Test-Path -LiteralPath $artifact)) {
        throw "Signing artifact not found: $artifact"
    }
}

$certificate = $null
if ($CertificateThumbprint) {
    $certificate = Get-Item -LiteralPath "Cert:\CurrentUser\My\$CertificateThumbprint"
} elseif ($PfxPath) {
    $securePassword = ConvertTo-SecureString -String $PfxPassword -AsPlainText -Force
    $certificate = Import-PfxCertificate `
        -FilePath $PfxPath `
        -CertStoreLocation 'Cert:\CurrentUser\My' `
        -Password $securePassword `
        -Exportable
} elseif ($GenerateDevelopmentCertificate) {
    $certificate = New-SelfSignedCertificate `
        -Type CodeSigningCert `
        -Subject 'CN=GCAI JARVIS Development Code Signing' `
        -FriendlyName 'GCAI JARVIS Development Code Signing' `
        -CertStoreLocation 'Cert:\CurrentUser\My' `
        -KeyAlgorithm RSA `
        -KeyLength 3072 `
        -KeyExportPolicy Exportable `
        -NotAfter (Get-Date).AddYears(3)
} else {
    throw 'Provide -CertificateThumbprint, -PfxPath, or -GenerateDevelopmentCertificate.'
}

$results = foreach ($artifact in $artifacts) {
    $signature = Set-AuthenticodeSignature `
        -FilePath $artifact `
        -Certificate $certificate `
        -HashAlgorithm SHA256 `
        -TimestampServer $TimestampServer
    [pscustomobject]@{
        path = $artifact
        status = $signature.Status.ToString()
        status_message = $signature.StatusMessage
        signer = $signature.SignerCertificate.Subject
        thumbprint = $signature.SignerCertificate.Thumbprint
    }
}

if (-not $ReportPath) {
    $ReportPath = Join-Path $releaseRoot 'signing-report.json'
}
$results | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ReportPath -Encoding utf8
$results | Format-Table path, status, signer -AutoSize
