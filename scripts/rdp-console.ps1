# RDP + tscon 会话交接：把当前 RDP 会话交回物理控制台，让桌面保持解锁。
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\rdp-console.ps1                 # 只读状态
#   powershell -ExecutionPolicy Bypass -File scripts\rdp-console.ps1 -Action plan    # 只打印命令
#   powershell -ExecutionPolicy Bypass -File scripts\rdp-console.ps1 -Action handoff # 执行交接
#   powershell -ExecutionPolicy Bypass -File scripts\rdp-console.ps1 -Action handoff -Elevate
# 注意：handoff 需要管理员权限，执行后当前 RDP 连接会立即断开（预期行为）。
param(
    [ValidateSet("status", "plan", "handoff")]
    [string]$Action = "status",
    [double]$Timeout = 15.0,
    [switch]$Elevate
)

$ErrorActionPreference = "Stop"

# -Elevate：用管理员身份重新起一个自己，参数原样带过去；UAC 被取消时明确失败。
if ($Elevate) {
    $arguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSCommandPath`"",
        "-Action", $Action,
        "-Timeout", $Timeout
    )
    try {
        $proc = Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -Verb RunAs -Wait -PassThru
        exit $proc.ExitCode
    } catch {
        Write-Host "提权失败（可能取消了 UAC）：$($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
}

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 优先用仓库内 .venv；开发/未装依赖时退回 PATH 里的 python。
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $command = Get-Command python -ErrorAction SilentlyContinue
    if (-not $command) {
        Write-Host "找不到虚拟环境：$python" -ForegroundColor Red
        Write-Host "请先运行：python -m venv .venv; .venv\Scripts\python -m pip install -e ." -ForegroundColor Yellow
        exit 1
    }
    $python = $command.Source
    Write-Host "未找到 $root\.venv，改用 PATH 里的 $python" -ForegroundColor Yellow
}

$env:PYTHONIOENCODING = "utf-8"
# 直接读 src/ 下的代码，未做 editable 安装时也能跑。
$src = Join-Path $root "src"
if (Test-Path (Join-Path $src "dafeiyu")) {
    if ($env:PYTHONPATH) {
        $env:PYTHONPATH = "$src;$env:PYTHONPATH"
    } else {
        $env:PYTHONPATH = $src
    }
}

& $python -m dafeiyu.rdp $Action --timeout $Timeout
$code = $LASTEXITCODE
if ($code -eq 3) {
    Write-Host "提示：tscon 需要管理员权限，加 -Elevate 重新运行可自动提权。" -ForegroundColor Yellow
}
exit $code
