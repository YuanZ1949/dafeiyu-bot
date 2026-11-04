# 停止 supervisor（连带 bot / opencode / napcat 子进程）。
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts/stop.ps1
# 先走面板 /api/shutdown 优雅退出，失败再强制结束进程树。
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "找不到虚拟环境：$python" -ForegroundColor Red
    Write-Host "先用 scripts/start.ps1 或 scripts/start.bat 创建环境" -ForegroundColor Yellow
    exit 1
}

$env:PYTHONIOENCODING = "utf-8"

# 微信 hook 服务是独立进程，supervisor 不管它，这里先单独收掉。
$hookScript = Join-Path $root "scripts\start-wechat-hook.ps1"
if (Test-Path -LiteralPath $hookScript) {
    & powershell -ExecutionPolicy Bypass -File $hookScript -Stop
}

& $python -m dafeiyu.supervisor.stop
exit $LASTEXITCODE
