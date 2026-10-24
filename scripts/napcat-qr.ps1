# Show a large, auto-refreshing NapCat login QR.
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts/napcat-qr.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/napcat-qr.ps1 -NoOpen
#   powershell -ExecutionPolicy Bypass -File scripts/napcat-qr.ps1 -ImageViewer
param(
    [int]$TimeoutSeconds = 120,
    [int]$MaxAgeSeconds = 150,
    [switch]$NoOpen,
    [switch]$ImageViewer
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$cache = Join-Path $root "vendor\napcat\napcat\cache"
$qr = Join-Path $cache "qrcode.png"
$big = Join-Path $cache "qrcode-big.png"
$html = Join-Path $cache "qrcode.html"
$template = Join-Path $PSScriptRoot "napcat-qr.html"

function Get-FreshQr {
    try {
        $item = Get-Item -LiteralPath $qr -ErrorAction Stop
        if ($item.Length -le 100) { return $null }
        $age = ((Get-Date) - $item.LastWriteTime).TotalSeconds
        if ($age -gt $MaxAgeSeconds) { return $null }
        return $item
    } catch {
        return $null
    }
}

# Wait for a QR that was written recently. A stale qrcode.png is ignored,
# otherwise the helper would open last week's code when NapCat is already logged in.
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$info = $null
while ((Get-Date) -lt $deadline) {
    $candidate = Get-FreshQr
    if ($candidate) {
        Start-Sleep -Milliseconds 350
        $again = Get-FreshQr
        if ($again -and $again.Length -eq $candidate.Length) {
            $info = $again
            break
        }
    }
    Start-Sleep -Milliseconds 500
}

if (-not $info) {
    Write-Host "No fresh QR code found under vendor/napcat/napcat/cache." -ForegroundColor Yellow
    Write-Host "NapCat may already be logged in, or it is still starting." -ForegroundColor Yellow
    exit 0
}

if (-not (Test-Path -LiteralPath $template)) {
    throw "Missing QR page template: $template"
}
if (-not (Test-Path -LiteralPath $cache)) {
    New-Item -ItemType Directory -Path $cache | Out-Null
}

Copy-Item -LiteralPath $template -Destination $html -Force

function New-BigQr {
    param(
        [string]$Source,
        [string]$Target,
        [int]$Scale = 4
    )
    Add-Type -AssemblyName System.Drawing
    $img = [System.Drawing.Image]::FromFile($Source)
    try {
        $w = [int]($img.Width * $Scale)
        $h = [int]($img.Height * $Scale)
        $bmp = New-Object System.Drawing.Bitmap -ArgumentList $w, $h
        try {
            $g = [System.Drawing.Graphics]::FromImage($bmp)
            try {
                $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::NearestNeighbor
                $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::Half
                $g.DrawImage($img, 0, 0, $w, $h)
            } finally {
                $g.Dispose()
            }
            $tmp = $Target + ".tmp"
            $bmp.Save($tmp, [System.Drawing.Imaging.ImageFormat]::Png)
            Move-Item -LiteralPath $tmp -Destination $Target -Force
        } finally {
            $bmp.Dispose()
        }
    } finally {
        $img.Dispose()
    }
}

try {
    New-BigQr -Source $qr -Target $big -Scale 4
    Write-Host "Big QR: $big" -ForegroundColor Green
} catch {
    Write-Host "Could not create qrcode-big.png: $($_.Exception.Message)" -ForegroundColor Yellow
}

Write-Host "Login page: $html" -ForegroundColor Green
Write-Host "Raw QR:     $qr" -ForegroundColor DarkGray
Write-Host "The page reloads qrcode.png every 2 seconds, so it follows NapCat refreshes." -ForegroundColor DarkGray

if (-not $NoOpen) {
    if ($ImageViewer -and (Test-Path -LiteralPath $big)) {
        Start-Process -FilePath $big
    } else {
        Start-Process -FilePath $html
    }
}
