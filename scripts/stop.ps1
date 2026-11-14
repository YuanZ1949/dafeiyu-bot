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

# 读配置的统一入口。
# PS 5.1 的坑：原生命令把 stderr 重定向（2>$null / 2>&1）时，若
# $ErrorActionPreference = "Stop"，写 stderr 会被当成 NativeCommandError 抛出。
# 本项目 python 一 import 就往 stderr 打一行能力探测，于是每次读配置都进 catch，
# 表现为「读取配置失败」并静默退回默认值。这里在读取期间临时放宽为 Continue。
function Invoke-PythonRead([string]$Code) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $lines = & $python -c $Code 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0 -or -not $lines) {
        return $null
    }
    $last = $lines | Where-Object { $_ -is [string] } | Select-Object -Last 1
    if (-not $last) {
        return $null
    }
    return ([string]$last).Trim()
}

# 微信 hook 服务是独立进程，supervisor 不管它，这里先单独收掉。
# 端口必须跟配置走：start-all 按 wechat.hook_url 启动，写死 30011 会漏掉 30012 上的实例。
$hookScript = Join-Path $root "scripts\start-wechat-hook.ps1"
$hookPort = 30011
$hookPortText = Invoke-PythonRead "from urllib.parse import urlsplit; from dafeiyu.config import load_settings; print(urlsplit(load_settings().wechat.hook_url).port or 30011)"
if ($hookPortText) {
    $parsedPort = 0
    if ([int]::TryParse($hookPortText, [ref]$parsedPort)) {
        $hookPort = $parsedPort
    }
} else {
    Write-Host "读取 hook 端口失败，回退 30011" -ForegroundColor DarkYellow
}
if (Test-Path -LiteralPath $hookScript) {
    & powershell -ExecutionPolicy Bypass -File $hookScript -Stop -Port $hookPort
}

& $python -m dafeiyu.supervisor.stop
exit $LASTEXITCODE
