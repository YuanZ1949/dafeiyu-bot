# 一键导出诊断包：时间、版本、pip freeze、端口、/health、最近日志与脱敏后的 config.yaml。
# 机密与运行数据不进包：data/、.env、config.yaml 原文由 Python 侧拒绝或脱敏。
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts/diag.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/diag.ps1 -OutFile D:\diag.zip
#   powershell -ExecutionPolicy Bypass -File scripts/diag.ps1 -LogLines 500
#   powershell -ExecutionPolicy Bypass -File scripts/diag.ps1 -DryRun
param(
    [string]$OutFile = "",
    [ValidateRange(1, 100000)]
    [int]$LogLines = 200,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot

# 统一按 UTF-8 收子进程输出：非中文区域的 Windows 重定向到文件时也不会编码失败。
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # 某些宿主不允许改控制台编码，退回默认即可。
}

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $venvPython) {
    $python = $venvPython
} else {
    $python = "python"
}

# 仓库是 src 布局，没 pip install -e 也要能 import dafeiyu。
$env:PYTHONPATH = Join-Path $root "src"

$arguments = @("-m", "dafeiyu.diag", "--root", $root, "--log-lines", "$LogLines")
if ($OutFile) {
    if (-not [System.IO.Path]::IsPathRooted($OutFile)) {
        $OutFile = Join-Path (Get-Location).Path $OutFile
    }
    $arguments += @("--out", $OutFile)
}
if ($DryRun) {
    $arguments += "--dry-run"
}

Push-Location $root
try {
    & $python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "python 退出码 $LASTEXITCODE"
    }
} catch {
    Write-Host "诊断包生成失败：$_" -ForegroundColor Red
    Write-Host "       可先加 -DryRun 看计划；缺依赖时先运行 scripts\setup.ps1。" -ForegroundColor Yellow
    exit 1
} finally {
    Pop-Location
}
