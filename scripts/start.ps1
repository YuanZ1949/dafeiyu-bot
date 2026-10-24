# Dafeiyu QQ bot launcher
# Usage: powershell -ExecutionPolicy Bypass -File scripts/start.ps1
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "[1/3] Creating venv .venv ..." -ForegroundColor Cyan
    python -m venv .venv
}

Write-Host "[2/3] Checking dependencies ..." -ForegroundColor Cyan
& .venv\Scripts\python -m pip install -q -e .

if (-not (Test-Path "config.yaml")) {
    Write-Host "config.yaml is missing. Restore it from the repository root." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path ".env")) {
    Write-Host ".env is missing. Run:" -ForegroundColor Yellow
    Write-Host "  Copy-Item .env.example .env" -ForegroundColor Yellow
    Write-Host "then fill in DEEPSEEK_API_KEY and BOT_QQ." -ForegroundColor Yellow
    exit 1
}

Write-Host "[3/3] Starting Dafeiyu ... (Ctrl+C to quit)" -ForegroundColor Cyan
& .venv\Scripts\python -m dafeiyu
