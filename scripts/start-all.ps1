# Start NapCat (embedded under vendor/napcat) and then the bot.
# Usage: powershell -ExecutionPolicy Bypass -File scripts/start-all.ps1
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$napcatDir = Join-Path $root "vendor\napcat"
$napcatBat = Join-Path $napcatDir "napcat.bat"

if (-not (Test-Path $napcatBat)) {
    Write-Host "NapCat is not installed under vendor/napcat." -ForegroundColor Red
    Write-Host "Run this first:" -ForegroundColor Yellow
    Write-Host "  powershell -ExecutionPolicy Bypass -File scripts/setup-napcat.ps1" -ForegroundColor Yellow
    exit 1
}

$running = Get-CimInstance Win32_Process -Filter "Name='node.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains($napcatDir) }

if ($running) {
    Write-Host "[1/2] NapCat is already running." -ForegroundColor Green
} else {
    Write-Host "[1/2] Starting NapCat in a new window ..." -ForegroundColor Cyan
    Start-Process -FilePath $napcatBat -WorkingDirectory $napcatDir
    Start-Sleep -Seconds 3
}

Write-Host "[2/2] Starting Dafeiyu ... (Ctrl+C to stop the bot)" -ForegroundColor Cyan
& (Join-Path $PSScriptRoot "start.ps1")
