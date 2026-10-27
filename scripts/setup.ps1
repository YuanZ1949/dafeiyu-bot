# Dafeiyu first-run setup: venv + dependencies + config + env check
# Usage: powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "[1/4] Creating venv .venv ..." -ForegroundColor Cyan
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
    Write-Host "[2/4] Installing dependencies ..." -ForegroundColor Cyan
    & .venv\Scripts\python -m pip install -q --disable-pip-version-check -e .
    Set-Content -LiteralPath $marker -Value (Get-Date).ToString("o")
} else {
    Write-Host "[2/4] Dependencies already installed." -ForegroundColor Green
}

if (-not (Test-Path "config.yaml")) {
    Write-Host "[3/4] Generating config.yaml from config.example.yaml ..." -ForegroundColor Cyan
    & .venv\Scripts\python -m dafeiyu.cli init
} else {
    Write-Host "[3/4] config.yaml already exists." -ForegroundColor Green
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "[4/4] Created .env from .env.example." -ForegroundColor Yellow
    Write-Host "      Fill in BOT_QQ and DEEPSEEK_API_KEY, then run:" -ForegroundColor Yellow
    Write-Host "      .venv\Scripts\python -m dafeiyu.cli doctor" -ForegroundColor Yellow
    exit 0
}

Write-Host "[4/4] Checking environment ..." -ForegroundColor Cyan
& .venv\Scripts\python -m dafeiyu.cli doctor
exit $LASTEXITCODE
