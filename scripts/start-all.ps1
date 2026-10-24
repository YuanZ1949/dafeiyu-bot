# 启动 supervisor（接管 OpenCode / bot / NapCat），然后打开本地控制台。
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts/start-all.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/start-all.ps1 -NoBrowser
# 控制台里可以启动/停止/重启各个进程、切换大脑模式、看日志和二维码。
param(
    [switch]$NoBrowser,
    [switch]$NoQr,
    [int]$WaitSeconds = 60
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $python)) {
    Write-Host "找不到虚拟环境：$python" -ForegroundColor Red
    Write-Host "先运行：python -m venv .venv; .venv\Scripts\python -m pip install -e ." -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$env:PYTHONIOENCODING = "utf-8"

# 端口从配置里读，读不到就退回默认值
$port = 8787
try {
    $printed = & $python -c "from dafeiyu.config import load_settings; print(load_settings().supervisor.dashboard.port)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $printed) { $port = [int]($printed | Select-Object -Last 1) }
} catch {
    Write-Host "读取配置失败，使用默认端口 8787" -ForegroundColor DarkYellow
}

$pidFile = Join-Path $root "data\supervisor.pid"
$alreadyRunning = $false
if (Test-Path -LiteralPath $pidFile) {
    $oldPid = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
        $alreadyRunning = $true
        Write-Host "supervisor 已在运行（pid $oldPid），直接打开控制台。" -ForegroundColor Green
    }
}

if (-not $alreadyRunning) {
    Write-Host "启动 supervisor（隐藏窗口）..." -ForegroundColor Cyan
    Start-Process -FilePath $pythonw -ArgumentList "-m", "dafeiyu.supervisor" `
        -WorkingDirectory $root -WindowStyle Hidden
}

$url = "http://127.0.0.1:$port/"
$deadline = (Get-Date).AddSeconds($WaitSeconds)
$healthy = $false
while ((Get-Date) -lt $deadline) {
    try {
        $resp = Invoke-WebRequest -Uri "${url}api/health" -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200) { $healthy = $true; break }
    } catch {
        Start-Sleep -Milliseconds 500
    }
}

if (-not $healthy) {
    Write-Host "控制台在 $WaitSeconds 秒内没有就绪。" -ForegroundColor Red
    Write-Host "查看日志：logs\supervisor\supervisor.log" -ForegroundColor Yellow
    exit 1
}

Write-Host "控制台已就绪：$url" -ForegroundColor Green
Write-Host "（关掉这个窗口不会停止后台进程；要停请用控制台上的「退出控制台」。）" -ForegroundColor DarkGray

if (-not $NoBrowser) {
    Start-Process $url
}
