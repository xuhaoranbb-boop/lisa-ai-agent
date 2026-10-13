param(
    [string]$Title = 'Ashley Effects',
    [string]$Output = 'artifacts\jarvis-window.png'
)

Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class WindowCaptureNative {
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
    [DllImport("user32.dll")]
    public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdcBlt, uint nFlags);
}
"@

$process = Get-Process electron |
    Where-Object { $_.MainWindowTitle -eq $Title } |
    Select-Object -First 1
if (-not $process) {
    throw "Window not found: $Title"
}

$rect = New-Object WindowCaptureNative+RECT
[WindowCaptureNative]::GetWindowRect($process.MainWindowHandle, [ref]$rect) | Out-Null
$width = $rect.Right - $rect.Left
$height = $rect.Bottom - $rect.Top
if ($width -le 0 -or $height -le 0) {
    throw "Window has invalid bounds: ${width}x${height}"
}

$bitmap = New-Object System.Drawing.Bitmap $width, $height
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$hdc = $graphics.GetHdc()
[WindowCaptureNative]::PrintWindow($process.MainWindowHandle, $hdc, 0) | Out-Null
$graphics.ReleaseHdc($hdc)
$graphics.Dispose()

$target = [System.IO.Path]::GetFullPath($Output)
[System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($target)) | Out-Null
$bitmap.Save($target, [System.Drawing.Imaging.ImageFormat]::Png)
$bitmap.Dispose()
Write-Output $target
