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

$marker = ".venv\.deps-installed"
$needInstall = $true
if ((Test-Path $marker) -and (Test-Path "pyproject.toml")) {
    if ((Get-Item $marker).LastWriteTime -ge (Get-Item "pyproject.toml").LastWriteTime) {
        $needInstall = $false
    }
}
if ($needInstall) {
    Write-Host "[2/3] Installing dependencies ..." -ForegroundColor Cyan
    & .venv\Scripts\python -m pip install -q --disable-pip-version-check -e .
    Set-Content -LiteralPath $marker -Value (Get-Date).ToString("o")
} else {
    Write-Host "[2/3] Dependencies already installed." -ForegroundColor Green
}

if (-not (Test-Path "config.yaml")) {
    Write-Host "config.yaml is missing. Run scripts/setup.ps1 to generate it." -ForegroundColor Red
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
