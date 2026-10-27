# 拉起 supervisor（托管 OpenCode / bot / NapCat），等面板就绪。
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts/start-all.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/start-all.ps1 -NoBrowser
# 双击启动用 scripts/start.bat。
# 浏览器默认由 supervisor 按 config.yaml 的 dashboard.open_browser 自己打开；
# 本脚本只在 open_browser=false 且未传 -NoBrowser 时补开一个，避免开两个标签页。
param(
    [switch]$NoBrowser,
    [int]$WaitSeconds = 60
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $python)) {
    Write-Host "找不到虚拟环境：$python" -ForegroundColor Red
    Write-Host "请先运行：python -m venv .venv; .venv\Scripts\python -m pip install -e ." -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$env:PYTHONIOENCODING = "utf-8"

# 端口和「面板自己会不会开浏览器」都以配置为准，不写死默认值
$port = 8787
$supervisorOpensBrowser = $true
try {
    $printed = & $python -c "from dafeiyu.config import load_settings; d = load_settings().supervisor.dashboard; print(f'{d.port} {int(d.open_browser)}')" 2>$null
    if ($LASTEXITCODE -eq 0 -and $printed) {
        $parts = ($printed | Select-Object -Last 1).Trim() -split '\s+'
        if ($parts.Count -ge 2) {
            $port = [int]$parts[0]
            $supervisorOpensBrowser = ($parts[1] -eq "1")
        }
    }
} catch {
    Write-Host "读取配置失败，使用默认端口 8787" -ForegroundColor DarkYellow
}

$pidFile = Join-Path $root "data\supervisor.pid"
$alreadyRunning = $false
if (Test-Path -LiteralPath $pidFile) {
    $oldPid = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
        $alreadyRunning = $true
        Write-Host "supervisor 已在运行（pid $oldPid），不再重复启动。" -ForegroundColor Green
    }
}

if (-not $alreadyRunning) {
    Write-Host "启动 supervisor（后台窗口隐藏）..." -ForegroundColor Cyan
    Start-Process -FilePath $pythonw -ArgumentList "-m", "dafeiyu.supervisor" `
        -WorkingDirectory $root -WindowStyle Hidden
}

$url = "http://127.0.0.1:$port/"
$deadline = (Get-Date).AddSeconds($WaitSeconds)
$healthy = $false
# 探 /login 而不是 /api/health：后者在开启登录（默认，见 dashboard.auth=auto）时返回 401，
# /login 是公开路径，auth 关闭时 302 到 /，跟随重定向后都是 200。
$probe = "${url}login"
while ((Get-Date) -lt $deadline) {
    try {
        $resp = Invoke-WebRequest -Uri $probe -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200) { $healthy = $true; break }
    } catch {
        Start-Sleep -Milliseconds 500
    }
}

if (-not $healthy) {
    Write-Host "面板在 $WaitSeconds 秒内没有就绪" -ForegroundColor Red
    Write-Host "查看日志：logs\supervisor\supervisor.log" -ForegroundColor Yellow
    exit 1
}

Write-Host "面板已就绪：$url" -ForegroundColor Green
Write-Host "停止请用 scripts/stop.ps1，或用面板右上角的退出按钮。" -ForegroundColor DarkGray

if ($NoBrowser) {
    Write-Host "（已传 -NoBrowser，不打开浏览器）" -ForegroundColor DarkGray
} elseif ($supervisorOpensBrowser) {
    Write-Host "（浏览器由 supervisor 自动打开；config.yaml dashboard.open_browser=true）" -ForegroundColor DarkGray
} else {
    Start-Process $url
}
